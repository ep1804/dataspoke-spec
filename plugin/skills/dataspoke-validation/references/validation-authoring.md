# Validation routine authoring — design, code, wire, test, backfill

Read this before designing or generating any check. It covers what `SKILL.md`'s guided route
points to at each step: the owner-question set, the open method set and scoring arithmetic, the
failure/outage policy, a fixed worked example, per-engine wiring, and the test/backfill checklist.

## Ask before measuring

Four facts decide the whole design and none of them are in the data — ask them in one pass, before
the first analysis query, and record the answers in the plan:

| Ask | Why it cannot wait |
|---|---|
| How far back should the analysis go, and is any stretch of it unrepresentative? (default: three months — long enough to hold a weekly shape, a few month-boundaries, and a genuine incident or two; short enough to still describe the table as it is today) | A window covering a migration, a backfill, or a known incident teaches the model that the incident is normal — every band comes out too wide to catch the next one. Move off the default deliberately: a table younger than three months has no choice, a suspected yearly shape needs roughly two years, a window stretched to cover a migration measures two different tables under one name. |
| Which days are calendar or operating-regime days — month boundaries, settlements, campaigns, schedules, holidays? | A day that is many sigma out by design breaches every cycle unless the model carries a term for it. This is the single most common source of a check nobody trusts. |
| Is anything here strange on purpose — duplicate keys, rows that look double-counted, a column that is null by design? | Otherwise the analysis reports it as a finding and the check hard-fails on it forever; only the owner can say it's intentional. |
| Does a run rewrite partitions it already wrote (a D-1/D-2 trailing window)? | Decides which partition is settled and therefore judgeable, the conf's `cadence_offset`, and whether the pipeline needs a settled-slice helper at the call site. |

