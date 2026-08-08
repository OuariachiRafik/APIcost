# P4 — Semantic caching

**Use cases:** UC-20, UC-21, UC-22, UC-23, UC-24, UC-25
**Status:** ✅ complete — all four acceptance criteria pass

## Acceptance criteria

| # | Criterion | Result |
|---|---|---|
| 1 | Equivalent-but-different prompts hit at the default threshold | ✅ against the real embedding model |
| 2 | Raising the threshold to 0.99 makes it a miss | ✅ |
| 3 | Cache hits return in <30 ms p95 | ✅ **7.4–23.5 ms in-proxy** (was 36–48 ms; see below). Measured in-process per [ADR 0007](../adr/0007-cache-hit-latency-budget.md) |
| 4 | Dollars-saved reconciles exactly with `requests_log` | ✅ hits cost 0; avoided cost recorded |

The e2e cache suite runs clean: three consecutive full-file runs, 11/11 each.

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

## The latency gap, found by profiling

Per-stage instrumentation (`StageTimer`, the stages BUILD_SPEC §6.6 asks for) turned a 30 ms mystery
into a one-line bug in about ten minutes, after I had wasted considerably longer guessing at it.

The breakdown pointed straight at it:

```
t_respond        31.04 ms      <- everything between lookup and response
t_cache_lookup    0.82 ms
t_policy          0.09 ms
t_record_hit         absent    <- never fired
```

`t_record_hit` only fires on an exact-hash hit. Its absence, next to 31 ms in the stage that follows,
said the exact path was **missing every single time** and falling through to embedding plus a vector
search.

The cause: `store()` wrote a bare entry id into Redis while `_lookup_exact` had been changed to read
a JSON blob. Every lookup hit `json.JSONDecodeError`, which is caught and treated as a miss — by
design, since a malformed cache entry should not fail a request. So the cache kept returning correct
answers via the slow path, and **no behavioural test could see it**. An identical prompt embeds to
cosine ~1.0, so a vector hit is indistinguishable from an exact hit by its result.

**11.14 ms p95** after the fix. `tests/integration/test_cache_store_roundtrip.py` now asserts on the
mechanism — that what `store` writes is what `lookup_exact` reads, and that the Redis entry is
self-sufficient — because only a mechanism test catches this class of bug.

## The flakiness, and why neither cause was a product bug

**The threshold test sat on the boundary.** Its prompt pair scored cosine **0.9929**, and the test
asserted a *miss* at a 0.99 threshold — three thousandths of margin. Replaced with a pair measured at
**0.9812**, decisively between the 0.95 default and 0.99.

**The embedding budget is marginal under load.** Embedding measures 14 ms p50 against its 40 ms
budget on a quiet machine, but a full test run can push it over — at which point the pipeline
correctly skips the cache write, and the next assertion finds an empty cache. The budget is widened
for this test module only; 40 ms remains the production figure. The product behaviour was right, the
test was measuring the host's spare CPU.

## Not built

`GET /cache/stats` (UC-25) and `POST /cache/invalidate` (UC-23) have their service-layer
implementations in `cache/semantic.py` but no HTTP endpoints or UI yet.
