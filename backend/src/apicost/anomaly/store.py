"""Persistence for rolling baselines (BUILD_SPEC §6.5).

Redis holds the working copy — read and written once per ledger batch — and
``rolling_stats`` in Postgres is the durable one, checkpointed on an interval.
Flushing Redis is a routine operation, and without the Postgres copy it would
silently reset every project to cold start and disable anomaly detection for the
next 30 windows. Nobody would notice until an incident went unreported.

**This module exists because of a conflict between two authoritative documents.**
BUILD_SPEC §3 puts Redis checkpointing in ``stats/rolling.py``; CLAUDE.md §Style
requires ``stats/`` to be pure, with no I/O and no ORM imports, and it is a
``mypy --strict`` target. CLAUDE.md wins — its instructions override — so the
state machine stayed in ``stats/rolling.py`` and its I/O moved here. See
ADR 0008.
"""

from __future__ import annotations

import json
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import text

from apicost.core.logging import get_logger
from apicost.db.session import get_admin_engine
from apicost.stats.rolling import RollingStats, rolling_from_dict

__all__ = [
    "ROLLING_KEY_PREFIX",
    "checkpoint_to_postgres",
    "load_rolling",
    "rolling_key",
    "save_rolling",
]

_logger = get_logger(__name__)

ROLLING_KEY_PREFIX = "apicost:stats:rolling:"
METRIC = "spend_rate"


def rolling_key(project_id: str) -> str:
    return f"{ROLLING_KEY_PREFIX}{project_id}"


async def load_rolling(redis: Redis, project_id: str) -> RollingStats:
    """Working copy from Redis, falling back to the Postgres checkpoint.

    The fallback is what makes a Redis flush survivable. It costs one query per
    project on the first batch after a flush and nothing thereafter.
    """
    try:
        raw = await redis.get(rolling_key(project_id))
        if raw is not None:
            return rolling_from_dict(json.loads(raw))
    except Exception as exc:
        _logger.warning(
            "rolling_stats_redis_read_failed",
            subsystem="anomaly",
            project_id=project_id,
            error_type=type(exc).__name__,
        )

    return await _load_from_postgres(project_id)


async def save_rolling(redis: Redis, project_id: str, stats: RollingStats) -> None:
    """Write the working copy. Never raises."""
    try:
        await redis.set(rolling_key(project_id), json.dumps(stats.to_dict()))
    except Exception as exc:
        _logger.warning(
            "rolling_stats_redis_write_failed",
            subsystem="anomaly",
            project_id=project_id,
            error_type=type(exc).__name__,
        )


async def _load_from_postgres(project_id: str) -> RollingStats:
    try:
        async with get_admin_engine().begin() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT observation_count, mean, m2, window_started_at, "
                        "window_cost, window_requests FROM rolling_stats "
                        "WHERE project_id = :project_id AND metric = :metric"
                    ),
                    {"project_id": project_id, "metric": METRIC},
                )
            ).first()
    except Exception as exc:
        _logger.warning(
            "rolling_stats_postgres_read_failed",
            subsystem="anomaly",
            project_id=project_id,
            error_type=type(exc).__name__,
        )
        return RollingStats()

    if row is None:
        return RollingStats()

    return rolling_from_dict(
        {
            "baseline": {"count": row.observation_count, "mean": row.mean, "m2": row.m2},
            "window_started_at": row.window_started_at,
            "window_cost": row.window_cost,
            "window_requests": row.window_requests,
        }
    )


async def checkpoint_to_postgres(states: dict[str, RollingStats]) -> int:
    """Persist working copies durably. Returns rows written.

    Called at the end of a drain batch rather than per observation: BUILD_SPEC
    §6.5 asks for a 60 s checkpoint, and the drain runs every 5 s, so this is
    already far more often than required while staying off the request path
    entirely.

    A project whose row was deleted (project removed mid-batch) is skipped by
    the foreign key rather than failing the batch — hence the per-project
    try, and hence the fact that a lost checkpoint costs accuracy, not data.
    """
    if not states:
        return 0

    written = 0
    try:
        async with get_admin_engine().begin() as conn:
            for project_id, stats in states.items():
                await conn.execute(
                    text(
                        "INSERT INTO rolling_stats (project_id, metric, observation_count, "
                        "mean, m2, window_started_at, window_cost, window_requests, updated_at) "
                        "VALUES (:project_id, :metric, :count, :mean, :m2, :started, "
                        ":cost, :requests, now()) "
                        "ON CONFLICT (project_id, metric) DO UPDATE SET "
                        "observation_count = EXCLUDED.observation_count, "
                        "mean = EXCLUDED.mean, m2 = EXCLUDED.m2, "
                        "window_started_at = EXCLUDED.window_started_at, "
                        "window_cost = EXCLUDED.window_cost, "
                        "window_requests = EXCLUDED.window_requests, "
                        "updated_at = now()"
                    ),
                    {
                        "project_id": project_id,
                        "metric": METRIC,
                        "count": stats.baseline.count,
                        "mean": stats.baseline.mean,
                        "m2": stats.baseline.m2,
                        "started": stats.window_started_at,
                        "cost": stats.window_cost,
                        "requests": stats.window_requests,
                    },
                )
                written += 1
    except Exception as exc:
        _logger.warning(
            "rolling_stats_checkpoint_failed",
            subsystem="anomaly",
            error_type=type(exc).__name__,
        )
        return written

    return written


def project_meta_from_row(row: Any) -> dict[str, str]:
    """Extract the identity fields an alert needs from a ledger event."""
    return {
        "user_id": str(row.get("user_id", "")),
        "project_id": str(row.get("project_id", "")),
    }