Two more when the pipeline itself does not answer them: what breaks downstream if this table is
wrong (ranks candidate measurements), and how far back the backfill should go (an operating-cost
decision, the user's call, not a correctness fact).

A recurring special day is modeled, not skipped — excluding the outlier days leaves the days most
likely to break unchecked, which is backwards. Add the day as its own term once enough prior
occurrences exist to fit it, and warn when the window is too short to do so; that gate is a second
cold-start ramp on top of the criterion-level one below, and a freshly registered conf may
legitimately sit below its own honest floor for a while.

## Reuse search — before authoring anything new

`dataspoke-api GET '/spoke/validation?coverage=covered'` surfaces other datasets' registered
confs; `description` conventionally names the implementing module (per
[`validation-conf.md`](validation-conf.md)), so a match is recognizable. Also grep the user's own
shared/validation package for an existing check of the same shape. A reused module must still be
checked against this dataset's semantics before wiring it in — do not assume a name match is a
semantic match.

**Registering the conf before the pipeline ever calls the routine is load-bearing on both the
reused and freshly-authored path.** A missing conf does not raise: the routine's public entry
point catches `404 CONFIG_NOT_FOUND` into a logged warning, not an exception, so a pipeline
calling an unregistered check completes as if it had validated while nothing reaches DataSpoke's
history.

## Open method set — choose from what the measured history supports

There is no universal default check. Design starts from measured history over the representative
window (above), and a dataset routinely needs more than one method:

| Method | Suitable evidence | Judged from |
|---|---|---|
| Invariant | A guarantee whose history is all zeros — an orphan-row count, a duplicate-key count | The first target, with no history at all |
| Forecast | A level that moves with the calendar | A minimum point count onward |
| Relation | Two measurements that should move together — a stable ratio | A minimum number of paired days |

**Choose forecast variables by stability, not by importance** — rank candidates by
weekday-adjusted log-residual spread where that precondition holds, and keep the tight ones. Row
count is often *not* the best candidate: a series that moves 7% on row count while a null rate on
the same partition moves 0.2% over the same window means the null rate carries the check, and a
row-count forecast would fit a band wide enough to admit almost anything. Keep a volume criterion
anyway when every other criterion is a ratio, since a partition that loses a third of its rows
uniformly moves no ratio at all.

The invariant layer is what makes a check useful on day one — it needs no history, so a brand-new
conf judges something real from its first partition instead of reporting a long meaningless
warm-up, and it is the layer a forecast rule structurally cannot express.

Checks stay isolated by default: adding one next to an existing check is not license to extract a
shared engine unless the user asks for that refactor separately — and when they do, the existing
check's unchanged outputs are the pass condition for that separate change, never bundled into the
change that adds the new check.

### Freeze the module name before the first result lands

Each new implementation gets a unique, stable module code name (per [`validation-conf.md`](validation-conf.md)'s
`description` convention) before it ever posts a result — not after. The name reaches the module's
filename, the conf `description`, the reuse search's grep target, and every result posted under
it. Renaming is cheap before the first result exists and expensive after: it orphans the series
the next run reads as its own baseline, and severs the reuse catalogue's ability to recognize the
implementation by its old name. Treat a post-results rename as a versioning decision — a new name
alongside the old, not an in-place edit — the same way [`validation-conf.md`](validation-conf.md)
treats a `variables[]` rename, not as a cosmetic cleanup.

## Scoring — a criterion that cannot be judged counts against the score

```python
score = 1.0 - len(breached) / total_criteria
```

`total_criteria` is **derived** from the configured criterion groups (`len(invariant_criteria) +
len(forecast_criteria) + len(relation_criteria)`, or however the routine names its groups) —
never a separately written constant, so adding a criterion cannot leave a stale divisor behind. A
criterion that cannot yet be judged — insufficient history for its method — is added to `breached`
exactly as a criterion that was judged and failed. There is no cold-start sentinel score and no
special-cased "not yet scoring" branch:

- A conf registered but never backfilled scores low from the first run, instead of reporting a
  clean `1.0` forever while validating nothing.
- A partition that could judge nothing lands at `0.0` on its own; one that could judge most things
  lands just below `1.0`. No sentinel value needs explaining.
- The cold-start ramp becomes arithmetic the plan can quote before the backfill runs — e.g. a
  9-invariant/8-forecast/4-relation check (21 criteria) opens at `1 - 12/21 = 0.4286` with only its
  history-free invariants passing, and cannot reach `1.0` until every forecast and relation
  criterion has cleared its own minimum history. State these floors and the day each one clears in
  the plan.

A single-criterion check is the degenerate case of the same rule: `score = 0.0` on insufficient
history is the correct verdict for "this dataset claims to be validated and is not" — never `1.0`.

## Failure policy — the routine reports, it never raises

- **Nothing raises out of the routine's public entry point** — not an unreachable result store,
  not a bug in the scoring, and **not a bad partition either**. A failed check is a `score: 0.0`
  (or whatever the breach arithmetic yields) in the history, never a raised exception. Structure it
  as a thin public function wrapping a private one: the public entry point is a bare `except
  Exception` → warn (log) → return, so a bug never escapes into the pipeline's own error handling;
  the private function raises freely, which keeps bugs visible in unit tests and any direct call.
- **The backstop is the governance layer, not the pipeline.** For a dataset whose conf *is*
  registered, the `validation-score` metric reports it out of `valid_in_time` once the latest
  result scores `< 1.0` — so raising inside the routine buys nothing that metric doesn't already
  catch, at the cost of a red pipeline task and a blocked downstream. That backstop does **not**
  cover an unregistered conf — the reuse-search ordering above is what guards that case, since
  validation runs *after* the write and can only report on the run, never improve it.
- **Report the worst finding rather than staying silent.** "No data at all" is a result to *post*,
  not a reason to skip the post: measure what can be measured (a row count of `0`), score it, post
  it at the partition's `data_time`. A posted low score is caught on `validation-score`'s very next
  measurement, while a *missing* point leaves the previous passing result standing as the latest
  result until it ages out of the metric's window. Silence delays detection; it never speeds it up.

### Outage vs. fatal — for the result-store calls specifically

A blanket `except RequestException` around every DataSpoke call is wrong: it produces a check that
reports "fine" forever while validating nothing. Use an allowlist instead, with the reasoning
stated so it doesn't erode over time:

| Treat as an outage → skip this partition, keep going | Keep fatal → abandon the call |
|---|---|
| Connection refused / DNS failure | **4xx**: `401`/`403`, `404 CONFIG_NOT_FOUND`, `422` |
| Read/connect timeout | Malformed API URL / misconfigured origin |
| Retries exhausted against `429` | A response that parsed but violated a checked invariant |
| **Every `5xx`**, on every route, with no exceptions | Anything raised by the metric query itself |

The asymmetry: an outage is transient and self-heals, so skipping costs one partition's verdict.
The right column is a standing fault that recurs on every run — it needs a human, not a retry.
"Fatal" here means "stop this call," never "fail the pipeline" — it sits *inside* the entry
point's catch-all, not instead of it. Put the classifier in one shared place next to the DataSpoke
HTTP calls, not duplicated per scorer — every check needs the same verdict on the store's
reachability, independent of what it's checking.

**Classify every `5xx` as an outage on every route, including the result `POST` — do not special-case
away from it.** One route-specific detail changes only what the outage *means*, never whether it
is one: on the result `POST`, `502 DATAHUB_UNAVAILABLE` means the row is already committed and only
the downstream DataHub emit failed, while `503` (`STORAGE_UNAVAILABLE` or
`PERIPHERAL_NOT_CONFIGURED`) means nothing was committed. Both are still outages to the caller —
the difference only matters if the routine later needs to know whether a retry would duplicate a
committed row (it will not — reads collapse last-write-wins per `data_time`). A response handler
that classifies `502`/`503` explicitly and then falls through to a bare `raise_for_status()` for
every other status leaks `500`/`504` out as an unclassified `HTTPError` instead of an `_Outage` —
this is a defect, not a stricter policy; every `5xx` belongs in the same bucket.

## Worked example — two criteria, one invariant and one forecast

This is one worked shape illustrating the open method set, not a default suggestion made before
looking at the data. It has an invariant (`dup_key_rate` must be exactly zero) and one forecast
(`row_count` via Prophet) — enough to show the scoring arithmetic without hiding it inside a large
criterion count.

The conf this expects (registered after plan approval, `description` naming the module and this
target):

```json
{
  "description": "[TEAM_prophet_01] partition D-1 dup_key_rate==0 & row_count in 95% Prophet interval",
  "variables": [
    {"name": "row_count", "description": "rows in the partition just written"},
    {"name": "dup_key_rate", "description": "fraction of rows whose key repeats in the partition"}
  ],
  "attribute": {"cadence_unit": 86400, "cadence_offset": 0},
  "parameter": [
    {"name": "lookback_days_max", "value": "112", "description": "max history fed to Prophet"},
    {"name": "lookback_days_min", "value": "7", "description": "min contiguous history required, ending at the target day"},
    {"name": "growth", "value": "linear", "description": "Prophet growth argument"},
    {"name": "weekly_seasonality", "value": "True", "description": "Prophet weekly_seasonality argument"},
    {"name": "interval_width", "value": "0.95", "description": "Prophet prediction-interval width"}
  ]
}
```

`src/team_a/common/validator/prophet_01/__init__.py` (illustrative — team package name and exact
structure are the team's call, per the reuse-search convention above; a pattern many teams
recognize is `<team-package>/validator/<algorithm>_<NN>`, the `_NN` suffix disambiguating multiple
variants of the same algorithm):

```python
import logging
import os
from datetime import datetime, timedelta

import pandas as pd
import requests

logger = logging.getLogger(__name__)

DATASPOKE = os.environ["DATASPOKE_API_URL"].rstrip("/")  # must include the /api/v1 prefix
TOKEN = os.environ["DATASPOKE_API_TOKEN"]
H = {"Authorization": f"Bearer {TOKEN}"}

INVARIANT_CRITERIA = ("dup_key_rate_zero",)
FORECAST_CRITERIA = ("row_count_in_interval",)
TOTAL_CRITERIA = len(INVARIANT_CRITERIA) + len(FORECAST_CRITERIA)  # derived, never hardcoded

# Module defaults — the shipped behavior a malformed `parameter` degrades to.
_DEFAULT_LOOKBACK_MAX = 112
_DEFAULT_LOOKBACK_MIN = 7
_DEFAULT_GROWTH = "linear"
_DEFAULT_WEEKLY_SEASONALITY = True
_DEFAULT_INTERVAL_WIDTH = 0.95


def _parse_params(raw: dict[str, str]) -> dict:
    """Parse conf `parameter` values, falling back to the module default per-field on
    anything malformed. Never raises — a bad parameter must not disable the check that
    would have caught a bad partition. Each field is parsed independently, so one
    malformed value doesn't discard the other, perfectly valid ones."""
    out = {
        "lookback_days_max": _DEFAULT_LOOKBACK_MAX,
        "lookback_days_min": _DEFAULT_LOOKBACK_MIN,
        "growth": _DEFAULT_GROWTH,
        "weekly_seasonality": _DEFAULT_WEEKLY_SEASONALITY,
        "interval_width": _DEFAULT_INTERVAL_WIDTH,
    }
    if "lookback_days_max" in raw:
        try:
            v = int(raw["lookback_days_max"])
            out["lookback_days_max"] = v if v > 0 else _DEFAULT_LOOKBACK_MAX
        except (TypeError, ValueError) as exc:
            logger.warning("prophet_01: bad lookback_days_max, using default: %s", exc)
    if "lookback_days_min" in raw:
        try:
            v = int(raw["lookback_days_min"])
            out["lookback_days_min"] = (
                v if 0 < v <= out["lookback_days_max"] else _DEFAULT_LOOKBACK_MIN
            )
        except (TypeError, ValueError) as exc:
            logger.warning("prophet_01: bad lookback_days_min, using default: %s", exc)
    if "growth" in raw:
        if raw["growth"] in ("linear", "logistic", "flat"):
            out["growth"] = raw["growth"]
        else:
            logger.warning("prophet_01: bad growth %r, using default", raw["growth"])
    if "weekly_seasonality" in raw:
        out["weekly_seasonality"] = raw["weekly_seasonality"].strip().lower() == "true"
    if "interval_width" in raw:
        try:
            v = float(raw["interval_width"])
            out["interval_width"] = v if 0.0 < v < 1.0 else _DEFAULT_INTERVAL_WIDTH
        except (TypeError, ValueError) as exc:
            logger.warning("prophet_01: bad interval_width, using default: %s", exc)
    return out


class _Outage(Exception):
    """Result store unreachable — skip this partition, do not fail the pipeline."""


def _dataspoke_get(url: str, **kw) -> dict:
    try:
        resp = requests.get(url, headers=H, timeout=10, **kw)
    except (requests.ConnectionError, requests.Timeout) as exc:
        raise _Outage(str(exc)) from exc
    if resp.status_code in (401, 403, 404, 422):
        resp.raise_for_status()  # fatal — a standing fault, not an outage
    if resp.status_code >= 500:
        raise _Outage(f"HTTP {resp.status_code}")
    resp.raise_for_status()
    return resp.json()


def _dataspoke_post_result(url: str, body: dict) -> None:
    try:
        resp = requests.post(url, headers=H, json=body, timeout=10)
    except (requests.ConnectionError, requests.Timeout) as exc:
        raise _Outage(str(exc)) from exc
    if resp.status_code in (401, 403, 404, 422):
        resp.raise_for_status()  # fatal
    if resp.status_code == 502:
        # The row is committed locally before the DataHub emit; 502 here means only
        # that emit failed (502 DATAHUB_UNAVAILABLE) — an outage to the caller, not a
        # lost measurement.
        raise _Outage("HTTP 502 (result already stored; DataHub emit deferred)")
    if resp.status_code == 503:
        # Unlike 502: nothing was committed (503 STORAGE_UNAVAILABLE or
        # 503 PERIPHERAL_NOT_CONFIGURED) — still an outage, not a lost-and-fatal case.
        raise _Outage(f"HTTP 503 ({resp.text[:200]})")
    if resp.status_code >= 500:
        # Every other 5xx (500, 504, ...) is an outage too — classify it explicitly
        # rather than letting it fall through to raise_for_status() below, which would
        # surface as an unclassified HTTPError and get logged as a scoring bug instead
        # of a transient, retry-safe store outage.
        raise _Outage(f"HTTP {resp.status_code}")
    resp.raise_for_status()


def _measure_partition(dataset_urn: str, data_time: datetime) -> tuple[int, float]:
    """The one engine-specific step: (row_count, dup_key_rate). Replace this body with
    the real aggregation for your stack — see the wiring section below for PySpark /
    awswrangler shapes."""
    raise NotImplementedError("wire in the per-engine aggregation")


def _build_note(breached: list[str]) -> str | None:
    """Build a score_note that fits in 200 chars, closing with a remaining-count marker
    rather than truncating mid-name. Never interpolate a raw source string here. The
    final `[:200]` is a hard backstop, not the normal path: criterion names are short,
    controlled identifiers, so the marker branch below already fits in practice — the
    slice only protects against an unusually long name/count combination, and an
    over-length `score_note` is a fatal 422 that costs the partition its entire result,
    so this must never be allowed to fail regardless of edge cases."""
    if not breached:
        return None
    stem = f"breached {len(breached)}/{TOTAL_CRITERIA}: "
    out, budget = stem, 200 - len(stem)
    included = 0
    for i, name in enumerate(breached):
        piece = name if included == 0 else f", {name}"
        remaining = len(breached) - included - 1
        marker = f" (+{remaining} more)" if remaining else ""
        if len(piece) + len(marker) > budget:
            return (out + f" (+{len(breached) - included} more)")[:200]
        out += piece
        budget -= len(piece)
        included += 1
    return out[:200]


def _check(dataset_urn: str, data_time: datetime) -> None:
    """Raises freely — bugs stay visible in tests and direct calls."""
    # 1) COMPUTE first — the one hard gate this routine has (can the partition even be
    #    measured). Nothing else is worth fetching if this fails.
    row_count, dup_key_rate = _measure_partition(dataset_urn, data_time)

    # Everything below is resolved lazily, after that gate: an eager conf read ahead of
    # the measurement would turn an unmeasurable partition into a swallowed exception
    # with nothing useful logged about why.
    from prophet import Prophet  # heavy import, kept inside the function

    conf_url = f"{DATASPOKE}/spoke/common/data/{dataset_urn}/attr/validation/conf"
    result_url = f"{DATASPOKE}/spoke/common/data/{dataset_urn}/attr/validation/result"

    conf = _dataspoke_get(conf_url)
    raw_params = {p["name"]: p["value"] for p in conf.get("parameter", [])}
    params = _parse_params(raw_params)

    breached: list[str] = []

    # 2) INVARIANT — judged from the first partition, no history needed.
    if dup_key_rate != 0.0:
        breached.append("dup_key_rate_zero")

    # 3) FORECAST — bounded baseline read: `until` equals the point under judgment, so
    #    a retry or a repeat backfill can never leak that point — or anything after
    #    it — into its own baseline.
    since = (data_time.date() - timedelta(days=params["lookback_days_max"])).isoformat()
    page = _dataspoke_get(result_url, params={
        "from": since, "until": data_time.isoformat(), "limit": 10000,
    })
    hist = list(reversed(page["results"]))  # oldest -> newest

    # A historical row that posted a subset of declared variables is legal (per
    # validation-conf.md); `.get` and drop it from the series rather than raising —
    # a missing key is thin history, exactly like too few rows overall, not a defect.
    series = [
        (h["data_time"][:10], v)
        for h in hist
        if (v := h.get("variables", {}).get("row_count")) is not None
    ]

    if len(series) < params["lookback_days_min"]:
        # Cold start counts against the score — see § Scoring above. This is not a
        # verdict on *this* partition; it is still a breach, because "could not judge"
        # must never read the same as "judged and passed."
        breached.append("row_count_in_interval")
        logger.info(
            "prophet_01: only %d prior points, need %d — counted as unjudged, not skipped",
            len(series), params["lookback_days_min"],
        )
    else:
        df = pd.DataFrame(series, columns=["ds", "y"])
        model = Prophet(
            growth=params["growth"],
            weekly_seasonality=params["weekly_seasonality"],
            interval_width=params["interval_width"],
        )
        model.fit(df)
        forecast = model.predict(pd.DataFrame({"ds": [data_time.date().isoformat()]}))
        low, high = float(forecast["yhat_lower"].iloc[0]), float(forecast["yhat_upper"].iloc[0])
        if not (low <= row_count <= high):
            breached.append("row_count_in_interval")
        # A genuinely empty partition (row_count == 0) needs no special case: it flows
        # through this same comparison and naturally breaches whenever the forecast
        # expected anything above zero.

    score = 1.0 - len(breached) / TOTAL_CRITERIA

    # 4) POST — per § Failure policy: post even a low score rather than skip.
    #    data_time must be an aware UTC datetime — a naive value serializes without an
    #    offset and is rejected 422 INVALID_PARAMETER, which this routine then treats
    #    as fatal.
    body = {
        "data_time": data_time.isoformat(),
        "score": score,
        "variables": {"row_count": float(row_count), "dup_key_rate": float(dup_key_rate)},
    }
    note = _build_note(breached)
    if note is not None:
        body["score_note"] = note
    _dataspoke_post_result(result_url, body)


def validate_partition(dataset_urn: str, data_time: datetime) -> None:
    """Public entry point. Never raises — a store outage, a scoring bug, or a bad
    partition alike become a logged warning; on a store outage nothing is posted (the
    governance backstop only covers a landed result, so this path relies on the
    registration-ordering rule above, not on this function raising)."""
    try:
        _check(dataset_urn, data_time)
    except _Outage as exc:
        logger.warning("prophet_01: DataSpoke unreachable, skipping this partition: %s", exc)
    except Exception:
        logger.exception("prophet_01: unexpected failure validating %s", dataset_urn)
```

This is one worked shape, not a template to reproduce verbatim — adapt `_measure_partition`'s body
to the real engine, and the criterion set to what the user's data actually supports (fewer or more
invariants, additional forecast or relation criteria). The pattern that matters is structural:
thin never-raising public function, freely-raising private function, the measurement itself as the
one hard gate before anything else resolves lazily, a derived denominator, a bounded baseline read
on every fetch, every `5xx` as an outage, and always-post-on-reachable regardless of the verdict.

