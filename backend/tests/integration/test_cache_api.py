"""Cache reporting and invalidation — UC-23, UC-25."""

from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import AsyncClient

from tests.integration.test_usage_api import insert_rows, setup_user

pytestmark = pytest.mark.integration


@pytest.mark.usefixtures("clean_db")
async def test_cache_stats_reports_hit_rate_and_savings(api_client: AsyncClient) -> None:
    auth, user_id, project_id = await setup_user(api_client, "cachestats@example.com")
    await insert_rows(
        user_id,
        project_id,
        [
            {
                "cache_hit": True,
                "cost_usd": Decimal("0"),
                "cost_would_have_been_usd": Decimal("3.00"),
            },
            {
                "cache_hit": True,
                "cost_usd": Decimal("0"),
                "cost_would_have_been_usd": Decimal("1.00"),
            },
            {"cost_usd": Decimal("2.00"), "cost_would_have_been_usd": Decimal("2.00")},
            {"cost_usd": Decimal("2.00"), "cost_would_have_been_usd": Decimal("2.00")},
        ],
    )

    body = (await api_client.get("/cache/stats", headers=auth)).json()

    assert body["hits"] == 2
    assert body["requests"] == 4
    assert body["hit_rate"] == pytest.approx(0.5)
    # Caching saved the whole avoided call, because it never happened.
    assert Decimal(body["savings_usd"]) == Decimal("4.00")


@pytest.mark.usefixtures("clean_db")
async def test_cache_stats_excludes_routing_savings(api_client: AsyncClient) -> None:
    """The two mechanisms are reported separately and never conflated (§6)."""
    auth, user_id, project_id = await setup_user(api_client, "cachesep@example.com")
    await insert_rows(
        user_id,
        project_id,
        [
            {
                "cache_hit": True,
                "cost_usd": Decimal("0"),
                "cost_would_have_been_usd": Decimal("5.00"),
            },
            {
                "routed": True,
                "model_used": "gpt-4o-mini",
                "cost_usd": Decimal("1.00"),
                "cost_would_have_been_usd": Decimal("4.00"),
            },
        ],
    )

    body = (await api_client.get("/cache/stats", headers=auth)).json()

    assert Decimal(body["savings_usd"]) == Decimal("5.00"), (
        "routing savings leaked into the cache report"
    )


@pytest.mark.usefixtures("clean_db")
async def test_cache_stats_is_scoped_to_the_caller(api_client: AsyncClient) -> None:
    _auth_a, user_a, project_a = await setup_user(api_client, "cs-a@example.com")
    auth_b, user_b, project_b = await setup_user(api_client, "cs-b@example.com")

    await insert_rows(
        user_a,
        project_a,
        [{"cache_hit": True, "cost_would_have_been_usd": Decimal("99.00")}],
    )
    await insert_rows(user_b, project_b, [{"cost_usd": Decimal("1.00")}])

    body = (await api_client.get("/cache/stats", headers=auth_b)).json()
    assert body["hits"] == 0
    assert Decimal(body["savings_usd"]) == Decimal("0")


@pytest.mark.usefixtures("clean_db")
async def test_invalidate_requires_ownership(api_client: AsyncClient) -> None:
    _auth_a, _user_a, project_a = await setup_user(api_client, "inv-a@example.com")
    auth_b, _user_b, _project_b = await setup_user(api_client, "inv-b@example.com")

    response = await api_client.post(
        "/cache/invalidate", headers=auth_b, json={"project_id": project_a}
    )
    assert response.status_code == 404


@pytest.mark.usefixtures("clean_db")
async def test_invalidate_reports_what_it_removed(api_client: AsyncClient) -> None:
    auth, _user_id, project_id = await setup_user(api_client, "inv@example.com")

    response = await api_client.post(
        "/cache/invalidate", headers=auth, json={"project_id": project_id}
    )

    assert response.status_code == 200
    assert response.json() == {"project_id": project_id, "entries_removed": 0}


@pytest.mark.usefixtures("clean_db")
async def test_cache_endpoints_require_authentication(api_client: AsyncClient) -> None:
    assert (await api_client.get("/cache/stats")).status_code == 401
    assert (await api_client.post("/cache/invalidate", json={"project_id": "x"})).status_code == 401
