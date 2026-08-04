"""Provider key API — UC-02, UC-03.

The last test in this file is the one BUILD_SPEC §4 P1 names explicitly: "No
test can retrieve a stored provider key in plaintext through any API path." It
walks every route the API exposes rather than the handful we expect to matter,
so a future endpoint that leaks a key fails this test the day it is added.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from tests.integration.conftest import admin_engine, register

pytestmark = pytest.mark.integration

OPENAI_KEY = "sk-proj-IntegrationTestProviderKey0123456789"
ANTHROPIC_KEY = "sk-ant-api03-IntegrationTestKey9876543210"


@pytest.mark.usefixtures("clean_db")
async def test_add_key_returns_metadata_only(api_client: AsyncClient) -> None:
    user = await register(api_client, "keys@example.com")

    response = await api_client.post(
        "/keys", headers=user.auth, json={"provider": "openai", "api_key": OPENAI_KEY}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["provider"] == "openai"
    assert body["last4"] == OPENAI_KEY[-4:]
    assert body["is_active"] is True
    assert OPENAI_KEY not in response.text
    assert set(body) == {"id", "provider", "last4", "is_active", "added_at", "last_used_at"}


@pytest.mark.usefixtures("clean_db")
async def test_list_keys_never_includes_key_material(api_client: AsyncClient) -> None:
    user = await register(api_client, "list@example.com")
    await api_client.post(
        "/keys", headers=user.auth, json={"provider": "openai", "api_key": OPENAI_KEY}
    )
    await api_client.post(
        "/keys", headers=user.auth, json={"provider": "anthropic", "api_key": ANTHROPIC_KEY}
    )

    response = await api_client.get("/keys", headers=user.auth)

    assert response.status_code == 200
    assert len(response.json()) == 2
    assert OPENAI_KEY not in response.text
    assert ANTHROPIC_KEY not in response.text


@pytest.mark.usefixtures("clean_db")
async def test_stored_row_holds_no_plaintext(api_client: AsyncClient) -> None:
    """Read the raw row: even with database access, the key is not there."""
    user = await register(api_client, "atrest@example.com")
    await api_client.post(
        "/keys", headers=user.auth, json={"provider": "openai", "api_key": OPENAI_KEY}
    )

    engine = admin_engine()
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT encrypted_key, wrapped_data_key, nonce, key_last4 FROM provider_keys")
            )
            row = result.one()
    finally:
        await engine.dispose()

    blob = bytes(row[0]) + bytes(row[1]) + bytes(row[2])
    assert OPENAI_KEY.encode() not in blob
    assert b"sk-proj" not in blob
    assert row[3] == OPENAI_KEY[-4:]


@pytest.mark.usefixtures("clean_db")
async def test_duplicate_active_key_is_rejected(api_client: AsyncClient) -> None:
    user = await register(api_client, "dupe@example.com")
    await api_client.post(
        "/keys", headers=user.auth, json={"provider": "openai", "api_key": OPENAI_KEY}
    )

    duplicate = await api_client.post(
        "/keys", headers=user.auth, json={"provider": "openai", "api_key": OPENAI_KEY}
    )
    assert duplicate.status_code == 409


@pytest.mark.usefixtures("clean_db")
async def test_delete_then_re_add_is_the_rotation_path(api_client: AsyncClient) -> None:
    """UC-03: rotation is delete-then-add, and it must actually work."""
    user = await register(api_client, "rotate-key@example.com")
    created = await api_client.post(
        "/keys", headers=user.auth, json={"provider": "openai", "api_key": OPENAI_KEY}
    )
    key_id = created.json()["id"]

    deleted = await api_client.delete(f"/keys/{key_id}", headers=user.auth)
    assert deleted.status_code == 204

    replacement = "sk-proj-ReplacementKeyAfterRotation000000"
    re_added = await api_client.post(
        "/keys", headers=user.auth, json={"provider": "openai", "api_key": replacement}
    )
    assert re_added.status_code == 201
    assert re_added.json()["last4"] == replacement[-4:]


@pytest.mark.usefixtures("clean_db")
async def test_cannot_delete_another_users_key(api_client: AsyncClient) -> None:
    victim = await register(api_client, "victim@example.com")
    attacker = await register(api_client, "attacker@example.com")

    created = await api_client.post(
        "/keys", headers=victim.auth, json={"provider": "openai", "api_key": OPENAI_KEY}
    )
    victim_key_id = created.json()["id"]

    response = await api_client.delete(f"/keys/{victim_key_id}", headers=attacker.auth)
    assert response.status_code == 404

    still_there = await api_client.get("/keys", headers=victim.auth)
    assert len(still_there.json()) == 1


@pytest.mark.usefixtures("clean_db")
async def test_keys_are_not_visible_across_users(api_client: AsyncClient) -> None:
    victim = await register(api_client, "owner@example.com")
    attacker = await register(api_client, "other@example.com")

    await api_client.post(
        "/keys", headers=victim.auth, json={"provider": "openai", "api_key": OPENAI_KEY}
    )

    response = await api_client.get("/keys", headers=attacker.auth)
    assert response.json() == []


@pytest.mark.usefixtures("clean_db")
async def test_unauthenticated_access_is_refused(api_client: AsyncClient) -> None:
    assert (await api_client.get("/keys")).status_code == 401
    assert (
        await api_client.post("/keys", json={"provider": "openai", "api_key": OPENAI_KEY})
    ).status_code == 401


@pytest.mark.usefixtures("clean_db")
async def test_no_api_path_returns_a_provider_key_in_plaintext(
    api_client: AsyncClient,
) -> None:
    """BUILD_SPEC §4 P1 acceptance criterion 3, over every registered GET route."""
    user = await register(api_client, "sweep@example.com")
    created = await api_client.post(
        "/keys", headers=user.auth, json={"provider": "openai", "api_key": OPENAI_KEY}
    )
    key_id = created.json()["id"]

    project = await api_client.post("/projects", headers=user.auth, json={"name": "prod"})
    project_id = project.json()["id"]
    await api_client.post(
        f"/projects/{project_id}/proxy-keys", headers=user.auth, json={"name": "default"}
    )

    from apicost.main_api import app

    # Driven off the OpenAPI document rather than app.routes: included routers
    # are nested objects in this FastAPI version, so a naive walk sees only the
    # docs endpoints and the sweep would pass while checking nothing. The
    # schema is also the artifact the TypeScript client is generated from, so
    # anything a client can call appears here by construction.
    substitutions = {"{project_id}": project_id, "{key_id}": key_id}

    checked: list[str] = []
    for path, operations in app.openapi()["paths"].items():
        if "get" not in operations:
            continue

        url: str = path
        for placeholder, value in substitutions.items():
            url = url.replace(placeholder, value)
        if "{" in url:  # a path parameter we have no value for
            continue

        response = await api_client.get(url, headers=user.auth)
        checked.append(url)
        assert OPENAI_KEY not in response.text, f"{url} leaked the provider key"
        assert "sk-proj" not in response.text, f"{url} leaked a key fragment"

    # Guard against the sweep silently degenerating to nothing.
    assert len(checked) >= 5, f"route sweep covered only {checked}"
    assert "/keys" in checked
    assert f"/projects/{project_id}" in checked
    assert f"/projects/{project_id}/proxy-keys" in checked
