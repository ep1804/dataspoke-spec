---
name: dataspoke-governance
description: Guide DataSpoke Governance metric configuration and operation (UC5) on a deployed instance. Use for "how do I set up a governance metric", "track validation coverage for my team", "is this data documented", creating/updating/deleting/enabling/running metrics, and interpreting results, dataset scope/verdicts, freshness, unresolved URNs, or events. Supports ingestion-freshness, validation-score, and doc-health through the public REST API. Also triggers on the equivalent phrasing in the user's own working language.
argument-hint: "[question or metric action]"
allowed-tools: Read, Write, Bash(dataspoke-api *), Bash(dataspoke-schema *), AskUserQuestion
---

## Purpose and boundary

Guide the complete lifecycle of active Governance metrics through DataSpoke's public API. Every
call uses `dataspoke-api`. Never use admin/internal routes, the operational database, cluster
access, or direct DataHub access, and never bypass a missing public capability. If API access is
missing, send the user to `/dataspoke:dataspoke-access`.

The wire format is always JSON. `references/governance-metric.md` is the curated reference for
routes, definition fields, built-in series, the `dataset_filter` grammar, JSON scaffolds, and
errors — **read it before scaffolding or writing any definition**. The deployment's live OpenAPI
document, read through `dataspoke-schema`, remains authoritative whenever the two differ. Passive
metrics are reserved in this release: do not propose them (`mode: "passive"` is not implemented).

## Guided lifecycle

Scope, schedule, and configure one named active metric; create it disabled; review its resolved
scope; dry-run; enable; then return to its trend and affected datasets.

1. **Check access and inspect.** `GET /auth/me` — report the **account's** current role (not the
   token's effective role, which no route returns directly — a write `403` despite this passing
   means the token itself needs re-minting; see `/dataspoke:dataspoke-access`). List
   `GET /spoke/governance/metric` and, for a named metric, read `GET .../{metric_id}/attr/conf`.
   Writes require Editor or Admin. Confirm a `metric_id`'s availability with a direct `GET`, never
   by inferring it from an incomplete paginated page.
2. **Load the live contract.** Query the narrow relevant operation via `dataspoke-schema` (not
   `--list`) immediately before every definition write, every delete, and every dry or non-dry
   `method/run`. Validate the body against that live schema and its cross-field constraints.
3. **Scaffold.** Start from the matching JSON example in `references/governance-metric.md` and
   adapt title, description, schedule, series styling, and `dataset_filter` with the user. A new
   metric is scaffolded `is_enabled: false`. For a new scope, run the reference's four preflight
   checks (team tag exists, id is free, filter targets a real scope, user wants all three types)
   before writing anything.
4. **Preview and confirm.** Show the exact HTTP method, public route, and JSON body. Create a
   missing metric with `POST /spoke/governance/metric`; replace or partially change an existing
   one at `.../{metric_id}/attr/conf` with `PUT`/`PATCH` — never as an implicit upsert. An update
   that changes `dataset_filter` (or can otherwise change the resolved scope) writes with
   `is_enabled: false`, never leaving a prior schedule enabled against an unreviewed scope.
   Confirm immediately before the call; if state changed underneath it, stop and report the
   response instead of retrying blind.
5. **Inspect the resolved scope.** After a create or scope-changing update, page through
   `GET .../{metric_id}/dataset` and show the resolved set and its scope-relative
   `attrs_synced_at`. An empty or unintended scope returns to an explicitly confirmed definition
   edit — do not proceed to a dry run or enablement on an unreviewed scope.
6. **Exercise safely.** `dataspoke-api --confirm POST '.../{metric_id}/method/run?dry_run=true'`
   after the scope review and before enabling the schedule. A dry run still mutates nothing
   server-side but is a non-idempotent trigger, so it goes through the same confirmation gate as
   any other write; it evaluates but persists neither a result, a verdict replacement, nor an
   event.
7. **Enable deliberately.** Fresh confirmation before enabling scheduled execution, deleting a
   definition, or running non-dry — state the metric id, operation, reviewed scope, schedule
   effect, and exact JSON payload where applicable. For UC5's enabled Imazon doc-health example
   (`references/governance-metric.md`), confirm the enabled create body, then confirm the
   immediate non-dry run separately.
8. **Trend and act.** Query `/attr/result` over the chosen time range, inspect its persisted
   failing-dataset `breakdown`, and use `/dataset` and `/event` for current verdicts and recorded
   lifecycle changes.

An update that cannot change scope may take the shorter path — contract validation, exact payload
preview, confirmation, apply — without disabling and re-reviewing an unchanged dataset set.

The skill reports API errors without bypassing them: create/update identity semantics,
read-only-role rejection, disabled/concurrent-run conflicts, unsupported passive mode, invalid
filters, and unresolved dataset literals all surface verbatim. `references/governance-metric.md`
owns the full error table.

The three seeded factory metrics (`ingestion-freshness`, `validation-score`, `doc-health`) are
normally left unchanged — a team-specific policy is a new `metric_id`, not an edit to a factory
row. See `references/governance-metric.md` for why, and why deleting one isn't durable.
