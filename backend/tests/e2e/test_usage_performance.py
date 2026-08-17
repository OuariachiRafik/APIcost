"""P3 acceptance criterion 2.

    "Usage endpoints respond in <500 ms p95 against 1M seeded ledger rows."

Run against whatever is already in ``requests_log``, and skip when the table is
too small to make the measurement mean anything. Seed it first:

    make seed rows=1000000

Marked ``perf`` and excluded from the default run, for a specific reason: the
functional tests truncate ``requests_log``, so a benchmark sharing a run with
them measures an empty table. Keeping it in the default suite would mean it
either skipped silently or reported a performance guarantee nobody checked.

    make bench        # seeds if needed, then measures
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from apicost.config import get_settings
from apicost.core.security import issue_access_token
from apicost.db.redis import close_redis
from apicost.db.session import dispose_engine, get_admin_engine
from apicost.metrics.latency import percentile

pytestmark = [pytest.mark.integration, pytest.mark.perf]

MIN_ROWS = 500_000
P95_BUDGET_MS = 500.0
SAMPLES = 12


async def _row_count() -> int:
    async with get_admin_engine().connect() as conn:
        result = await conn.execute(text("SELECT count(*) FROM requests_log"))
        return int(result.scalar() or 0)


async def _demo_user_id() -> str | None:
    async with get_admin_engine().connect() as conn:
        result = await conn.execute(
            text("SELECT user_id FROM requests_log LIMIT 1"),
        )
        value = result.scalar()
        return str(value) if value else None


@pytest.fixture
async def seeded_user_auth() -> AsyncIterator[dict[str, str]]:
    """A token for whichever user already owns the seeded rows.

    Minted directly rather than by registering a user and re-pointing the rows
    at them: that `UPDATE` rewrote 841k rows, and the resulting dead tuples
    meant the benchmark was largely measuring table bloat it had just created.
    """
    # Dispose before the first query, not only after the last one. Engines are
    # cached process-wide and asyncpg connections are bound to the loop that
    # opened them, so an earlier test file in the same session leaves one tied
    # to a loop that is now closed. `make bench` runs the latency harness first
    # and every test here then errored at setup with "attached to a different
    # loop" — before even reaching the skip that says the ledger is too small.
    await dispose_engine()

    count = await _row_count()
    if count < MIN_ROWS:
        pytest.skip(
            f"requests_log has {count:,} rows; need {MIN_ROWS:,}. Run: make seed rows=1000000"
        )

    user_id = await _demo_user_id()
    assert user_id is not None

    # `requests_log.user_id` carries no foreign key — the ledger is written by
    # a worker that must never block on a join. So seeded rows outlive a
    # truncated `users` table, and the owner may need recreating.
    async with get_admin_engine().begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash) "
                "VALUES (:id, :email, :hash) ON CONFLICT (id) DO NOTHING"
            ),
            {"id": user_id, "email": f"perf-{user_id}@example.com", "hash": "x"},
        )

    token = issue_access_token(user_id, get_settings().jwt_secret.get_secret_value())
    try:
        yield {"Authorization": f"Bearer {token}"}
    finally:
        # Engines are cached process-wide but asyncpg connections are bound to
        # the loop that opened them, and each test gets a fresh loop.
        await dispose_engine()
        await close_redis()


async def _measure(client: AsyncClient, url: str, auth: dict[str, str]) -> list[float]:
    durations: list[float] = []
    for index in range(SAMPLES + 2):
        started = time.perf_counter()
        response = await client.get(url, headers=auth)
        elapsed = (time.perf_counter() - started) * 1000.0
        assert response.status_code == 200, f"{url} -> {response.status_code}"
        if index >= 2:  # discard cold-cache warmups
            durations.append(elapsed)
    return durations


@pytest.mark.parametrize(
    "url",
    [
        "/usage?range=30d",
        "/usage?range=90d",
        "/usage/breakdown?by=model",
        "/usage/breakdown?by=endpoint",
        "/usage/token-distribution",
        "/requests?limit=50",
        "/requests?limit=50&decision=cache_hit",
    ],
)
async def test_usage_endpoint_p95_under_budget(
    api_client: AsyncClient, seeded_user_auth: dict[str, str], url: str
) -> None:
    count = await _row_count()
    durations = await _measure(api_client, url, seeded_user_auth)
    p95 = percentile(durations, 95)

    print(f"\n  {url:<44} p95 {p95:7.1f} ms  ({count:,} rows)")

    assert p95 < P95_BUDGET_MS, (
        f"{url} p95 is {p95:.1f} ms against {count:,} rows, over the {P95_BUDGET_MS} ms NFR"
    )


async def test_deep_pagination_does_not_degrade(
    api_client: AsyncClient, seeded_user_auth: dict[str, str]
) -> None:
    """Keyset pagination must cost the same on page 40 as on page 1.

    This is the property offset pagination cannot provide, and the reason the
    request log uses a cursor.
    """
    first_page_times: list[float] = []
    deep_page_times: list[float] = []

    cursor: str | None = None
    for page_index in range(40):
        url = f"/requests?limit=50{f'&cursor={cursor}' if cursor else ''}"
        started = time.perf_counter()
        response = await api_client.get(url, headers=seeded_user_auth)
        elapsed = (time.perf_counter() - started) * 1000.0
        assert response.status_code == 200

        (first_page_times if page_index < 3 else deep_page_times).append(elapsed)

        cursor = response.json()["next_cursor"]
        if cursor is None:
            break

    if not deep_page_times:
        pytest.skip("not enough rows to reach a deep page")

    shallow = percentile(first_page_times, 95)
    deep = percentile(deep_page_times, 95)
    print(f"\n  page 1-3 p95 {shallow:.1f} ms   page 4+ p95 {deep:.1f} ms")

    assert deep < P95_BUDGET_MS
    assert deep < shallow * 4 + 50, (
        f"deep pages ({deep:.1f} ms) are much slower than early ones "
        f"({shallow:.1f} ms) — pagination is not staying O(1)"
    )
