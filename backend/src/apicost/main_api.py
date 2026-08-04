"""ASGI entrypoint — control plane (port 8001).

The dashboard's REST API. Not on anyone's critical path, so it may run heavy
queries and slower work (CODEBASE_GUIDE §2). It must never be able to exhaust
the proxy's connection pool — which is why it runs as a separate process with
its own pool, not as another router on the proxy app.

P0 exposes health endpoints only. Auth, keys, projects, usage, and the rest
arrive from P1 onward via ``api/routers/``.
"""

from __future__ import annotations

from fastapi import FastAPI

from apicost.app import create_app

app: FastAPI = create_app(
    service="api",
    title="APICost Dashboard API",
    description="Control plane: accounts, projects, keys, usage, and advice.",
    enable_cors=True,
)
