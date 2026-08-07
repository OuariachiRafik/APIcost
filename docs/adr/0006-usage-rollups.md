# ADR 0006 — Daily usage rollups

**Status:** accepted · **Date:** 2026-08-05 · **Phase:** P3

## Context

BUILD_SPEC §4 P3 requires "usage endpoints respond in <500 ms p95 against 1M seeded ledger rows".
The endpoints were built to aggregate `requests_log` directly. Measured against 841k seeded rows,
after fixing partition pruning and collapsing two scans into one:

| Endpoint | p95 | Budget |
|---|---|---|
| `/usage?range=30d` | 2,283 ms | 500 ms |
| `/usage?range=90d` | 3,756 ms | 500 ms |
| `/usage/breakdown?by=model` | 1,283 ms | 500 ms |
| `/usage/token-distribution` | 2,475 ms | 500 ms |
| `/requests?limit=50` | **77 ms** | 500 ms |

The request log is fine — keyset pagination touches 50 rows regardless of table size, and deep pages
measured *faster* than early ones. The aggregations are not fine, and no amount of indexing fixes
them: summing 800,000 rows means reading 800,000 rows. A covering index would buy perhaps 3×, which
is still several times over budget at 90 days, and the gap widens as a user's history grows — which
is precisely backwards from what a dashboard should do.

## Decision

Pre-aggregate into daily rollups, maintained by the worker, and serve the aggregation endpoints from
them.

Two tables, because they have different grains:

- **`usage_rollup_daily`** — keyed by `(user_id, project_id, day, model_used, endpoint, provider)`.
  Serves the spend series and every breakdown dimension by summing a few hundred rows instead of a
  few hundred thousand. For one user with 90 days of history across 5 models, 2 endpoints and 3
  providers, that is a few thousand rows at most.
- **`token_bucket_rollup_daily`** — keyed by `(user_id, project_id, day, bucket_index)`. Histogram
  buckets cannot be derived from summed totals, so they get their own grain.

Rollups are **recomputed, not incremented**: the worker deletes and rebuilds the last N days on each
pass. Incremental counters drift the moment anything is retried, backfilled, or corrected, and a
spend figure that silently drifts is worse than one that is a few minutes stale. Recomputation is
idempotent and self-healing.

The **request log keeps reading `requests_log` directly.** It is already fast, it needs per-request
detail a rollup cannot carry, and it is the screen users check when they distrust the aggregates —
it should not share a failure mode with them.

## Consequences

- **This is an addition to BUILD_SPEC §7's data model.** The spec did not anticipate it; the
  performance criterion it does state cannot be met without it.
- **Aggregates lag by up to the rollup interval** (5 minutes). Endpoints report `stale_after` so the
  UI can say so rather than implying real-time precision. The request log stays live, so anyone
  wanting to confirm a specific call has an accurate place to look.
- `requests_log` remains the system of record. Rollups are derived and disposable: dropping and
  rebuilding them loses nothing.
- P6's rolling statistics and P8's advisory jobs can read rollups instead of raw rows, which is
  where the next scaling problem would otherwise have appeared.
