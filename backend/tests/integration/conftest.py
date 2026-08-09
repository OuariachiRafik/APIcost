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
from apicost.db.session import dispose_engine, get_admin_engine

pytestmark = pytest.mark.integration


def admin_engine() -> AsyncEngine:
    """An engine connected as the schema owner, bypassing RLS.

    For tests that need to inspect rows *as stored* — checking that ciphertext
    on disk contains no plaintext, for instance. The application never uses
    this connection; that is the entire point of the two-role split.
    """
    return create_async_engine(get_settings().database_admin_url)


_CLEANUP_TABLES = (
    # Children before parents, so foreign keys never block a delete.
    "requests_log",
    # Not user-scoped, but its primary key is Stripe's event id and that is the
    # idempotency mechanism — leaving rows behind makes a webhook test pass in
    # isolation and fail in a suite that ran it before.
    "billing_events",
    "cache_entries",
    "usage_rollup_daily",
    "token_bucket_rollup_daily",
    "routing_rules",
    "proxy_keys",
    "projects",
    "provider_keys",
    "refresh_tokens",
    "users",
)


async def _wipe() -> None:
    """Empty every table.

    DELETE rather than TRUNCATE: TRUNCATE does DDL-level work on every
    partition and index of `requests_log` — measured at 2.9 s against 75 ms for
    the equivalent DELETEs, and this runs twice per test.

    It has to go through the **admin** engine, though, and that difference is
    easy to miss: TRUNCATE is not subject to row-level security, but DELETE is.
    On the application role, with no `app.user_id` set, every one of these would
    match zero rows and silently clean nothing.
    """
    async with get_admin_engine().begin() as conn:
        for table in _CLEANUP_TABLES:
            await conn.execute(text(f"DELETE FROM {table}"))


@pytest.fixture
async def clean_db() -> AsyncIterator[None]:
    """Empty every table, before and after."""
    await _wipe()
    try:
        yield
    finally:
        await _wipe()
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
