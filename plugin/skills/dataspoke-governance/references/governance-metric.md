# Governance metric — exact contract

Read this before scaffolding, previewing, or writing any metric definition. It is a curated,
narrow reference — **the deployment is still the authority**; re-check the live contract via
`dataspoke-schema governance/metric` whenever something here does not match what it answers.

## Routes

| Question | Call |
|---|---|
| Metric list and last completed run | `GET /spoke/governance/metric` |
| Summary / latest attributes | `GET /spoke/governance/metric/{metric_id}` and `GET .../{metric_id}/attr` |
| Full definition | `GET .../{metric_id}/attr/conf` |
| Create | `POST /spoke/governance/metric` |
| Replace / partial update | `PUT` / `PATCH .../{metric_id}/attr/conf` |
| Delete | `DELETE .../{metric_id}/attr/conf` |
| Run (dry or non-dry) | `POST .../{metric_id}/method/run?dry_run=true\|false` |
| Result history | `GET .../{metric_id}/attr/result?from=...&to=...` |
| Scoped datasets and verdicts | `GET .../{metric_id}/dataset` (repeat `met=true\|false\|unknown` as needed) |
| Run and definition events | `GET .../{metric_id}/event?from=...&to=...` |

Follow `plugin/references/pagination.md` on every list call; do not report a first page as the
complete estate.

## Definition fields

```json
{
  "metric_id": "prod-ingestion-freshness",
  "mode": "active",
  "is_enabled": false,
  "metric_type": "ingestion-freshness",
  "title": "Production ingestion freshness",
  "description": "Counts primary production datasets with ingestion evidence inside a two-day window.",
  "metrics": [
    {"name": "total", "color": "#64748B", "idx": 1},
    {"name": "ingested_in_time", "color": "#22C55E", "idx": 2}
  ],
  "metric_conf": {"time_window_sec": 172800},
  "schedule_tier": "daily",
  "dataset_filter": "origin = 'PROD' AND is_primary = true"
}
```

`metric_id` is a client-supplied kebab-case slug (`^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]$`),
create-only — omit it from every `PUT`/`PATCH` body, since the path already identifies the metric.
`mode` is `"active"`; `mode: "passive"` is reserved (`501 NOT_IMPLEMENTED`) — do not propose it.
`schedule_tier` is `hourly`, `daily`, `weekly`, or `null` (on-demand only via `method/run`) —
confirm the enum against the live contract, since a deployment may narrow it. Descriptor `idx`
values are unique positive display positions; `color` is `#RRGGBB`.

### Built-in types and their series

| `metric_type` | Series names | `metric_conf` |
|---|---|---|
| `ingestion-freshness` | `total`, `ingested_in_time` | `{"time_window_sec": <int>}` |
| `validation-score` | `valid_confd`, `valid_in_time` | `{"time_window_sec": <int>}` |
| `doc-health` | `total`, `doc_health` | `{}` — rejects any key |

All emitted values are floats; all are dataset counts. `time_window_sec` is a positive integer
seconds count in `[1, 315360000]` (one second to ten years); out of range, non-integer, or boolean
is `422 INVALID_PARAMETER`, on `PATCH` against the merged `metric_conf` too. Do not silently
substitute a different unit.

- **Ingestion freshness**: `total` is every dataset in the resolved filter scope; `ingested_in_time`
  is those whose latest ingestion evidence is inside `time_window_sec` before the measurement.
  Evidence is the owning source's per-dataset observation when DataHub reports one, otherwise its
  newest non-dry-run `INGESTION.COMPLETE`.
- **Validation score**: `valid_confd` is datasets (within scope) with a validation configuration;
  `valid_in_time` is configured datasets whose **latest result overall** (not merely a result
  selected from the window) has `score >= 1.0` and lands inside a window anchored on that
  dataset's own validation-cadence declaration (`attr/validation/conf.attribute`,
  `/dataspoke:dataspoke-validation`'s territory) rather than on the measurement instant. The
  metric never searches backward for an older qualifying row. Datasets without a configuration are
  not failures — they're unevaluated and read `met: "unknown"` from `/dataset`. The resolved-scope
  count itself is not an emitted value — read it from `breakdown.dataset_count` instead.
