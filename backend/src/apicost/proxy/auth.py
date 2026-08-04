"""Proxy-key authentication and its Redis cache.

P1 defines the cache *contract* — the key layout, the TTL, and the purge — so
revocation can be correct from the moment keys can be issued. The lookup path
that reads this cache on the hot path arrives with the proxy in P2.

The invariant that matters (CODEBASE_GUIDE §7.2): revocation must invalidate
the cache entry in the same operation as the database write. Otherwise a
revoked key keeps working until the TTL expires, and UC-07 promises "within one
second", not "within sixty".
"""

from __future__ import annotations

from typing import Final

from redis.asyncio import Redis

from apicost.core.logging import get_logger

__all__ = [
    "AUTH_CACHE_PREFIX",
    "AUTH_CACHE_TTL_SECONDS",
    "auth_cache_key",
    "purge_auth_cache",
    "purge_auth_cache_many",
]

AUTH_CACHE_PREFIX: Final = "apicost:auth:key:"
AUTH_CACHE_TTL_SECONDS: Final = 60
"""60 s per BUILD_SPEC §4 P2. Short enough to bound damage, long enough to keep
the hot path off Postgres."""

_logger = get_logger(__name__)


def auth_cache_key(proxy_key_hash: str) -> str:
    """Redis key for a proxy key's cached resolution.

    Keyed by the SHA-256 hash, never the raw key: the cache is one more place a
    credential could be read from, and a hash is not a credential.
    """
    return f"{AUTH_CACHE_PREFIX}{proxy_key_hash}"


async def purge_auth_cache(redis: Redis, proxy_key_hash: str) -> None:
    """Drop one proxy key's cache entry.

    Failures are logged and swallowed. The caller has already revoked the key
    in Postgres, which is the durable control; a Redis outage must not turn a
    revocation into an error the user retries. Worst case the key remains
    usable until the 60 s TTL lapses.
    """
    try:
        await redis.delete(auth_cache_key(proxy_key_hash))
    except Exception:
        _logger.warning("auth_cache_purge_failed", subsystem="auth_cache")


async def purge_auth_cache_many(redis: Redis, proxy_key_hashes: list[str]) -> None:
    """Drop several entries at once — used by the emergency kill switch (UC-33)."""
    if not proxy_key_hashes:
        return
    try:
        await redis.delete(*[auth_cache_key(h) for h in proxy_key_hashes])
    except Exception:
        _logger.warning(
            "auth_cache_purge_failed", subsystem="auth_cache", count=len(proxy_key_hashes)
        )
