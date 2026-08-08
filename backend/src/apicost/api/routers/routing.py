"""Routing rules and reporting — UC-15, UC-18, UC-19.

The savings figure here is the one that must never flatter itself. Routing
savings are the difference between what the requested model would have cost and
what the used model did, **minus the full cost of every escalation retry**
(BUILD_SPEC §4 P5). Cache hits are excluded entirely — they are counted once,
by the cache report, and counting them twice would be the single easiest way to
turn this product's central claim into a lie (CODEBASE_GUIDE §6).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, status
from pydantic import BaseModel, Field
from sqlalchemy import select, text

from apicost.api.deps import CurrentUser, DbSession, require_project
from apicost.api.routers.usage import TimeRange, resolve_window
from apicost.core.ids import new_id
from apicost.core.logging import get_logger
from apicost.db.models import RoutingRule
from apicost.db.redis import get_redis
from apicost.proxy.auth import purge_project_auth_cache

router = APIRouter(tags=["routing"])

_logger = get_logger(__name__)

RuleType = Literal["override", "exclude"]


class CreateRuleRequest(BaseModel):
    project_id: str = Field(min_length=1)
    rule_type: RuleType
    match_condition: dict[str, Any] = Field(default_factory=dict)
    target_model: str | None = None
    priority: int = 0


class RuleResponse(BaseModel):
    id: str
    project_id: str
    rule_type: str
    match_condition: dict[str, Any]
    target_model: str | None
    priority: int
    is_active: bool
    created_at: datetime


class TierRow(BaseModel):
    model: str
    requests: int
    share: float


class RoutingStatsResponse(BaseModel):
    start: datetime
    end: datetime

    routed_requests: int
    passthrough_requests: int
    escalations: int
    escalation_rate: float

    savings_usd: Decimal
    """Routing savings only: (would-have-cost minus actual) over routed,
    non-cached rows, minus the cost of escalation retries. Can go negative on
    an endpoint where escalation fires often — reported honestly, because that
    is the signal to exclude it."""

    gross_savings_usd: Decimal
    escalation_cost_usd: Decimal
    tier_distribution: list[TierRow]


def _to_response(rule: RoutingRule) -> RuleResponse:
    return RuleResponse(
        id=rule.id,
        project_id=rule.project_id,
        rule_type=rule.rule_type,
        match_condition=rule.match_condition or {},
        target_model=rule.target_model,
        priority=rule.priority,
        is_active=rule.is_active,
        created_at=rule.created_at,
    )


@router.post("/routing-rules", response_model=RuleResponse, status_code=status.HTTP_201_CREATED)
async def create_rule(
    payload: CreateRuleRequest, user: CurrentUser, session: DbSession
) -> RuleResponse:
    """Create an override or exclude rule — UC-15, UC-19."""
    from apicost.core.errors import InvalidRequestError

    project = await require_project(payload.project_id, user, session)

    if payload.rule_type == "override" and not payload.target_model:
        raise InvalidRequestError("An override rule needs a target_model")

    rule = RoutingRule(
        id=new_id(),
        user_id=user.id,
        project_id=project.id,
        rule_type=payload.rule_type,
        match_condition=payload.match_condition,
        target_model=payload.target_model,
        priority=payload.priority,
    )
    session.add(rule)
    await session.flush()

    # Rules are carried in the cached auth resolution, so a new rule would
    # otherwise not take effect for up to 60 s.
    await purge_project_auth_cache(session, get_redis(), user.id, project.id)

    _logger.info(
        "routing_rule_created",
        user_id=user.id,
        project_id=project.id,
        rule_type=rule.rule_type,
    )
    return _to_response(rule)


@router.get("/routing-rules", response_model=list[RuleResponse])
async def list_rules(
    user: CurrentUser, session: DbSession, project_id: str | None = None
) -> list[RuleResponse]:
    query = select(RoutingRule).where(RoutingRule.user_id == user.id)
    if project_id:
        query = query.where(RoutingRule.project_id == project_id)

    result = await session.execute(query.order_by(RoutingRule.priority.desc()))
    return [_to_response(rule) for rule in result.scalars()]


@router.delete("/routing-rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(rule_id: str, user: CurrentUser, session: DbSession) -> None:
    from apicost.core.errors import NotFoundError

    result = await session.execute(
        select(RoutingRule).where(RoutingRule.id == rule_id, RoutingRule.user_id == user.id)
    )
    rule = result.scalar_one_or_none()
    if rule is None:
        raise NotFoundError("Routing rule not found")

    project_id = rule.project_id
    await session.delete(rule)
    await session.flush()
    await purge_project_auth_cache(session, get_redis(), user.id, project_id)

    _logger.info("routing_rule_deleted", user_id=user.id, rule_id=rule_id)


@router.get("/routing/stats", response_model=RoutingStatsResponse)
async def routing_stats(
    user: CurrentUser,
    session: DbSession,
    range: TimeRange = "30d",
    project_id: str | None = None,
) -> RoutingStatsResponse:
    """Routing savings, reported separately from caching — UC-18."""
    window_start, window_end, _ = resolve_window(range, None, None)

    clause = "user_id = :user_id AND timestamp >= :start AND timestamp < :end"
    params: dict[str, Any] = {
        "user_id": user.id,
        "start": window_start,
        "end": window_end,
    }
    if project_id:
        clause += " AND project_id = :project_id"
        params["project_id"] = project_id

    totals = (
        await session.execute(
            text(
                f"""
                SELECT
                  COUNT(*) FILTER (WHERE routed AND NOT cache_hit)        AS routed,
                  COUNT(*) FILTER (WHERE NOT routed AND NOT cache_hit)    AS passthrough,
                  COUNT(*) FILTER (WHERE escalation_triggered)            AS escalations,
                  -- The gross win: what routing avoided, before retries.
                  COALESCE(SUM(cost_would_have_been_usd - cost_usd)
                           FILTER (WHERE routed AND NOT cache_hit
                                   AND NOT escalation_triggered), 0)      AS gross,
                  -- An escalated request paid for both calls and ended up on
                  -- the model originally asked for, so routing saved nothing
                  -- and cost the cheap attempt. That is a real loss.
                  COALESCE(SUM(cost_usd - cost_would_have_been_usd)
                           FILTER (WHERE escalation_triggered), 0)        AS escalation_cost
                FROM requests_log
                WHERE {clause}
                """
            ),
            params,
        )
    ).one()

    tiers = await session.execute(
        text(
            f"""
            SELECT model_used AS model, COUNT(*) AS requests
            FROM requests_log
            WHERE {clause} AND NOT cache_hit
            GROUP BY 1
            ORDER BY 2 DESC
            """
        ),
        params,
    )
    tier_rows = list(tiers)
    total_requests = sum(row.requests for row in tier_rows) or 1

    gross = Decimal(str(totals.gross))
    escalation_cost = Decimal(str(totals.escalation_cost))

    return RoutingStatsResponse(
        start=window_start,
        end=window_end,
        routed_requests=totals.routed,
        passthrough_requests=totals.passthrough,
        escalations=totals.escalations,
        escalation_rate=(totals.escalations / totals.routed) if totals.routed else 0.0,
        savings_usd=gross - escalation_cost,
        gross_savings_usd=gross,
        escalation_cost_usd=escalation_cost,
        tier_distribution=[
            TierRow(
                model=row.model,
                requests=row.requests,
                share=row.requests / total_requests,
            )
            for row in tier_rows
        ],
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)
