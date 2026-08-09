"""Prompt and context advice — UC-26, UC-27, UC-28.

Everything here is advisory. Nothing in this router changes how a request is
served; it reports what was observed and offers a candidate the user may adopt.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import text

from apicost.advisor.prompts import (
    analyse_context,
    suggest_compression,
)
from apicost.api.deps import CurrentUser, DbSession, require_project
from apicost.api.routers.usage import TimeRange, resolve_window
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


class ContextReport(BaseModel):
    warned_requests: int
    total_requests: int
    warned_fraction: float
    estimated_wasted_usd: float
    by_endpoint: list[ContextWarningRow]


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


@router.get("/advisor/context")
async def context_report(
    user: CurrentUser,
    session: DbSession,
    project_id: str,
    range: TimeRange = "30d",
) -> ContextReport:
    """Which endpoints resend the most stale history — UC-26.

    Reads the flags the proxy recorded, not prompts. A project that never opted
    into storing raw content gets exactly the same report.
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

    return ContextReport(
        warned_requests=warned,
        total_requests=total,
        warned_fraction=round(warned / total, 4) if total else 0.0,
        estimated_wasted_usd=round(wasted, 6),
        by_endpoint=by_endpoint,
    )


@router.get("/advisor/token-heavy")
async def token_heavy_endpoints(
    user: CurrentUser,
    session: DbSession,
    project_id: str,
    range: TimeRange = "30d",
    limit: int = 20,
) -> list[TokenHeavyRow]:
    """Endpoints ranked by average tokens per request — UC-28.

    Average, not total. Total ranks by traffic and tells the user what they
    already know — their busiest endpoint is busiest. The average is what
    identifies the endpoint whose *shape* is expensive, which is the one worth
    changing.
    """
    await require_project(project_id, user, session)
    start, end, _ = resolve_window(range, None, None)

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
                "user_id": user.id,
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
