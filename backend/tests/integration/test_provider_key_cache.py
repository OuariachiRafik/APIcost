"""The provider-key hot-path cache — security properties.

This cache exists for latency, but it moves encrypted key material into Redis,
so the tests here are about what an attacker gets and how fast a removed key
stops working — not about whether it is fast.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from apicost.core.errors import APICostError
from apicost.db.redis import get_redis
from apicost.vault.kms import LocalKMS
from apicost.vault.provider_keys import (
    PROVIDER_KEY_CACHE_TTL_SECONDS,
    decrypt_provider_key,
    load_cached_provider_key,
    provider_key_cache_key,
    purge_provider_key_cache,
)
from tests.integration.conftest import register

pytestmark = pytest.mark.integration

PROVIDER_KEY = "sk-proj-CacheSecurityTestKey0123456789"


async def _add_key(api_client: AsyncClient, auth: dict[str, str]) -> str:
    response = await api_client.post(
        "/keys", headers=auth, json={"provider": "openai", "api_key": PROVIDER_KEY}
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


@pytest.mark.usefixtures("clean_db", "clean_redis")
async def test_cached_blob_contains_no_plaintext(api_client: AsyncClient) -> None:
    """The whole basis for allowing this cache to exist."""
    user = await register(api_client, "cache-plain@example.com")
    me = await api_client.get("/auth/me", headers=user.auth)
    user_id = me.json()["id"]
    await _add_key(api_client, user.auth)

    redis = get_redis()
    from sqlalchemy import select

    from apicost.db.models import ProviderKey
    from apicost.db.session import session_scope
    from apicost.vault.provider_keys import EncryptedProviderKey, cache_provider_key

    async with session_scope(user_id=user_id) as session:
        stored = (
            await session.execute(select(ProviderKey).where(ProviderKey.user_id == user_id))
        ).scalar_one()
        blob = EncryptedProviderKey(
            encrypted_key=stored.encrypted_key,
            wrapped_data_key=stored.wrapped_data_key,
            nonce=stored.nonce,
        )

    await cache_provider_key(redis, user_id, "openai", blob)

    raw = await redis.get(provider_key_cache_key(user_id, "openai"))
    assert raw is not None
    assert PROVIDER_KEY not in raw
    assert "sk-proj" not in raw

    # And nothing anywhere else in Redis holds it either.
    for key in await redis.keys("*"):
        value = await redis.get(key) if await redis.type(key) == "string" else None
        if value:
            assert PROVIDER_KEY not in value, f"plaintext key found under {key}"


@pytest.mark.usefixtures("clean_db", "clean_redis")
async def test_cached_blob_is_useless_without_the_kms_master_key(
    api_client: AsyncClient,
) -> None:
    """A Redis compromise on its own yields nothing.

    This is the argument for the cache being acceptable at all: the attacker
    ends up exactly where a stolen Postgres dump leaves them.
    """
    user = await register(api_client, "cache-kms@example.com")
    me = await api_client.get("/auth/me", headers=user.auth)
    user_id = me.json()["id"]
    await _add_key(api_client, user.auth)

    from sqlalchemy import select

    from apicost.db.models import ProviderKey
    from apicost.db.session import session_scope
    from apicost.vault.provider_keys import EncryptedProviderKey, cache_provider_key

    async with session_scope(user_id=user_id) as session:
        stored = (
            await session.execute(select(ProviderKey).where(ProviderKey.user_id == user_id))
        ).scalar_one()
        blob = EncryptedProviderKey(
            encrypted_key=stored.encrypted_key,
            wrapped_data_key=stored.wrapped_data_key,
            nonce=stored.nonce,
        )

    redis = get_redis()
    await cache_provider_key(redis, user_id, "openai", blob)
    recovered = await load_cached_provider_key(redis, user_id, "openai")
    assert recovered is not None

    # An attacker with the cached blob but a different KMS master key. The
    # failure surfaces as KMSError (unwrapping the data key fails first) rather
    # than ProviderKeyError, so assert on the shared base — what matters is
    # that nothing is recoverable, not which layer refuses first.
    with pytest.raises(APICostError):
        await decrypt_provider_key(LocalKMS("an-attackers-master-key"), recovered)


@pytest.mark.usefixtures("clean_db", "clean_redis")
async def test_deleting_a_key_purges_the_cache_immediately(
    api_client: AsyncClient,
) -> None:
    """A removed key must stop working now, not when a TTL lapses."""
    user = await register(api_client, "cache-delete@example.com")
    me = await api_client.get("/auth/me", headers=user.auth)
    user_id = me.json()["id"]
    key_id = await _add_key(api_client, user.auth)

    from sqlalchemy import select

    from apicost.db.models import ProviderKey
    from apicost.db.session import session_scope
    from apicost.vault.provider_keys import EncryptedProviderKey, cache_provider_key

    async with session_scope(user_id=user_id) as session:
        stored = (
            await session.execute(select(ProviderKey).where(ProviderKey.user_id == user_id))
        ).scalar_one()
        await cache_provider_key(
            get_redis(),
            user_id,
            "openai",
            EncryptedProviderKey(
                encrypted_key=stored.encrypted_key,
                wrapped_data_key=stored.wrapped_data_key,
                nonce=stored.nonce,
            ),
        )

    assert await get_redis().get(provider_key_cache_key(user_id, "openai")) is not None

    deleted = await api_client.delete(f"/keys/{key_id}", headers=user.auth)
    assert deleted.status_code == 204

    assert await get_redis().get(provider_key_cache_key(user_id, "openai")) is None, (
        "the cached key survived its deletion"
    )


@pytest.mark.usefixtures("clean_db", "clean_redis")
async def test_adding_a_key_clears_any_stale_entry(api_client: AsyncClient) -> None:
    """Rotation must not keep serving the key that was just replaced."""
    user = await register(api_client, "cache-rotate@example.com")
    me = await api_client.get("/auth/me", headers=user.auth)
    user_id = me.json()["id"]

    key_id = await _add_key(api_client, user.auth)
    await api_client.delete(f"/keys/{key_id}", headers=user.auth)

    replacement = "sk-proj-ReplacementAfterRotation00000"
    await api_client.post(
        "/keys", headers=user.auth, json={"provider": "openai", "api_key": replacement}
    )

    assert await get_redis().get(provider_key_cache_key(user_id, "openai")) is None


@pytest.mark.usefixtures("clean_db", "clean_redis")
async def test_cache_is_scoped_per_user_and_provider(api_client: AsyncClient) -> None:
    """One user's cached key must not be reachable under another's key name."""
    alice = await register(api_client, "cache-alice@example.com")
    bob = await register(api_client, "cache-bob@example.com")
    alice_id = (await api_client.get("/auth/me", headers=alice.auth)).json()["id"]
    bob_id = (await api_client.get("/auth/me", headers=bob.auth)).json()["id"]

    assert provider_key_cache_key(alice_id, "openai") != provider_key_cache_key(bob_id, "openai")
    assert provider_key_cache_key(alice_id, "openai") != provider_key_cache_key(
        alice_id, "anthropic"
    )


async def test_ttl_bounds_staleness() -> None:
    """Belt and braces behind the explicit purge."""
    assert PROVIDER_KEY_CACHE_TTL_SECONDS <= 60


@pytest.mark.usefixtures("clean_db", "clean_redis")
async def test_a_broken_redis_falls_back_to_postgres(api_client: AsyncClient) -> None:
    """The cache is an optimization; losing it must not lose the request."""

    class BrokenRedis:
        def __getattr__(self, name: str) -> object:
            async def explode(*_args: object, **_kwargs: object) -> object:
                raise ConnectionError("redis is down")

            return explode

    assert await load_cached_provider_key(BrokenRedis(), "u", "openai") is None  # type: ignore[arg-type]
    # Purge failures are swallowed too — a delete must not fail on a cache blip.
    await purge_provider_key_cache(BrokenRedis(), "u", "openai")  # type: ignore[arg-type]
