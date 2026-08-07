"""Per-request decision log — UC-12.

BUILD_SPEC §4 P3 calls this "the single most important trust-building screen in
the product", and that shapes the design: for every call it must be obvious
what we did (`cache_hit | routed | passthrough`), which model was asked for
versus used, what it cost, and why.

Pagination is **keyset**, not offset. Offset pagination degrades linearly as a
user scrolls — `OFFSET 900000` makes Postgres walk 900,000 rows to discard
them — and this table grows without bound. The cursor is
``(timestamp, id)``, which matches the `(user_id, timestamp DESC)` index and
stays O(1) at any depth.
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import text

from apicost.api.deps import CurrentUser, DbSession
from apicost.core.errors import InvalidRequestError, NotFoundError

router = APIRouter(prefix="/requests", tags=["requests"])

MAX_PAGE_SIZE = 200

Decision = Literal["cache_hit", "routed", "passthrough", "escalated", "error"]


class RequestRow(BaseModel):
    id: str
    request_id: str
    timestamp: datetime
    project_id: str
    endpoint: str
    provider: str

    decision: Decision
    """Pre-computed so the table does not have to re-derive it per row, and so
    the meaning lives in one place rather than in every client."""

    model_requested: str
    model_used: str
    tokens_in: int
    tokens_out: int
    tokens_estimated: bool
    cost_usd: Decimal
    cost_would_have_been_usd: Decimal | None
    saved_usd: Decimal
    latency_ms: float
    ttft_ms: float | None
    cache_hit: bool
    cache_similarity: float | None
    routed: bool
    routing_reason_code: str | None
    escalation_triggered: bool
    status: int
    error_code: str | None
    streamed: bool


class RequestPage(BaseModel):
    rows: list[RequestRow]
    next_cursor: str | None
    has_more: bool


def encode_cursor(timestamp: datetime, row_id: str) -> str:
    payload = json.dumps({"t": timestamp.isoformat(), "i": row_id})
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode())
        return datetime.fromisoformat(payload["t"]), str(payload["i"])
    except (binascii.Error, ValueError, KeyError, TypeError) as exc:
        raise InvalidRequestError("Malformed cursor") from exc


def _decision(row: Any) -> Decision:
    """One row's decision, in precedence order.

    A cache hit outranks everything: the provider was never called, so it
    cannot also be a routing win (CODEBASE_GUIDE §6).
    """
    if row.cache_hit:
        return "cache_hit"
    if row.status >= 400:
        return "error"
    if row.escalation_triggered:
        return "escalated"
    if row.routed:
        return "routed"
    return "passthrough"


def _to_row(row: Any) -> RequestRow:
    would_have_been = row.cost_would_have_been_usd
    saved = (would_have_been - row.cost_usd) if would_have_been is not None else Decimal("0")

    return RequestRow(
        id=row.id,
        request_id=row.request_id,
        timestamp=row.timestamp,
        project_id=row.project_id,
        endpoint=row.endpoint,
        provider=row.provider,
        decision=_decision(row),
        model_requested=row.model_requested,
        model_used=row.model_used,
        tokens_in=row.tokens_in,
        tokens_out=row.tokens_out,
        tokens_estimated=row.tokens_estimated,
        cost_usd=row.cost_usd,
        cost_would_have_been_usd=would_have_been,
        saved_usd=saved,
        latency_ms=row.latency_ms,
        ttft_ms=row.ttft_ms,
        cache_hit=row.cache_hit,
        cache_similarity=row.cache_similarity,
        routed=row.routed,
        routing_reason_code=row.routing_reason_code,
        escalation_triggered=row.escalation_triggered,
        status=row.status,
        error_code=row.error_code,
        streamed=row.streamed,
    )


@router.get("", response_model=RequestPage)
async def list_requests(
    user: CurrentUser,
    session: DbSession,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
    project_id: str | None = None,
    model: str | None = None,
    decision: Decision | None = None,
    status_min: int | None = None,
    search: str | None = None,
) -> RequestPage:
    """The decision log, newest first, keyset-paginated."""
    conditions = ["user_id = :user_id"]
    params: dict[str, Any] = {"user_id": user.id, "limit": limit + 1}

    if cursor:
        cursor_timestamp, cursor_id = decode_cursor(cursor)
        # Strict tuple comparison, so a row is never skipped or repeated when
        # several share a timestamp.
        conditions.append("(timestamp, id) < (:cursor_ts, :cursor_id)")
        params["cursor_ts"] = cursor_timestamp
        params["cursor_id"] = cursor_id

    if project_id:
        conditions.append("project_id = :project_id")
        params["project_id"] = project_id
    if model:
        conditions.append("model_used = :model")
        params["model"] = model
    if status_min is not None:
        conditions.append("status >= :status_min")
        params["status_min"] = status_min
    if search:
        conditions.append("request_id = :search")
        params["search"] = search

    if decision == "cache_hit":
        conditions.append("cache_hit")
    elif decision == "routed":
        conditions.append("routed AND NOT cache_hit AND NOT escalation_triggered")
    elif decision == "escalated":
        conditions.append("escalation_triggered")
    elif decision == "passthrough":
        conditions.append("NOT cache_hit AND NOT routed AND status < 400")
    elif decision == "error":
        conditions.append("status >= 400")

    result = await session.execute(
        text(
            f"""
            SELECT * FROM requests_log
            WHERE {" AND ".join(conditions)}
            ORDER BY timestamp DESC, id DESC
            LIMIT :limit
            """
        ),
        params,
    )

    fetched = list(result)
    has_more = len(fetched) > limit
    page = fetched[:limit]

    next_cursor = encode_cursor(page[-1].timestamp, page[-1].id) if page and has_more else None

    return RequestPage(
        rows=[_to_row(row) for row in page],
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.get("/{request_id}", response_model=RequestRow)
async def get_request(request_id: str, user: CurrentUser, session: DbSession) -> RequestRow:
    """One request, for the detail drawer — UC-12, UC-16."""
    result = await session.execute(
        text(
            "SELECT * FROM requests_log WHERE user_id = :user_id "
            "AND request_id = :request_id ORDER BY timestamp DESC LIMIT 1"
        ),
        {"user_id": user.id, "request_id": request_id},
    )
    row = result.one_or_none()
    if row is None:
        raise NotFoundError("No such request")
    return _to_row(row)
