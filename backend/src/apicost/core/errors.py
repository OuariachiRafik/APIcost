"""Typed exceptions and RFC 7807 ``application/problem+json`` handlers.

Error bodies follow RFC 7807 (BUILD_SPEC §8) and carry the ``request_id`` so a
user reporting a failure gives us something we can grep for.

Two rules govern everything in this module:

* Detail strings are passed through :func:`~apicost.core.logging.redact_text`
  before they reach the client. An error message is a response body, and hard
  rule 3 covers response bodies.
* An *unhandled* exception never has its message forwarded to the caller at
  all. We do not know what is in it, so it gets a fixed generic string and the
  real detail goes to the log.
"""

from __future__ import annotations

from typing import Any, ClassVar

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from apicost.core.logging import get_logger, get_request_id, redact_text

__all__ = [
    "PROBLEM_JSON",
    "APICostError",
    "AuthenticationError",
    "AuthorizationError",
    "BudgetExceededError",
    "ConflictError",
    "InvalidRequestError",
    "NotFoundError",
    "ServiceUnavailableError",
    "UpstreamError",
    "register_exception_handlers",
]

PROBLEM_JSON = "application/problem+json"

_logger = get_logger(__name__)


class APICostError(Exception):
    """Base class for every error this application raises deliberately."""

    status_code: ClassVar[int] = 500
    title: ClassVar[str] = "Internal Server Error"
    type_uri: ClassVar[str] = "about:blank"

    def __init__(self, detail: str | None = None, **extra: Any) -> None:
        self.detail = detail or self.title
        self.extra = extra
        super().__init__(self.detail)

    def to_problem(self, instance: str | None = None) -> dict[str, Any]:
        """Render as an RFC 7807 problem document."""
        problem: dict[str, Any] = {
            "type": self.type_uri,
            "title": self.title,
            "status": self.status_code,
            "detail": redact_text(self.detail),
        }
        if instance is not None:
            problem["instance"] = instance
        request_id = get_request_id()
        if request_id is not None:
            problem["request_id"] = request_id
        problem.update(self.extra)
        return problem


class InvalidRequestError(APICostError):
    status_code = 400
    title = "Invalid Request"


class AuthenticationError(APICostError):
    status_code = 401
    title = "Authentication Failed"


class BudgetExceededError(APICostError):
    """Raised by a ``hard_stop`` budget — the one deliberate fail-closed path."""

    status_code = 402
    title = "Budget Exceeded"


class AuthorizationError(APICostError):
    status_code = 403
    title = "Not Authorized"


class NotFoundError(APICostError):
    status_code = 404
    title = "Not Found"


class ConflictError(APICostError):
    status_code = 409
    title = "Conflict"


class UpstreamError(APICostError):
    status_code = 502
    title = "Upstream Provider Error"


class ServiceUnavailableError(APICostError):
    status_code = 503
    title = "Service Unavailable"


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def apicost_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render a deliberate :class:`APICostError` as problem+json."""
    assert isinstance(exc, APICostError)
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.to_problem(instance=request.url.path),
        media_type=PROBLEM_JSON,
    )


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render Starlette/FastAPI ``HTTPException`` in the same problem+json shape."""
    assert isinstance(exc, StarletteHTTPException)
    problem: dict[str, Any] = {
        "type": "about:blank",
        "title": str(exc.detail),
        "status": exc.status_code,
        "detail": redact_text(str(exc.detail)),
        "instance": request.url.path,
    }
    request_id = get_request_id()
    if request_id is not None:
        problem["request_id"] = request_id
    return JSONResponse(
        status_code=exc.status_code,
        content=problem,
        headers=getattr(exc, "headers", None),
        media_type=PROBLEM_JSON,
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all.

    The exception message is deliberately *not* forwarded: an unexpected
    exception may have interpolated a provider key into its message, and we
    cannot audit what we did not write.
    """
    _logger.exception(
        "unhandled_exception",
        path=request.url.path,
        method=request.method,
        exc_type=type(exc).__name__,
    )
    problem: dict[str, Any] = {
        "type": "about:blank",
        "title": "Internal Server Error",
        "status": 500,
        "detail": "An unexpected error occurred.",
        "instance": request.url.path,
    }
    request_id = get_request_id()
    if request_id is not None:
        problem["request_id"] = request_id
    return JSONResponse(status_code=500, content=problem, media_type=PROBLEM_JSON)


def register_exception_handlers(app: FastAPI) -> None:
    """Attach all three handlers to an app. Called by both ASGI entrypoints."""
    app.add_exception_handler(APICostError, apicost_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