- **Doc health**: `total` is the resolved scope; `doc_health` counts datasets with both a
  non-empty table description and a non-empty description on every column. In the result
  breakdown, a failing dataset's `missing_column_descriptions: []` is **not** good news — it can
  mean "no columns known" (no schema metadata at all) exactly as easily as "nothing missing."
  Check `missing_table_description` and the dataset's schema before reading an empty column list
  as clean.

### `dataset_filter` grammar

A SQL `WHERE`-clause string over the dataset registry, not arbitrary SQL or DataHub search:

| Column | Kind | Operators |
|---|---|---|
| `dataset_urn`, `origin`, `platform_urn` | scalar | `=`, `!=`, `IN (...)`, `NOT IN (...)` |
| `tag_urns`, `glossary_term_urns` | array | `'value' IN column`, `'value' NOT IN column` |
| `is_primary` | boolean | `= TRUE` / `= FALSE` only — bare word, never quoted, no negation |

`AND`/`OR`/`NOT`/`IN`, `TRUE`/`FALSE`, and column names are case-insensitive; **string values are
case-sensitive**. String literals use single quotes (`''` escapes an embedded quote); parentheses
nest at most two deep; mixing `AND`/`OR` at one level requires parentheses. Caps: ≤8,000 characters
and ≤1,000 string literals — the same bound on every route that writes a filter. `422
INVALID_DATASET_FILTER` reports the character position of the error; show it and help fix the
filter, never widen the scope to make the error go away.

**An empty string matches every registered dataset.** The seeded factory metrics ship with an
empty `dataset_filter` by design (below) — enabling one as-is runs it against the whole estate,
not a scoped subset.

## The three factory metrics — leave them alone, add a new id instead

