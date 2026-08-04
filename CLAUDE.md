# CLAUDE.md

APICost — a proxy-based LLM cost-optimization product for solo developers. Users swap their API base
URL; we cache, route, log, and protect their spend, then show them the savings.

## Read first

- `docs/BUILD_SPEC.md` — the authoritative build plan. Phases, decisions, acceptance criteria.
- `docs/CODEBASE_GUIDE.md` — architecture, request lifecycle, invariants, glossary.
- `docs/use-cases.md` — UC-01..UC-39. Every feature traces to one of these.

## How to work here

- **One phase at a time.** Phases are defined in `BUILD_SPEC.md` §4. Do not start the next phase until
  the current phase's acceptance criteria pass.
- Before writing code for a phase, restate the acceptance criteria and the use-case IDs you're
  satisfying, and confirm the plan.
- Write the tests for a component in the same change as the component.
- If you need to deviate from the spec, write a short ADR in `docs/adr/` explaining why, and say so
  explicitly rather than silently diverging.
- Update `docs/CODEBASE_GUIDE.md` in the same commit as any architectural change.

## Hard rules

1. **Fail open.** If the cache, router, stats, or logging path fails or exceeds its budget, forward
   the original request unmodified to the originally requested model. The only exception is a
   `hard_stop` budget, which fails closed.
2. **150 ms total** for all optimization work, enforced by one shared `Deadline`, not per-step
   timeouts.
3. **No secrets in logs**, responses, error messages, or stack traces. Ever.
4. **No plaintext provider keys at rest.** Envelope encryption, decrypted in memory only at
   forward-time.
5. **Every user-scoped query** is filtered by `user_id` in the app *and* protected by RLS in Postgres.
6. **Never modify the response body schema** returned to the caller. APICost metadata goes in headers.
7. **No synchronous Postgres writes on the proxy critical path.** Ledger events go to a Redis stream.
8. **Config lives in `config.py`.** No `os.environ` reads anywhere else.
9. **Raw prompt/response text is not stored** unless the project opts in.

## Commands

```
make dev       # docker compose up: postgres, redis, mailpit, proxy, api, worker, web
make test      # pytest + vitest
make lint      # ruff, mypy, eslint
make migrate   # alembic upgrade head
make revision  # alembic revision --autogenerate
make seed      # demo user + synthetic ledger history
```

Ports: proxy `:8000`, dashboard API `:8001`, web `:5173`, mailpit `:8025`.

## Stack (decided — don't re-litigate)

Python 3.12 · FastAPI · SQLAlchemy 2.0 async · Alembic · Postgres 16 + pgvector · Redis 7 · ARQ ·
fastembed (bge-small-en-v1.5) · scikit-learn · React 18 + TypeScript + Vite + Tailwind + TanStack
Query + Recharts · Stripe · Resend.

## Style

- `ruff` for lint and format; `mypy --strict` on `apicost.core`, `apicost.metrics`, `apicost.stats`,
  `apicost.advisor`.
- Type hints everywhere. Pydantic v2 models at every boundary.
- Keep `metrics/`, `stats/`, and `advisor/breakeven.py` pure — no I/O, no ORM imports.
- Prefer explicit over clever, especially in `proxy/pipeline.py`.
