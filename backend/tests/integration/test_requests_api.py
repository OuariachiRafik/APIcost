"""The per-request decision log — UC-12.

BUILD_SPEC calls this the most important trust-building screen in the product,
so these tests are about whether a user can actually tell what happened to
their request, and whether the table stays fast as it grows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient

from apicost.api.routers.requests import decode_cursor, encode_cursor
from tests.integration.test_usage_api import insert_rows, setup_user

pytestmark = pytest.mark.integration


def test_cursor_round_trip() -> None:
    when = datetime(2026, 8, 5, 12, 30, tzinfo=UTC)
    decoded_time, decoded_id = decode_cursor(encode_cursor(when, "01JABC"))
    assert decoded_time == when
    assert decoded_id == "01JABC"


@pytest.mark.usefixtures("clean_db")
async def test_decision_is_derived_for_every_row(api_client: AsyncClient) -> None:
    """The column a user reads first: what did you actually do with my call?"""
    auth, user_id, project_id = await setup_user(api_client, "decisions@example.com")
    await insert_rows(
        user_id,
        project_id,
        [
            {"cache_hit": True},
            {"routed": True, "model_used": "gpt-4o-mini"},
            {},  # passthrough
            {"status": 429, "error_code": "rate_limit"},
            {"routed": True, "escalation_triggered": True},
        ],
    )

    rows = (await api_client.get("/requests", headers=auth)).json()["rows"]
    decisions = {row["decision"] for row in rows}

    assert decisions == {"cache_hit", "routed", "passthrough", "error", "escalated"}


@pytest.mark.usefixtures("clean_db")
async def test_a_cache_hit_outranks_a_routing_flag(api_client: AsyncClient) -> None:
    """Precedence matters — the provider was never called, so it is a cache hit."""
    auth, user_id, project_id = await setup_user(api_client, "precedence@example.com")
    await insert_rows(user_id, project_id, [{"cache_hit": True, "routed": True}])

    rows = (await api_client.get("/requests", headers=auth)).json()["rows"]
    assert rows[0]["decision"] == "cache_hit"


@pytest.mark.usefixtures("clean_db")
async def test_rows_carry_what_was_asked_for_versus_what_was_used(
    api_client: AsyncClient,
) -> None:
    auth, user_id, project_id = await setup_user(api_client, "models@example.com")
    await insert_rows(
        user_id,
        project_id,
        [
            {
                "model_requested": "gpt-4o",
                "model_used": "gpt-4o-mini",
                "routed": True,
                "cost_usd": Decimal("0.10"),
                "cost_would_have_been_usd": Decimal("1.00"),
                "routing_reason_code": "CLASSIFIER_CHEAP_TIER",
            }
        ],
    )

    row = (await api_client.get("/requests", headers=auth)).json()["rows"][0]

    assert row["model_requested"] == "gpt-4o"
    assert row["model_used"] == "gpt-4o-mini"
    assert Decimal(row["saved_usd"]) == Decimal("0.90")
    assert row["routing_reason_code"] == "CLASSIFIER_CHEAP_TIER"


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("clean_db")
async def test_keyset_pagination_walks_every_row_exactly_once(
    api_client: AsyncClient,
) -> None:
    """No duplicates and no gaps, which offset pagination cannot promise."""
    auth, user_id, project_id = await setup_user(api_client, "paging@example.com")

    base = datetime.now(UTC)
    await insert_rows(
        user_id,
        project_id,
        [{"timestamp": base - timedelta(seconds=index)} for index in range(55)],
    )

    seen: list[str] = []
    cursor: str | None = None

    for _ in range(10):
        url = f"/requests?limit=10{f'&cursor={cursor}' if cursor else ''}"
        page = (await api_client.get(url, headers=auth)).json()
        seen.extend(row["id"] for row in page["rows"])
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert len(seen) == 55
    assert len(set(seen)) == 55, "a row was returned twice"


@pytest.mark.usefixtures("clean_db")
async def test_pagination_is_stable_when_timestamps_collide(
    api_client: AsyncClient,
) -> None:
    """Identical timestamps are common at speed; the id breaks the tie."""
    auth, user_id, project_id = await setup_user(api_client, "collide@example.com")

    same_moment = datetime.now(UTC)
    await insert_rows(user_id, project_id, [{"timestamp": same_moment} for _ in range(20)])

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(5):
        url = f"/requests?limit=5{f'&cursor={cursor}' if cursor else ''}"
        page = (await api_client.get(url, headers=auth)).json()
        seen.extend(row["id"] for row in page["rows"])
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert len(set(seen)) == 20


@pytest.mark.usefixtures("clean_db")
async def test_malformed_cursor_is_rejected(api_client: AsyncClient) -> None:
    auth, _, _ = await setup_user(api_client, "badcursor@example.com")
    response = await api_client.get("/requests?cursor=not-a-cursor", headers=auth)
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("clean_db")
async def test_filter_by_decision_and_model(api_client: AsyncClient) -> None:
    auth, user_id, project_id = await setup_user(api_client, "filters@example.com")
    await insert_rows(
        user_id,
        project_id,
        [
            {"cache_hit": True, "model_used": "gpt-4o"},
            {"model_used": "gpt-4o"},
            {"model_used": "gpt-4o-mini"},
            {"status": 500},
        ],
    )

    cached = (await api_client.get("/requests?decision=cache_hit", headers=auth)).json()
    assert len(cached["rows"]) == 1

    mini = (await api_client.get("/requests?model=gpt-4o-mini", headers=auth)).json()
    assert len(mini["rows"]) == 1

    errors = (await api_client.get("/requests?decision=error", headers=auth)).json()
    assert len(errors["rows"]) == 1
    assert errors["rows"][0]["status"] == 500


@pytest.mark.usefixtures("clean_db")
async def test_request_detail_by_request_id(api_client: AsyncClient) -> None:
    auth, user_id, project_id = await setup_user(api_client, "detail@example.com")
    await insert_rows(user_id, project_id, [{"model_used": "gpt-4o"}])

    listed = (await api_client.get("/requests", headers=auth)).json()["rows"][0]
    detail = await api_client.get(f"/requests/{listed['request_id']}", headers=auth)

    assert detail.status_code == 200
    assert detail.json()["request_id"] == listed["request_id"]


@pytest.mark.usefixtures("clean_db")
async def test_cannot_read_another_users_request(api_client: AsyncClient) -> None:
    auth_a, user_a, project_a = await setup_user(api_client, "req-a@example.com")
    auth_b, _, _ = await setup_user(api_client, "req-b@example.com")

    await insert_rows(user_a, project_a, [{"model_used": "gpt-4o"}])
    theirs = (await api_client.get("/requests", headers=auth_a)).json()["rows"][0]

    assert (await api_client.get("/requests", headers=auth_b)).json()["rows"] == []
    assert (
        await api_client.get(f"/requests/{theirs['request_id']}", headers=auth_b)
    ).status_code == 404


@pytest.mark.usefixtures("clean_db")
async def test_page_size_is_capped(api_client: AsyncClient) -> None:
    auth, _, _ = await setup_user(api_client, "cap@example.com")
    assert (await api_client.get("/requests?limit=5000", headers=auth)).status_code == 422
