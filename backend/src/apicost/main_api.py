"""ASGI entrypoint — control plane (port 8001).

The dashboard's REST API. Not on anyone's critical path, so it may run heavy
queries and slower work (CODEBASE_GUIDE §2). It must never be able to exhaust
the proxy's connection pool — which is why it runs as a separate process with
its own pool, not as another router on the proxy app.

P1 adds auth, provider keys, projects, and proxy keys. Usage, cache, routing,
budgets, alerts, advisor, and billing routers arrive with their phases.
"""

from __future__ import annotations

from fastapi import FastAPI

from apicost.api.routers import auth, connection, keys, projects, proxy_keys
from apicost.app import create_app

app: FastAPI = create_app(
    service="api",
    title="APICost Dashboard API",
    description="Control plane: accounts, projects, keys, usage, and advice.",
    enable_cors=True,
)

app.include_router(auth.router)
app.include_router(keys.router)
app.include_router(projects.router)
app.include_router(proxy_keys.router)
app.include_router(connection.router)
