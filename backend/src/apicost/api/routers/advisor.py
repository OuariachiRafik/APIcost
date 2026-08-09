"""Prompt and context advice — UC-26, UC-27, UC-28.

Everything here is advisory. Nothing in this router changes how a request is
served; it reports what was observed and offers a candidate the user may adopt.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import text

from apicost.advisor.breakeven import GpuOption, break_even_analysis
from apicost.advisor.nightly import DEFAULT_GPU, GPU_OPTIONS
from apicost.advisor.prompts import (
    analyse_context,
    suggest_compression,
)
from apicost.api.deps import CurrentUser, DbSession, require_project
from apicost.api.routers.usage import TimeRange, resolve_window
from apicost.core.errors import NotFoundError
from apicost.core.logging import get_logger

router = APIRouter(tags=["advisor"])

_logger = get_logger(__name__)


class ContextWarningRow(BaseModel):
    endpoint: str
    requests: int
    warned_requests: int
    avg_reclaimable_tokens: float
    avg_message_count: float
    estimated_wasted_usd: float


class PromptOptimizationReport(BaseModel):
    """UC-26, UC-27 and UC-28 in one payload, per BUILD_SPEC §8.

    The two halves belong together: knowing an endpoint is token-heavy is only
    actionable once you also know how much of those tokens is stale history.
    Split across two endpoints, a dashboard would have to join them to say
    anything useful.
    """

    warned_requests: int
    total_requests: int
    warned_fraction: float
    estimated_wasted_usd: float
    by_endpoint: list[ContextWarningRow]
    token_heavy: list[TokenHeavyRow]
    """UC-28, ranked by average tokens per request.

    `GET /usage/breakdown?by=endpoint` (P3) also exposes `avg_tokens` and can
    answer UC-28 on its own. This is the same fact reported next to the context
    warnings, which is where it leads to an action; the breakdown endpoint
    remains the general-purpose one. See ADR 0009."""


class TokenHeavyRow(BaseModel):
    endpoint: str
    requests: int
    avg_tokens_in: float
    avg_tokens_out: float
    avg_tokens_total: float
    total_cost_usd: float


class CompressRequest(BaseModel):
    body: dict[str, Any] = Field(description="The request body you would send, verbatim.")
    token_threshold: int | None = Field(default=None, ge=0)
    relevance_threshold: float | None = Field(default=None, ge=0.0, le=1.0)


class StaleMessageOut(BaseModel):
    index: int
    role: str
    tokens: int
    relevance: float


class CompressResponse(BaseModel):
    warn: bool
    reason: str
    total_tokens: int
    message_count: int
    stale: list[StaleMessageOut]
    reclaimable_tokens: int
    reclaimable_fraction: float

    suggestion: dict[str, Any] | None = None
    """The candidate request, its token counts, and which messages it drops.
    ``None`` when there is nothing worth suggesting. **Never applied
    automatically** (BUILD_SPEC §4 P7)."""


@router.post("/advisor/compress")
async def compress(payload: CompressRequest, user: CurrentUser) -> CompressResponse:
    """Analyse a prompt and return a trimmed candidate — UC-26, UC-27.

    Takes the body rather than a stored request id, deliberately. Raw prompts
    are not stored unless the project opted in (hard rule 9), so a
    id-based endpoint would work for some users and not others, and the ones it
    failed for would be the privacy-conscious ones. This works for everybody and
    stores nothing.
    """
    kwargs: dict[str, Any] = {}
    if payload.token_threshold is not None:
        kwargs["token_threshold"] = payload.token_threshold
    if payload.relevance_threshold is not None:
        kwargs["relevance_threshold"] = payload.relevance_threshold

    verdict = analyse_context(payload.body, **kwargs)
    candidate = suggest_compression(payload.body, verdict)

    suggestion: dict[str, Any] | None = None
    if candidate is not None:
        suggestion = {
            "messages": candidate.messages,
            "tokens_before": candidate.tokens_before,
            "tokens_after": candidate.tokens_after,
            "tokens_saved": candidate.tokens_saved,
            "fraction_saved": round(candidate.fraction_saved, 4),
            "removed_indices": candidate.removed_indices,
            "strategy": candidate.strategy,
            "applied": False,
        }

    return CompressResponse(
        warn=verdict.warn,
        reason=verdict.reason,
        total_tokens=verdict.total_tokens,
        message_count=verdict.message_count,
        stale=[
            StaleMessageOut(index=s.index, role=s.role, tokens=s.tokens, relevance=s.relevance)
            for s in verdict.stale
        ],
        reclaimable_tokens=verdict.reclaimable_tokens,
        reclaimable_fraction=round(verdict.reclaimable_fraction, 4),
        suggestion=suggestion,
    )


@router.get("/advisor/prompt-optimizations")
async def prompt_optimizations(
    user: CurrentUser,
    session: DbSession,
    project_id: str,
    range: TimeRange = "30d",
) -> PromptOptimizationReport:
    """Prompt and context optimisation opportunities — UC-26, UC-28.

    Reads the flags the proxy recorded, not prompts. A project that never opted
    into storing raw content gets exactly the same report.

    UC-27's *suggestion* is a separate POST: generating a compressed candidate
    needs the prompt itself, and we do not store prompts (hard rule 9), so it
    cannot be served from a GET over history.
    """
    await require_project(project_id, user, session)
    start, end, _ = resolve_window(range, None, None)

    rows = (
        await session.execute(
            text(
                """
                SELECT endpoint,
                       count(*)                                    AS requests,
                       count(*) FILTER (WHERE context_warning)     AS warned,
                       COALESCE(avg(context_reclaimable_tokens)
                                FILTER (WHERE context_warning), 0) AS avg_reclaimable,
                       COALESCE(avg(context_message_count)
                                FILTER (WHERE context_warning), 0) AS avg_messages,
                       COALESCE(sum(
                           CASE WHEN context_warning AND tokens_in > 0
                                THEN cost_usd * (
                                    context_reclaimable_tokens::numeric
                                    / NULLIF(tokens_in, 0)
                                )
                                ELSE 0 END), 0)                    AS wasted
                FROM requests_log
                WHERE user_id = :user_id AND project_id = :project_id
                  AND timestamp >= :start AND timestamp < :end
                GROUP BY endpoint
                ORDER BY warned DESC, requests DESC
                """
            ),
            {"user_id": user.id, "project_id": project_id, "start": start, "end": end},
        )
    ).mappings()

    by_endpoint: list[ContextWarningRow] = []
    total = 0
    warned = 0
    wasted = 0.0

    for row in rows:
        total += int(row["requests"])
        warned += int(row["warned"])
        wasted += float(row["wasted"])
        by_endpoint.append(
            ContextWarningRow(
                endpoint=row["endpoint"],
                requests=int(row["requests"]),
                warned_requests=int(row["warned"]),
                avg_reclaimable_tokens=round(float(row["avg_reclaimable"]), 1),
                avg_message_count=round(float(row["avg_messages"]), 1),
                estimated_wasted_usd=round(float(row["wasted"]), 6),
            )
        )

    return PromptOptimizationReport(
        warned_requests=warned,
        total_requests=total,
        warned_fraction=round(warned / total, 4) if total else 0.0,
        estimated_wasted_usd=round(wasted, 6),
        by_endpoint=by_endpoint,
        token_heavy=await _token_heavy(session, user.id, project_id, start, end),
    )


async def _token_heavy(
    session: Any,
    user_id: str,
    project_id: str,
    start: Any,
    end: Any,
    limit: int = 20,
) -> list[TokenHeavyRow]:
    """Endpoints ranked by average tokens per request — UC-28.

    Average, not total. Total ranks by traffic and tells the user what they
    already know — their busiest endpoint is busiest. The average is what
    identifies the endpoint whose *shape* is expensive, which is the one worth
    changing.
    """
    rows = (
        await session.execute(
            text(
                """
                SELECT endpoint,
                       count(*)                       AS requests,
                       avg(tokens_in)                 AS avg_in,
                       avg(tokens_out)                AS avg_out,
                       avg(tokens_in + tokens_out)    AS avg_total,
                       sum(cost_usd)                  AS cost
                FROM requests_log
                WHERE user_id = :user_id AND project_id = :project_id
                  AND timestamp >= :start AND timestamp < :end
                GROUP BY endpoint
                ORDER BY avg_total DESC
                LIMIT :limit
                """
            ),
            {
                "user_id": user_id,
                "project_id": project_id,
                "start": start,
                "end": end,
                "limit": limit,
            },
        )
    ).mappings()

    return [
        TokenHeavyRow(
            endpoint=row["endpoint"],
            requests=int(row["requests"]),
            avg_tokens_in=round(float(row["avg_in"] or 0), 1),
            avg_tokens_out=round(float(row["avg_out"] or 0), 1),
            avg_tokens_total=round(float(row["avg_total"] or 0), 1),
            total_cost_usd=round(float(row["cost"] or 0), 6),
        )
        for row in rows
    ]


# -- UC-35, UC-37: recommendations ------------------------------------------


class RecommendationResponse(BaseModel):
    id: str
    kind: str
    title: str
    detail: dict[str, Any]
    projected_savings_usd: float
    confidence: str
    sample_size: int
    status: str
    generated_at: Any


class DismissRequest(BaseModel):
    status: str = Field(default="dismissed", pattern="^(adopted|dismissed)$")


@router.get("/advisor/recommendations")
async def list_recommendations(
    user: CurrentUser,
    session: DbSession,
    project_id: str,
    include_dismissed: bool = False,
) -> list[RecommendationResponse]:
    """Recommendations for a project, largest projected saving first — UC-35, UC-37."""
    await require_project(project_id, user, session)

    sql = (
        "SELECT id, kind, title, detail, projected_savings_usd, confidence, "
        "sample_size, status, generated_at FROM advisor_recommendations "
        "WHERE user_id = :user_id AND project_id = :project_id"
    )
    if not include_dismissed:
        sql += " AND status <> 'dismissed'"
    sql += " ORDER BY projected_savings_usd DESC"

    rows = (
        await session.execute(text(sql), {"user_id": user.id, "project_id": project_id})
    ).mappings()

    return [
        RecommendationResponse(
            id=row["id"],
            kind=row["kind"],
            title=row["title"],
            detail=row["detail"],
            projected_savings_usd=float(row["projected_savings_usd"]),
            confidence=row["confidence"],
            sample_size=row["sample_size"],
            status=row["status"],
            generated_at=row["generated_at"],
        )
        for row in rows
    ]


@router.post("/advisor/recommendations/{recommendation_id}/status")
async def set_recommendation_status(
    recommendation_id: str,
    payload: DismissRequest,
    user: CurrentUser,
    session: DbSession,
) -> RecommendationResponse:
    """Adopt or dismiss. A dismissed recommendation is never re-suggested."""
    row = (
        (
            await session.execute(
                text(
                    "UPDATE advisor_recommendations SET status = :status, "
                    "dismissed_at = CASE WHEN :status = 'dismissed' THEN now() ELSE NULL END "
                    "WHERE id = :id AND user_id = :user_id "
                    "RETURNING id, kind, title, detail, projected_savings_usd, confidence, "
                    "sample_size, status, generated_at"
                ),
                {"id": recommendation_id, "user_id": user.id, "status": payload.status},
            )
        )
        .mappings()
        .first()
    )

    if row is None:
        raise NotFoundError("No such recommendation")

    await session.flush()
    return RecommendationResponse(
        id=row["id"],
        kind=row["kind"],
        title=row["title"],
        detail=row["detail"],
        projected_savings_usd=float(row["projected_savings_usd"]),
        confidence=row["confidence"],
        sample_size=row["sample_size"],
        status=row["status"],
        generated_at=row["generated_at"],
    )


# -- UC-36: break-even ------------------------------------------------------


class BreakEvenResponse(BaseModel):
    recommendation: str
    monthly_tokens: int
    api_monthly_cost_usd: float
    gpu_monthly_cost_usd: float
    n_gpus: int
    gpu_option: str
    break_even_tokens: int | None
    capacity_tokens_per_gpu: float
    monthly_saving_usd: float
    caveats: list[str]
    options: list[dict[str, Any]]
    """Every instance type scored, so the dashboard can show the comparison
    rather than a single verdict the user has to trust."""


@router.get("/advisor/breakeven")
async def breakeven(
    user: CurrentUser,
    session: DbSession,
    project_id: str,
    gpu: str | None = None,
    utilization: float = 0.5,
) -> BreakEvenResponse:
    """Self-hosting vs pay-per-token at this project's real volume — UC-36.

    The caveats travel in the payload rather than living in the UI. A bare
    "self-hosting is cheaper" is a misleading recommendation (BUILD_SPEC §6.7),
    and a caveat the frontend can forget to render is a caveat that will be
    forgotten.
    """
    await require_project(project_id, user, session)
    start, end, _ = resolve_window("30d", None, None)

    row = (
        await session.execute(
            text(
                "SELECT COALESCE(sum(tokens_in + tokens_out), 0) AS tokens, "
                "COALESCE(sum(cost_usd), 0) AS cost FROM requests_log "
                "WHERE user_id = :user_id AND project_id = :project_id "
                "AND timestamp >= :start AND timestamp < :end AND NOT cache_hit"
            ),
            {"user_id": user.id, "project_id": project_id, "start": start, "end": end},
        )
    ).one()

    tokens = int(row.tokens)
    cost = float(row.cost)
    per_token = cost / tokens if tokens > 0 else 0.0

    chosen = next((o for o in GPU_OPTIONS if o.name == gpu), DEFAULT_GPU)
    result = break_even_analysis(tokens, per_token, chosen, utilization=utilization)

    options = [_option_summary(tokens, per_token, option, utilization) for option in GPU_OPTIONS]

    return BreakEvenResponse(
        recommendation=result.recommendation,
        monthly_tokens=result.monthly_tokens,
        api_monthly_cost_usd=result.api_monthly_cost_usd,
        gpu_monthly_cost_usd=result.gpu_monthly_cost_usd,
        n_gpus=result.n_gpus,
        gpu_option=result.gpu_option,
        break_even_tokens=result.break_even_tokens,
        capacity_tokens_per_gpu=round(result.capacity_tokens_per_gpu, 1),
        monthly_saving_usd=result.monthly_saving_usd,
        caveats=result.caveats,
        options=options,
    )


def _option_summary(
    tokens: int, per_token: float, option: GpuOption, utilization: float
) -> dict[str, Any]:
    scored = break_even_analysis(tokens, per_token, option, utilization=utilization)
    return {
        "name": option.name,
        "cost_per_hour_usd": option.cost_per_hour_usd,
        "n_gpus": scored.n_gpus,
        "gpu_monthly_cost_usd": scored.gpu_monthly_cost_usd,
        "monthly_saving_usd": scored.monthly_saving_usd,
        "recommendation": scored.recommendation,
        "break_even_tokens": scored.break_even_tokens,
    }
