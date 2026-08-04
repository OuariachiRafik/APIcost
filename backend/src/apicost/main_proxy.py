"""ASGI entrypoint — data plane (port 8000).

This process sits on the critical path of somebody's production application.
Its constraints (CODEBASE_GUIDE §2): under 100 ms of added latency, stateless,
fail-open, and no synchronous Postgres writes. Nothing that violates those may
be mounted here.

P0 exposes health endpoints only. ``/v1/chat/completions`` and
``/v1/embeddings`` arrive in P2 via ``proxy/ingress.py``.
"""

from __future__ import annotations

from fastapi import FastAPI

from apicost.app import create_app

app: FastAPI = create_app(
    service="proxy",
    title="APICost Proxy",
    description="OpenAI-compatible proxy: cache, route, log, protect.",
)
