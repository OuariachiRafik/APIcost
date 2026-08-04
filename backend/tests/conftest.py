"""Shared test fixtures.

Two rules shape this file:

* ``make test`` must pass with no Docker running (P0 acceptance criterion 3).
  Anything needing live Postgres or Redis is marked ``integration`` and skipped
  automatically when the service is unreachable.
* Settings are cached process-wide, so any test touching the environment has to
  clear that cache on both sides of itself.
"""

from __future__ import annotations

import socket
from collections.abc import AsyncIterator, Iterator
from urllib.parse import urlparse

import pytest
from httpx import ASGITransport, AsyncClient

from apicost.config import Settings, get_settings
from apicost.core.logging import clear_request_id


@pytest.fixture(autouse=True)
def _reset_process_state() -> Iterator[None]:
    """Keep cached settings and bound context out of each other's tests."""
    get_settings.cache_clear()
    clear_request_id()
    yield
    get_settings.cache_clear()
    clear_request_id()


@pytest.fixture
def settings() -> Settings:
    return get_settings()


@pytest.fixture
async def proxy_client() -> AsyncIterator[AsyncClient]:
    """HTTP client bound to the data-plane app."""
    from apicost.main_proxy import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://proxy.test"
    ) as client:
        yield client


@pytest.fixture
async def api_client() -> AsyncIterator[AsyncClient]:
    """HTTP client bound to the control-plane app."""
    from apicost.main_api import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://api.test") as client:
        yield client


# ---------------------------------------------------------------------------
# Integration gating
# ---------------------------------------------------------------------------


def _port_open(host: str, port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _reachable(url: str, default_port: int) -> bool:
    parsed = urlparse(url)
    return _port_open(parsed.hostname or "localhost", parsed.port or default_port)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip ``integration`` tests when their dependencies are not up."""
    settings = Settings()
    postgres_up = _reachable(settings.database_url, 5432)
    redis_up = _reachable(settings.redis_url, 6379)
    if postgres_up and redis_up:
        return

    missing = ", ".join(
        name for name, up in (("postgres", postgres_up), ("redis", redis_up)) if not up
    )
    skip = pytest.mark.skip(reason=f"integration dependencies unreachable: {missing}")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)
