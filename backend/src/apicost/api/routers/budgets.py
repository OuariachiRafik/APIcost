"""Budgets, alert history, and the kill switch — UC-29, UC-30, UC-33, UC-34."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select, text

from apicost.api.deps import CurrentUser, DbSession, require_project
from apicost.budgets.enforcement import budget_counter_key
from apicost.core.errors import NotFoundError
from apicost.core.ids import new_id
from apicost.core.logging import get_logger
from apicost.db.models import AlertEvent, Budget
from apicost.db.redis import get_redis
from apicost.db.session import session_scope
from apicost.proxy.auth import purge_project_auth_cache

router = APIRouter(tags=["budgets"])

_logger = get_logger(__name__)

Period = Literal["daily", "weekly", "monthly"]
Action = Literal["alert_only", "soft_throttle", "hard_stop"]


MIN_LIMIT_USD = Decimal("0.000001")
"""One micro-dollar, the precision of ``budgets.limit_usd``.

Validated here and not only by the CHECK constraint: a smaller value rounds to
zero on the way into ``numeric(12, 6)``, so the database rejects a row the API
had already accepted and the user gets a 500 out of a request that was merely
too precise. A 422 saying what the minimum is costs nothing and explains
itself."""


class CreateBudgetRequest(BaseModel):
    project_id: str = Field(min_length=1)
    period: Period
    limit_usd: Decimal = Field(ge=MIN_LIMIT_USD)
    action: Action = "alert_only"


class UpdateBudgetRequest(BaseModel):
    limit_usd: Decimal | None = Field(default=None, ge=MIN_LIMIT_USD)
    action: Action | None = None
    is_active: bool | None = None


class BudgetResponse(BaseModel):
    id: str
    project_id: str
    period: str
    limit_usd: Decimal
    action: str
    is_active: bool
    spent_usd: float
    """Live, from the Redis counter the proxy enforces against — not
    recomputed from the ledger. A dashboard that showed a different number
    from the one doing the blocking would be worse than showing nothing."""

    fraction_used: float
    created_at: datetime


class AlertResponse(BaseModel):
    id: str
    project_id: str
    alert_type: str
    severity: str
    title: str
    detail: dict[str, Any]
    status: str
    notified_at: datetime | None
    resolved_at: datetime | None
    resolution: str | None
    created_at: datetime


class ResolveAlertRequest(BaseModel):
    status: Literal["acknowledged", "resolved"] = "resolved"
    resolution: str | None = Field(default=None, max_length=2000)


class KillSwitchResponse(BaseModel):
    project_id: str
    keys_revoked: int
    took_ms: float
    alert_id: str | None


# -- Budgets ------------------------------------------------------- UC-29/30


@router.post("/budgets", status_code=status.HTTP_201_CREATED)
async def create_budget(
    payload: CreateBudgetRequest, user: CurrentUser, session: DbSession
) -> BudgetResponse:
    project = await require_project(payload.project_id, user, session)

    existing = await session.execute(
        select(Budget).where(
            Budget.project_id == project.id,
            Budget.user_id == user.id,
            Budget.period == payload.period,
        )
    )
    budget = existing.scalar_one_or_none()

    if budget is None:
        budget = Budget(
            id=new_id(),
            user_id=user.id,
            project_id=project.id,
            period=payload.period,
            limit_usd=payload.limit_usd,
            action=payload.action,
        )
        session.add(budget)
    else:
        # One budget per period is a database constraint, so a second POST is
        # an update rather than a 409. Setting a budget twice is a thing users
        # do, and failing it teaches them nothing.
        budget.limit_usd = payload.limit_usd
        budget.action = payload.action
        budget.is_active = True

    await session.flush()
    await _purge_after_commit(user.id, project.id)

    return await _to_response(budget)


@router.get("/budgets")
async def list_budgets(
    user: CurrentUser, session: DbSession, project_id: str | None = None
) -> list[BudgetResponse]:
    query = select(Budget).where(Budget.user_id == user.id)
    if project_id:
        await require_project(project_id, user, session)
        query = query.where(Budget.project_id == project_id)

    result = await session.execute(query.order_by(Budget.created_at))
    return [await _to_response(b) for b in result.scalars()]


@router.patch("/budgets/{budget_id}")
async def update_budget(
    budget_id: str, payload: UpdateBudgetRequest, user: CurrentUser, session: DbSession
) -> BudgetResponse:
    result = await session.execute(
        select(Budget).where(Budget.id == budget_id, Budget.user_id == user.id)
    )
    budget = result.scalar_one_or_none()
    if budget is None:
        raise NotFoundError("No such budget")

    if payload.limit_usd is not None:
        budget.limit_usd = payload.limit_usd
    if payload.action is not None:
        budget.action = payload.action
    if payload.is_active is not None:
        budget.is_active = payload.is_active

    budget.updated_at = datetime.now(UTC)
    await session.flush()
    await _purge_after_commit(user.id, budget.project_id)
    return await _to_response(budget)


@router.delete("/budgets/{budget_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_budget(budget_id: str, user: CurrentUser, session: DbSession) -> None:
    result = await session.execute(
        select(Budget).where(Budget.id == budget_id, Budget.user_id == user.id)
    )
    budget = result.scalar_one_or_none()
    if budget is None:
        raise NotFoundError("No such budget")

    project_id = budget.project_id
    await session.delete(budget)
    await session.flush()
    await _purge_after_commit(user.id, project_id)


# -- Alert history ---------------------------------------------------- UC-34


@router.get("/alerts")
async def list_alerts(
    user: CurrentUser,
    session: DbSession,
    project_id: str | None = None,
    alert_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[AlertResponse]:
    query = select(AlertEvent).where(AlertEvent.user_id == user.id)
    if project_id:
        await require_project(project_id, user, session)
        query = query.where(AlertEvent.project_id == project_id)
    if alert_status:
        query = query.where(AlertEvent.status == alert_status)

    result = await session.execute(query.order_by(AlertEvent.created_at.desc()).limit(limit))
    return [_alert_response(a) for a in result.scalars()]


@router.post("/alerts/{alert_id}/resolve")
async def resolve_alert(
    alert_id: str, payload: ResolveAlertRequest, user: CurrentUser, session: DbSession
) -> AlertResponse:
    """Record what the user did about an alert (UC-34).

    The resolution note is the part worth keeping. "Was this real, and what did
    we do" is the question a user asks of an alert history six weeks later, and
    a status flag alone cannot answer it.
    """
    result = await session.execute(
        select(AlertEvent).where(AlertEvent.id == alert_id, AlertEvent.user_id == user.id)
    )
    alert = result.scalar_one_or_none()
    if alert is None:
        raise NotFoundError("No such alert")

    alert.status = payload.status
    alert.resolution = payload.resolution
    alert.resolved_at = datetime.now(UTC) if payload.status == "resolved" else None
    await session.flush()
    return _alert_response(alert)


# -- Kill switch ------------------------------------------------------ UC-33


@router.post("/projects/{project_id}/kill")
async def kill_project(
    project_id: str, user: CurrentUser, session: DbSession
) -> KillSwitchResponse:
    """Revoke every proxy key for a project, immediately (UC-33).

    Must take effect in under a second (BUILD_SPEC §4 P6), which rules out
    waiting for the 60 s auth cache to expire — so the revocation and the cache
    purge happen together, and the purge is what actually makes it instant.

    Provider keys are deliberately untouched. The user is containing a leak of
    *our* credential; destroying their OpenAI key as well would turn one
    incident into two, and it is not ours to destroy.
    """
    started = time.perf_counter()
    project = await require_project(project_id, user, session)
    alert_id = new_id()

    # A separate, immediately-committed transaction. The request-scoped session
    # does not commit until after the handler returns, and the purge below has
    # to happen *after* the revocation is durable: purge first and a concurrent
    # proxy request re-resolves from rows that are still live, re-caching a
    # working key for another 60 s. That is the one failure a kill switch
    # cannot have.
    async with session_scope(user.id) as write:
        result = await write.execute(
            text(
                "UPDATE proxy_keys SET revoked_at = now() "
                "WHERE project_id = :project_id AND user_id = :user_id "
                "AND revoked_at IS NULL RETURNING proxy_key_hash"
            ),
            {"project_id": project.id, "user_id": user.id},
        )
        hashes = [row.proxy_key_hash for row in result]

        await write.execute(
            text(
                "INSERT INTO alert_events (id, user_id, project_id, alert_type, severity, "
                "title, detail, status) VALUES (:id, :user_id, :project_id, 'kill_switch', "
                "'critical', :title, CAST(:detail AS jsonb), 'resolved')"
            ),
            {
                "id": alert_id,
                "user_id": user.id,
                "project_id": project.id,
                "title": f"Proxy access killed for {project.name}",
                "detail": json.dumps({"keys_revoked": len(hashes)}),
            },
        )

    from apicost.proxy.auth import purge_auth_cache_many

    await purge_auth_cache_many(get_redis(), hashes)

    took_ms = (time.perf_counter() - started) * 1000.0

    _logger.warning(
        "kill_switch_used",
        project_id=project.id,
        keys_revoked=len(hashes),
        took_ms=round(took_ms, 2),
    )

    return KillSwitchResponse(
        project_id=project.id,
        keys_revoked=len(hashes),
        took_ms=round(took_ms, 2),
        alert_id=alert_id,
    )


# -- helpers ----------------------------------------------------------------


async def _purge_after_commit(user_id: str, project_id: str) -> None:
    """Drop the project's cached auth resolutions in a committed transaction.

    The request-scoped session has not committed yet — ``session_scope`` does
    that after the handler returns — so purging on it would clear the cache
    while the old rows are still what Postgres would serve. The next proxied
    request would repopulate it with the pre-change settings and the new budget
    would take up to 60 s to bite, which for a hard stop is the wrong direction
    to be slow in.
    """
    async with session_scope(user_id) as scoped:
        await purge_project_auth_cache(scoped, get_redis(), user_id, project_id)


async def _to_response(budget: Budget) -> BudgetResponse:
    spent = await _current_spend(budget.project_id, budget.period)
    limit = float(budget.limit_usd)
    return BudgetResponse(
        id=budget.id,
        project_id=budget.project_id,
        period=budget.period,
        limit_usd=budget.limit_usd,
        action=budget.action,
        is_active=budget.is_active,
        spent_usd=round(spent, 6),
        fraction_used=round(spent / limit, 4) if limit > 0 else 0.0,
        created_at=budget.created_at,
    )


async def _current_spend(project_id: str, period: str) -> float:
    try:
        raw = await get_redis().get(budget_counter_key(project_id, period))
        return float(raw) if raw is not None else 0.0
    except (TypeError, ValueError):
        return 0.0
    except Exception:
        # Reporting, not enforcement — this one may fail open.
        _logger.warning("budget_spend_read_failed", subsystem="budgets", project_id=project_id)
        return 0.0


def _alert_response(alert: AlertEvent) -> AlertResponse:
    return AlertResponse(
        id=alert.id,
        project_id=alert.project_id,
        alert_type=alert.alert_type,
        severity=alert.severity,
        title=alert.title,
        detail=alert.detail,
        status=alert.status,
        notified_at=alert.notified_at,
        resolved_at=alert.resolved_at,
        resolution=alert.resolution,
        created_at=alert.created_at,
    )
