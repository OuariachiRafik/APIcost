"""Feed drained ledger rows through the fast-path detector — UC-31.

Called by the ledger drain, once per batch, after the rows are safely in
Postgres. Ordering matters: scoring first and writing second would mean a
failed insert had already consumed the window and moved the baseline forward,
so the same traffic could never be re-scored on retry.

Everything here is best-effort. An exception must not cost the drain its batch;
the ledger is the product's system of record and anomaly detection is a feature
on top of it.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import text

from apicost.anomaly.alerts import AlertRequest, raise_alert
from apicost.anomaly.store import checkpoint_to_postgres, load_rolling, save_rolling
from apicost.anomaly.zscore import score
from apicost.config import Settings, get_settings
from apicost.core.logging import get_logger
from apicost.db.session import get_admin_engine
from apicost.stats.rolling import RollingStats, observe

__all__ = ["process_ledger_batch"]

_logger = get_logger(__name__)


async def process_ledger_batch(
    redis: Redis,
    rows: list[dict[str, Any]],
    *,
    settings: Settings | None = None,
) -> int:
    """Fold a drained batch into each project's baseline and score closures.

    Returns the number of alerts raised.
    """
    cfg = settings or get_settings()
    if not rows:
        return 0

    by_project: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        project_id = str(row.get("project_id") or "")
        if project_id:
            by_project[project_id].append(row)

    alerts = 0
    to_checkpoint: dict[str, RollingStats] = {}

    for project_id, project_rows in by_project.items():
        try:
            alerts += await _process_project(redis, project_id, project_rows, cfg, to_checkpoint)
        except Exception as exc:
            _logger.warning(
                "anomaly_project_failed",
                subsystem="anomaly",
                project_id=project_id,
                error_type=type(exc).__name__,
            )

    if to_checkpoint:
        await checkpoint_to_postgres(to_checkpoint)

    return alerts


async def _process_project(
    redis: Redis,
    project_id: str,
    rows: list[dict[str, Any]],
    cfg: Settings,
    to_checkpoint: dict[str, RollingStats],
) -> int:
    stats = await load_rolling(redis, project_id)

    # Sorted by the event's own timestamp, not arrival order. A batch can
    # contain out-of-order events — two proxy processes writing to one stream —
    # and folding them out of order would open and close windows at random.
    ordered = sorted(rows, key=lambda r: _epoch(r.get("timestamp")))

    alerts = 0
    for row in ordered:
        result = observe(
            stats,
            cost_usd=_cost(row),
            at=_epoch(row.get("timestamp")),
        )
        stats = result.stats

        if result.closed_rate is None:
            continue

        # The baseline inside `stats` already includes the window that just
        # closed. Score against the baseline *before* it was added, or a spike
        # large enough would help hide itself.
        verdict = score(
            _baseline_excluding(stats, result.closed_rate),
            result.closed_rate,
            z_threshold=cfg.anomaly_z_threshold,
            min_observations=cfg.anomaly_min_observations,
        )

        if verdict.anomalous:
            raised = await _alert(redis, project_id, row, verdict, result.closed_requests, cfg)
            if raised:
                alerts += 1

    await save_rolling(redis, project_id, stats)
    to_checkpoint[project_id] = stats
    return alerts


def _baseline_excluding(stats: RollingStats, rate: float) -> Any:
    """The baseline as it stood before ``rate`` was folded in.

    Welford's update is exactly invertible, and inverting it is cheaper and
    less error-prone than threading a pre-update copy through ``observe``'s
    return value — the copy is the thing a future edit forgets to keep in sync.
    """
    from apicost.stats.welford import WelfordState

    baseline = stats.baseline
    if baseline.count <= 1:
        return WelfordState()

    count = baseline.count - 1
    mean = (baseline.mean * baseline.count - rate) / count
    m2 = baseline.m2 - (rate - mean) * (rate - baseline.mean)
    return WelfordState(count=count, mean=mean, m2=max(0.0, m2))


async def _alert(
    redis: Redis,
    project_id: str,
    row: dict[str, Any],
    verdict: Any,
    window_requests: int,
    cfg: Settings,
) -> bool:
    owner = await _project_owner(project_id)
    if owner is None:
        return False

    user_id, project_name, email = owner

    alert_id = await raise_alert(
        redis,
        AlertRequest(
            user_id=user_id,
            project_id=project_id,
            project_name=project_name,
            alert_type="spend_spike",
            severity="critical" if verdict.multiple >= 10 else "warning",
            title=f"Spend spike on {project_name}",
            detail={
                "window_spend_usd": f"${verdict.rate_usd:,.4f}",
                "normal_spend_usd": f"${verdict.baseline_mean:,.4f}",
                "times_normal": f"{verdict.multiple:,.1f}x",
                "z_score": f"{verdict.z:,.1f}",
                "requests_in_window": window_requests,
                "based_on_windows": verdict.observations,
            },
            email=email,
        ),
        settings=cfg,
    )
    return alert_id is not None


async def _project_owner(project_id: str) -> tuple[str, str, str] | None:
    """(user_id, project_name, email) for the alert, or None if it vanished."""
    try:
        async with get_admin_engine().begin() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT p.user_id, p.name, u.email FROM projects p "
                        "JOIN users u ON u.id = p.user_id WHERE p.id = :project_id"
                    ),
                    {"project_id": project_id},
                )
            ).first()
    except Exception as exc:
        _logger.warning(
            "anomaly_owner_lookup_failed",
            subsystem="anomaly",
            project_id=project_id,
            error_type=type(exc).__name__,
        )
        return None

    if row is None:
        return None
    return str(row.user_id), str(row.name), str(row.email)


def _cost(row: dict[str, Any]) -> float:
    try:
        return float(row.get("cost_usd") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _epoch(value: Any) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            return 0.0
    return 0.0
