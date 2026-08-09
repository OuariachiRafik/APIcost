"""Ledger writes, off the critical path.

The rule (CLAUDE.md hard rule 7, CODEBASE_GUIDE §8.4): **never block the proxy
on logging.** A ledger event is pushed to a Redis Stream and an ARQ worker
drains it into ``requests_log`` in batches. If Redis is unavailable, the event
is dropped and a counter is incremented — the completion still returns.

That tradeoff is deliberate and worth stating plainly: under a Redis outage we
lose usage records rather than fail the user's request. Losing observability is
recoverable; taking down somebody's production application is not.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis

from apicost.config import Settings, get_settings
from apicost.core.logging import get_logger

__all__ = ["LedgerEvent", "emit_ledger_event", "serialize_event"]

_logger = get_logger(__name__)

_dropped_events = 0
"""Process-local count of events we could not enqueue. Surfaced in logs; a
real counter goes to metrics when there is a metrics backend."""


@dataclass
class LedgerEvent:
    """One row destined for ``requests_log``.

    Compact by design — this is serialized on the hot path. It carries no
    prompt or response text: raw content is not stored unless the project opts
    in (CLAUDE.md hard rule 9), and that opt-in path is handled separately.
    """

    request_id: str
    user_id: str
    project_id: str
    timestamp: str
    endpoint: str
    provider: str
    model_requested: str
    model_used: str

    tokens_in: int = 0
    tokens_out: int = 0
    tokens_estimated: bool = False

    cost_usd: str = "0"
    cost_would_have_been_usd: str | None = None
    """Strings, not floats: these are Decimals and JSON has no decimal type.
    Round-tripping through float would reintroduce exactly the drift
    ledger/cost.py takes care to avoid."""

    latency_ms: float = 0.0
    ttft_ms: float | None = None
    itl_ms: float | None = None
    tps: float | None = None

    cache_hit: bool = False
    cache_similarity: float | None = None
    routed: bool = False
    routing_reason_code: str | None = None
    routing_model_version: str | None = None
    escalation_triggered: bool = False

    context_warning: bool = False
    """UC-26: this request resent history that looked stale. A verdict, never
    the prompt it was computed from (hard rule 9)."""
    context_reclaimable_tokens: int | None = None
    context_message_count: int | None = None

    status: int = 200
    error_code: str | None = None
    streamed: bool = False

    extra: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def now() -> str:
        return datetime.now(UTC).isoformat()


def serialize_event(event: LedgerEvent) -> dict[str, str]:
    """Flatten to the string map a Redis stream entry holds."""
    payload = asdict(event)
    extra = payload.pop("extra", {})

    fields: dict[str, str] = {}
    for key, value in payload.items():
        if value is None:
            continue
        if isinstance(value, bool):
            fields[key] = "1" if value else "0"
        elif isinstance(value, Decimal):
            fields[key] = str(value)
        else:
            fields[key] = str(value)

    if extra:
        fields["extra"] = json.dumps(extra, separators=(",", ":"))

    return fields


async def emit_ledger_event(
    redis: Redis, event: LedgerEvent, settings: Settings | None = None
) -> bool:
    """Push an event onto the ledger stream. Never raises.

    Returns True when enqueued. A False return is a dropped ledger row, not a
    failed request — the caller has already answered the user by this point.
    """
    global _dropped_events
    cfg = settings or get_settings()

    try:
        await redis.xadd(
            cfg.ledger_stream_key,
            serialize_event(event),  # type: ignore[arg-type]
            maxlen=cfg.ledger_stream_maxlen,
            approximate=True,
        )
        return True
    except Exception:
        _dropped_events += 1
        _logger.warning(
            "ledger_event_dropped",
            subsystem="ledger",
            request_id=event.request_id,
            dropped_total=_dropped_events,
        )
        return False


def dropped_event_count() -> int:
    """How many events this process failed to enqueue."""
    return _dropped_events
