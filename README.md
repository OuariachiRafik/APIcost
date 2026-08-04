# APICost

A proxy-based LLM cost-optimization product for solo developers. Swap one config value — your API
base URL — and APICost caches, routes, logs, and protects your spend, then shows you what it saved.

- **Proxy (data plane)** — OpenAI-compatible HTTP service on the critical path of your app.
- **Dashboard (control plane)** — React app + REST API where you configure and observe.

## Documentation

| Read this | For |
|---|---|
| [`docs/BUILD_SPEC.md`](docs/BUILD_SPEC.md) | The authoritative build plan: phases, locked decisions, acceptance criteria |
| [`docs/CODEBASE_GUIDE.md`](docs/CODEBASE_GUIDE.md) | Architecture, request lifecycle, invariants, glossary |
| [`docs/use-cases.md`](docs/use-cases.md) | UC-01..UC-39 — every feature traces to one |
| [`docs/adr/`](docs/adr/) | Decisions that deviate from or extend the spec |

## Status

**Phase 0 complete** — scaffolding and infrastructure. Both ASGI apps serve health endpoints; there
are no product endpoints yet. Auth, projects, and keys land in P1; the proxy path in P2.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) — manages both dependencies and the pinned Python 3.12
  (`curl -LsSf https://astral.sh/uv/install.sh | sh`). You do **not** need a system Python 3.12.
- Docker with Compose v2 (`docker compose`, not `docker-compose`)
- Node 20+
- `make`

## Getting started

```bash
cp .env.example .env     # adjust if you like; defaults work for local dev
make install             # uv sync + npm install
make dev                 # postgres, redis, mailpit, proxy, api, worker, web
make migrate             # apply Alembic migrations
make test                # pytest + vitest
make lint                # ruff, mypy, eslint, prettier
```

| Service | URL |
|---|---|
| Proxy (data plane) | http://localhost:8000 |
| Dashboard API (control plane) | http://localhost:8001 |
| Web | http://localhost:5173 |
| Mailpit | http://localhost:8025 |

Verify the stack:

```bash
curl -s localhost:8000/readyz
curl -s localhost:8001/readyz
```

Both return `200` with `{"status":"ready","checks":{"postgres":true,"redis":true}}`.

`make help` lists every target.

## Layout

```
backend/src/apicost/
  config.py        the only place that reads the environment
  main_proxy.py    ASGI app — data plane  (:8000)
  main_api.py      ASGI app — control plane (:8001)
  core/            logging + redaction, errors, ids, security, deadline
  db/              engine, session, RLS scoping, Redis
  proxy/ cache/ routing/     data-plane subsystems
  ledger/ stats/ anomaly/ budgets/ advisor/ metrics/   analysis and enforcement
  vault/ notify/ billing/ api/ worker/                 control-plane subsystems
web/src/           React 18 + TypeScript + Vite + Tailwind + TanStack Query + Recharts
```

## Working on this codebase

Read `CLAUDE.md` first. The rules that matter most:

1. **Fail open.** If cache, routing, stats, or logging fails, forward the original request unchanged.
   The only fail-closed path is a `hard_stop` budget.
2. **150 ms** total for all optimization work, on one shared `Deadline`.
3. **No secrets in logs, responses, or stack traces.** Ever.
4. **One phase at a time.** Do not start the next phase until the current one's acceptance criteria
   pass.
