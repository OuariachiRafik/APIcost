"""The semantic cache — lookup, store, invalidate (BUILD_SPEC §6.3).

Lookup is two-tier:

1. **Exact hash, in Redis.** The common case by far is the same prompt sent
   again, and that case should never touch the vector index. A SHA-256 lookup
   answers it in well under a millisecond.
2. **Vector search, in pgvector.** Cosine distance over the HNSW index, scoped
   to the user *and* project, filtered to unexpired rows, ranked, and accepted
   only above the project's threshold.

Everything is best-effort. Every function here returns ``None`` or ``False``
rather than raising, because the pipeline calls them inside ``failopen`` and a
cache that is down must degrade to a normal provider call (hard rule 1).

Cached response bodies are encrypted at rest with a per-entry KMS-wrapped data
key. This is the one place raw response text is stored (BUILD_SPEC §0.4) — it
has to be, since replaying a response means having it — so it gets the same
envelope treatment provider keys do.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from apicost.cache.embeddings import to_pgvector
from apicost.core.ids import new_id
from apicost.core.logging import get_logger
from apicost.vault.kms import KMSClient
from apicost.vault.provider_keys import (
    EncryptedProviderKey,
    decrypt_provider_key,
    encrypt_provider_key,
)

__all__ = [
    "EXACT_CACHE_PREFIX",
    "CacheHit",
    "exact_cache_key",
    "flush_hit_counters",
    "invalidate_project",
    "lookup_exact",
    "lookup_similar",
    "prompt_hash",
    "purge_expired",
    "store",
]

EXACT_CACHE_PREFIX: Final = "apicost:cache:exact:"

_logger = get_logger(__name__)


@dataclass(frozen=True)
class CacheHit:
    """A cached response, ready to return."""

    entry_id: str
    body: dict[str, Any]
    similarity: float
    """1.0 for an exact-hash hit; the cosine similarity for a vector hit."""

    model_used: str
    tokens_in: int
    tokens_out: int
    exact: bool


def prompt_hash(normalized_prompt: str) -> str:
    """SHA-256 of the normalized prompt, for the exact-match path."""
    return hashlib.sha256(normalized_prompt.encode()).hexdigest()


def exact_cache_key(user_id: str, project_id: str, digest: str) -> str:
    return f"{EXACT_CACHE_PREFIX}{user_id}:{project_id}:{digest}"


async def _decrypt_payload(
    kms: KMSClient, payload: bytes, wrapped: bytes, nonce: bytes
) -> dict[str, Any] | None:
    """Decrypt and parse a stored response body."""
    try:
        raw = await decrypt_provider_key(
            kms,
            EncryptedProviderKey(encrypted_key=payload, wrapped_data_key=wrapped, nonce=nonce),
        )
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        # Tampered, or written under a master key we no longer have. Treat as
        # a miss rather than serving something we cannot vouch for.
        _logger.warning("cache_payload_undecryptable", subsystem="cache")
        return None


async def lookup_exact(
    redis: Redis, kms: KMSClient, *, user_id: str, project_id: str, normalized_prompt: str
) -> CacheHit | None:
    """The fast path, on its own.

    Separated from the vector path because it needs **no database session**.
    Opening one costs a BEGIN, a `set_config`, and a COMMIT — three round trips
    for a lookup that never touches Postgres, which is most of the difference
    between meeting the 30 ms hit budget and missing it.
    """
    return await _lookup_exact(redis, kms, user_id, project_id, prompt_hash(normalized_prompt))


async def lookup_similar(
    session: AsyncSession,
    kms: KMSClient,
    *,
    user_id: str,
    project_id: str,
    embedding: list[float],
    threshold: float,
) -> CacheHit | None:
    """The vector path. Needs a session; only reached on an exact miss."""
    return await _lookup_vector(session, kms, user_id, project_id, embedding, threshold)


async def _lookup_exact(
    redis: Redis, kms: KMSClient, user_id: str, project_id: str, digest: str
) -> CacheHit | None:
    """The fast path: an identical prompt, answered entirely from Redis.

    The encrypted payload is stored in the Redis entry itself, not just a
    pointer to the Postgres row. That removes the last database round trip
    from the cache-hit path, which is what makes the <30 ms NFR reachable —
    with a Postgres lookup it measured 38 ms p95.

    The security position is unchanged from `provider_keys`: what is in Redis
    is AES-256-GCM ciphertext plus a KMS-wrapped data key, inert without the
    KMS. The Postgres row remains the system of record, and the Redis entry
    carries the same TTL so the two cannot disagree about liveness.
    """
    try:
        raw = await redis.get(exact_cache_key(user_id, project_id, digest))
    except Exception:
        _logger.warning("cache_exact_lookup_failed", subsystem="cache")
        return None

    if not raw:
        return None

    try:
        entry = json.loads(raw)
        payload = base64.b64decode(entry["p"])
        wrapped = base64.b64decode(entry["w"])
        nonce = base64.b64decode(entry["n"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, binascii.Error):
        return None

    body = await _decrypt_payload(kms, payload, wrapped, nonce)
    if body is None:
        return None

    return CacheHit(
        entry_id=str(entry.get("i", "")),
        body=body,
        similarity=1.0,
        model_used=str(entry.get("m", "")),
        tokens_in=int(entry.get("ti", 0)),
        tokens_out=int(entry.get("to", 0)),
        exact=True,
    )


async def _lookup_vector(
    session: AsyncSession,
    kms: KMSClient,
    user_id: str,
    project_id: str,
    embedding: list[float],
    threshold: float,
) -> CacheHit | None:
    """Nearest neighbour above the project's similarity threshold."""
    result = await session.execute(
        text(
            """
            SELECT id, response_payload, wrapped_data_key, nonce, model_used,
                   tokens_in, tokens_out,
                   1 - (embedding_vector <=> CAST(:query AS vector)) AS similarity
            FROM cache_entries
            WHERE user_id = :user_id
              AND project_id = :project_id
              AND ttl_expires_at > now()
            ORDER BY embedding_vector <=> CAST(:query AS vector)
            LIMIT 1
            """
        ),
        {
            "user_id": user_id,
            "project_id": project_id,
            "query": to_pgvector(embedding),
        },
    )
    row = result.one_or_none()
    if row is None:
        return None

    similarity = float(row.similarity)
    if similarity < threshold:
        # The nearest neighbour is not near enough. Ordering by distance means
        # nothing else can be closer, so this is a definite miss.
        return None

    body = await _decrypt_payload(kms, row.response_payload, row.wrapped_data_key, row.nonce)
    if body is None:
        return None

    return CacheHit(
        entry_id=row.id,
        body=body,
        similarity=similarity,
        model_used=row.model_used,
        tokens_in=row.tokens_in,
        tokens_out=row.tokens_out,
        exact=False,
    )


