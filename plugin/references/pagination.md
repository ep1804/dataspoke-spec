# Pagination — read every DataSpoke list route to completion

Every skill that reads a collection follows this doctrine. Reporting a first page as the
complete collection is the most common way a skill misinforms a user ("only 3 sources", "only 12
datasets are validated") when the true count is larger.

## The standard envelope

Every list route returns the same three keys:

```json
{"results": [...], "offset": 0, "limit": 20, "total_count": 137}
```

- `limit` defaults to **20** unless a route documents a different default.
- Keep requesting the next `offset` (`offset += limit`) until `offset + len(results) >=
  total_count`, or `results` comes back empty.
- Never stop after one page and report it as the whole set. If a task only needs to *check for
  existence* of a match (e.g. "does a source named X exist"), that is a different question from
  "list everything" — say which one is being answered.
- `total_count` is authoritative for the collection size; do not infer it from `len(results)` on
  a single page.

## The one documented deviation: validation result history

`GET /spoke/common/data/{dataset_urn}/attr/validation/result` still returns the standard
`offset`/`limit`/`total_count` envelope, but differs in three ways from every other list route —
verify the exact values against `bin/dataspoke-schema attr/validation/result` or
[`API.md`](../../spec/API.md), since a deployment may not match this summary:

| | Other list routes | Validation result history |
|---|---|---|
| Time-bound params | `from` / `to` | `from` / **`until`** |
| Default `limit` | 20 | raised (hundreds–thousands) |
| Max `limit` | — | capped |
| Order | insertion / creation | fixed `data_time DESC` |

Because the window is time-bounded rather than page-bounded, traverse past a page by narrowing
`from`/`until` to the unread remainder of the range rather than by incrementing `offset` — the
window is what determines the result set on this route, and `offset` addresses a page inside
whatever the window already returned.

The window is **half-open**: `from <= data_time < until`. Passing the same value for both matches
nothing. A validation baseline read — used by a routine deciding a score — always supplies `until`
equal to the `data_time` under judgment, so the baseline contains neither that point nor any
future point; see `dataspoke-validation`'s own references for why this bound is load-bearing.

## Every other time-bounded collection

Event and result-history routes outside validation use `from`/`to` (not `from`/`until`) alongside
the standard `offset`/`limit`/`total_count` envelope — confirm the exact parameter names for a
given route against live OpenAPI before relying on this summary, since a route's parameter names
are part of its own contract, not a plugin-wide guarantee.
