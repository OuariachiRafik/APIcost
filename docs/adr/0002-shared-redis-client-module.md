# ADR 0002 — A shared Redis client module at `db/redis.py`

**Status:** accepted · **Date:** 2026-08-04 · **Phase:** P0

## Context

BUILD_SPEC §3 lists three files under `db/`: `base.py`, `session.py`, and `models.py`. It does not
name a home for the Redis client, yet Redis is required by four separate subsystems across later
phases — proxy-key auth caching (§6.1), budget counters (§4 P6), rolling-stats checkpoints (§6.5),
and the ledger stream (§4 P2) — and by the `/readyz` endpoint in P0 itself.

Three options were considered:

1. Construct a client inside each consumer. Produces four connection pools per process and four
   places to get the connection settings wrong.
2. Put it in `db/session.py`. That file's stated purpose in §3 is "async engine, session factory, RLS
   session var" — all Postgres. Adding a second datastore there muddies it.
3. Add `db/redis.py`.

## Decision

Add `db/redis.py`, holding a lazily-constructed process-wide `redis.asyncio.Redis` and the
`check_redis` readiness probe. It mirrors `db/session.py`'s shape exactly: a `get_*` accessor, a
`close`/`dispose` for shutdown, and a `check_*` probe returning `bool` rather than raising.

## Consequences

- This is a **deviation from BUILD_SPEC §3**, which said to create exactly that structure. It adds
  one file; it moves nothing and renames nothing.
- `db/` now means "datastore connectivity" rather than "Postgres". `CODEBASE_GUIDE.md` §4 has been
  updated to say so.
- Consumers in P1+ import `get_redis()` instead of building their own client, so connection limits
  and TLS settings are configured in one place.

If a future phase wants the original structure back, the merge target is `db/session.py`; nothing
else imports these symbols by module path.