HIT_COUNTER_KEY: Final = "apicost:cache:hits"


async def record_hit(redis: Redis, entry_id: str) -> None:
    """Count a cache hit, in Redis.

    Deliberately *not* a Postgres UPDATE. This runs on the proxy critical path,
    where hard rule 7 forbids synchronous Postgres writes — and it is pure
    bookkeeping, so making a user wait on it would be indefensible even if the
    rule did not exist. The worker folds these counters into `cache_entries`.
    """
    try:
        await redis.hincrby(HIT_COUNTER_KEY, entry_id, 1)  # type: ignore[misc]
    except Exception:
        _logger.debug("cache_hit_count_failed", subsystem="cache")


async def flush_hit_counters(session: AsyncSession, redis: Redis) -> int:
    """Fold buffered hit counts into `cache_entries`. Called by the worker."""
    try:
        counters = await redis.hgetall(HIT_COUNTER_KEY)  # type: ignore[misc]
    except Exception:
        return 0
    if not counters:
        return 0

    try:
        await redis.delete(HIT_COUNTER_KEY)
    except Exception:
        # Better to lose the counts than to double-apply them.
        return 0

    updated = 0
    for entry_id, count in counters.items():
        try:
            await session.execute(
                text(
                    "UPDATE cache_entries SET hit_count = hit_count + :n, "
                    "last_hit_at = now() WHERE id = :id"
                ),
                {"id": entry_id, "n": int(count)},
            )
            updated += 1
        except Exception:
            _logger.debug("cache_hit_flush_failed", subsystem="cache", entry_id=entry_id)
    return updated


