"""Monthly request counting for plan limits.

Redis, never Postgres. The proxy consults this on every request and hard rule 7
forbids a synchronous Postgres read there; the counter is incremented alongside
the ledger emit, exactly as budget counters are.

The key embeds the month, so rollover is free — no reset job to fail to run.
"""

from __future__ import annotations

from datetime import UTC, datetime

from redis.asyncio import Redis

from apicost.core.logging import get_logger
from apicost.db.redis import get_redis

__all__ = ["PLAN_USAGE_PREFIX", "monthly_request_count", "plan_usage_key", "record_request"]

_logger = get_logger(__name__)

PLAN_USAGE_PREFIX = "apicost:plan:requests:"

_TTL_SECONDS = 60 * 60 * 24 * 40
"""Outlives its month with room for a late reconciliation, then expires on its
own rather than accumulating a key per user per month forever."""


def plan_usage_key(user_id: str, at: datetime | None = None) -> str:
    now = at or datetime.now(UTC)
    return f"{PLAN_USAGE_PREFIX}{user_id}:{now.strftime('%Y-%m')}"


async def record_request(redis: Redis, user_id: str, at: datetime | None = None) -> None:
    """Count one request against the plan. Never raises."""
    try:
        key = plan_usage_key(user_id, at)
        pipe = redis.pipeline()
        pipe.incr(key)
        pipe.expire(key, _TTL_SECONDS)
        await pipe.execute()
    except Exception as exc:
        _logger.warning(
            "plan_usage_write_failed", subsystem="billing", error_type=type(exc).__name__
        )


async def monthly_request_count(user_id: str, at: datetime | None = None) -> int:
    """Requests this calendar month. 0 if unreadable.

    Zero on failure is the permissive answer, and that is deliberate: an
    unreadable counter must not tell a paying customer they are over their
    limit. Plan limits warn rather than block anyway (billing/plans.py), so the
    cost of guessing low is an unshown upgrade prompt.
    """
    try:
        raw = await get_redis().get(plan_usage_key(user_id, at))
        return int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return 0
    except Exception:
        _logger.warning("plan_usage_read_failed", subsystem="billing")
        return 0
