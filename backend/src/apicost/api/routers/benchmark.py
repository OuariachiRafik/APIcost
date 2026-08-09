"""Peer benchmark and digest preferences — UC-38, UC-39."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import text

from apicost.advisor.benchmark import (
    MIN_COHORT_SIZE,
    CohortStats,
    compare_to_peers,
)
from apicost.api.deps import CurrentUser, DbSession
from apicost.core.logging import get_logger
from apicost.db.session import get_admin_engine
from apicost.notify.digest import unsubscribe_page

router = APIRouter(tags=["benchmark"])

_logger = get_logger(__name__)

LOOKBACK_DAYS = 30


class PeerBenchmarkResponse(BaseModel):
    available: bool
    reason: str
    your_cost_per_request: float
    your_requests: int
    cohort_size: int
    cohort_p25: float
    cohort_p50: float
    cohort_p75: float
    percentile_band: str
    verdict: str
    minimum_cohort_size: int


@router.get("/benchmark/peer")
async def peer_benchmark(user: CurrentUser, session: DbSession) -> PeerBenchmarkResponse:
    (
        """Cost per request against an anonymized cohort — UC-39.

    Two guarantees, and the second is the one that constrains the code:

    - No statistic is published below a cohort of """
        + str(MIN_COHORT_SIZE)
        + """ accounts.
    - Nothing traceable to another account is ever returned. The cohort query
      aggregates other users' *aggregates*; no row, id, email, or individual
      total leaves the database.

    The caller is excluded from their own cohort. Including them would let a
    user in a small cohort infer the rest by watching their own number move
    the median.
    """
    )
    since = datetime.now(UTC) - timedelta(days=LOOKBACK_DAYS)

    mine = (
        await session.execute(
            text(
                "SELECT count(*) AS requests, COALESCE(sum(cost_usd), 0) AS cost "
                "FROM requests_log WHERE user_id = :user_id AND timestamp >= :since"
            ),
            {"user_id": user.id, "since": since},
        )
    ).one()

    your_requests = int(mine.requests)
    your_cpr = float(mine.cost) / your_requests if your_requests else 0.0

    cohort = await _cohort(user.id, since)
    result = compare_to_peers(your_cpr, your_requests, cohort)

    return PeerBenchmarkResponse(
        available=result.available,
        reason=result.reason,
        your_cost_per_request=round(result.your_cost_per_request, 8),
        your_requests=result.your_requests,
        cohort_size=result.cohort_size,
        cohort_p25=round(result.cohort_p25, 8),
        cohort_p50=round(result.cohort_p50, 8),
        cohort_p75=round(result.cohort_p75, 8),
        percentile_band=result.percentile_band,
        verdict=result.verdict,
        minimum_cohort_size=MIN_COHORT_SIZE,
    )


async def _cohort(exclude_user_id: str, since: datetime) -> CohortStats | None:
    """Percentiles of cost-per-request across other active accounts.

    Runs on the **admin** engine and it has to: the whole point is to read
    across users, which RLS exists to prevent. That is safe here only because
    what comes back is three percentiles and a count — the query cannot return
    a user id, and the aggregation happens in Postgres rather than in Python
    over a list of other people's rows.

    Excludes accounts with no traffic, so the cohort is "people using the
    product" rather than "rows in the users table", and the minimum cohort
    size means what it sounds like.
    """
    async with get_admin_engine().begin() as conn:
        row = (
            await conn.execute(
                text(
                    """
                    WITH per_user AS (
                        SELECT user_id,
                               sum(cost_usd) / NULLIF(count(*), 0) AS cost_per_request
                        FROM requests_log
                        WHERE timestamp >= :since AND user_id <> :exclude
                        GROUP BY user_id
                        HAVING count(*) > 0
                    )
                    SELECT count(*) AS size,
                           percentile_cont(0.25) WITHIN GROUP (ORDER BY cost_per_request) AS p25,
                           percentile_cont(0.50) WITHIN GROUP (ORDER BY cost_per_request) AS p50,
                           percentile_cont(0.75) WITHIN GROUP (ORDER BY cost_per_request) AS p75
                    FROM per_user
                    """
                ),
                {"since": since, "exclude": exclude_user_id},
            )
        ).one()

    size = int(row.size)
    if size < MIN_COHORT_SIZE:
        # Return None rather than the row. The percentiles were computed by
        # Postgres, but they do not enter Python and cannot be logged, cached,
        # or accidentally surfaced by a later edit.
        return None

    return CohortStats(
        size=size,
        p25_cost_per_request=float(row.p25 or 0),
        p50_cost_per_request=float(row.p50 or 0),
        p75_cost_per_request=float(row.p75 or 0),
    )


# -- UC-38: unsubscribe ------------------------------------------------------


@router.get("/unsubscribe/{token}", response_class=HTMLResponse)
async def unsubscribe(token: str) -> HTMLResponse:
    """One click, no session, no JavaScript — UC-38.

    Unauthenticated by necessity: the link is opened from a mail client months
    after the fact, and an unsubscribe that requires logging in is one people
    report as spam instead. The token is a 256-bit CSPRNG value and is the only
    thing it can act on.
    """
    async with get_admin_engine().begin() as conn:
        result = await conn.execute(
            text(
                "UPDATE users SET digest_enabled = false "
                "WHERE digest_unsubscribe_token = :token RETURNING id"
            ),
            {"token": token},
        )
        row = result.first()

    if row is None:
        # Deliberately the same shape of page as success, with no hint about
        # whether the token exists. Nothing here should be an oracle.
        return HTMLResponse(unsubscribe_page(False), status_code=404)

    _logger.info("digest_unsubscribed", subsystem="notify")
    return HTMLResponse(unsubscribe_page(True))
