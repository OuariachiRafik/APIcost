"""Projects and proxy keys — UC-04, UC-05, UC-07."""

from __future__ import annotations

import time

import pytest
from httpx import AsyncClient

from apicost.core.security import hash_proxy_key
from apicost.db.redis import get_redis
from apicost.proxy.auth import AUTH_CACHE_TTL_SECONDS, auth_cache_key
from tests.integration.conftest import register

pytestmark = pytest.mark.integration


@pytest.mark.usefixtures("clean_db")
async def test_create_project_uses_spec_defaults(api_client: AsyncClient) -> None:
    user = await register(api_client, "proj@example.com")

    response = await api_client.post("/projects", headers=user.auth, json={"name": "production"})

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "production"
    assert body["cache_enabled"] is True
    assert body["similarity_threshold"] == 0.95
    assert body["cache_ttl_seconds"] == 86_400
    assert body["store_raw_content"] is False, "raw content must default to off (hard rule 9)"


@pytest.mark.usefixtures("clean_db")
async def test_projects_are_listed_per_user(api_client: AsyncClient) -> None:
    alice = await register(api_client, "alice@example.com")
    bob = await register(api_client, "bob@example.com")

    await api_client.post("/projects", headers=alice.auth, json={"name": "alice-prod"})
    await api_client.post("/projects", headers=alice.auth, json={"name": "alice-staging"})
    await api_client.post("/projects", headers=bob.auth, json={"name": "bob-prod"})

    assert len((await api_client.get("/projects", headers=alice.auth)).json()) == 2
    bob_projects = (await api_client.get("/projects", headers=bob.auth)).json()
    assert len(bob_projects) == 1
    assert bob_projects[0]["name"] == "bob-prod"


@pytest.mark.usefixtures("clean_db")
async def test_cannot_read_another_users_project(api_client: AsyncClient) -> None:
    alice = await register(api_client, "a@example.com")
    bob = await register(api_client, "b@example.com")

    project_id = (
        await api_client.post("/projects", headers=alice.auth, json={"name": "secret"})
    ).json()["id"]

    response = await api_client.get(f"/projects/{project_id}", headers=bob.auth)
    assert response.status_code == 404


@pytest.mark.usefixtures("clean_db")
async def test_settings_update_and_threshold_bounds(api_client: AsyncClient) -> None:
    user = await register(api_client, "settings@example.com")
    project_id = (await api_client.post("/projects", headers=user.auth, json={"name": "p"})).json()[
        "id"
    ]

    updated = await api_client.put(
        f"/projects/{project_id}/settings",
        headers=user.auth,
        json={"similarity_threshold": 0.99, "cache_enabled": False},
    )
    assert updated.status_code == 200
    assert updated.json()["similarity_threshold"] == 0.99
    assert updated.json()["cache_enabled"] is False
    assert updated.json()["cache_ttl_seconds"] == 86_400, "untouched fields must persist"

    # UC-21 fixes the range at 0.80 to 0.99.
    for out_of_range in (0.5, 1.0):
        rejected = await api_client.put(
            f"/projects/{project_id}/settings",
            headers=user.auth,
            json={"similarity_threshold": out_of_range},
        )
        assert rejected.status_code == 422


# ---------------------------------------------------------------------------
# Proxy keys
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("clean_db")
async def test_proxy_key_is_returned_exactly_once(api_client: AsyncClient) -> None:
    user = await register(api_client, "px@example.com")
    project_id = (
        await api_client.post("/projects", headers=user.auth, json={"name": "prod"})
    ).json()["id"]

    created = await api_client.post(
        f"/projects/{project_id}/proxy-keys", headers=user.auth, json={"name": "default"}
    )

    assert created.status_code == 201
    raw_key = created.json()["key"]
    assert raw_key.startswith("apc_live_")

    listed = await api_client.get(f"/projects/{project_id}/proxy-keys", headers=user.auth)
    assert listed.status_code == 200
    assert raw_key not in listed.text, "the raw key must never appear again"
    assert listed.json()[0]["last4"] == raw_key[-4:]
    assert "key" not in listed.json()[0]


