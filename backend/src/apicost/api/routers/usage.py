"""Usage and spend reporting — UC-08, UC-09, UC-10, UC-11, UC-13, UC-28.

Every query here reads ``requests_log`` and nothing else (CODEBASE_GUIDE §5).

Two constraints shape the SQL:

* **<500 ms p95 against a million rows** (BUILD_SPEC §4 P3). Aggregation happens
  in Postgres against the `(user_id, timestamp DESC)` index, never by pulling
  rows into Python.
* **Both isolation controls, always.** An explicit ``user_id`` filter *and* the
  RLS policy the session is scoped by (hard rule 5).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import text

from apicost.api.deps import CurrentUser, DbSession
from apicost.core.errors import InvalidRequestError
from apicost.core.logging import get_logger

router = APIRouter(prefix="/usage", tags=["usage"])

_logger = get_logger(__name__)

TimeRange = Literal["today", "7d", "30d", "90d", "custom"]
BreakdownDimension = Literal["model", "project", "endpoint", "provider"]

_RANGE_DELTAS: dict[str, timedelta] = {
    "today": timedelta(days=1),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "90d": timedelta(days=90),
}


def resolve_window(
    range_: str, start: datetime | None, end: datetime | None
) -> tuple[datetime, datetime, str]:
    """Turn a range selector into concrete bounds and a bucket size.

    Bucketing follows the window so a chart never has to render thousands of
    points: hourly for a day, daily beyond that.
    """
    now = datetime.now(UTC)

    if range_ == "custom":
        if start is None or end is None:
            raise InvalidRequestError("A custom range needs both 'start' and 'end'")
        if end <= start:
            raise InvalidRequestError("'end' must be after 'start'")
        window_start, window_end = start, end
    else:
        delta = _RANGE_DELTAS.get(range_)
        if delta is None:
            raise InvalidRequestError(f"Unknown range {range_!r}")
        window_start, window_end = now - delta, now

    span = window_end - window_start
    bucket = "hour" if span <= timedelta(days=2) else "day"
    return window_start, window_end, bucket


class UsagePoint(BaseModel):
    bucket: datetime
    cost_usd: Decimal
    cost_would_have_been_usd: Decimal
    requests: int
    tokens_in: int
    tokens_out: int
    cache_hits: int


class UsageSummary(BaseModel):
    """Totals for the window, plus the savings split by mechanism.

    Caching and routing savings are computed separately and never
    double-counted — a cache hit is not also a routing win (CODEBASE_GUIDE §6).
    """

    total_cost_usd: Decimal
    total_would_have_been_usd: Decimal
    cache_savings_usd: Decimal
    routing_savings_usd: Decimal
    total_requests: int
    cache_hits: int
    cache_hit_rate: float
    total_tokens_in: int
    total_tokens_out: int


class UsageResponse(BaseModel):
    range: str
    start: datetime
    end: datetime
    bucket: str
    summary: UsageSummary
    series: list[UsagePoint]


def _scope(project_id: str | None) -> tuple[str, dict[str, Any]]:
    """The shared WHERE clause for `requests_log`. ``user_id`` first, always."""
    clause = "user_id = :user_id AND timestamp >= :start AND timestamp < :end"
    params: dict[str, Any] = {}
    if project_id:
        clause += " AND project_id = :project_id"
        params["project_id"] = project_id
    return clause, params


def _rollup_scope(
    user_id: str, window_start: datetime, window_end: datetime, project_id: str | None
) -> tuple[str, dict[str, Any]]:
    """The same, against the daily rollups, whose grain is a date."""
    clause = "user_id = :user_id AND day >= :start_day AND day <= :end_day"
    params: dict[str, Any] = {
        "user_id": user_id,
        "start_day": window_start.astimezone(UTC).date(),
        "end_day": window_end.astimezone(UTC).date(),
    }
    if project_id:
        clause += " AND project_id = :project_id"
        params["project_id"] = project_id
    return clause, params


@router.get("", response_model=UsageResponse)
async def get_usage(
    user: CurrentUser,
    session: DbSession,
    range: TimeRange = "30d",
    project_id: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> UsageResponse:
    """Spend over time — UC-08."""
    window_start, window_end, bucket = resolve_window(range, start, end)
    rollup_where, rollup_params = _rollup_scope(user.id, window_start, window_end, project_id)
    # The rollup grain is a day, so sub-daily bucketing is not available from it.
    bucket = "day"

    # Read the daily rollup, not `requests_log`. Aggregating raw rows measured
    # 2.3 s at 30 days and 3.8 s at 90 against a 500 ms budget; the rollup turns
    # hundreds of thousands of rows into hundreds. See ADR 0006.
    series_rows = await session.execute(
        text(
            f"""
            SELECT day AS bucket,
                   COALESCE(SUM(cost_usd), 0)            AS cost_usd,
                   COALESCE(SUM(would_have_been_usd), 0) AS would_have_been,
                   COALESCE(SUM(requests), 0)            AS requests,
                   COALESCE(SUM(tokens_in), 0)           AS tokens_in,
                   COALESCE(SUM(tokens_out), 0)          AS tokens_out,
                   COALESCE(SUM(cache_hits), 0)          AS cache_hits,
                   COALESCE(SUM(cache_savings_usd), 0)   AS cache_savings,
                   COALESCE(SUM(routing_savings_usd), 0) AS routing_savings
            FROM usage_rollup_daily
            WHERE {rollup_where}
            GROUP BY 1
            ORDER BY 1
            """
        ),
        rollup_params,
    )

    series: list[UsagePoint] = []
    total_cost = Decimal("0")
    total_would_have_been = Decimal("0")
    total_cache_savings = Decimal("0")
    total_routing_savings = Decimal("0")
    total_requests = 0
    total_cache_hits = 0
    total_tokens_in = 0
    total_tokens_out = 0

    for row in series_rows:
        series.append(
            UsagePoint(
                bucket=datetime.combine(row.bucket, time.min, tzinfo=UTC),
                cost_usd=row.cost_usd,
                cost_would_have_been_usd=row.would_have_been,
                requests=row.requests,
                tokens_in=row.tokens_in,
                tokens_out=row.tokens_out,
                cache_hits=row.cache_hits,
            )
        )
        total_cost += row.cost_usd
        total_would_have_been += row.would_have_been
        total_cache_savings += row.cache_savings
        total_routing_savings += row.routing_savings
        total_requests += row.requests
        total_cache_hits += row.cache_hits
        total_tokens_in += row.tokens_in
        total_tokens_out += row.tokens_out

    hit_rate = (total_cache_hits / total_requests) if total_requests else 0.0

    return UsageResponse(
        range=range,
        start=window_start,
        end=window_end,
        bucket=bucket,
        summary=UsageSummary(
            total_cost_usd=total_cost,
            total_would_have_been_usd=total_would_have_been,
            cache_savings_usd=total_cache_savings,
            routing_savings_usd=total_routing_savings,
            total_requests=total_requests,
            cache_hits=total_cache_hits,
            cache_hit_rate=hit_rate,
            total_tokens_in=total_tokens_in,
            total_tokens_out=total_tokens_out,
        ),
        series=series,
    )


class BreakdownRow(BaseModel):
    key: str
    cost_usd: Decimal
    requests: int
    tokens_in: int
    tokens_out: int
    avg_tokens: float
    share: float


class BreakdownResponse(BaseModel):
    by: str
    start: datetime
    end: datetime
    rows: list[BreakdownRow]


_BREAKDOWN_COLUMNS: dict[str, str] = {
    "model": "model_used",
    "project": "project_id",
    "endpoint": "endpoint",
    "provider": "provider",
}


@router.get("/breakdown", response_model=BreakdownResponse)
async def get_breakdown(
    user: CurrentUser,
    session: DbSession,
    by: BreakdownDimension = "model",
    range: TimeRange = "30d",
    project_id: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> BreakdownResponse:
    """Spend split by model, project, endpoint, or provider — UC-09, UC-10, UC-28.

    ``avg_tokens`` is what UC-28 ranks endpoints by, so prompt optimization
    effort lands where it pays.
    """
    window_start, window_end, _ = resolve_window(range, start, end)
    rollup_where, rollup_params = _rollup_scope(user.id, window_start, window_end, project_id)

    # `by` is validated by the Literal type and mapped through a fixed dict, so
    # nothing caller-supplied reaches the SQL text.
    column = _BREAKDOWN_COLUMNS[by]

    rows = await session.execute(
        text(
            f"""
            SELECT {column}                                 AS key,
                   COALESCE(SUM(cost_usd), 0)               AS cost_usd,
                   COALESCE(SUM(requests), 0)               AS requests,
                   COALESCE(SUM(tokens_in), 0)              AS tokens_in,
                   COALESCE(SUM(tokens_out), 0)             AS tokens_out,
                   CASE WHEN SUM(requests) > 0
                        THEN (SUM(tokens_in) + SUM(tokens_out))::float / SUM(requests)
                        ELSE 0 END                          AS avg_tokens
            FROM usage_rollup_daily
            WHERE {rollup_where}
            GROUP BY 1
            ORDER BY 2 DESC
            """
        ),
        rollup_params,
    )

    materialised = list(rows)
    total = sum(row.cost_usd for row in materialised) or Decimal("0")

    return BreakdownResponse(
        by=by,
        start=window_start,
        end=window_end,
        rows=[
            BreakdownRow(
                key=str(row.key),
                cost_usd=row.cost_usd,
                requests=row.requests,
                tokens_in=row.tokens_in,
                tokens_out=row.tokens_out,
                avg_tokens=float(row.avg_tokens),
                share=float(row.cost_usd / total) if total else 0.0,
            )
            for row in materialised
        ],
    )


class HistogramBucket(BaseModel):
    label: str
    lower: int
    upper: int | None
    requests: int
    cost_usd: Decimal


class TokenDistributionResponse(BaseModel):
    start: datetime
    end: datetime
    buckets: list[HistogramBucket]

    median_tokens_bucket: int
    """Lower bound of the bucket the median falls in — not the median itself.

    Percentiles are derived from the rollup histogram (ADR 0006), and the exact
    value is gone by construction. Named for what it is so no caller reads it
    as an exact token count.
    """

    p95_tokens_bucket: int


# Log-ish boundaries: request sizes span orders of magnitude, so linear buckets
# would put almost everything in the first one.
_TOKEN_BUCKETS: list[tuple[int, int | None]] = [
    (0, 100),
    (100, 500),
    (500, 1_000),
    (1_000, 2_000),
    (2_000, 4_000),
    (4_000, 8_000),
    (8_000, 16_000),
    (16_000, 32_000),
    (32_000, None),
]


@router.get("/token-distribution", response_model=TokenDistributionResponse)
async def get_token_distribution(
    user: CurrentUser,
    session: DbSession,
    range: TimeRange = "30d",
    project_id: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> TokenDistributionResponse:
    """Histogram of request sizes — UC-11."""
    window_start, window_end, _ = resolve_window(range, start, end)
    rollup_where, rollup_params = _rollup_scope(user.id, window_start, window_end, project_id)

    rows = await session.execute(
        text(
            f"""
            SELECT bucket_index,
                   COALESCE(SUM(requests), 0)     AS requests,
                   COALESCE(SUM(cost_usd), 0)     AS cost_usd,
                   COALESCE(SUM(tokens_total), 0) AS tokens_total
            FROM token_bucket_rollup_daily
            WHERE {rollup_where}
            GROUP BY 1
            ORDER BY 1
            """
        ),
        rollup_params,
    )
    counts = {row.bucket_index: (row.requests, row.cost_usd, row.tokens_total) for row in rows}

    # Percentiles from bucket counts rather than raw rows: the exact value is
    # not recoverable from a histogram, so this reports the bucket a percentile
    # falls in. Named `_bucket_floor` in the response so nobody reads it as an
    # exact token count.
    total_requests = int(sum(count for count, _, _ in counts.values()))
    median_tokens = _percentile_from_buckets(counts, total_requests, 0.50)
    p95_tokens = _percentile_from_buckets(counts, total_requests, 0.95)

    return TokenDistributionResponse(
        start=window_start,
        end=window_end,
        buckets=[
            HistogramBucket(
                label=f"{lower:,}-{upper:,}" if upper else f"{lower:,}+",
                lower=lower,
                upper=upper,
                requests=counts.get(index, (0, Decimal("0"), 0))[0],
                cost_usd=counts.get(index, (0, Decimal("0"), 0))[1],
            )
            for index, (lower, upper) in enumerate(_TOKEN_BUCKETS)
        ],
        median_tokens_bucket=median_tokens,
        p95_tokens_bucket=p95_tokens,
    )


@router.get("/export.csv")
async def export_csv(
    user: CurrentUser,
    session: DbSession,
    range: TimeRange = "30d",
    project_id: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> StreamingResponse:
    """Streaming CSV export — UC-13.

    Streamed rather than assembled: an export can cover a million rows, and
    building that in memory would take the API process down.
    """
    window_start, window_end, _ = resolve_window(range, start, end)
    where, extra = _scope(project_id)
    params = {"user_id": user.id, "start": window_start, "end": window_end, **extra}

    columns = [
        "timestamp",
        "request_id",
        "project_id",
        "endpoint",
        "provider",
        "model_requested",
        "model_used",
        "tokens_in",
        "tokens_out",
        "tokens_estimated",
        "cost_usd",
        "cost_would_have_been_usd",
        "latency_ms",
        "cache_hit",
        "routed",
        "routing_reason_code",
        "status",
        "error_code",
    ]

    async def rows() -> AsyncIterator[str]:
        yield ",".join(columns) + "\n"

        result = await session.stream(
            text(
                f"SELECT {', '.join(columns)} FROM requests_log "
                f"WHERE {where} ORDER BY timestamp DESC"
            ),
            params,
        )
        async for row in result:
            yield ",".join(_csv_cell(value) for value in row) + "\n"

    filename = f"apicost-usage-{window_start.date()}-to-{window_end.date()}.csv"
    return StreamingResponse(
        rows(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _csv_cell(value: object) -> str:
    """Render one CSV cell, quoting only when necessary."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    text_value = str(value)
    if any(character in text_value for character in (",", '"', "\n", "\r")):
        escaped = text_value.replace('"', '""')
        return f'"{escaped}"'
    return text_value


def _percentile_from_buckets(
    counts: dict[int, tuple[int, Decimal, int]], total: int, fraction: float
) -> int:
    """The lower bound of the bucket a percentile falls into.

    A histogram cannot give an exact percentile — that information is gone by
    construction. Reporting the bucket floor is honest; interpolating inside a
    bucket would invent precision the data does not have.
    """
    if total <= 0:
        return 0
    target = float(total) * fraction
    running = 0.0
    for index in range(len(_TOKEN_BUCKETS)):
        running += float(counts.get(index, (0, Decimal("0"), 0))[0])
        if running >= target:
            return _TOKEN_BUCKETS[index][0]
    return _TOKEN_BUCKETS[-1][0]
