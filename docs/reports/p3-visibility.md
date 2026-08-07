# P3 — Visibility & reporting

**Use cases:** UC-08, UC-09, UC-10, UC-11, UC-12, UC-13 (and UC-28's data, ahead of P7)

## Acceptance criteria

| # | Criterion | Result |
|---|---|---|
| 1 | All six use cases visible in the UI with real data from P2 | ✅ dashboard, breakdowns, histogram, request log with detail drawer, CSV export |
| 2 | Usage endpoints respond in <500 ms p95 against 1M seeded ledger rows | ✅ **after two fixes** — see below |

## The performance criterion failed, twice, before it passed

This is the most useful thing in this report. The endpoints were written the obvious way — aggregate
`requests_log` directly — and measured against 841k seeded rows:

| Endpoint | First measurement | After fixes | Budget |
|---|---|---|---|
| `/usage?range=30d` | 4,051 ms | **87 ms** | 500 ms |
| `/usage?range=90d` | 3,756 ms | **15 ms** | 500 ms |
| `/usage/breakdown?by=model` | 1,283 ms | **16 ms** | 500 ms |
| `/usage/breakdown?by=endpoint` | 1,485 ms | **11 ms** | 500 ms |
| `/usage/token-distribution` | 2,475 ms | **15 ms** | 500 ms |
| `/requests?limit=50` | 77 ms | 16 ms | 500 ms |

**Fix 1 — the ledger partitions only ever grew forwards.** Migration 0003 created partitions from the
deploy date onward, so *every* row older than that fell into the DEFAULT partition: 810k of 841k in
this case. DEFAULT cannot be pruned by a range predicate, so "last 30 days" scanned all history. This
would have hit any backfill, any data import, and every developer's seeded database. Migration 0005
creates a window either side of today and rehomes the orphaned rows; `ensure_partitions` now
maintains backwards as well as forwards.

**Fix 2 — aggregation cannot meet the budget at all.** Even with pruning fixed, summing hundreds of
thousands of rows takes hundreds of milliseconds and gets worse as a user's history grows, which is
exactly backwards for a dashboard. [ADR 0006](../adr/0006-usage-rollups.md) adds daily rollups
maintained by the worker. 841,195 raw rows collapse to **2,036 rollup rows**.

Worth noting what did *not* need fixing: the request log was already fast, and deep pages measured
*faster* than early ones (73 ms at page 40 vs 77 ms at page 1). Keyset pagination behaving exactly as
intended.

## What shipped

- `GET /usage`, `/usage/breakdown`, `/usage/token-distribution`, `/usage/export.csv` (streaming),
  `GET /requests`, `GET /requests/{id}`.
- `ledger/rollup.py` and the worker job that keeps rollups fresh.
- `make seed rows=N` — shaped synthetic history, not uniform noise: weekday rhythm, a long tail of
  model usage, a 3% long-context tail, occasional errors.
- Web: nav shell, spend overview with a savings-versus-spend chart, breakdown table, request-size
  histogram, request log with filters and a detail drawer, CSV export.

## Two honesty notes in the API

- **Percentiles are now bucket floors.** Deriving them from the rollup histogram means the exact
  value no longer exists. The fields are named `median_tokens_bucket` / `p95_tokens_bucket` rather
  than implying a precision the data cannot support.
- **Aggregates lag by up to a minute**; the request log stays live. Anyone who distrusts a number has
  an accurate place to check it.

## Decisions recorded

- [ADR 0005](../adr/0005-react-router.md) — react-router added. §2's stack list names no router while
  §3 assumes nine routes; this is an addition where the spec was silent, not a substitution.
- [ADR 0006](../adr/0006-usage-rollups.md) — daily rollups, with the measurements that forced them.

## The regression, and how it was fixed

**Proxy overhead went from 14.5 ms p95 (end of P2) to 122 ms (end of P3)**, measured on a quiet
machine, against a 100 ms NFR. This is real, not benchmark noise — it reproduces in isolation.

Where it comes from: the proxy does **one Postgres query per request** to load the caller's
encrypted provider key (`ingress._load_provider_key`). Profiled directly, a trivial `SELECT` against
the tiny `provider_keys` table now takes ~15 ms, against ~4 ms for a bare `SELECT 1` on the same
session. The query did not change; the database around it did — P3 added 22 monthly partitions with
five indexes each (139 relations) plus the rollup tables.

Why I stopped rather than fixing it now: the obvious fix is to stop querying Postgres on the hot
path at all, which CODEBASE_GUIDE §2 already says ("If you're tempted to add a database query here,
don't — put it in Redis or move it off the path"). But the thing being cached is *encrypted provider
key material*, and every option has a security consequence worth thinking about properly:

- caching ciphertext in Redis widens where key material lives;
- caching in-process conflicts with "no local state in the proxy" (§8.3) and means a deleted provider
  key keeps working until the TTL lapses.

**Resolved.** The encrypted blob is now cached in Redis, and the measured overhead went
**122 ms → 5.9 ms** — better than P2's 14.5 ms, because the hot path now makes *zero* Postgres
queries, which is what CODEBASE_GUIDE §2 asked for all along.

Why Redis rather than in-process:

- What is cached is AES-256-GCM ciphertext plus a KMS-wrapped data key. An attacker holding the
  whole Redis dataset ends up exactly where a stolen Postgres dump leaves them — nowhere, without
  the KMS master key. There is a test asserting precisely this.
- In-process caching is narrower exposure but strictly worse security: it breaks "no local state in
  the proxy" (§8.3) and, more importantly, a **deleted provider key would keep working** per
  instance with no way to purge it.
- Deletion and rotation purge the cache in the same operation as the database write — the contract
  proxy-key revocation already meets (UC-07). A removed key stops working immediately, not when a
  TTL lapses.

`tests/integration/test_provider_key_cache.py` covers the security properties rather than the speed:
no plaintext anywhere in Redis, the blob is undecryptable with the wrong master key, deletion purges
immediately, rotation clears stale entries, keys are scoped per user *and* provider, and a dead
Redis falls back to Postgres rather than failing the request.

## Also open

`GET /usage/breakdown?by=project` returns project **ids**, not names. The rollup does not carry the
name and joining per row would undo the point. The UI should resolve ids against `/projects`; it does
not yet.
