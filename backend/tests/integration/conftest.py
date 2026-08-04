"""Fixtures for tests that need live Postgres and Redis.

Every test here starts from an empty database. Truncating ``users`` cascades
through every P1 table, which is both faster than recreating the schema and a
standing check that the foreign keys are actually wired up.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from apicost.config import get_settings
from apicost.db.redis import close_redis, get_redis
from apicost.db.session import dispose_engine, get_engine

pytestmark = pytest.mark.integration


def admin_engine() -> AsyncEngine:
    """An engine connected as the schema owner, bypassing RLS.

    For tests that need to inspect rows *as stored* — checking that ciphertext
    on disk contains no plaintext, for instance. The application never uses
    this connection; that is the entire point of the two-role split.
    """
    return create_async_engine(get_settings().database_admin_url)


@pytest.fixture
async def clean_db() -> AsyncIterator[None]:
    """Empty every table, before and after."""
    statement = text("TRUNCATE users, refresh_tokens, provider_keys, projects, proxy_keys CASCADE")
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.execute(statement)
    try:
        yield
    finally:
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.execute(statement)
        await dispose_engine()
        await close_redis()


@pytest.fixture
async def clean_redis() -> AsyncIterator[None]:
    redis = get_redis()
    await redis.flushdb()
    yield


class ApiUser:
    """A registered account plus the tokens needed to act as it."""

    def __init__(self, client: AsyncClient, email: str, access: str, refresh: str) -> None:
        self.client = client
        self.email = email
        self.access_token = access
        self.refresh_token = refresh

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    async def post(self, url: str, **kwargs: object) -> object:
        return await self.client.post(url, headers=self.auth, **kwargs)  # type: ignore[arg-type]


async def register(
    client: AsyncClient, email: str, password: str = "a-very-long-password"
) -> ApiUser:
    """Sign a user up and return an authenticated handle."""
    response = await client.post("/auth/signup", json={"email": email, "password": password})
    assert response.status_code == 201, response.text
    body = response.json()
    return ApiUser(client, email, body["access_token"], body["refresh_token"])
