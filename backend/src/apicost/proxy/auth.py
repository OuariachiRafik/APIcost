"""Proxy-key authentication: raw key to user, project, and settings.

This runs on every proxied request, so it is Redis-first with a 60 s TTL and a
Postgres fallback (BUILD_SPEC §4 P2). The cache is keyed by the SHA-256 hash of
the presented key, never the key itself.

The invariant that governs the whole file (CODEBASE_GUIDE §7.2): revocation
must invalidate the cache entry in the same operation as the database write.
The control plane does that in ``api/routers/proxy_keys.py``; what lives here
is the contract both halves share.

Note the asymmetry with the rest of the pipeline: **authentication does not
fail open.** A cache miss falls back to Postgres, and a Postgres failure is a
503, not an anonymous request. Fail-open applies to optimizations — cache,
routing, stats, logging — never to deciding who someone is.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from redis.asyncio import Redis
from sqlalchemy import select

from apicost.core.errors import AuthenticationError
from apicost.core.logging import get_logger
from apicost.core.security import PROXY_KEY_PREFIX, hash_proxy_key
from apicost.db.models import Project, ProxyKey
from apicost.db.session import session_scope, set_rls_user

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "AUTH_CACHE_PREFIX",
    "AUTH_CACHE_TTL_SECONDS",
    "ResolvedKey",
    "auth_cache_key",
    "extract_bearer_token",
    "purge_auth_cache",
    "purge_auth_cache_many",
    "purge_project_auth_cache",
    "resolve_proxy_key",
]

AUTH_CACHE_PREFIX: Final = "apicost:auth:key:"
AUTH_CACHE_TTL_SECONDS: Final = 60
"""60 s per BUILD_SPEC §4 P2. Short enough to bound the damage if a purge is
missed, long enough to keep the hot path off Postgres."""

_logger = get_logger(__name__)


@dataclass(frozen=True)
class ResolvedKey:
    """Everything the pipeline needs about the caller, resolved once."""

    proxy_key_id: str
    user_id: str
    project_id: str
    project_name: str

    cache_enabled: bool
    similarity_threshold: float
    cache_ttl_seconds: int
    routing_enabled: bool
    escalation_enabled: bool
    store_raw_content: bool

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> ResolvedKey | None:
        try:
            data: dict[str, Any] = json.loads(raw)
            return cls(**data)
        except (json.JSONDecodeError, TypeError, ValueError):
            # A cache entry written by an older shape. Treat as a miss and
            # re-resolve rather than serving something half-understood.
            return None


def auth_cache_key(proxy_key_hash: str) -> str:
    """Redis key for a proxy key's cached resolution.

    Keyed by hash, never the raw key: the cache is one more place a credential
    could be read from, and a hash is not a credential.
    """
    return f"{AUTH_CACHE_PREFIX}{proxy_key_hash}"


def extract_bearer_token(authorization: str | None) -> str:
    """Pull the proxy key out of an ``Authorization`` header.

    Raises:
        AuthenticationError: Missing, malformed, or not one of our keys.
    """
    if not authorization:
        raise AuthenticationError("Missing Authorization header")

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthenticationError("Authorization header must be 'Bearer <key>'")

    token = token.strip()
    if not token.startswith(PROXY_KEY_PREFIX):
        # Almost always a provider key pasted in by mistake. Say so without
        # echoing any of it back.
        raise AuthenticationError(
            "That does not look like an APICost proxy key. "
            f"Expected a key beginning with {PROXY_KEY_PREFIX!r}."
        )

    return token


async def resolve_proxy_key(redis: Redis, raw_key: str) -> ResolvedKey:
    """Resolve a raw proxy key to its user, project, and settings.

    Raises:
        AuthenticationError: Unknown or revoked key.
    """
    key_hash = hash_proxy_key(raw_key)
    cache_key = auth_cache_key(key_hash)

    try:
        cached = await redis.get(cache_key)
    except Exception:
        # Redis down: fall through to Postgres. Slower, still correct.
        cached = None
        _logger.warning("auth_cache_read_failed", subsystem="auth_cache")

    if cached:
        resolved = ResolvedKey.from_json(cached)
        if resolved is not None:
            return resolved

    resolved = await _resolve_from_database(key_hash)

    try:
        await redis.set(cache_key, resolved.to_json(), ex=AUTH_CACHE_TTL_SECONDS)
    except Exception:
        _logger.warning("auth_cache_write_failed", subsystem="auth_cache")

    return resolved


async def _resolve_from_database(key_hash: str) -> ResolvedKey:
    """Look the key up in Postgres, then read its project as that user.

    Two steps, deliberately. The ``proxy_keys`` read runs unscoped by
    necessity — the key hash is *how* we learn which user this is, so there is
    no ``app.user_id`` to set yet (migration 0004). Once the row is in hand we
    scope the session and read the project under the normal strict policy, so
    ``projects`` never has to be readable pre-authentication.

    The lookup is by unique hash and by nothing caller-supplied.
    """
    async with session_scope() as session:
        result = await session.execute(select(ProxyKey).where(ProxyKey.proxy_key_hash == key_hash))
        proxy_key = result.scalar_one_or_none()

        if proxy_key is None:
            raise AuthenticationError("Invalid proxy key")

        # Now we know who this is — scope the session before touching projects.
        await set_rls_user(session, proxy_key.user_id)

        project_result = await session.execute(
            select(Project).where(
                Project.id == proxy_key.project_id,
                Project.user_id == proxy_key.user_id,
            )
        )
        project = project_result.scalar_one_or_none()

    if project is None:
        raise AuthenticationError("This key's project no longer exists")

    if proxy_key.revoked_at is not None:
        raise AuthenticationError("This proxy key has been revoked")

    if project.archived_at is not None:
        raise AuthenticationError("This project has been archived")

    return ResolvedKey(
        proxy_key_id=proxy_key.id,
        user_id=proxy_key.user_id,
        project_id=project.id,
        project_name=project.name,
        cache_enabled=project.cache_enabled,
        similarity_threshold=project.similarity_threshold,
        cache_ttl_seconds=project.cache_ttl_seconds,
        routing_enabled=project.routing_enabled,
        escalation_enabled=project.escalation_enabled,
        store_raw_content=project.store_raw_content,
    )


async def purge_auth_cache(redis: Redis, proxy_key_hash: str) -> None:
    """Drop one proxy key's cache entry.

    Failures are logged and swallowed. The caller has already revoked the key
    in Postgres, which is the durable control; a Redis outage must not turn a
    revocation into an error the user retries. Worst case the key stays usable
    until the 60 s TTL lapses.
    """
    try:
        await redis.delete(auth_cache_key(proxy_key_hash))
    except Exception:
        _logger.warning("auth_cache_purge_failed", subsystem="auth_cache")


async def purge_project_auth_cache(
    session: AsyncSession, redis: Redis, user_id: str, project_id: str
) -> None:
    """Drop every cached resolution for a project's keys.

    Called whenever project settings change. The cached ``ResolvedKey`` carries
    the project's toggles and thresholds, so without this a user who adjusts
    the similarity threshold sees no effect until the TTL lapses.
    """
    from sqlalchemy import select

    from apicost.db.models import ProxyKey

    result = await session.execute(
        select(ProxyKey.proxy_key_hash).where(
            ProxyKey.user_id == user_id,
            ProxyKey.project_id == project_id,
            ProxyKey.revoked_at.is_(None),
        )
    )
    await purge_auth_cache_many(redis, list(result.scalars()))


async def purge_auth_cache_many(redis: Redis, proxy_key_hashes: list[str]) -> None:
    """Drop several entries at once — used by the kill switch (UC-33)."""
    if not proxy_key_hashes:
        return
    try:
        await redis.delete(*[auth_cache_key(h) for h in proxy_key_hashes])
    except Exception:
        _logger.warning(
            "auth_cache_purge_failed", subsystem="auth_cache", count=len(proxy_key_hashes)
        )


async def touch_last_used(redis: Redis, proxy_key_id: str) -> None:
    """Record that a key was used, coalesced through Redis.

    Writing ``last_used_at`` to Postgres on every request would put a
    synchronous write on the critical path, which hard rule 7 forbids. A
    timestamp in Redis is folded into the database by the worker.
    """
    try:
        await redis.hset(  # type: ignore[misc]
            "apicost:proxy_key:last_used",
            proxy_key_id,
            datetime.now(UTC).isoformat(),
        )
    except Exception:
        _logger.debug("last_used_touch_failed", subsystem="auth_cache")
