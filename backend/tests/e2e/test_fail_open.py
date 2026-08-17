"""The fail-open suite — BUILD_SPEC §9.

    "one test per subsystem that injects a failure (raise, hang past deadline,
     dependency down) and asserts the completion still returns correctly. This
     suite is the product's reliability guarantee — do not let it rot."

P2 covers the subsystems that exist: the ledger, the auth cache, and the
optimization budget. Cache and routing tests join this file in P4 and P5, in
the same shape.

The one deliberate exception in the whole system is a ``hard_stop`` budget,
which fails *closed*. That arrives in P6 and belongs here too, asserting the
opposite.
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import AsyncClient
from openai import AsyncOpenAI

from apicost.core.deadline import Deadline, failopen
from apicost.ledger.writer import LedgerEvent, emit_ledger_event
from tests.e2e.conftest import LiveServer, provision_account
from tests.e2e.stub_provider import COMPLETION_TEXT

pytestmark = pytest.mark.integration


class BrokenRedis:
    """Every operation raises, as if Redis were unreachable."""

    def __getattr__(self, name: str) -> object:
        async def explode(*_args: object, **_kwargs: object) -> object:
            raise ConnectionError("redis is down")

        return explode


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


async def test_ledger_write_failure_is_swallowed() -> None:
    """Dropping a ledger event must never fail a request (§4 P2)."""
    event = LedgerEvent(
        request_id="01JTEST",
        user_id="u",
        project_id="p",
        timestamp=LedgerEvent.now(),
        endpoint="chat/completions",
        provider="openai",
        model_requested="gpt-4o",
        model_used="gpt-4o",
    )

    enqueued = await emit_ledger_event(BrokenRedis(), event)  # type: ignore[arg-type]

    assert enqueued is False  # reported, not raised


@pytest.mark.usefixtures("clean_all")
async def test_completion_still_returns_with_redis_down(
    live_proxy: LiveServer, api_base: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2 acceptance criterion 2, end to end.

        "Killing Redis mid-request degrades logging but the completion still
         returns (fail-open)."

    The account is provisioned and one request made *before* Redis is broken,
    so the auth cache is warm — which is exactly the real failure mode: Redis
    dies while traffic is flowing.
    """
    proxy_key = await provision_account(api_base, "redisdown@example.com")
    client = AsyncOpenAI(base_url=f"{live_proxy.url}/v1", api_key=proxy_key, max_retries=0)

    warm = await client.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "before"}]
    )
    assert warm.choices[0].message.content == COMPLETION_TEXT

    # Now Redis goes away, mid-flight.
    import apicost.db.redis as redis_module

    monkeypatch.setattr(redis_module, "get_redis", lambda *a, **k: BrokenRedis())

    response = await client.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "after"}]
    )

    # The user's application is unaffected.
    assert response.choices[0].message.content == COMPLETION_TEXT
    assert response.usage is not None


@pytest.mark.usefixtures("clean_all")
async def test_streaming_still_works_with_redis_down(
    live_proxy: LiveServer, api_base: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same guarantee on the streaming path, where the ledger write happens
    in a `finally` after the last byte reaches the client."""
    proxy_key = await provision_account(api_base, "redisstream@example.com")
    client = AsyncOpenAI(base_url=f"{live_proxy.url}/v1", api_key=proxy_key, max_retries=0)

    await client.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "warm"}]
    )

    import apicost.db.redis as redis_module

    monkeypatch.setattr(redis_module, "get_redis", lambda *a, **k: BrokenRedis())

    stream = await client.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "hi"}], stream=True
    )

    pieces = [
        chunk.choices[0].delta.content
        async for chunk in stream
        if chunk.choices and chunk.choices[0].delta.content
    ]

    assert "".join(pieces) == COMPLETION_TEXT


# ---------------------------------------------------------------------------
# Auth cache
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("clean_all")
async def test_auth_falls_back_to_postgres_when_the_cache_is_unreadable(
    live_proxy: LiveServer, api_base: AsyncClient
) -> None:
    """A cold cache is a slower request, not a failed one.

    Note this is *not* fail-open in the usual sense: authentication degrades to
    a database read, it never degrades to letting the request through.
    """
    from apicost.proxy.auth import resolve_proxy_key

    proxy_key = await provision_account(api_base, "authfallback@example.com")

    resolved = await resolve_proxy_key(BrokenRedis(), proxy_key)  # type: ignore[arg-type]

    assert resolved.user_id
    assert resolved.project_name == "e2e"


@pytest.mark.usefixtures("clean_all")
async def test_authentication_does_not_fail_open(
    live_proxy: LiveServer, api_base: AsyncClient
) -> None:
    """The boundary of the fail-open rule.

    Optimizations fail open. Identity never does — an unknown key is refused
    even when every cache and optimization is unavailable.
    """
    async with AsyncClient() as raw:
        response = await raw.post(
            f"{live_proxy.url}/v1/chat/completions",
            headers={"Authorization": "Bearer apc_live_neverIssuedThisKey000000000"},
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("clean_all")
async def test_a_broken_context_advisory_still_serves_the_request(
    live_proxy: LiveServer, api_base: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§9 asks for one of these per subsystem, and P7 added one without a test.

    The advisory is the least important thing the pipeline does. If it raises,
    the user should get their completion and lose only the suggestion.
    """
    key = await provision_account(api_base, "ctxfailopen@example.com")

    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("advisory is broken")

    monkeypatch.setattr("apicost.proxy.pipeline.analyse_context", explode)

    async with AsyncClient(timeout=30.0) as raw:
        response = await raw.post(
            f"{live_proxy.url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "still works?"}],
            },
        )

    assert response.status_code == 200, response.text
    assert response.json()["choices"], "the completion did not come back"
    assert "x-apicost-context-warning" not in response.headers


async def test_a_hanging_subsystem_is_cut_off_at_the_budget() -> None:
    """A step that hangs past the deadline must not hang the request."""
    deadline = Deadline(budget_ms=40.0)
    started = asyncio.get_running_loop().time()

    async with failopen("cache", deadline) as guard:
        await asyncio.sleep(10.0)  # a wedged dependency

    elapsed_ms = (asyncio.get_running_loop().time() - started) * 1000.0

    assert guard.failed
    assert guard.reason == "timeout"
    assert elapsed_ms < 1000, f"took {elapsed_ms:.0f} ms against a 40 ms budget"


async def test_a_raising_subsystem_does_not_propagate() -> None:
    deadline = Deadline(budget_ms=150.0)

    async with failopen("routing", deadline) as guard:
        raise ValueError("classifier artifact is corrupt")

    assert guard.failed
    assert guard.reason == "ValueError"


async def test_the_total_budget_is_not_multiplied_by_step_count() -> None:
    """Three steps that each hang cannot spend three budgets."""
    deadline = Deadline(budget_ms=60.0)
    started = asyncio.get_running_loop().time()

    for subsystem in ("cache", "routing", "stats"):
        async with failopen(subsystem, deadline):
            await asyncio.sleep(5.0)

    elapsed_ms = (asyncio.get_running_loop().time() - started) * 1000.0
    assert elapsed_ms < 1000, (
        f"three hanging steps took {elapsed_ms:.0f} ms; the shared budget is 60 ms"
    )
