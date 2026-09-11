# Validation conf & result — exact contract

Read this before generating any conf or result body, or reconstructing one from memory. Confirm
the live shape with `dataspoke-schema attr/validation/conf` / `dataspoke-schema
attr/validation/result` whenever this file and the deployment might have drifted.

## Conf — four sections, not two

```json
{
  "description": "[TEAM_prophet_01] partition D-1 row_count · Prophet 95% interval on 112d history",
  "variables": [{"name": "row_count", "description": "rows in the partition just written"}],
  "attribute": {"cadence_unit": 86400, "cadence_offset": 0},
  "parameter": [
    {"name": "lookback_days_max", "value": "112", "description": "max history fed to the model"}
  ]
}
```

| Section | Direction | Who reads it | Required on `PUT` |
|---|---|---|---|
| `description` | pipeline → reader | humans, the reuse search | yes (≤2,000 chars, empty allowed) |
| `variables` | pipeline → conf | the routine's own `POST` (key allowlist) | yes (list; see below) |
| `attribute` | pipeline → Governance | the `validation-score` metric | no — omitting it stores the all-defaults object, never leaves it absent |
| `parameter` | conf → pipeline | the routine (opaque hyperparameters) | no — omitting it on `PUT` **clears** any stored value |

`variables[]` items are `{name, description}`: `name` matches `[a-z][a-z0-9_]{0,99}` and is unique
within its own list; `description` is required, ≤200 chars, empty allowed — a separate, much
shorter field from the top-level `description`. `parameter[]` items add `value` (string, required,
≤200 chars, empty allowed) to the same `name`/`description` shape.

A `PUT` for a URN DataHub does not track returns `422 DATASET_NOT_IN_DATAHUB` — ingest the dataset
first.

### `description`: module, target, pass condition — copy the live shape

