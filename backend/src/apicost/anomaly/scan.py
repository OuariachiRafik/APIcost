"""The 5-minute IsolationForest sweep — UC-32, BUILD_SPEC §4 P6.

Reads each active project's recent ledger history, buckets it into 5-minute
windows, fits a forest on the older windows, and scores the most recent one.

"Active" is doing real work here. Fitting a forest per project every 5 minutes
does not scale to every project that has ever existed, and it does not need to:
a project with no traffic in the scan horizon cannot have an anomalous pattern.
The query finds candidates from the ledger itself, so the cost tracks projects
that are actually being used.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import text

from apicost.anomaly.alerts import AlertRequest, raise_alert
from apicost.anomaly.forest import detect, features_from_rows
from apicost.config import Settings, get_settings
from apicost.core.logging import get_logger
from apicost.db.redis import get_redis
from apicost.db.session import get_admin_engine

__all__ = ["WINDOW_MINUTES", "scan_usage_patterns"]

_logger = get_logger(__name__)

WINDOW_MINUTES = 5
HISTORY_HOURS = 6
"""72 windows of history. Enough for the forest's 24-window minimum with room
for the project to have been idle for part of it."""


async def scan_usage_patterns(
    redis: Redis | None = None,
    settings: Settings | None = None,
) -> int:
    """Score every recently active project. Returns alerts raised."""
    cfg = settings or get_settings()
    client = redis or get_redis(cfg)

    since = datetime.now(UTC) - timedelta(hours=HISTORY_HOURS)

    try:
        projects = await _active_projects(since)
    except Exception as exc:
        _logger.warning(
            "pattern_scan_project_query_failed",
            subsystem="anomaly",
            error_type=type(exc).__name__,
        )
        return 0

    alerts = 0
    for project_id, user_id, project_name, email in projects:
        try:
            if await _scan_project(client, project_id, user_id, project_name, email, since, cfg):
                alerts += 1
        except Exception as exc:
            _logger.warning(
                "pattern_scan_project_failed",
                subsystem="anomaly",
                project_id=project_id,
                error_type=type(exc).__name__,
            )

    if alerts:
        _logger.info("pattern_scan_alerts", subsystem="anomaly", alerts=alerts)
    return alerts


async def _active_projects(since: datetime) -> list[tuple[str, str, str, str]]:
    async with get_admin_engine().begin() as conn:
        rows = await conn.execute(
            text(
                "SELECT DISTINCT r.project_id, p.user_id, p.name, u.email "
                "FROM requests_log r "
                "JOIN projects p ON p.id = r.project_id "
                "JOIN users u ON u.id = p.user_id "
                "WHERE r.timestamp >= :since AND p.archived_at IS NULL"
            ),
            {"since": since},
        )
        return [(str(r.project_id), str(r.user_id), str(r.name), str(r.email)) for r in rows]


async def _scan_project(
    redis: Redis,
    project_id: str,
    user_id: str,
    project_name: str,
    email: str,
    since: datetime,
    cfg: Settings,
) -> bool:
    async with get_admin_engine().begin() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT timestamp, model_used, endpoint, cost_usd, prompt_hash "
                    "FROM requests_log WHERE project_id = :project_id "
                    "AND timestamp >= :since ORDER BY timestamp"
                ),
                {"project_id": project_id, "since": since},
            )
        ).mappings()
        history_rows = [dict(r) for r in rows]

    if not history_rows:
        return False

    windows = _bucket(history_rows)
    if len(windows) < 2:
        return False

    # The most recent bucket is scored; everything before it is the baseline.
    # The current bucket is excluded from the fit for the same reason the
    # z-score excludes its window: a point cannot be its own normal.
    *history, current = [features_from_rows(w, window_minutes=WINDOW_MINUTES) for w in windows]

    verdict = detect(history, current)
    if not verdict.anomalous:
        return False

    alert_id = await raise_alert(
        redis,
        AlertRequest(
            user_id=user_id,
            project_id=project_id,
            project_name=project_name,
            alert_type="usage_pattern",
            severity="critical",
            title=f"Unusual usage pattern on {project_name}",
            detail={
                "what_changed": ", ".join(verdict.contributors or []) or "overall usage shape",
                "requests_per_minute": f"{current.request_rate:,.1f}",
                "spend_per_minute": f"${current.cost_rate:,.4f}",
                "distinct_models": f"{current.model_entropy:,.2f} bits of variety",
                "unique_prompt_ratio": f"{current.unique_prompt_ratio:.0%}",
                "compared_against": f"{len(history)} recent {WINDOW_MINUTES}-minute windows",
            },
            email=email,
        ),
        settings=cfg,
    )
    return alert_id is not None


def _bucket(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group rows into fixed 5-minute buckets, dropping empty ones.

    Empty buckets are dropped rather than emitted as zero vectors: an idle
    stretch would otherwise dominate the training set and make any traffic at
    all look anomalous when the project woke up.
    """
    buckets: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        stamp = row.get("timestamp")
        if not isinstance(stamp, datetime):
            continue
        index = int(stamp.timestamp()) // (WINDOW_MINUTES * 60)
        buckets.setdefault(index, []).append(row)

    return [buckets[key] for key in sorted(buckets)]