## Wire the dataset-specific call site

Write/Edit into **their** pipeline file — their engine, their credentials, never DataSpoke's. This
is deliberately thin: resolve the URN, then call the utility (or an existing one, from the reuse
search) with that URN and the partition's `data_time`.

**Attachment point.** The validation goes *after* the write it validates, gated on that write
having succeeded. Validate what actually landed: prefer re-reading the destination partition over
reusing the in-memory DataFrame, since the two diverge exactly when something went wrong (partial
write, schema coercion, silently dropped rows). Say which one was chosen and why.

**For a pipeline that rewrites a trailing window** (owner-question set, above), identify the
settled slice by asking **"is the newest partition of this run settled yet?"** rather than
computing an arithmetic offset off the run date — the question form survives a change in the
rewrite span and states the actual reason a partition is or isn't judgeable, where an offset
computed from `today - N` silently goes stale the moment the rewrite span changes. Wire a
settled-slice helper at the call site when the answer is "not always the newest," and make sure
that helper's notion of "settled" agrees with the conf's `cadence_unit`/`cadence_offset` — see
[`validation-conf.md`](validation-conf.md).

```python
# Airflow task / pipeline script — dataset-specific, thin.
from team_a.common.validator.prophet_01 import validate_partition

validate_partition(
    dataset_urn="<confirmed dataset_urn>",
    data_time=partition_logical_date,   # see "data_time" below
)
```

