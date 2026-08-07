"""Shared ASGI application factory.

``main_proxy.py`` (data plane) and ``main_api.py`` (control plane) are separate
processes with different constraints, but they share the same request-id
plumbing, error rendering, and health surface. That wiring lives here so a fix
to ``/readyz`` cannot land on one app and be forgotten on the other.

This file is an addition to the layout in BUILD_SPEC §3; see
``docs/adr/0003-shared-asgi-app-factory.md``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from apicost.config import Settings, get_settings
from apicost.core.errors import register_exception_handlers
from apicost.core.ids import new_request_id
from apicost.core.logging import (
    bind_request_id,
    configure_logging,
    get_logger,
    reset_request_id,
)
from apicost.db.redis import check_redis, close_redis
from apicost.db.session import check_postgres, dispose_engine

__all__ = ["create_app", "health_router"]

REQUEST_ID_HEADER = "X-Request-Id"
APICOST_REQUEST_ID_HEADER = "X-APICost-Request-Id"

_logger = get_logger(__name__)

health_router = APIRouter(tags=["health"])


@health_router.get("/healthz", summary="Liveness probe")
async def healthz(request: Request) -> dict[str, str]:
    """Liveness: is the process up and serving?

    Deliberately checks no dependencies. A liveness probe that fails when
    Postgres blips would have the orchestrator restart a perfectly healthy
    proxy in the middle of an incident.
    """
    return {"status": "ok", "service": request.app.state.service}


@health_router.get("/readyz", summary="Readiness probe")
async def readyz(request: Request) -> Response:
    """Readiness: are Postgres and Redis both reachable?

    Both probes run concurrently and each is bounded by
    ``readiness_timeout_seconds``, so the endpoint cannot hang.
    """
    postgres_ok, redis_ok = await asyncio.gather(check_postgres(), check_redis())
    checks = {"postgres": postgres_ok, "redis": redis_ok}
    ready = all(checks.values())

    if not ready:
        _logger.warning("readiness_failed", **checks)

    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ready" if ready else "not_ready",
            "service": request.app.state.service,
            "checks": checks,
        },
    )


def create_app(
    *,
    service: str,
    title: str,
    description: str,
    settings: Settings | None = None,
    enable_cors: bool = False,
    on_startup: Callable[[], Awaitable[object]] | None = None,
    on_shutdown: Callable[[], Awaitable[None]] | None = None,
) -> FastAPI:
    """Build an ASGI app with the shared logging, error, and health wiring."""
    cfg = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(level=cfg.log_level, json_output=cfg.log_json, service=service)
        _logger.info("service_starting", service=service, environment=cfg.environment)
        if on_startup is not None:
            try:
                await on_startup()
            except Exception:
                # A failed warmup degrades a feature; it must not stop the
                # process from serving traffic.
                _logger.warning("startup_hook_failed", service=service, exc_info=True)
        try:
            yield
        finally:
            if on_shutdown is not None:
                await on_shutdown()
            await dispose_engine()
            await close_redis()
            _logger.info("service_stopped", service=service)

    app = FastAPI(
        title=title,
        description=description,
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if cfg.environment != "production" else None,
        redoc_url=None,
    )
    app.state.service = service
    app.state.settings = cfg

    @app.middleware("http")
    async def request_id_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Bind a fresh request id for the life of the request.

        The id is generated here rather than accepted from the client: an
        inbound header is attacker-controlled, and this value ends up in logs,
        the ledger, and a response header.
        """
        request_id = new_request_id()
        token = bind_request_id(request_id)
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        finally:
            reset_request_id(token)
        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers[APICOST_REQUEST_ID_HEADER] = request_id
        return response

    if enable_cors:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cfg.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=[REQUEST_ID_HEADER, APICOST_REQUEST_ID_HEADER],
        )

    register_exception_handlers(app)
    app.include_router(health_router)
    return app
