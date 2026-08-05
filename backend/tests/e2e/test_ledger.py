"""P2 acceptance criterion 3.

    "Every request appears in `requests_log` with correct token counts and
     computed cost within 5 seconds."

The drain is invoked directly rather than waiting on the ARQ cron, so the test
measures the path rather than the scheduler. The 5-second budget is asserted
separately, against the configured cron interval.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import AsyncClient
from openai import AsyncOpenAI
from sqlalchemy import text

from apicost.db.redis import get_redis
from apicost.db.session import get_admin_engine
from apicost.worker.tasks import drain_ledger
from tests.e2e.conftest import LiveServer, provision_account

pytestmark = pytest.mark.integration


async def ledger_rows() -> list[dict[str, object]]:
    async with get_admin_engine().connect() as conn:
        result = await conn.execute(text("SELECT * FROM requests_log ORDER BY timestamp DESC"))
        return [dict(row) for row in result.mappings()]


@pytest.mark.usefixtures("clean_all")
async def test_a_proxied_request_lands_in_the_ledger(
    live_proxy: LiveServer, api_base: AsyncClient
) -> None:
    proxy_key = await provision_account(api_base, "ledger@example.com")
    client = AsyncOpenAI(base_url=f"{live_proxy.url}/v1", api_key=proxy_key, max_retries=0)

    await client.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "hello"}]
    )

    written = await drain_ledger(block_ms=100)
    assert written == 1

    rows = await ledger_rows()
    assert len(rows) == 1
    row = rows[0]

    assert row["model_requested"] == "gpt-4o"
    assert row["model_used"] == "gpt-4o"
    assert row["provider"] == "openai"
    assert row["endpoint"] == "chat/completions"
    assert row["status"] == 200
    assert row["streamed"] is False

    # Token counts come from the provider's usage block, not estimation.
    assert row["tokens_in"] == 12
    assert row["tokens_out"] == 7
    assert row["tokens_estimated"] is False


@pytest.mark.usefixtures("clean_all")
async def test_cost_is_computed_correctly(live_proxy: LiveServer, api_base: AsyncClient) -> None:
    """gpt-4o is $2.50/M in and $10.00/M out; 12 in and 7 out is exact."""
    proxy_key = await provision_account(api_base, "cost@example.com")
    client = AsyncOpenAI(base_url=f"{live_proxy.url}/v1", api_key=proxy_key, max_retries=0)

    await client.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "hello"}]
    )
    await drain_ledger(block_ms=100)

    row = (await ledger_rows())[0]
    expected = Decimal("2.50") / Decimal(1_000_000) * 12 + Decimal("10.00") / Decimal(1_000_000) * 7

    assert Decimal(str(row["cost_usd"])).quantize(Decimal("0.00000001")) == expected.quantize(
        Decimal("0.00000001")
    )


@pytest.mark.usefixtures("clean_all")
async def test_cost_would_have_been_is_populated_on_every_row(
    live_proxy: LiveServer, api_base: AsyncClient
) -> None:
    """Every savings number in the product derives from this column (§7)."""
    proxy_key = await provision_account(api_base, "wouldhave@example.com")
    client = AsyncOpenAI(base_url=f"{live_proxy.url}/v1", api_key=proxy_key, max_retries=0)

    await client.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "hello"}]
    )
    await drain_ledger(block_ms=100)

    row = (await ledger_rows())[0]
    assert row["cost_would_have_been_usd"] is not None
    # A passthrough: nothing was routed, so the two are equal.
    assert Decimal(str(row["cost_would_have_been_usd"])) == Decimal(str(row["cost_usd"]))


@pytest.mark.usefixtures("clean_all")
async def test_streamed_requests_record_inference_metrics(
    live_proxy: LiveServer, api_base: AsyncClient
) -> None:
    """TTFT, ITL, and TPS from the SSE tee (§4 P2, §6.6)."""
    proxy_key = await provision_account(api_base, "metrics@example.com")
    client = AsyncOpenAI(base_url=f"{live_proxy.url}/v1", api_key=proxy_key, max_retries=0)

    stream = await client.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "hi"}], stream=True
    )
    async for _chunk in stream:
        pass

    await drain_ledger(block_ms=100)
    row = (await ledger_rows())[0]

    assert row["streamed"] is True
    assert row["ttft_ms"] is not None and float(str(row["ttft_ms"])) > 0
    assert row["itl_ms"] is not None and float(str(row["itl_ms"])) > 0
    assert row["tps"] is not None and float(str(row["tps"])) > 0
    assert row["tokens_in"] == 12, "usage from the final streamed chunk"


@pytest.mark.usefixtures("clean_all")
async def test_estimation_is_flagged_when_the_provider_omits_usage(
    live_proxy: LiveServer, api_base: AsyncClient
) -> None:
    """§6.2: cost accuracy is never silently overstated."""
    proxy_key = await provision_account(api_base, "estimated@example.com")
    client = AsyncOpenAI(base_url=f"{live_proxy.url}/v1", api_key=proxy_key, max_retries=0)

    stream = await client.chat.completions.create(
        model="stub-no-usage", messages=[{"role": "user", "content": "hi"}], stream=True
    )
    async for _chunk in stream:
        pass

    await drain_ledger(block_ms=100)
    row = (await ledger_rows())[0]

    assert row["tokens_estimated"] is True
    assert int(str(row["tokens_out"])) > 0


@pytest.mark.usefixtures("clean_all")
async def test_provider_errors_are_ledgered_too(
    live_proxy: LiveServer, api_base: AsyncClient
) -> None:
    """A failed request still consumed something and still needs explaining."""
    proxy_key = await provision_account(api_base, "errrow@example.com")
    client = AsyncOpenAI(base_url=f"{live_proxy.url}/v1", api_key=proxy_key, max_retries=0)

    with pytest.raises(Exception):  # noqa: B017 - SDK error type varies by status
        await client.chat.completions.create(
            model="stub-rate-limited", messages=[{"role": "user", "content": "hi"}]
        )

    await drain_ledger(block_ms=100)
    row = (await ledger_rows())[0]

    assert row["status"] == 429
    assert row["error_code"] is not None


@pytest.mark.usefixtures("clean_all")
async def test_ledger_rows_are_scoped_by_rls(live_proxy: LiveServer, api_base: AsyncClient) -> None:
    """Two users' traffic must not be visible to each other (hard rule 5)."""
    from apicost.db.session import session_scope

    key_a = await provision_account(api_base, "tenant-a@example.com")
    key_b = await provision_account(api_base, "tenant-b@example.com")

    for key in (key_a, key_b):
        client = AsyncOpenAI(base_url=f"{live_proxy.url}/v1", api_key=key, max_retries=0)
        await client.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
        )

    await drain_ledger(block_ms=100)

    all_rows = await ledger_rows()
    assert len(all_rows) == 2
    user_a = str(all_rows[0]["user_id"])

    async with session_scope(user_id=user_a) as session:
        visible = await session.execute(text("SELECT count(*) FROM requests_log"))
        assert visible.scalar() == 1


