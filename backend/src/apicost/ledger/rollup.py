"""Daily usage rollups — see docs/adr/0006-usage-rollups.md.

Rollups are **recomputed, not incremented**. Each pass deletes and rebuilds a
window of recent days from `requests_log`. Incremental counters drift the moment
anything is retried, backfilled, or corrected, and a spend figure that silently
drifts is worse than one that is five minutes stale. Recomputation is
idempotent and self-healing: run it twice, get the same answer; run it after a
gap, and the gap fills itself.

`requests_log` remains the system of record. Everything here is derived and
disposable.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Final

from sqlalchemy import text

from apicost.core.logging import get_logger
from apicost.db.session import get_admin_engine

__all__ = [
    "TOKEN_BUCKET_BOUNDS",
    "rebuild_rollups",
    "rollup_freshness",
]

_logger = get_logger(__name__)

DEFAULT_WINDOW_DAYS: Final = 3
"""How far back each pass rebuilds. Enough to absorb a late ledger drain, a
clock skew, or a worker that missed a few cycles."""

TOKEN_BUCKET_BOUNDS: Final[list[tuple[int, int | None]]] = [
    (0, 100),
    (100, 500),
    (500, 1_000),
    (1_000, 2_000),
    (2_000, 4_000),
    (4_000, 8_000),
    (8_000, 16_000),
    (16_000, 32_000),
    (32_000, None),
]


def _bucket_case_sql() -> str:
    """CASE mapping a row's token total to a histogram bucket index."""
    clauses = []
    for index, (lower, upper) in enumerate(TOKEN_BUCKET_BOUNDS):
        condition = f"(tokens_in + tokens_out) >= {lower}"
        if upper is not None:
            condition += f" AND (tokens_in + tokens_out) < {upper}"
        clauses.append(f"WHEN {condition} THEN {index}")
    return "CASE " + " ".join(clauses) + " END"


_REBUILD_USAGE = text(
    """
    INSERT INTO usage_rollup_daily (
        user_id, project_id, day, model_used, endpoint, provider,
        requests, tokens_in, tokens_out, cost_usd, would_have_been_usd,
        cache_hits, cache_savings_usd, routing_savings_usd, errors,
        latency_ms_sum, updated_at
    )
    SELECT
        user_id,
        project_id,
        (timestamp AT TIME ZONE 'UTC')::date AS day,
        model_used,
        endpoint,
        provider,
        COUNT(*),
        COALESCE(SUM(tokens_in), 0),
        COALESCE(SUM(tokens_out), 0),
        COALESCE(SUM(cost_usd), 0),
        COALESCE(SUM(cost_would_have_been_usd), 0),
        COUNT(*) FILTER (WHERE cache_hit),
        -- Caching avoided the whole call.
        COALESCE(SUM(cost_would_have_been_usd) FILTER (WHERE cache_hit), 0),
        -- Routing saved the difference, and only where caching did not already
        -- claim the row (CODEBASE_GUIDE §6 — never double-count).
        COALESCE(
            SUM(cost_would_have_been_usd - cost_usd)
            FILTER (WHERE routed AND NOT cache_hit), 0),
        COUNT(*) FILTER (WHERE status >= 400),
        COALESCE(SUM(latency_ms), 0),
        now()
    FROM requests_log
    WHERE timestamp >= :start AND timestamp < :end
    GROUP BY 1, 2, 3, 4, 5, 6
    """
)

_REBUILD_BUCKETS = text(
    f"""
    INSERT INTO token_bucket_rollup_daily (
        user_id, project_id, day, bucket_index, requests, cost_usd, tokens_total
    )
    SELECT
        user_id,
        project_id,
        (timestamp AT TIME ZONE 'UTC')::date AS day,
        {_bucket_case_sql()} AS bucket_index,
        COUNT(*),
        COALESCE(SUM(cost_usd), 0),
        COALESCE(SUM(tokens_in + tokens_out), 0)
    FROM requests_log
    WHERE timestamp >= :start AND timestamp < :end
    GROUP BY 1, 2, 3, 4
    """
)


async def rebuild_rollups(window_days: int = DEFAULT_WINDOW_DAYS, *, full: bool = False) -> int:
    """Rebuild rollups for the recent window, or for all history.

    Args:
        window_days: How many days back to rebuild.
        full: Rebuild everything. For backfills and after a schema change;
            proportional to table size, so not something to schedule.

    Returns:
        Number of rollup rows written.
    """
    engine = get_admin_engine()
    now = datetime.now(UTC)

    if full:
        async with engine.connect() as conn:
            earliest = await conn.execute(text("SELECT min(timestamp) FROM requests_log"))
            oldest = earliest.scalar()
        if oldest is None:
            return 0
        start = oldest.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        start = (now - timedelta(days=window_days)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

    end = now + timedelta(days=1)
    params = {"start": start, "end": end}

    async with engine.begin() as conn:
        # Delete then rebuild, in one transaction: a reader never sees a
        # half-rebuilt window.
        await conn.execute(
            text("DELETE FROM usage_rollup_daily WHERE day >= :day"),
            {"day": start.date()},
        )
        await conn.execute(
            text("DELETE FROM token_bucket_rollup_daily WHERE day >= :day"),
            {"day": start.date()},
        )
        usage_result = await conn.execute(_REBUILD_USAGE, params)
        bucket_result = await conn.execute(_REBUILD_BUCKETS, params)

    written = (usage_result.rowcount or 0) + (bucket_result.rowcount or 0)
    _logger.info(
        "usage_rollups_rebuilt",
        rows=written,
        window_days=None if full else window_days,
        full=full,
    )
    return written


async def rollup_freshness() -> datetime | None:
    """When the rollups were last rebuilt, for the staleness the API reports."""
    async with get_admin_engine().connect() as conn:
        result = await conn.execute(text("SELECT max(updated_at) FROM usage_rollup_daily"))
        value = result.scalar()
    return value if isinstance(value, datetime) else None


def bucket_label(index: int) -> str:
    lower, upper = TOKEN_BUCKET_BOUNDS[index]
    return f"{lower:,}-{upper:,}" if upper else f"{lower:,}+"


def day_floor(value: datetime) -> date:
    return value.astimezone(UTC).date()
