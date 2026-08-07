# P4 — Semantic caching

**Use cases:** UC-20, UC-21, UC-22, UC-23, UC-24, UC-25
**Status:** functionally implemented; **two acceptance items outstanding**

## Acceptance criteria

| # | Criterion | Result |
|---|---|---|
| 1 | Equivalent-but-different prompts hit at the default threshold | ✅ against the real embedding model |
| 2 | Raising the threshold to 0.99 makes it a miss | ✅ |
| 3 | Cache hits return in <30 ms p95 | ❌ **36–48 ms**, and the gap is not fully explained |
| 4 | Dollars-saved reconciles exactly with `requests_log` | ✅ hits cost 0; avoided cost recorded |

Every test passes in isolation. The e2e cache file is **flaky when run end-to-end** — a different
test fails on each run — which is itself an outstanding item, not a passing result.

## What shipped

- `cache/policy.py` — cacheability rules and prompt normalization (38 tests, pure).
- `cache/embeddings.py` — `bge-small-en-v1.5` via fastembed, warmed at startup, run in a thread so
  synchronous ONNX work cannot stall the event loop, with a 40 ms budget after which the request
  proceeds as a miss.
- `cache/semantic.py` — two-tier lookup, per-entry envelope encryption, invalidation, expiry.
- Migration 0007 — `cache_entries` with an HNSW cosine index and RLS.
- Pipeline integration inside `failopen`, SSE replay for streamed cache hits.
- `cache/maintenance.py` and worker jobs for hit-counter flushing and expiry sweeps.

## Defects found and fixed

**1. `record_hit` violated hard rule 7.** It issued a synchronous Postgres `UPDATE` on the proxy
critical path — for a counter. It now increments a Redis hash that the worker folds in.

**2. Project settings changes had no effect for up to 60 seconds.** The cached `ResolvedKey` carries
the project's threshold and toggles, and `PUT /settings` did not purge it. A user moving the
similarity slider would have seen nothing happen, then it would start working — which reads as a
broken product. Settings changes now purge the auth cache, as revocation does.

**3. The cache write could destroy the ledger row.** On the streaming path the cache write ran first
inside the generator's `finally`; an exception or cancellation there took `_record_stream` with it,
so **every streamed request silently lost its ledger row**. Caught by P2's tests. Ledger first now —
it is the system of record, a cache entry is disposable.

**4. Embedding ran before the exact-hash lookup**, paying tens of milliseconds on the fast path it
exists to avoid. Exact hash is now checked first, with no embedding and no database session.

## Outstanding

**Cache-hit latency: 36–48 ms against a 30 ms budget.** Measured floor on this machine is 1.8 ms of
HTTP and 0.45 ms per Redis round trip; the hit path makes five of those, so the work should cost
under 10 ms. **Roughly 30 ms is unaccounted for and I did not find it** — I was guessing rather than
profiling, and stopped. The benchmark is marked `perf` so it does not fail the functional suite, but
it is not silenced: `make bench` runs it and it is red.

**The e2e cache suite is flaky.** Each test passes alone; a different one fails on each full-file
run. The tests share process-wide state — the embedder, engines, Redis — across per-test live
servers, and the earlier event-loop/engine-disposal issues in this area suggest the same root cause.
Needs isolating before the suite can be trusted.

## Not built

`GET /cache/stats` (UC-25) and `POST /cache/invalidate` (UC-23) have their service-layer
implementations in `cache/semantic.py` but no HTTP endpoints or UI yet.
