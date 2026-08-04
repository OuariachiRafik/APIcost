# ADR 0003 — Shared ASGI app factory at `apicost/app.py`

**Status:** accepted · **Date:** 2026-08-04 · **Phase:** P0

## Context

BUILD_SPEC §2 fixes the proxy and the dashboard API as "two ASGI apps in one codebase, deployed as
separate processes", and §3 gives them `main_proxy.py` and `main_api.py`. Both apps need identical
request-id binding, RFC 7807 error handlers, and the `/healthz` + `/readyz` pair that P0's acceptance
criteria test on both ports.

Written twice, that is roughly fifty duplicated lines, including a readiness probe that is part of
the deployment's health contract. Divergence between the copies would be silent — each app's tests
would still pass against its own stale version.

## Decision

Add `apicost/app.py` exporting `create_app(...)` and a `health_router`. Each `main_*.py` stays a thin
entrypoint: it calls `create_app` with its own service name, title, and CORS setting, and from P1
onward mounts its own routers.

The two apps remain genuinely separate — separate `FastAPI` instances, separate processes, separate
ports, separate connection pools. Only their construction is shared.

## Consequences

- This is a **deviation from BUILD_SPEC §3**: one added file. `main_proxy.py` and `main_api.py` still
  exist and are still the entrypoints named in `docker-compose.yml`.
- Health behavior is defined once. A change to `/readyz` reaches both planes or neither.
- The shared factory must not accumulate plane-specific behavior. Data-plane concerns (fail-open
  wrappers, the shared `Deadline`, streaming) belong in `proxy/`, and control-plane concerns in
  `api/`. If `create_app` starts growing `if service == "proxy"` branches, that is the signal this
  decision needs revisiting.