**Metric computation, per engine.** The one engine-specific piece — replace the worked example's
`_measure_partition` stub with the real aggregation for the stack in front of you:

*PySpark* — re-read the destination partition, aggregate in one pass:

```python
def _measure_partition(dataset_urn: str, data_time: datetime) -> tuple[int, float]:
    from pyspark.sql import functions as F

    part = spark.read.format("delta").load(DEST).where(F.col("dt") == PARTITION)
    row_count = part.agg(F.count(F.lit(1))).first()[0]
    dup_key_rate = part.groupBy("key").count().where(F.col("count") > 1).count() / max(row_count, 1)
    return row_count, dup_key_rate
```

*awswrangler / pandas* — push the aggregation into Athena rather than pulling the partition:

```python
def _measure_partition(dataset_urn: str, data_time: datetime) -> tuple[int, float]:
    import awswrangler as wr

    df = wr.athena.read_sql_query(
        "SELECT COUNT(*) AS row_count, "
        "COUNT(*) - COUNT(DISTINCT key) AS dup_rows FROM {table} WHERE dt = :dt".format(table=TABLE),
        database=DB, params={"dt": PARTITION},      # parameterized — never f-string the value
    )
    row_count = int(df.row_count[0])
    return row_count, float(df.dup_rows[0]) / max(row_count, 1)
```

