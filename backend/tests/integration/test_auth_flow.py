"""Authentication end to end — UC-01."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.integration.conftest import register

pytestmark = pytest.mark.integration

PASSWORD = "a-sufficiently-long-password"


@pytest.mark.usefixtures("clean_db")
async def test_signup_returns_a_token_pair(api_client: AsyncClient) -> None:
    response = await api_client.post(
        "/auth/signup", json={"email": "New.User@Example.com", "password": PASSWORD}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"] and body["refresh_token"]
    assert PASSWORD not in response.text


@pytest.mark.usefixtures("clean_db")
async def test_email_is_normalized_and_unique_case_insensitively(
    api_client: AsyncClient,
) -> None:
    await register(api_client, "Case.Test@Example.com", PASSWORD)

    duplicate = await api_client.post(
        "/auth/signup", json={"email": "case.test@example.COM", "password": PASSWORD}
    )
    assert duplicate.status_code == 409

    # ...and login works regardless of the casing used.
    login = await api_client.post(
        "/auth/login", json={"email": "CASE.TEST@example.com", "password": PASSWORD}
    )
    assert login.status_code == 200


@pytest.mark.usefixtures("clean_db")
async def test_login_rejects_a_wrong_password(api_client: AsyncClient) -> None:
    await register(api_client, "login@example.com", PASSWORD)

    response = await api_client.post(
        "/auth/login", json={"email": "login@example.com", "password": "wrong-password-x"}
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid email or password"


@pytest.mark.usefixtures("clean_db")
async def test_unknown_email_is_indistinguishable_from_a_wrong_password(
    api_client: AsyncClient,
) -> None:
    """Same status and same message, so login cannot be used to enumerate accounts."""
    await register(api_client, "known@example.com", PASSWORD)

    wrong_password = await api_client.post(
        "/auth/login", json={"email": "known@example.com", "password": "not-the-password"}
    )
    unknown_email = await api_client.post(
        "/auth/login", json={"email": "nobody@example.com", "password": PASSWORD}
    )

    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json()["detail"] == unknown_email.json()["detail"]


@pytest.mark.usefixtures("clean_db")
async def test_me_requires_and_accepts_a_token(api_client: AsyncClient) -> None:
    user = await register(api_client, "me@example.com", PASSWORD)

    anonymous = await api_client.get("/auth/me")
    assert anonymous.status_code == 401

    authenticated = await api_client.get("/auth/me", headers=user.auth)
    assert authenticated.status_code == 200
    assert authenticated.json()["email"] == "me@example.com"


@pytest.mark.usefixtures("clean_db")
async def test_password_hash_is_never_exposed(api_client: AsyncClient) -> None:
    user = await register(api_client, "hash@example.com", PASSWORD)
    response = await api_client.get("/auth/me", headers=user.auth)
    assert "password" not in response.text.lower()
    assert "argon2" not in response.text.lower()


# ---------------------------------------------------------------------------
# Rotation and family revocation
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("clean_db")
async def test_refresh_rotates_the_token(api_client: AsyncClient) -> None:
    user = await register(api_client, "rotate@example.com", PASSWORD)

    response = await api_client.post(
        "/auth/refresh-token", json={"refresh_token": user.refresh_token}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["refresh_token"] != user.refresh_token
    assert body["access_token"]


@pytest.mark.usefixtures("clean_db")
async def test_reusing_a_consumed_token_revokes_the_whole_family(
    api_client: AsyncClient,
) -> None:
    """The core of the rotation scheme (BUILD_SPEC §4 P1).

    A replayed token means it leaked — the honest client would have rotated
    past it — so every descendant of that login is revoked, not just the
    replayed one.
    """
    user = await register(api_client, "reuse@example.com", PASSWORD)

    first = await api_client.post("/auth/refresh-token", json={"refresh_token": user.refresh_token})
    assert first.status_code == 200
    rotated = first.json()["refresh_token"]

    # An attacker replays the original, already-consumed token.
    replay = await api_client.post(
        "/auth/refresh-token", json={"refresh_token": user.refresh_token}
    )
    assert replay.status_code == 401

    # The legitimate client's current token is now dead too.
    legitimate = await api_client.post("/auth/refresh-token", json={"refresh_token": rotated})
    assert legitimate.status_code == 401


@pytest.mark.usefixtures("clean_db")
async def test_rotation_chain_survives_several_hops(api_client: AsyncClient) -> None:
    user = await register(api_client, "chain@example.com", PASSWORD)

    token = user.refresh_token
    for _ in range(5):
        response = await api_client.post("/auth/refresh-token", json={"refresh_token": token})
        assert response.status_code == 200
        token = response.json()["refresh_token"]


@pytest.mark.usefixtures("clean_db")
async def test_unknown_refresh_token_is_rejected(api_client: AsyncClient) -> None:
    response = await api_client.post(
        "/auth/refresh-token", json={"refresh_token": "not-a-real-token"}
    )
    assert response.status_code == 401


@pytest.mark.usefixtures("clean_db")
async def test_logout_revokes_the_family(api_client: AsyncClient) -> None:
    user = await register(api_client, "logout@example.com", PASSWORD)

    logout = await api_client.post("/auth/logout", json={"refresh_token": user.refresh_token})
    assert logout.status_code == 204

    refused = await api_client.post(
        "/auth/refresh-token", json={"refresh_token": user.refresh_token}
    )
    assert refused.status_code == 401


@pytest.mark.usefixtures("clean_db")
async def test_logout_with_an_unknown_token_still_succeeds(
    api_client: AsyncClient,
) -> None:
    """Logout must not double as an oracle for which tokens exist."""
    response = await api_client.post("/auth/logout", json={"refresh_token": "never-issued"})
    assert response.status_code == 204


@pytest.mark.usefixtures("clean_db")
async def test_separate_logins_have_independent_families(
    api_client: AsyncClient,
) -> None:
    """Revoking one device's session must not sign the other one out."""
    await register(api_client, "devices@example.com", PASSWORD)

    laptop = await api_client.post(
        "/auth/login", json={"email": "devices@example.com", "password": PASSWORD}
    )
    phone = await api_client.post(
        "/auth/login", json={"email": "devices@example.com", "password": PASSWORD}
    )
    laptop_refresh = laptop.json()["refresh_token"]
    phone_refresh = phone.json()["refresh_token"]

    await api_client.post("/auth/logout", json={"refresh_token": laptop_refresh})

    still_valid = await api_client.post(
        "/auth/refresh-token", json={"refresh_token": phone_refresh}
    )
    assert still_valid.status_code == 200
