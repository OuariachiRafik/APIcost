"""Cache housekeeping, run by the worker.

Two jobs, both off the critical path:

* fold the Redis hit counters into ``cache_entries`` — the proxy counts hits in
  Redis because hard rule 7 forbids synchronous Postgres writes on the request
  path;
* delete entries past their TTL. Expiry is enforced on read as well, so this is
  housekeeping rather than correctness: without it the table grows without
  bound and the HNSW index degrades.
"""

from __future__ import annotations

from apicost.cache.semantic import flush_hit_counters, purge_expired
from apicost.core.logging import get_logger
from apicost.db.redis import get_redis
from apicost.db.session import get_admin_engine

__all__ = ["run_cache_maintenance"]

_logger = get_logger(__name__)


async def run_cache_maintenance() -> int:
    """Flush hit counters and purge expired entries. Returns rows touched.

    Uses the admin engine: one pass spans every user's entries, so there is no
    single ``app.user_id`` to scope it to. It reads no user content — only
    counters and expiry timestamps.
    """
    touched = 0

    async with get_admin_engine().begin() as conn:
        from sqlalchemy.ext.asyncio import AsyncSession

        session = AsyncSession(bind=conn)
        touched += await flush_hit_counters(session, get_redis())
        touched += await purge_expired(session)

    if touched:
        _logger.info("cache_maintenance_complete", rows=touched)
    return touched