For a `wr.s3.to_parquet(..., dataset=True)` write, run this *after* the catalog update so the new
partition is visible to Athena.

**Airflow.** Keep validation a separate task downstream of the write, not a tail appended to it —
the write stays retryable on its own, and a validation failure is visible as its own task. Whether
a low score should fail the *task* is the user's call at the orchestrator level: ask, and default
to recording the result and letting the DAG continue, since DataSpoke is a result store rather than
a gate. This is distinct from the failure policy above — the routine never raises internally
regardless of this answer; this question is only about what the *orchestrator* does with a low
score once it's already recorded.

**Re-run safety.** A retry that re-calls the routine with the same `data_time` is safe: reads
collapse to the newest write per `data_time`, so the partition is corrected, not duplicated. No
dedup guard is needed — do not generate one.

**`data_time` must identify the logical target, not the moment of the run.** For a partition
target, use the partition's own timestamp — the `dt`/`ds` value, the Airflow logical date, the
window start — truncated to the grain the table is partitioned at. For a whole-dataset target, use
the as-of boundary of the inspected snapshot on the dataset's declared cadence grid. Never
`datetime.now()` in either case: it makes every run a distinct `data_time`, so retries stop
collapsing and accumulate as separate points, and comparing "today vs. the last 14 values" silently
compares against however many runs happened, not 14 days. State the chosen grain explicitly. It
must also be **timezone-aware UTC** — a naive value serializes with no offset and is rejected `422
INVALID_PARAMETER` (fatal, then swallowed by the entry point — a silent no-op, not a loud
failure). Attach `tzinfo=timezone.utc` explicitly if the partition value is naive.

