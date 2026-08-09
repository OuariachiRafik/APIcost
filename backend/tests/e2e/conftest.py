"""E2E fixtures: a live proxy on a real socket, pointed at a stub provider.

The proxy runs in-process on a real uvicorn server rather than through
ASGITransport, because the point of these tests is that an **unmodified**
``openai`` SDK works against it. That means real sockets, real chunked transfer
encoding, real SSE framing — the parts an in-process transport would paper
over.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator

import pytest
import uvicorn
from httpx import AsyncClient
from sqlalchemy import text

from apicost.config import get_settings
from apicost.db.redis import close_redis, get_redis
from apicost.db.session import dispose_engine, get_admin_engine
from tests.e2e.stub_provider import build_stub_provider, received_requests

pytestmark = pytest.mark.integration


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


class LiveServer:
    """A uvicorn server running in the test's own event loop."""

    def __init__(self, app: object, port: int) -> None:
        self.port = port
        self._config = uvicorn.Config(
            app,  # type: ignore[arg-type]
            host="127.0.0.1",
            port=port,
            log_level="warning",
            lifespan="on",
        )
        self._server = uvicorn.Server(self._config)
        self._task: asyncio.Task[None] | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self, timeout_seconds: float = 60.0) -> None:
        """Start the server and wait for it to accept connections.

        Generous timeout on purpose: the proxy's startup hook loads the
        embedding model and the routing classifier before it serves anything,
        which takes seconds on a cold process. That is the right tradeoff — the
        alternative is paying it on some unlucky user's first request — but it
        does mean a deployment's readiness probe needs a matching grace period.
        """
        self._task = asyncio.create_task(self._server.serve())
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            if self._server.started:
                return
            await asyncio.sleep(0.02)
        raise RuntimeError(f"server did not start within {timeout_seconds}s")

    async def stop(self) -> None:
        self._server.should_exit = True
        if self._task is not None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._task, timeout=5)


@pytest.fixture
async def stub_provider() -> AsyncIterator[LiveServer]:
    received_requests.clear()
    server = LiveServer(build_stub_provider(), _free_port())
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
async def live_proxy(
    stub_provider: LiveServer, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[LiveServer]:
    """The real proxy app, with every provider pointed at the stub."""
    monkeypatch.setenv("APICOST_PROVIDER_BASE_URL_OVERRIDE", stub_provider.url)
    get_settings.cache_clear()

    from apicost.main_proxy import app

    server = LiveServer(app, _free_port())
    await server.start()
    try:
        yield server
    finally:
        await server.stop()
        get_settings.cache_clear()


@pytest.fixture
async def api_base(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    """Control-plane client, for setting an account up before proxying."""
    from httpx import ASGITransport

    from apicost.main_api import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://api.test") as client:
        yield client


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
async def clean_all() -> AsyncIterator[None]:
    """Empty every table and the ledger stream."""
    await _wipe()
    await get_redis().flushdb()
    try:
        yield
    finally:
        await _wipe()
        await dispose_engine()
        await close_redis()


async def provision_account(api: AsyncClient, email: str) -> str:
    """Sign up, add a provider key, create a project, issue a proxy key.

    Returns the raw proxy key — the same four steps the onboarding wizard walks
    a real user through.
    """
    signup = await api.post(
        "/auth/signup", json={"email": email, "password": "a-very-long-password"}
    )
    assert signup.status_code == 201, signup.text
    auth = {"Authorization": f"Bearer {signup.json()['access_token']}"}

    key = await api.post(
        "/keys",
        headers=auth,
        json={"provider": "openai", "api_key": "sk-proj-StubProviderKey0123456789"},
    )
    assert key.status_code == 201, key.text

    project = await api.post("/projects", headers=auth, json={"name": "e2e"})
    assert project.status_code == 201, project.text

    proxy_key = await api.post(
        f"/projects/{project.json()['id']}/proxy-keys", headers=auth, json={}
    )
    assert proxy_key.status_code == 201, proxy_key.text

    raw_key: str = proxy_key.json()["key"]
    return raw_key