@pytest.mark.usefixtures("clean_db", "clean_redis")
async def test_revocation_purges_the_auth_cache_within_a_second(
    api_client: AsyncClient,
) -> None:
    """UC-07. The DB write alone would leave the key live for the 60 s TTL."""
    user = await register(api_client, "revoke@example.com")
    project_id = (
        await api_client.post("/projects", headers=user.auth, json={"name": "prod"})
    ).json()["id"]
    created = await api_client.post(
        f"/projects/{project_id}/proxy-keys", headers=user.auth, json={}
    )
    raw_key = created.json()["key"]
    key_id = created.json()["id"]

    # Simulate the proxy having cached this key's resolution (P2 populates it).
    redis = get_redis()
    cache_key = auth_cache_key(hash_proxy_key(raw_key))
    await redis.set(cache_key, '{"user_id":"x"}', ex=AUTH_CACHE_TTL_SECONDS)
    assert await redis.get(cache_key) is not None

    started = time.perf_counter()
    revoked = await api_client.delete(f"/proxy-keys/{key_id}", headers=user.auth)
    elapsed = time.perf_counter() - started

    assert revoked.status_code == 204
    assert await redis.get(cache_key) is None, "cache entry survived revocation"
    assert elapsed < 1.0, f"revocation took {elapsed:.3f}s, UC-07 allows under 1s"

    listed = await api_client.get(f"/projects/{project_id}/proxy-keys", headers=user.auth)
    assert listed.json()[0]["revoked_at"] is not None


@pytest.mark.usefixtures("clean_db", "clean_redis")
async def test_revocation_does_not_affect_other_projects(api_client: AsyncClient) -> None:
    """The second half of UC-07."""
    user = await register(api_client, "multi@example.com")
    prod_id = (await api_client.post("/projects", headers=user.auth, json={"name": "prod"})).json()[
        "id"
    ]
    staging_id = (
        await api_client.post("/projects", headers=user.auth, json={"name": "staging"})
    ).json()["id"]

    prod_key = (
        await api_client.post(f"/projects/{prod_id}/proxy-keys", headers=user.auth, json={})
    ).json()
    staging_key = (
        await api_client.post(f"/projects/{staging_id}/proxy-keys", headers=user.auth, json={})
    ).json()

    await api_client.delete(f"/proxy-keys/{prod_key['id']}", headers=user.auth)

    staging_listed = (
        await api_client.get(f"/projects/{staging_id}/proxy-keys", headers=user.auth)
    ).json()
    assert staging_listed[0]["id"] == staging_key["id"]
    assert staging_listed[0]["revoked_at"] is None


@pytest.mark.usefixtures("clean_db")
async def test_cannot_revoke_another_users_proxy_key(api_client: AsyncClient) -> None:
    victim = await register(api_client, "v@example.com")
    attacker = await register(api_client, "att@example.com")

    project_id = (
        await api_client.post("/projects", headers=victim.auth, json={"name": "prod"})
    ).json()["id"]
    key_id = (
        await api_client.post(f"/projects/{project_id}/proxy-keys", headers=victim.auth, json={})
    ).json()["id"]

    response = await api_client.delete(f"/proxy-keys/{key_id}", headers=attacker.auth)
    assert response.status_code == 404

    listed = (
        await api_client.get(f"/projects/{project_id}/proxy-keys", headers=victim.auth)
    ).json()
    assert listed[0]["revoked_at"] is None, "another user revoked a key they do not own"


@pytest.mark.usefixtures("clean_db")
async def test_cannot_issue_a_key_for_another_users_project(
    api_client: AsyncClient,
) -> None:
    victim = await register(api_client, "vv@example.com")
    attacker = await register(api_client, "aa@example.com")

    project_id = (
        await api_client.post("/projects", headers=victim.auth, json={"name": "prod"})
    ).json()["id"]

    response = await api_client.post(
        f"/projects/{project_id}/proxy-keys", headers=attacker.auth, json={}
    )
    assert response.status_code == 404
