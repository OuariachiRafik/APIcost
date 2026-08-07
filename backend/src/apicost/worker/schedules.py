"""ARQ worker definition and cron schedule.

Run with ``arq apicost.worker.schedules.WorkerSettings``.

The ledger drain runs every 5 seconds rather than on a longer cycle because
P2's acceptance criterion is that a request appears in ``requests_log`` within
5 seconds. The drain also blocks on the stream read, so a quiet system costs
one idle connection rather than a busy loop.
"""

from __future__ import annotations

from typing import Any, ClassVar

from arq import cron
from arq.connections import RedisSettings

from apicost.config import get_settings
from apicost.core.logging import configure_logging, get_logger
from apicost.db.redis import close_redis
from apicost.db.session import dispose_engine
from apicost.ledger.rollup import rebuild_rollups
from apicost.worker.tasks import drain_ledger, ensure_partitions

__all__ = ["WorkerSettings"]

_logger = get_logger(__name__)


async def drain_ledger_job(ctx: dict[str, Any]) -> int:
    """Cron entrypoint for the ledger drain."""
    return await drain_ledger(max_batches=10)


async def rebuild_rollups_job(ctx: dict[str, Any]) -> int:
    """Keep the usage rollups fresh (ADR 0006)."""
    return await rebuild_rollups()


async def ensure_partitions_job(ctx: dict[str, Any]) -> int:
    """Keep ``requests_log`` partitions provisioned ahead of time."""
    return await ensure_partitions()


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json, service="worker")
    _logger.info("worker_starting", environment=settings.environment)
    await ensure_partitions()
    await rebuild_rollups()


async def shutdown(ctx: dict[str, Any]) -> None:
    await dispose_engine()
    await close_redis()
    _logger.info("worker_stopped")


def _redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(get_settings().redis_url)


class WorkerSettings:
    """ARQ configuration."""

    functions: ClassVar[list[Any]] = [
        drain_ledger_job,
        rebuild_rollups_job,
        ensure_partitions_job,
    ]
    cron_jobs: ClassVar[list[Any]] = [
        # Every 5 s: the ledger visibility target in BUILD_SPEC §4 P2.
        cron(drain_ledger_job, second={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}),
        # Every minute: aggregates lag by at most that, and the API reports
        # how stale they are rather than implying they are live.
        cron(rebuild_rollups_job, second={30}),
        # Daily, well ahead of the month boundary.
        cron(ensure_partitions_job, hour=3, minute=0),
    ]
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = 10
    job_timeout = 120

    @staticmethod
    def redis_settings() -> RedisSettings:  # pragma: no cover - arq reads this
        return _redis_settings()
