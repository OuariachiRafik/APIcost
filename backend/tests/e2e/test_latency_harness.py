"""The NFR latency harness — BUILD_SPEC §5.

    "Proxy overhead, cache miss: <100 ms p95, excluding provider time.
     Load test with a stub provider, assert on decompose_latency output.
     Build the latency harness in P2, not at the end. It is what makes the
     rest of the NFR work honest."

Overhead is measured as a difference, not asserted from instrumentation we
wrote ourselves: the same request is timed against the stub provider directly
and then through the proxy. Whatever the proxy adds is the number that matters,
and it cannot be flattered by forgetting to instrument a stage.

The cache-hit target (<30 ms p95) belongs to P4 and joins this file there.
"""

from __future__ import annotations

import time

import pytest
from httpx import AsyncClient

from apicost.metrics.latency import decompose_latency, percentile
from tests.e2e.conftest import LiveServer, provision_account

pytestmark = pytest.mark.integration

SAMPLES = 40
WARMUP = 5

OVERHEAD_P95_BUDGET_MS = 100.0
"""BUILD_SPEC §5. Deliberately the published target, not a padded one."""


async def _time_requests(
    client: AsyncClient, url: str, headers: dict[str, str], count: int
) -> list[float]:
    durations: list[float] = []
    payload = {"model": "gpt-4o", "messages": [{"role": "user", "content": "benchmark"}]}

    for index in range(count + WARMUP):
        started = time.perf_counter()
        response = await client.post(url, headers=headers, json=payload)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        assert response.status_code == 200, response.text
        if index >= WARMUP:  # discard connection setup and JIT-ish warmup
            durations.append(elapsed_ms)

    return durations


@pytest.mark.usefixtures("clean_all")
async def test_proxy_overhead_on_a_cache_miss_is_under_budget(
    live_proxy: LiveServer, stub_provider: LiveServer, api_base: AsyncClient
) -> None:
    proxy_key = await provision_account(api_base, "latency@example.com")

    async with AsyncClient(timeout=30.0) as client:
        baseline = await _time_requests(
            client,
            f"{stub_provider.url}/chat/completions",
            {"Authorization": "Bearer sk-direct"},
            SAMPLES,
        )
        through_proxy = await _time_requests(
            client,
            f"{live_proxy.url}/v1/chat/completions",
            {"Authorization": f"Bearer {proxy_key}"},
            SAMPLES,
        )

    baseline_p95 = percentile(baseline, 95)
    proxy_p95 = percentile(through_proxy, 95)
    overhead_p95 = proxy_p95 - baseline_p95

    decomposition = decompose_latency(
        {"provider": baseline, "proxy_total": through_proxy},
        measured_total=through_proxy,
    )

    report = (
        f"\n  provider direct p95 : {baseline_p95:7.2f} ms"
        f"\n  through proxy   p95 : {proxy_p95:7.2f} ms"
        f"\n  APICost overhead p95: {overhead_p95:7.2f} ms  (budget {OVERHEAD_P95_BUDGET_MS} ms)"
        f"\n  bottleneck          : {decomposition.bottleneck}"
    )
    print(report)

    assert overhead_p95 < OVERHEAD_P95_BUDGET_MS, (
        f"proxy overhead p95 is {overhead_p95:.1f} ms, over the "
        f"{OVERHEAD_P95_BUDGET_MS} ms NFR{report}"
    )


@pytest.mark.usefixtures("clean_all")
async def test_streaming_time_to_first_token_is_not_delayed_by_the_tee(
    live_proxy: LiveServer, stub_provider: LiveServer, api_base: AsyncClient
) -> None:
    """The tee must not buffer (§4 P2).

    Buffering would show up here as a TTFT through the proxy close to the
    *total* stream duration rather than to the provider's own first byte.
    """
    proxy_key = await provision_account(api_base, "ttft@example.com")
    payload = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }

    async def first_byte_ms(url: str, headers: dict[str, str]) -> tuple[float, float]:
        async with AsyncClient(timeout=30.0) as client:
            started = time.perf_counter()
            first: float | None = None
            async with client.stream("POST", url, headers=headers, json=payload) as response:
                async for _chunk in response.aiter_bytes():
                    if first is None:
                        first = (time.perf_counter() - started) * 1000.0
            total = (time.perf_counter() - started) * 1000.0
            assert first is not None
            return first, total

    proxy_first, proxy_total = await first_byte_ms(
        f"{live_proxy.url}/v1/chat/completions", {"Authorization": f"Bearer {proxy_key}"}
    )

    # The stub sleeps 2 ms between words, so a buffered stream would put the
    # first byte at roughly the total duration instead of well before it.
    assert proxy_first < proxy_total * 0.8, (
        f"first byte at {proxy_first:.1f} ms of a {proxy_total:.1f} ms stream — "
        "the tee appears to be buffering"
    )


@pytest.mark.usefixtures("clean_all")
async def test_optimization_work_stays_inside_the_shared_budget(
    live_proxy: LiveServer, api_base: AsyncClient
) -> None:
    """The 150 ms ceiling holds even though P2 has no optimization steps yet.

    This asserts the budget is threaded through and measured, so P4 and P5 have
    a harness that already fails loudly if they overrun it.
    """
    from apicost.config import get_settings

    assert get_settings().optimization_budget_ms == 150

    proxy_key = await provision_account(api_base, "budget@example.com")

    async with AsyncClient(timeout=30.0) as client:
        durations = await _time_requests(
            client,
            f"{live_proxy.url}/v1/chat/completions",
            {"Authorization": f"Bearer {proxy_key}"},
            10,
        )

    assert percentile(durations, 95) < 1000.0
