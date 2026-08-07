"""Background tasks. P2 adds the ledger drain.

The drain is what makes hard rule 7 workable: the proxy pushes a compact event
to a Redis stream and returns, and this reads batches out and writes them to
``requests_log``.

It uses a Redis **consumer group**, not a plain read. The difference matters on
restart: with a consumer group an entry stays pending until it is explicitly
acknowledged, so a worker that dies mid-batch leaves its entries to be
reclaimed rather than silently dropping them. Ledger loss is tolerable under a
Redis outage — that tradeoff is already made — but it should not also happen
every time the worker is redeployed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import ResponseError
from sqlalchemy import text

from apicost.config import Settings, get_settings
from apicost.core.logging import get_logger
from apicost.db.redis import get_redis
from apicost.db.session import get_admin_engine

__all__ = [
    "CONSUMER_GROUP",
    "drain_ledger",
    "ensure_consumer_group",
    "ensure_partitions",
    "parse_event_fields",
]

CONSUMER_GROUP = "apicost-ledger-writers"
CONSUMER_NAME = "worker"

_logger = get_logger(__name__)


async def ensure_consumer_group(redis: Redis, settings: Settings | None = None) -> None:
    """Create the consumer group, tolerating the case where it exists."""
    cfg = settings or get_settings()
    try:
        await redis.xgroup_create(cfg.ledger_stream_key, CONSUMER_GROUP, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def _as_int(value: str | None, default: int = 0) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _as_float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _as_decimal(value: str | None) -> Decimal | None:
    try:
        return Decimal(value) if value not in (None, "") else None
    except (TypeError, InvalidOperation):
        return None


def _as_bool(value: str | None) -> bool:
    return value == "1"


def parse_event_fields(fields: dict[str, str]) -> dict[str, Any] | None:
    """Turn a stream entry back into row values.

    Returns ``None`` for an entry we cannot make sense of. A malformed event is
    logged and acknowledged rather than retried forever — one bad row must not
    wedge the drain and stall every subsequent write.
    """
    request_id = fields.get("request_id")
    user_id = fields.get("user_id")
    project_id = fields.get("project_id")

    if not (request_id and user_id and project_id):
        return None

    timestamp_raw = fields.get("timestamp")
    try:
        timestamp = datetime.fromisoformat(timestamp_raw) if timestamp_raw else datetime.now(UTC)
    except ValueError:
        timestamp = datetime.now(UTC)

    return {
        "request_id": request_id,
        "user_id": user_id,
        "project_id": project_id,
        "timestamp": timestamp,
        "endpoint": fields.get("endpoint", ""),
        "provider": fields.get("provider", ""),
        "model_requested": fields.get("model_requested", ""),
        "model_used": fields.get("model_used", ""),
        "tokens_in": _as_int(fields.get("tokens_in")),
        "tokens_out": _as_int(fields.get("tokens_out")),
        "tokens_estimated": _as_bool(fields.get("tokens_estimated")),
        "cost_usd": _as_decimal(fields.get("cost_usd")) or Decimal("0"),
        "cost_would_have_been_usd": _as_decimal(fields.get("cost_would_have_been_usd")),
        "latency_ms": _as_float(fields.get("latency_ms")) or 0.0,
        "ttft_ms": _as_float(fields.get("ttft_ms")),
        "itl_ms": _as_float(fields.get("itl_ms")),
        "tps": _as_float(fields.get("tps")),
        "cache_hit": _as_bool(fields.get("cache_hit")),
        "cache_similarity": _as_float(fields.get("cache_similarity")),
        "routed": _as_bool(fields.get("routed")),
        "routing_reason_code": fields.get("routing_reason_code"),
        "routing_model_version": fields.get("routing_model_version"),
        "escalation_triggered": _as_bool(fields.get("escalation_triggered")),
        "status": _as_int(fields.get("status"), 200),
        "error_code": fields.get("error_code"),
        "streamed": _as_bool(fields.get("streamed")),
    }


_INSERT_SQL = text(
    """
    INSERT INTO requests_log (
        id, timestamp, user_id, project_id, request_id, endpoint, provider,
        model_requested, model_used, tokens_in, tokens_out, tokens_estimated,
        cost_usd, cost_would_have_been_usd, latency_ms, ttft_ms, itl_ms, tps,
        cache_hit, cache_similarity, routed, routing_reason_code,
        routing_model_version, escalation_triggered, status, error_code, streamed
    ) VALUES (
        :id, :timestamp, :user_id, :project_id, :request_id, :endpoint, :provider,
        :model_requested, :model_used, :tokens_in, :tokens_out, :tokens_estimated,
        :cost_usd, :cost_would_have_been_usd, :latency_ms, :ttft_ms, :itl_ms, :tps,
        :cache_hit, :cache_similarity, :routed, :routing_reason_code,
        :routing_model_version, :escalation_triggered, :status, :error_code, :streamed
    )
    ON CONFLICT DO NOTHING
    """
)


async def drain_ledger(
    redis: Redis | None = None,
    settings: Settings | None = None,
    *,
    block_ms: int | None = None,
    max_batches: int | None = 1,
) -> int:
    """Move pending events from the stream into ``requests_log``.

    Returns the number of rows written. Runs as an ARQ cron job and is also
    called directly by tests.

    Writes go through :func:`~apicost.db.session.get_admin_engine`, which is
    exempt from RLS: one batch spans many users, so there is no single
    ``app.user_id`` to scope it to. See that function's docstring for why that
    is safe here and nowhere else.
    """
    cfg = settings or get_settings()
    client = redis or get_redis(cfg)

    await ensure_consumer_group(client, cfg)

    written = 0
    batches = 0

    while max_batches is None or batches < max_batches:
        batches += 1
        try:
            response = await client.xreadgroup(
                CONSUMER_GROUP,
                CONSUMER_NAME,
                {cfg.ledger_stream_key: ">"},
                count=cfg.ledger_batch_size,
                block=block_ms if block_ms is not None else cfg.ledger_block_ms,
            )
        except Exception:
            _logger.warning("ledger_drain_read_failed", subsystem="ledger")
            return written

        if not response:
            break

        for _stream, entries in response:
            rows: list[dict[str, Any]] = []
            ack_ids: list[str] = []

            for entry_id, fields in entries:
                ack_ids.append(entry_id)
                parsed = parse_event_fields(fields)
                if parsed is None:
                    _logger.warning("ledger_event_malformed", entry_id=entry_id)
                    continue
                parsed["id"] = parsed["request_id"]
                rows.append(parsed)

            if rows:
                try:
                    # `database_admin_url` — see the docstring above.
                    async with get_admin_engine().begin() as conn:
                        await conn.execute(_INSERT_SQL, rows)
                    written += len(rows)
                except Exception:
                    # Leave them unacknowledged so the next pass reclaims them.
                    _logger.warning("ledger_batch_write_failed", subsystem="ledger", rows=len(rows))
                    return written

            if ack_ids:
                try:
                    await client.xack(cfg.ledger_stream_key, CONSUMER_GROUP, *ack_ids)
                except Exception:
                    _logger.warning("ledger_ack_failed", subsystem="ledger")

    if written:
        _logger.info("ledger_drained", rows=written)

    return written


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = (year * 12 + (month - 1)) + delta
    return index // 12, index % 12 + 1


async def ensure_partitions(months_ahead: int = 3, months_back: int = 18) -> int:
    """Maintain ``requests_log`` partitions either side of today.

    Backward as well as forward, deliberately. Rows older than the newest
    partition — a backfill, an import, a seeded database — otherwise land in
    DEFAULT, which no range predicate can prune. That turns "last 30 days" into
    a scan of all history; it was measured at 4.0 s against a 500 ms budget
    before migration 0005 fixed it.
    """
    created = 0
    today = datetime.now(UTC).date()
    year, month = _shift_month(today.year, today.month, -months_back)

    for _ in range(months_back + months_ahead + 1):
        start = f"{year:04d}-{month:02d}-01"
        end_year, end_month = _shift_month(year, month, 1)
        end = f"{end_year:04d}-{end_month:02d}-01"
        name = f"requests_log_{year:04d}_{month:02d}"

        async with get_admin_engine().begin() as conn:
            exists = await conn.execute(
                text("SELECT to_regclass(:name) IS NOT NULL"), {"name": name}
            )
            if not exists.scalar():
                await conn.execute(
                    text(
                        f"CREATE TABLE {name} PARTITION OF requests_log "
                        f"FOR VALUES FROM ('{start}') TO ('{end}')"
                    )
                )
                created += 1

        year, month = end_year, end_month

    if created:
        _logger.info("ledger_partitions_created", count=created)
    return created
