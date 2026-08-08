"""Cache reporting and invalidation — UC-23, UC-25.

The savings figure here is the one users check to decide whether the product is
worth paying for, so it comes from the same rollup the spend dashboard reads
and is defined the same way: **caching saved the whole avoided call**, because
the provider was never called. It is never mixed with routing savings
(CODEBASE_GUIDE §6).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import text

from apicost.api.deps import CurrentUser, DbSession, require_project
from apicost.api.routers.usage import TimeRange, resolve_window
from apicost.cache.semantic import invalidate_project
from apicost.core.logging import get_logger
from apicost.db.redis import get_redis

router = APIRouter(prefix="/cache", tags=["cache"])

_logger = get_logger(__name__)


class CachePoint(BaseModel):
    day: datetime
    hits: int
    requests: int
    savings_usd: Decimal


class CacheStatsResponse(BaseModel):
    start: datetime
    end: datetime

    hit_rate: float
    hits: int
    requests: int
    savings_usd: Decimal
    """Sum of `cost_would_have_been_usd` over cache hits. The provider call did
    not happen, so the whole amount is saved."""

    entries: int
    live_entries: int
    """Entries not yet past their TTL — what could actually serve a request."""

    avg_hits_per_entry: float
    series: list[CachePoint]


class InvalidateRequest(BaseModel):
    project_id: str = Field(min_length=1)


class InvalidateResponse(BaseModel):
    project_id: str
    entries_removed: int


@router.get("/stats", response_model=CacheStatsResponse)
async def cache_stats(
    user: CurrentUser,
    session: DbSession,
    range: TimeRange = "30d",
    project_id: str | None = None,
) -> CacheStatsResponse:
    """Hit rate, dollars saved, and hits over time — UC-25."""
    window_start, window_end, _ = resolve_window(range, None, None)

    clause = "user_id = :user_id AND day >= :start_day AND day <= :end_day"
    params: dict[str, object] = {
        "user_id": user.id,
        "start_day": window_start.astimezone(UTC).date(),
        "end_day": window_end.astimezone(UTC).date(),
    }
    if project_id:
        clause += " AND project_id = :project_id"
        params["project_id"] = project_id

    rows = await session.execute(
        text(
            f"""
            SELECT day,
                   COALESCE(SUM(cache_hits), 0)        AS hits,
                   COALESCE(SUM(requests), 0)          AS requests,
                   COALESCE(SUM(cache_savings_usd), 0) AS savings
            FROM usage_rollup_daily
            WHERE {clause}
            GROUP BY 1
            ORDER BY 1
            """
        ),
        params,
    )

    series: list[CachePoint] = []
    total_hits = 0
    total_requests = 0
    total_savings = Decimal("0")

    for row in rows:
        series.append(
            CachePoint(
                day=datetime.combine(row.day, datetime.min.time(), tzinfo=UTC),
                hits=row.hits,
                requests=row.requests,
                savings_usd=row.savings,
            )
        )
        total_hits += row.hits
        total_requests += row.requests
        total_savings += row.savings

    entry_clause = "user_id = :user_id"
    entry_params: dict[str, object] = {"user_id": user.id}
    if project_id:
        entry_clause += " AND project_id = :project_id"
        entry_params["project_id"] = project_id

    entry_stats = (
        await session.execute(
            text(
                f"""
                SELECT COUNT(*)                                        AS entries,
                       COUNT(*) FILTER (WHERE ttl_expires_at > now())  AS live,
                       COALESCE(AVG(hit_count), 0)                     AS avg_hits
                FROM cache_entries
                WHERE {entry_clause}
                """
            ),
            entry_params,
        )
    ).one()

    return CacheStatsResponse(
        start=window_start,
        end=window_end,
        hit_rate=(total_hits / total_requests) if total_requests else 0.0,
        hits=total_hits,
        requests=total_requests,
        savings_usd=total_savings,
        entries=entry_stats.entries,
        live_entries=entry_stats.live,
        avg_hits_per_entry=float(entry_stats.avg_hits),
        series=series,
    )


@router.post("/invalidate", response_model=InvalidateResponse)
async def invalidate(
    payload: InvalidateRequest, user: CurrentUser, session: DbSession
) -> InvalidateResponse:
    """Clear a project's cache — UC-23.

    For when something upstream changed — a prompt template, a system prompt —
    and stale answers would now be wrong. Clears the Postgres rows and the
    Redis exact-match index together; leaving either behind would keep serving
    the answers the user just asked us to forget.
    """
    project = await require_project(payload.project_id, user, session)

    removed = await invalidate_project(session, get_redis(), user_id=user.id, project_id=project.id)

    _logger.info(
        "cache_invalidated_by_user",
        user_id=user.id,
        project_id=project.id,
        entries=removed,
    )
    return InvalidateResponse(project_id=project.id, entries_removed=removed)
