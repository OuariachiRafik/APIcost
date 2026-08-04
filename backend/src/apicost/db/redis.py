"""Shared Redis client.

Redis is not a cache of convenience in this system — it is load-bearing for
proxy-key auth lookups, budget counters, rolling-stats checkpoints, and the
ledger stream (BUILD_SPEC §2, §6.1). One pooled client per process, shared by
all of them.

This file is an addition to the layout in BUILD_SPEC §3; see
``docs/adr/0002-shared-redis-client-module.md``.
"""

from __future__ import annotations

import asyncio

from redis.asyncio import Redis, from_url

from apicost.config import Settings, get_settings

__all__ = ["check_redis", "close_redis", "get_redis"]

_client: Redis | None = None


def get_redis(settings: Settings | None = None) -> Redis:
    """Return the process-wide Redis client, creating it on first use."""
    global _client
    if _client is None:
        cfg = settings or get_settings()
        _client = from_url(cfg.redis_url, decode_responses=True)
    return _client


async def close_redis() -> None:
    """Close the client and drop the reference. Called from app shutdown."""
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None


async def check_redis(timeout: float | None = None) -> bool:
    """Readiness probe: does the server answer a PING inside the budget?

    Returns ``False`` on any failure. Note this is a *readiness* signal only —
    on the proxy hot path a Redis failure must degrade to fail-open behavior,
    never to an error (CLAUDE.md hard rule 1).
    """
    settings = get_settings()
    budget = timeout if timeout is not None else settings.readiness_timeout_seconds
    try:
        async with asyncio.timeout(budget):
            client = get_redis(settings)
            await client.ping()
        return True
    except Exception:
        return False
