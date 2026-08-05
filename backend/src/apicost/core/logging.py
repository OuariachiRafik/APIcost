"""Structured logging with mandatory secret redaction.

Two things happen here, and the second one is a security control:

1. ``structlog`` is configured to emit JSON with a ``request_id`` taken from a
   context variable, so every line produced while serving a request is
   traceable end to end (CODEBASE_GUIDE §8.5).
2. Every event — including rendered exception tracebacks — passes through
   :func:`redact` before it reaches a renderer. A provider key, proxy key, or
   anything else matching a known credential prefix never reaches an output
   stream (BUILD_SPEC §0.2, CLAUDE.md hard rule 3).

The redaction pass is a *second line of defense*, not a licence to log secrets.
Do not rely on it: never put a credential in an event in the first place.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Mapping
from contextvars import ContextVar, Token
from typing import Any, Final

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

__all__ = [
    "REDACTION_PLACEHOLDER",
    "SecretRedactionFilter",
    "bind_request_id",
    "clear_request_id",
    "configure_logging",
    "get_logger",
    "get_request_id",
    "redact",
    "redact_text",
    "reset_request_id",
]

REDACTION_PLACEHOLDER: Final = "***REDACTED***"

# Known credential shapes. Order matters: the most specific prefix must be
# tried first, or "sk-" would swallow the "ant-" of an Anthropic key and leave
# a misleading marker behind.
#
#   sk-ant-...  Anthropic provider keys
#   sk-...      OpenAI-style provider keys
#   apc_...     our own proxy keys (apc_live_<32 bytes base62>)
#
# The lookbehind stops a prefix from matching mid-token: without it, the "sk-"
# inside "ask-me-later" matches and ordinary prose gets mangled.
#
# Redaction must also be **idempotent** — a record can pass through more than
# one filter, and re-redacting already-redacted text must be a no-op. Two
# things make that hold, and both are load-bearing:
#
#   * the trailing lookahead rejects a match that runs into a placeholder;
#   * the possessive `{4,}+` stops the engine from backtracking to a shorter
#     body to dodge that lookahead. Without it, "apc_live_***REDACTED***"
#     re-matches as "apc_" + "live_" and gains a second placeholder.
_SECRET_PATTERN: Final = re.compile(
    r"(?<![A-Za-z0-9_\-])(sk-ant-|sk-|apc_live_|apc_test_|apc_)"
    r"([A-Za-z0-9_\-]{4,}+)(?!\*\*\*REDACTED)"
)

_request_id: ContextVar[str | None] = ContextVar("apicost_request_id", default=None)


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def redact_text(text: str) -> str:
    """Replace every credential-shaped substring in ``text``.

    The scheme prefix is preserved so logs remain diagnosable ("an Anthropic
    key leaked into this message") without exposing the secret itself.
    """
    return _SECRET_PATTERN.sub(lambda m: f"{m.group(1)}{REDACTION_PLACEHOLDER}", text)


def redact(value: Any) -> Any:
    """Recursively redact strings inside ``value``.

    Handles the containers that realistically appear in a log event: strings,
    mappings, sequences, sets, and exceptions (whose ``str()`` is redacted by
    the caller once rendered).
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, set):
        return {redact(item) for item in value}
    if isinstance(value, BaseException):
        # An exception's message can carry a key that was interpolated into it.
        return redact_text(str(value))
    return value


def redaction_processor(
    _logger: WrappedLogger, _method_name: str, event_dict: EventDict
) -> EventDict:
    """structlog processor applying :func:`redact` to the whole event.

    Must be placed *after* ``format_exc_info`` in the chain so the rendered
    traceback string is scrubbed too.
    """
    return {key: redact(value) for key, value in event_dict.items()}


class SecretRedactionFilter(logging.Filter):
    """stdlib ``logging`` filter, for libraries that bypass structlog.

    uvicorn, SQLAlchemy, and httpx all log through the stdlib. Attaching this
    to the root handler means their output is scrubbed on the same terms.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(redact(arg) for arg in record.args)
            elif isinstance(record.args, Mapping):
                record.args = {key: redact(val) for key, val in record.args.items()}
        return True


# ---------------------------------------------------------------------------
# Request-scoped context
# ---------------------------------------------------------------------------


def bind_request_id(request_id: str) -> Token[str | None]:
    """Bind ``request_id`` to the current context; returns a reset token."""
    structlog.contextvars.bind_contextvars(request_id=request_id)
    return _request_id.set(request_id)


def reset_request_id(token: Token[str | None]) -> None:
    """Undo a :func:`bind_request_id`, restoring the previous value."""
    structlog.contextvars.unbind_contextvars("request_id")
    _request_id.reset(token)


def get_request_id() -> str | None:
    """Return the request id bound to the current context, if any."""
    return _request_id.get()


def clear_request_id() -> None:
    """Drop the bound request id entirely. Mainly for test isolation."""
    structlog.contextvars.unbind_contextvars("request_id")
    _request_id.set(None)


def add_request_id(_logger: WrappedLogger, _method_name: str, event_dict: EventDict) -> EventDict:
    """Inject the context-bound ``request_id`` into every event."""
    request_id = _request_id.get()
    if request_id is not None:
        event_dict.setdefault("request_id", request_id)
    return event_dict


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def configure_logging(
    *, level: str = "INFO", json_output: bool = True, service: str | None = None
) -> None:
    """Configure structlog and the stdlib root logger.

    Called once per process from each app's lifespan. Idempotent.
    """
    numeric_level = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)

    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        add_request_id,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        # Everything above may have produced strings; nothing below may leak.
        redaction_processor,
        renderer,
    ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        # Not cached: a bound logger captures the output stream it was built
        # with, so caching makes reconfiguration a no-op for every logger that
        # has already been used once. The per-call cost is a dict lookup.
        cache_logger_on_first_use=False,
    )

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.addFilter(SecretRedactionFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(numeric_level)

    _redact_third_party_loggers()

    if service is not None:
        structlog.contextvars.bind_contextvars(service=service)


# Loggers that install their own handlers and set ``propagate = False``, so
# records never reach the root handler configured above. uvicorn's access log
# is the one that matters most: it renders the request line verbatim, and a
# caller can put anything in a query string.
_THIRD_PARTY_LOGGERS: Final = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "gunicorn.error",
    "gunicorn.access",
    "sqlalchemy.engine",
    "httpx",
    "httpcore",
    "arq",
)


def _redact_third_party_loggers() -> None:
    """Attach the redaction filter to loggers that bypass the root handler.

    The filter goes on both the logger and each of its handlers: a logger-level
    filter catches records emitted directly, a handler-level filter catches
    records that arrive by propagation from a child.
    """
    for name in _THIRD_PARTY_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, SecretRedactionFilter) for f in logger.filters):
            logger.addFilter(SecretRedactionFilter())
        for handler in logger.handlers:
            if not any(isinstance(f, SecretRedactionFilter) for f in handler.filters):
                handler.addFilter(SecretRedactionFilter())


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