The convention is a fixed set of *facts*, not a fixed string. It names the implementing module (so
the reuse search — `GET /spoke/validation?coverage=covered`, below — can recognize a match by
grepping `description`), states which partition is scored (unrecoverable from the variable names
alone, and the fact that keeps this conf's `cadence_unit`/`cadence_offset` honest against the
pipeline's actual settled-partition logic), and states what makes it pass. **Read a live conf
before writing one** — `GET /spoke/validation?coverage=covered` and inspect a few `description`
values — and match the deployment's actual form rather than inventing one from this example; a
deployment's confs are the only accurate record of the convention in force.

### `attribute` — the dataset's real arrival cadence

`{cadence_unit, cadence_offset}`, both in seconds; `cadence_offset` counts whole `cadence_unit`
periods of lag (offset = *n* − 1 for D-*n* data):

| Cadence | `cadence_unit` | `cadence_offset` |
|---|---|---|
| Daily, arrives D-1 | `86400` | `0` |
| Daily, arrives D-3 | `86400` | `2` |
| Hourly, previous hour | `3600` | `0` |

Ask the user the real cadence rather than accepting the default silently — an all-defaults conf
declares "daily, no lag," and getting this wrong is silent in both directions: too-fresh a default
makes an on-time dataset read as failing every day on the governance `validation-score` metric;
too-lax lets a genuinely stale dataset read as passing. The scored-partition statement in
`description` and this section must tell the same arrival story — a scorer judging the settled
D-7 partition under a default `offset: 0` conf reads as six days stale forever, and nothing in
either layer notices the disagreement on its own.

### `parameter` vs `variables` — opposite directions

Both are `[{name, description}]`-shaped (`parameter` adds `value`), and it is easy to get the
direction backwards — the API cannot catch it, since both are just named lists:

| | Direction | Type | Meaning |
|---|---|---|---|
| `parameter` | conf → code | string | An argument **to** the routine — a hyperparameter, retunable without a redeploy |
| `variables` | code → conf | float | A measurement the code **produces**, stored as a timeseries |

A `variables` series is the routine's own intermediate store — a later run reads it back as its
baseline. A check must never re-derive history by rescanning the source: that costs the query
again and gets a *worse* answer, since it cannot see what an earlier run actually observed at the
time, only what the source looks like now. Renaming a `variables` key orphans the series (past
results keep the keys they were posted with); posting an undeclared key is `422
UNKNOWN_VARIABLE`; a subset of declared variables is fine, including an empty map; `parameter` and
`variables` are independent namespaces, so a name may appear in both.

## Verb matters per section, not per body

| | `PUT` | `PATCH` |
|---|---|---|
| Omitted `description`/`variables` | required (rejected if missing) | unchanged |
| Omitted `attribute` | stored as all-defaults | unchanged |
| Supplied `attribute` | wholesale | **wholesale — not a deep merge** |
| Omitted `parameter` | **section cleared** | unchanged |
| `parameter: null` | cleared | cleared |
| `parameter: []` | rejected `422` | rejected `422` |

So `PATCH {"attribute": {"cadence_offset": 7}}` also resets `cadence_unit` to its default — resend
the whole `attribute` object on any `PATCH` that touches it. `null` is the only spelling of "clear
the parameters." A `description`-only `PATCH` (the reuse-naming step) leaves `variables`,
`attribute`, and `parameter` untouched.

## Result body

```json
{
  "data_time": "2026-05-07T00:00:00Z",
  "score": 0.8,
  "variables": {"row_count": 1250.0},
  "score_note": "breached 1/18: null_rate_col_07"
}
```

| Field | Required | Rule |
|---|---|---|
| `data_time` | yes | RFC3339 UTC, timezone-aware. A naive value serializes with no offset and is rejected `422 INVALID_PARAMETER`. |
| `score` | yes | `0.0 ≤ score ≤ 1.0`; outside that range is `422 INVALID_SCORE`. |
| `variables` | yes | Keys must match declared `variables[].name`; unknown keys are `422 UNKNOWN_VARIABLE`. A subset of declared names is fine, including `{}`. |
| `score_note` | no | ≤200 chars. `null`/omitted/empty all store as absent. Unlike `variables`, it is not declared in the conf — its wording is free to change between runs without orphaning anything. |

Returns `201`.

### `score_note` — construct it, don't hope it fits

The note explains a verdict; it never carries one — nothing scores off it, and a generated scorer
must not drift toward encoding structure in it that a reader is expected to parse back out. Build
it from values the routine controls (criterion names, counts) — never interpolate a raw source
string, since an over-length or control-character note is `422 INVALID_PARAMETER`, which is fatal
and costs the partition its **entire result**, not just its note.

For a check with more than a few criteria, naming every breach can exceed 200 characters well
before the criterion count gets large. Build the note to fit rather than hoping it does: write the
shared stem, append breached names while they fit, and close with a `(+N more)` marker so the
count — not an arbitrary truncation point — is what survives:

```text
breached 3/21: null_rate_col_07, dup_key_rate (+1 more)
```

A clean pass (`score == 1.0`) normally omits the note.

## Reads

**Time window is half-open, order is fixed, set `limit` explicitly.** See
[`plugin/references/pagination.md`](../../../references/pagination.md) for the shared doctrine and
this route's specific deviation (`from`/**`until`**, raised default/cap, fixed `data_time DESC`).
A validation baseline read supplies `until` equal to the `data_time` under judgment on every call
— relying on an unbounded read is how a retry or a repeat backfill leaks the point under judgment,
or a future point, into its own baseline.

**Reads collapse last-write-wins per `data_time`, then sort.** The table itself is append-only —
there is no uniqueness on `(dataset_urn, data_time)`, so POSTing the same `data_time` twice stores
two rows — but `GET …/result` collapses to the newest by ingestion time for each distinct
`data_time` **first**, and only then applies the fixed `data_time DESC` order; the response is
never sorted over the raw, uncollapsed rows. A retried run therefore corrects its partition rather
than duplicating it in the history. `total_count` counts distinct `data_time` values after that
collapse, not stored rows — confirm the exact figure against `dataspoke-schema
attr/validation/result` or live `API.md` rather than assuming it matches `len(results)`, and treat
`total_count == len(results)` as a good sanity guard on a bounded read, not a substitute for
setting `limit` explicitly.

**One row per distinct `data_time`, not per day.** `data_time` is a timestamp, not a date. Two
runs stamping different times inside the same day are two separate partitions and both are
returned. Whether a series reads as daily is entirely a property of what the routine puts in
`data_time`.

`coverage` on the cross-dataset list (`GET /spoke/validation`) selects the row set: `covered`
(the default), `uncovered`, or `both`. `covered` answers "what **is** validated," never "what
**could be**" — and the covered set is typically a small minority of the estate, so don't report a
first, default-filtered list as the full coverage picture. `uncovered` rows come back with
`description`, `variable_count`, and `latest_*` all `null`; that is the shape of "no conf," not
missing data.

## Destructive operations — warn explicitly before either

**`DELETE` conf is a hard delete**, not an archive. In one transaction it removes the conf row,
**all of the dataset's validation results**, and its `VALIDATION.*` events, then hard-deletes the
assertion entity from DataHub. It returns `204`; afterwards the dataset reads as never-created
(`GET`/`PATCH` → `404 CONFIG_NOT_FOUND`) and a fresh `PUT` starts an empty slot. The history is
unrecoverable — get explicit agreement before calling it. If the user only wants to stop
validating, stop calling the routine instead of deleting the slot.

**Changing `variables[]` breaks history continuity.** `PUT`/`PATCH` replaces the declared
`variables[]`, but past results keep whatever keys they were posted with. Renaming `row_count` to
`rows` leaves every historical row keyed `row_count`, so a baseline query returns a series the new
routine cannot read, and the next `POST` fails `422 UNKNOWN_VARIABLE` until the routine is updated
to match. When a rename or removal is requested, say what it does to the existing series and offer
adding a new variable alongside the old one instead.

## Errors

| Code | Meaning | Response |
|---|---|---|
| `403 READ_ONLY_ROLE` | effective role is Reader | send to `/dataspoke:dataspoke-access`, do not retry |
| `404 CONFIG_NOT_FOUND` | no conf for this dataset | register one, or stop — do not treat as "validated: unknown data" |
| `422 DATASET_NOT_IN_DATAHUB` | URN not ingested | send to `/dataspoke:dataspoke-ingestion` first |
| `422 UNKNOWN_VARIABLE` | result key not declared in conf | fix the key, or update the conf deliberately (see continuity warning above) |
| `422 INVALID_SCORE` | `score` outside `[0,1]` | fix the routine's scoring logic |
| `422 INVALID_PARAMETER` | malformed `data_time`, `score_note`, or field length/charset | fix the offending field — this is fatal on the result POST, so the whole result is lost |

Confirm before any write; surface these verbatim rather than working around them.