On API startup, an idempotent bootstrap inserts one row per built-in `metric_type` **if absent**,
disabled, with an empty filter — and never overwrites an existing row. **Leave those three rows
alone; a new team- or scope-specific policy is a new `metric_id`, not an edit to a factory row**
(e.g. `validation-score-orders` rather than editing `validation-score`'s seeded row). Deleting a
factory row is not durable — the bootstrap re-inserts it, disabled with an empty filter, on the
next API start — so if a factory metric genuinely needs retiring, `PATCH {"is_enabled": false}` is
the durable way; deleting only produces a stale-looking gap until the next restart.

### Four preflight checks before writing a new scope

Before the first write for a new team- or scope-specific metric:

1. **The team tag or scope literal actually exists in the catalog** — a `dataset_filter` that
   references a nonexistent tag URN is syntactically valid and resolves to an empty scope, which
   is indistinguishable from "correct filter, no matching datasets yet" without checking.
2. **The `metric_id` is free** — `GET /spoke/governance/metric/{metric_id}` directly; do not infer
   availability from an incomplete paginated list page. A taken id is `409 METRIC_EXISTS` on
   create.
3. **The filter matches a non-empty scope** — no public pre-create route resolves an arbitrary new
   filter, so this is confirmed only after creation, via `GET .../{metric_id}/dataset` (see the
   guided flow's scope-review step in `SKILL.md`) — not claimed as validated before then.
4. **The user actually wants all three built-in types for this scope**, or only some of them — a
   one-team convention (`<metric_type>_<team>` or similar) commonly registers all three together,
   but that's a choice to confirm, not assume.

## JSON scaffolds

JSON is the authoring representation — it's the format the API accepts. Convert YAML only if the
user brings it (§YAML below).

### Ingestion freshness

```json
{
  "metric_id": "prod-ingestion-freshness",
  "mode": "active",
  "is_enabled": false,
  "metric_type": "ingestion-freshness",
  "title": "Production ingestion freshness",
  "description": "Counts primary production datasets with ingestion evidence inside a two-day window.",
  "metrics": [
    {"name": "total", "color": "#64748B", "idx": 1},
    {"name": "ingested_in_time", "color": "#22C55E", "idx": 2}
  ],
  "metric_conf": {"time_window_sec": 172800},
  "schedule_tier": "daily",
  "dataset_filter": "origin = 'PROD' AND is_primary = true"
}
```

### Validation score

Only `cadence_offset` shifts the anchored window (`cadence_unit` alone, at `offset: 0`, has no
effect) — a dataset left at the validation conf's default (no lag) when its data actually arrives
with a declared lag reads as failing this metric on every run even though nothing is actually
stale. A genuinely infrequent dataset with *no* lag needs a wider `time_window_sec` on this metric
instead; widening `cadence_unit` alone on the validation conf changes nothing here.

```json
{
  "metric_id": "prod-validation-score",
  "mode": "active",
  "is_enabled": false,
  "metric_type": "validation-score",
  "title": "Production validation coverage",
  "description": "Counts primary production datasets configured and validated within their two-day window.",
  "metrics": [
    {"name": "valid_confd", "color": "#3B82F6", "idx": 1},
    {"name": "valid_in_time", "color": "#22C55E", "idx": 2}
  ],
  "metric_conf": {"time_window_sec": 172800},
  "schedule_tier": "daily",
  "dataset_filter": "origin = 'PROD' AND is_primary = true"
}
```

### Documentation health

```json
{
  "metric_id": "governed-doc-health",
  "mode": "active",
  "is_enabled": false,
  "metric_type": "doc-health",
  "title": "Governed asset documentation health",
  "description": "Counts primary datasets in the governed catalog scope that meet documentation health.",
  "metrics": [
    {"name": "total", "color": "#64748B", "idx": 1},
    {"name": "doc_health", "color": "#A855F7", "idx": 2}
  ],
  "metric_conf": {},
  "schedule_tier": "weekly",
  "dataset_filter": "'urn:li:tag:area:catalog' IN tag_urns AND is_primary = true"
}
```

### UC5 Imazon sequence: enabled DEV doc health

Deliberately different from the three safe scaffolds above: creates an **enabled** daily DEV
doc-health metric, then immediately runs it non-dry. Preserve these wire fields if the user asks
for that scenario, but display the derived JSON and get confirmation first for the enabled create
and again for the run.

```json
{
  "metric_id": "doc-health-dev",
  "mode": "active",
  "is_enabled": true,
  "metric_type": "doc-health",
  "title": "Doc Health (DEV)",
  "description": "Daily documentation-completeness check across DEV datasets",
  "metrics": [
    {"name": "total", "color": "#2563EB", "idx": 1},
    {"name": "doc_health", "color": "#16A34A", "idx": 2}
  ],
  "metric_conf": {},
  "schedule_tier": "daily",
  "dataset_filter": "origin = 'DEV'"
}
```

## Writing the request

Write the verified JSON to a scratch file with the `Write` tool and send it with `dataspoke-api`'s
`@PATH` body form — never inline a `dataset_filter` (which routinely contains single quotes, e.g.
`origin = 'PROD'`) as a literal shell argument:

```
Write /tmp/metric.json:
{"metric_id":"prod-ingestion-freshness","mode":"active","is_enabled":false,"metric_type":"ingestion-freshness","title":"Production ingestion freshness","description":"Counts primary production datasets with ingestion evidence inside a two-day window.","metrics":[{"name":"total","color":"#64748B","idx":1},{"name":"ingested_in_time","color":"#22C55E","idx":2}],"metric_conf":{"time_window_sec":172800},"schedule_tier":"daily","dataset_filter":"origin = 'PROD' AND is_primary = true"}
```
```bash
dataspoke-api --confirm POST /spoke/governance/metric @/tmp/metric.json
```

For `PUT`/`PATCH`, construct the verified operation-specific JSON first — omitting `metric_id` —
write it the same way, and pass it as `@PATH`.

Use `PUT` only for intentional full replacement and `PATCH` only for an intentional partial
change — `metric_conf` is replaced **wholesale** on `PATCH`, not deep-merged: patching only
`metric_type` without also resending its matching `metrics`/`metric_conf` fails, because the
*merged* definition is what gets validated. Before enabling a schedule with
`{"is_enabled": true}`, explain that scheduled execution begins according to `schedule_tier`,
recommend a successful dry run first, and confirm immediately before the `PATCH`. For deletion,
explain that `DELETE .../{metric_id}/attr/conf` removes the metric definition (and, for a factory
metric, that the row reappears on the next API start — see above), then confirm.

## YAML — optional authoring aid, only if the user brings it

The wire format is always JSON; treat any YAML the user supplies as a JSON-compatible notation,
not general YAML. Accept only mappings with string keys, arrays, strings, finite JSON numbers,
`true`, `false`, and `null`; reject the document before preview or send if it contains duplicate
mapping keys, merge keys (`<<`), tags, anchors or aliases, timestamps, binary values, or
non-finite numbers (`.nan`/`.inf`). Convert it losslessly, show both representations, and compare
the parsed YAML value tree against the value tree parsed back from the serialized JSON preview —
keys, array order, scalar types, and values must be equal, or stop and show the mismatch rather
than send. JSON object key order and whitespace are not semantic.

## Interpreting reads

- `/attr/result` rows are the persisted history: each has `values` and a per-dataset `breakdown`
  containing only affected/failing datasets. The immediate `method/run` detail has `values`,
  `unresolved_urns`, and `breakdown_summary`; its `dataset_count` is the resolved scope and its
  `affected_count` is the size of that breakdown. If a ratio such as `ingested_in_time / total` is
  useful, label it client-derived and handle a zero denominator; never present it as a stored
  server value.
- `unresolved_urns` — well-formed dataset URN literals that matched no registered dataset at run
  time. Report them; do not silently drop them or rewrite the filter to make them disappear.
- Dataset `met: "true"` means the latest persisted evaluation met the criterion; `"false"` means it
  did not; `"unknown"` means the dataset is in scope but has no verdict (never evaluated, newly in
  scope, or not evaluable for that metric type). A non-dry run replaces the metric's persisted
  verdict set wholesale; a dry run leaves the prior set unchanged and persists no result, verdict
  replacement, or event.
- `last_check_at` is per-dataset evidence/run timing. `attrs_synced_at` is the newest registry
  attribute-sync timestamp among datasets in this metric's scope — scope-relative, unaffected by
  verdict filtering or paging, and neither measurement time nor registry-wide freshness.
- `/event` is the persisted feed of run-completion and definition-change events. Do not claim
  dry-run history beyond what the live schema explicitly exposes.

## Errors: stop and surface, never work around

- `403 READ_ONLY_ROLE`: send to `/dataspoke:dataspoke-access` rather than retrying.
- `409 METRIC_EXISTS`: create id already exists — inspect it, ask whether the user intends an
  explicit update or a different id.
- `404 METRIC_NOT_FOUND`: update/run target is absent — do not turn the failed update into an
  implicit create.
- `409 METRIC_DISABLED`: a non-dry run requires an enabled metric — offer a dry run or an
  explicitly confirmed enable operation.
- `409 METRIC_RUNNING`: another run is active — report it and inspect events rather than launching
  a parallel workaround.
- `501 NOT_IMPLEMENTED` for passive mode: reserved — do not emulate it elsewhere.
- `422 INVALID_PARAMETER`: a definition field is malformed (bad `metric_id` slug, unsupported
  `metric_type`, a `metrics[].name` not one of the type's emitted series, a duplicate `name`/`idx`,
  a `time_window_sec` outside range). Fix the JSON against the live schema; do not retry the same
  body.
- `422 INVALID_DATASET_FILTER`: show the API detail and character position, then help edit the
  filter — never widen scope to dodge the error.
- `422 INVALID_DATASET_URN`: surface the malformed literal and require correction.