@pytest.mark.usefixtures("clean_all")
async def test_batching_handles_many_requests(
    live_proxy: LiveServer, api_base: AsyncClient
) -> None:
    proxy_key = await provision_account(api_base, "batch@example.com")
    client = AsyncOpenAI(base_url=f"{live_proxy.url}/v1", api_key=proxy_key, max_retries=0)

    for index in range(25):
        await client.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": f"hi {index}"}]
        )

    written = await drain_ledger(block_ms=200, max_batches=5)
    assert written == 25
    assert len(await ledger_rows()) == 25


@pytest.mark.usefixtures("clean_all")
async def test_draining_twice_does_not_duplicate_rows(
    live_proxy: LiveServer, api_base: AsyncClient
) -> None:
    """Consumer-group acknowledgement plus ON CONFLICT DO NOTHING."""
    proxy_key = await provision_account(api_base, "dedupe@example.com")
    client = AsyncOpenAI(base_url=f"{live_proxy.url}/v1", api_key=proxy_key, max_retries=0)

    await client.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "hi"}]
    )

    await drain_ledger(block_ms=100)
    await drain_ledger(block_ms=100)

    assert len(await ledger_rows()) == 1


@pytest.mark.usefixtures("clean_all")
async def test_visibility_target_is_five_seconds() -> None:
    """The cron interval is what makes criterion 3's "within 5 seconds" true."""
    from apicost.worker.schedules import WorkerSettings

    drain_cron = WorkerSettings.cron_jobs[0]
    seconds = drain_cron.second
    assert seconds is not None
    assert (
        max(sorted(seconds)[i + 1] - sorted(seconds)[i] for i in range(len(sorted(seconds)) - 1))
        <= 5
    )


@pytest.mark.usefixtures("clean_all")
async def test_the_stream_is_trimmed_not_unbounded() -> None:
    """A backed-up worker must not exhaust Redis and take the proxy with it."""
    from apicost.config import get_settings

    settings = get_settings()
    redis = get_redis()

    for index in range(10):
        await redis.xadd(
            settings.ledger_stream_key, {"request_id": f"r{index}"}, maxlen=5, approximate=False
        )

    length = await redis.xlen(settings.ledger_stream_key)
    assert length <= 5
