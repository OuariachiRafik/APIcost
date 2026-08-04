"""Readiness against real Postgres and Redis.

Skipped automatically when the services are not up — see
``pytest_collection_modifyitems`` in ``tests/conftest.py``. Run them with
``make dev`` in another terminal.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from apicost.db.redis import check_redis, close_redis
from apicost.db.session import check_postgres, dispose_engine

pytestmark = pytest.mark.integration


async def test_postgres_probe_succeeds() -> None:
    try:
        assert await check_postgres() is True
    finally:
        await dispose_engine()


async def test_redis_probe_succeeds() -> None:
    try:
        assert await check_redis() is True
    finally:
        await close_redis()


async def test_proxy_readyz_against_live_dependencies(
    proxy_client: AsyncClient,
) -> None:
    try:
        response = await proxy_client.get("/readyz")
        assert response.status_code == 200
        assert response.json()["checks"] == {"postgres": True, "redis": True}
    finally:
        await dispose_engine()
        await close_redis()


async def test_api_readyz_against_live_dependencies(api_client: AsyncClient) -> None:
    try:
        response = await api_client.get("/readyz")
        assert response.status_code == 200
    finally:
        await dispose_engine()
        await close_redis()


async def test_pgvector_extension_is_installed() -> None:
    """Migration 0001 must have run; the semantic cache depends on it (§6.3)."""
    from sqlalchemy import text

    from apicost.db.session import get_engine

    try:
        engine = get_engine()
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'"))
            assert result.scalar() == 1
    finally:
        await dispose_engine()