**When the pipeline runs more than once per day**, the fetched history holds one point per run, not
per day. Either scale the window to the run frequency (a 14-run baseline for an hourly job is ~14
hours — usually not what the user means), or bucket by day before comparing:

```python
by_day = {}                                                        # newest-first input,
for r in hist:                                                     # so the first hit per
    by_day.setdefault(r["data_time"][:10], r)                      # day is that day's latest
series = [by_day[d]["variables"]["row_count"] for d in sorted(by_day)]
```

Ask which the user wants rather than assuming — the right answer depends on whether the metric is
per-partition (row count) or per-day (daily total).

**Credentials.** The routine reads `DATASPOKE_API_URL` / `DATASPOKE_API_TOKEN` from the pipeline's
environment. `DATASPOKE_API_URL` here must include the `/api/v1` prefix — the generated routine
builds URLs by simple string concatenation, unlike `dataspoke-api` (the plugin's own CLI wrapper),
which tolerates either the bare origin or the `/api/v1`-suffixed form and normalizes it; if the
pipeline's env var is provisioned from the same value a person copies out of `dataspoke-api`'s own
config, confirm which shape it actually is before assuming. Never inline a `dsk_` token into
generated code, and never point the pipeline at `~/.dataspoke/config.json` — that file is the
plugin's local credential store, not a deployment artifact. Tell the user to provision the token
the way their orchestrator handles secrets.

**Confirm the shapes before generating code against them.** Read the real contract rather than
trusting this file:

```bash
dataspoke-schema attr/validation/result       # request + response schemas
dataspoke-schema attr/validation/conf
```

`/redoc` is the same document rendered for **humans** — give the user that URL (it is in
`~/.dataspoke/config.json` as `redoc_url`) when they want to browse it themselves.

## Test, run once, then backfill

1. **Unit tests**, no network: monkeypatch the metric query and every result-store call
   (`_dataspoke_get`/`_dataspoke_post_result` in the worked example). Cover: every criterion's pass
   and breach branch; the cold-start branch counting as a breach, not a sentinel pass; **both
   outage directions** (an injected connection error or any `5xx` is caught and logged, not
   raised; an injected `404`/`401`/`422` propagates out of the *private* function — assert this
   directly against `_check`, not the public wrapper); `score_note` construction, including the
   truncation-with-marker path on a long breach list; the `data_time` derivation; a malformed
   `parameter` falling back to the module default; and that **nothing escapes the public entry
   point** — call `validate_partition` with every failure mode injected and assert it never raises.
   One subtlety: the conf read is not opt-in — it runs on every call — so its stub must be
   `autouse`, or any test whose partition reaches the scoring step reaches for the real network.
2. **One real partition**: run the routine once against real data, then read the result back
   through `dataspoke-api GET .../attr/validation/result` and compare `data_time`, `score`,
   `variables`, and `score_note` against the pipeline's own log — don't just trust that the `POST`
   returned `2xx`.
3. **Backfill oldest-to-newest**, only after that single-partition check passes. Re-running is safe
   (last-write-wins per `data_time`). Two things to tell the user up front: the cold-start stretch
   (fewer than the forecast/relation minimum points) reads as a low score by design, not a bug —
   quote the floor and the day it clears, per § Scoring; and **a backfill of the underlying data
   itself should run with validation off**, then be validated in a second pass — judging a
   partition while the baseline is still being written measures the backfill's own progress, not
   the data.

**Sanity-check the chosen variable against real history before shipping.** A metric that is
constant by construction (e.g. row count on a table with a fixed daily volume enforced upstream)
gives a check that never fires; one whose series doesn't fit the comparison rule (a forecast method
on a wildly non-seasonal series) fires constantly. Look at the real history and say so if either
looks likely, proposing a different variable rather than shipping either.

**Engine trap worth naming.** If the metric query runs on a managed query engine, a workgroup with
no configured output location needs an explicit one, and the pipeline's execution role may have no
write access to that bucket — which surfaces as a *query-level* `access denied when writing output
to url ...` rather than a recognizable IAM error. Prove one real query runs under the pipeline's
own credentials before designing a scorer around it.
