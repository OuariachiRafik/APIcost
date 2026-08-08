"""Raising an alert: dedupe, persist, notify — UC-31, UC-32, UC-34.

Both detectors funnel through :func:`raise_alert`. It does three things in a
fixed order, and the order is the interesting part:

1. **Claim the cooldown** (Redis ``SET NX``, 30 min per type per project,
   BUILD_SPEC §6.8). An incident that lasts an hour produces one email, not
   sixty. Claimed *first* and atomically, so two workers scoring the same
   window cannot both send.
2. **Write the row** to ``alert_events``. This is the user's history (UC-34),
   and it is written even when the email later fails — an alert that happened
   but could not be delivered is still something the user needs to find in the
   dashboard.
3. **Send the email**, and record whether it went.

Doing it the other way round — send, then persist — loses the record whenever
the process dies mid-send, which is exactly when a user most wants to know what
we saw.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import text

from apicost.config import Settings, get_settings
from apicost.core.ids import new_id
from apicost.core.logging import get_logger
from apicost.db.session import get_admin_engine
from apicost.notify.email import EmailSender, Message, get_sender

__all__ = [
    "ALERT_COOLDOWN_PREFIX",
    "AlertRequest",
    "cooldown_key",
    "raise_alert",
]

_logger = get_logger(__name__)

ALERT_COOLDOWN_PREFIX = "apicost:alert:cooldown:"


def cooldown_key(project_id: str, alert_type: str) -> str:
    """Per project *and* per type.

    Not per user: a spend spike on a staging project should not suppress a
    leaked-key alert on production. And not global per project either — the two
    alert types mean different things and a user needs both."""
    return f"{ALERT_COOLDOWN_PREFIX}{project_id}:{alert_type}"


@dataclass(frozen=True)
class AlertRequest:
    user_id: str
    project_id: str
    project_name: str
    alert_type: str
    title: str
    detail: dict[str, Any]
    severity: str = "warning"
    email: str | None = None


async def raise_alert(
    redis: Redis,
    request: AlertRequest,
    *,
    settings: Settings | None = None,
    sender: EmailSender | None = None,
) -> str | None:
    """Record and notify, honouring the cooldown. Returns the alert id.

    ``None`` means suppressed by cooldown or failed. Never raises: this runs
    inside the ledger drain, and an alerting failure must not cost the ledger
    a batch.
    """
    resolved = settings or get_settings()

    try:
        claimed = await redis.set(
            cooldown_key(request.project_id, request.alert_type),
            "1",
            nx=True,
            ex=resolved.alert_cooldown_seconds,
        )
    except Exception as exc:
        # Redis is unreachable. Alert anyway, and accept the risk of duplicates:
        # a user receiving the same warning twice is a nuisance, a user never
        # hearing that their key leaked is the failure this module exists to
        # prevent.
        _logger.warning(
            "alert_cooldown_unavailable",
            subsystem="anomaly",
            project_id=request.project_id,
            error_type=type(exc).__name__,
        )
        claimed = True

    if not claimed:
        _logger.info(
            "alert_suppressed_by_cooldown",
            subsystem="anomaly",
            project_id=request.project_id,
            alert_type=request.alert_type,
        )
        return None

    alert_id = new_id()
    try:
        await _persist(alert_id, request)
    except Exception as exc:
        _logger.error(
            "alert_persist_failed",
            subsystem="anomaly",
            project_id=request.project_id,
            alert_type=request.alert_type,
            error_type=type(exc).__name__,
        )
        return None

    if request.email:
        transport = sender or get_sender(resolved)
        delivered = await transport.send(_render(request))
        if delivered:
            await _mark_notified(alert_id)

    _logger.info(
        "alert_raised",
        subsystem="anomaly",
        project_id=request.project_id,
        alert_type=request.alert_type,
        alert_id=alert_id,
        severity=request.severity,
    )
    return alert_id


async def _persist(alert_id: str, request: AlertRequest) -> None:
    """Insert the row through the admin engine.

    The worker drains a stream spanning every project, so there is no single
    ``app.user_id`` to scope the session to, and RLS would reject the insert.
    The ``user_id`` written here comes from the ledger row, never from anything
    caller-supplied, and every *read* of this table goes through the scoped
    application role (CODEBASE_GUIDE §3).
    """
    import json

    async with get_admin_engine().begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO alert_events "
                "(id, user_id, project_id, alert_type, severity, title, detail, status) "
                "VALUES (:id, :user_id, :project_id, :alert_type, :severity, :title, "
                "CAST(:detail AS jsonb), 'open')"
            ),
            {
                "id": alert_id,
                "user_id": request.user_id,
                "project_id": request.project_id,
                "alert_type": request.alert_type,
                "severity": request.severity,
                "title": request.title,
                "detail": json.dumps(request.detail),
            },
        )


async def _mark_notified(alert_id: str) -> None:
    try:
        async with get_admin_engine().begin() as conn:
            await conn.execute(
                text("UPDATE alert_events SET notified_at = :now WHERE id = :id"),
                {"id": alert_id, "now": datetime.now(UTC)},
            )
    except Exception as exc:
        _logger.warning(
            "alert_notified_flag_failed",
            subsystem="anomaly",
            error_type=type(exc).__name__,
        )


def _render(request: AlertRequest) -> Message:
    """Plain text, with the numbers in it.

    "Anomaly detected on your project" tells a user nothing they can act on at
    3am. The rate, the baseline, and the specific next action do.
    """
    lines = [
        request.title,
        "",
        f"Project: {request.project_name}",
        "",
    ]
    for key, value in request.detail.items():
        label = key.replace("_", " ").capitalize()
        lines.append(f"  {label}: {value}")

    lines += [
        "",
        _advice(request.alert_type),
        "",
        "-- APICost",
    ]
    return Message(
        to=request.email or "",
        subject=f"[APICost] {request.title}",
        text="\n".join(lines),
    )


def _advice(alert_type: str) -> str:
    if alert_type == "usage_pattern":
        return (
            "If you do not recognise this traffic, revoke the project's proxy "
            "keys now — one action, from the project page. Your provider keys "
            "are unaffected and nothing else stops working."
        )
    if alert_type == "spend_spike":
        return (
            "If this was not intentional, check for a retry loop. You can cap "
            "this project with a hard-stop budget so it cannot happen again."
        )
    return "Review this project's recent activity in the dashboard."
