# P0 — Scaffolding & infrastructure

**Use cases:** none. P0 is `—` in both the phase table and the traceability matrix.
**Commit:** `ff2707f`

## Acceptance criteria

| # | Criterion | Result |
|---|---|---|
| 1 | `make dev` brings up both apps and dependencies | ✅ postgres, redis, mailpit, proxy, api, worker, web |
| 2 | Both `/readyz` return 200 | ✅ `{"status":"ready","checks":{"postgres":true,"redis":true}}` |
| 3 | `make test` passes, zero failures | ✅ 49 backend + 6 web |

Also verified per §11: Alembic `upgrade → downgrade → upgrade` actually drops and recreates the
`vector` extension; ruff, mypy (strict on `core`), eslint and prettier clean.

## What shipped

- Repo layout exactly per §3, with an `__init__.py` home for every package later phases fill.
- `config.py` as the single environment reader (hard rule 8).
- `core/logging.py`: structlog JSON, `request_id` context var, secret redaction.
- `core/errors.py`: typed exceptions and RFC 7807 handlers.
- `db/`: async engine, session factory, `set_config`-based RLS scoping.
- Two ASGI apps sharing `create_app()`; compose v2 with health-gated startup; CI.
- Web scaffold: React 18 + TS + Vite + Tailwind + TanStack Query + Vitest.

## Defects found

**Redaction was not idempotent.** With filters on both a logger and its handler,
`apc_live_***REDACTED***` re-matched as `apc_` + `live_` and accumulated placeholders. Fixed with a
possessive quantifier plus a trailing lookahead. The secret was never exposed — the output was just
progressively mangled — but non-idempotent redaction is the kind of thing later code comes to rely
on.

## Decisions recorded

- [ADR 0001](../adr/0001-uv-as-python-toolchain.md) — uv manages dependencies *and* the pinned 3.12,
  because the dev machine's `python3` is a conda 3.13 that shadows everything.
- [ADR 0002](../adr/0002-shared-redis-client-module.md) — `db/redis.py`, an addition to §3.
- [ADR 0003](../adr/0003-shared-asgi-app-factory.md) — `app.py`, so `/readyz` cannot be fixed on one
  plane and forgotten on the other.

## Environment notes

Postgres is published on host port **5433**, not 5432. A developer with a system Postgres on the
default port would otherwise have `make migrate` run against their own database.