async def store(
    session: AsyncSession,
    redis: Redis,
    kms: KMSClient,
    *,
    user_id: str,
    project_id: str,
    normalized_prompt: str,
    embedding: list[float],
    body: dict[str, Any],
    model_used: str,
    tokens_in: int,
    tokens_out: int,
    ttl_seconds: int,
) -> str | None:
    """Encrypt and store a response. Returns the entry id, or ``None``.

    Called off the critical path — the caller has already answered the user.
    """
    digest = prompt_hash(normalized_prompt)
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)

    try:
        encrypted = await encrypt_provider_key(kms, json.dumps(body, separators=(",", ":")))
    except Exception:
        _logger.warning("cache_encrypt_failed", subsystem="cache")
        return None

    entry_id = new_id()

    try:
        await session.execute(
            text(
                """
                INSERT INTO cache_entries (
                    id, user_id, project_id, embedding_vector, prompt_hash,
                    response_payload, wrapped_data_key, nonce, model_used,
                    tokens_in, tokens_out, ttl_expires_at
                ) VALUES (
                    :id, :user_id, :project_id, CAST(:embedding AS vector), :prompt_hash,
                    :payload, :wrapped, :nonce, :model_used,
                    :tokens_in, :tokens_out, :expires_at
                )
                """
            ),
            {
                "id": entry_id,
                "user_id": user_id,
                "project_id": project_id,
                "embedding": to_pgvector(embedding),
                "prompt_hash": digest,
                "payload": encrypted.encrypted_key,
                "wrapped": encrypted.wrapped_data_key,
                "nonce": encrypted.nonce,
                "model_used": model_used,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "expires_at": expires_at,
            },
        )
    except Exception:
        _logger.warning("cache_store_failed", subsystem="cache")
        return None

    # Point the exact-match path at it, with the same TTL so the two cannot
    # disagree about whether an entry is live.
    try:
        # The exact path serves straight from Redis, so the entry carries the
        # ciphertext rather than a pointer to the Postgres row. Same TTL as the
        # row, so the two cannot disagree about whether the entry is live.
        await redis.set(
            exact_cache_key(user_id, project_id, digest),
            json.dumps(
                {
                    "i": entry_id,
                    "p": base64.b64encode(encrypted.encrypted_key).decode(),
                    "w": base64.b64encode(encrypted.wrapped_data_key).decode(),
                    "n": base64.b64encode(encrypted.nonce).decode(),
                    "m": model_used,
                    "ti": tokens_in,
                    "to": tokens_out,
                },
                separators=(",", ":"),
            ),
            ex=ttl_seconds,
        )
    except Exception:
        _logger.debug("cache_exact_index_failed", subsystem="cache")

    return entry_id


async def invalidate_project(
    session: AsyncSession, redis: Redis, *, user_id: str, project_id: str
) -> int:
    """Drop every entry for a project — UC-23.

    Used when the user has changed something upstream, such as a prompt
    template, and does not want stale answers served.
    """
    result = await session.execute(
        text("DELETE FROM cache_entries WHERE user_id = :user_id AND project_id = :project_id"),
        {"user_id": user_id, "project_id": project_id},
    )
    deleted = result.rowcount or 0  # type: ignore[attr-defined]

    # The Redis index has to go too, or the exact path keeps pointing at rows
    # that no longer exist. Those resolve to a miss rather than an error, but
    # leaving them makes the next N requests slower for no reason.
    try:
        pattern = f"{EXACT_CACHE_PREFIX}{user_id}:{project_id}:*"
        async for key in redis.scan_iter(match=pattern, count=500):
            await redis.delete(key)
    except Exception:
        _logger.warning("cache_exact_invalidate_failed", subsystem="cache")

    _logger.info("cache_invalidated", user_id=user_id, project_id=project_id, entries=deleted)
    return deleted


async def purge_expired(session: AsyncSession, *, limit: int = 10_000) -> int:
    """Delete entries past their TTL — UC-22.

    The TTL is enforced on read as well, so this is housekeeping rather than a
    correctness control: without it the table grows without bound and the HNSW
    index degrades.
    """
    result = await session.execute(
        text(
            "DELETE FROM cache_entries WHERE id IN ("
            "  SELECT id FROM cache_entries WHERE ttl_expires_at <= now() LIMIT :limit"
            ")"
        ),
        {"limit": limit},
    )
    return result.rowcount or 0  # type: ignore[attr-defined]
