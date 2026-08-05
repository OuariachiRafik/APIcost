"""ASGI entrypoint — data plane (port 8000).

This process sits on the critical path of somebody's production application.
Its constraints (CODEBASE_GUIDE §2): under 100 ms of added latency, stateless,
fail-open, and no synchronous Postgres writes. Nothing that violates those may
be mounted here.

P2 adds the OpenAI-compatible surface: ``/v1/chat/completions`` and
``/v1/embeddings`` via ``proxy/ingress.py``.

Errors here are rendered in **OpenAI's** ``{"error": {...}}`` shape rather than
the RFC 7807 problem+json the control plane uses. The caller is an SDK that
expects the provider's error format, and handing it something else would break
error handling in the applications we promised not to disturb.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from apicost.app import create_app
from apicost.core.errors import APICostError
from apicost.core.logging import get_logger
from apicost.proxy import ingress
from apicost.proxy.providers.base import close_http_client

_logger = get_logger(__name__)

app: FastAPI = create_app(
    service="proxy",
    title="APICost Proxy",
    description="OpenAI-compatible proxy: cache, route, log, protect.",
    on_shutdown=close_http_client,
)

app.include_router(ingress.router)


@app.exception_handler(APICostError)
async def openai_shaped_error(request: Request, exc: Exception) -> JSONResponse:
    """Render deliberate errors the way an OpenAI client expects."""
    assert isinstance(exc, APICostError)
    return JSONResponse(
        status_code=exc.status_code,
        content=ingress.error_to_openai_shape(exc),
        headers={"X-APICost-Request-Id": getattr(request.state, "request_id", "")},
    )


@app.exception_handler(Exception)
async def openai_shaped_unhandled(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all, in the same shape, leaking nothing from the exception."""
    _logger.exception(
        "unhandled_proxy_exception",
        path=request.url.path,
        exc_type=type(exc).__name__,
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "message": "An unexpected error occurred.",
                "type": "internal_error",
                "code": 500,
            }
        },
        headers={"X-APICost-Request-Id": getattr(request.state, "request_id", "")},
    )
