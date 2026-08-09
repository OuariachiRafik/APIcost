"""P8 acceptance — UC-35, UC-36, UC-37.

The nightly job is driven directly rather than waiting for the cron, but it is
the same function the cron calls, reading the same ledger.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from apicost.advisor.nightly import generate_recommendations
from apicost.db.session import get_admin_engine
from tests.e2e.conftest import provision_account

pytestmark = pytest.mark.integration


async def login(api: AsyncClient, email: str) -> tuple[dict[str, str], str]:
    response = await api.post(
        "/auth/login", json={"email": email, "password": "a-very-long-password"}
    )
    auth = {"Authorization": f"Bearer {response.json()['access_token']}"}
    project_id = (await api.get("/projects", headers=auth)).json()[0]["id"]
    return auth, project_id


async def _user_id(project_id: str) -> str:
    async with get_admin_engine().connect() as conn:
        row = (
            await conn.execute(
                text("SELECT user_id FROM projects WHERE id = :id"), {"id": project_id}
            )
        ).one()
        return str(row.user_id)


async def seed_ledger(
    project_id: str,
    user_id: str,
    *,
    routed: int,
    passthrough: int,
    escalated: int = 0,
    cheap_cost: float = 0.001,
    full_cost: float = 0.010,
    tokens_each: int = 1000,
    endpoint: str = "/v1/chat/completions",
) -> None:
    """Write ledger rows describing a history the advisor can reason about."""
    now = datetime.now(UTC)
    rows: list[dict[str, Any]] = []

    for index in range(routed):
        rows.append(
            {
                "id": f"routed-{endpoint}-{index}",
                "timestamp": now - timedelta(hours=index % 240),
                "user_id": user_id,
                "project_id": project_id,
                "request_id": f"routed-{endpoint}-{index}",
                "endpoint": endpoint,
                "provider": "openai",
                "model_requested": "gpt-4o",
                "model_used": "gpt-4o-mini",
                "tokens_in": tokens_each,
                "tokens_out": tokens_each,
                "cost_usd": cheap_cost,
                "cost_would_have_been_usd": full_cost,
                "cache_hit": False,
                "routed": True,
                "escalation_triggered": index < escalated,
                "status": 200,
            }
        )

    for index in range(passthrough):
        rows.append(
            {
                "id": f"pass-{endpoint}-{index}",
                "timestamp": now - timedelta(hours=index % 240),
                "user_id": user_id,
                "project_id": project_id,
                "request_id": f"pass-{endpoint}-{index}",
                "endpoint": endpoint,
                "provider": "openai",
                "model_requested": "gpt-4o",
                "model_used": "gpt-4o",
                "tokens_in": tokens_each,
                "tokens_out": tokens_each,
                "cost_usd": full_cost,
                "cost_would_have_been_usd": full_cost,
                "cache_hit": False,
                "routed": False,
                "escalation_triggered": False,
                "status": 200,
            }
        )

    async with get_admin_engine().begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO requests_log (id, timestamp, user_id, project_id, request_id, "
                "endpoint, provider, model_requested, model_used, tokens_in, tokens_out, "
                "cost_usd, cost_would_have_been_usd, cache_hit, routed, escalation_triggered, "
                "status) VALUES (:id, :timestamp, :user_id, :project_id, :request_id, "
                ":endpoint, :provider, :model_requested, :model_used, :tokens_in, :tokens_out, "
                ":cost_usd, :cost_would_have_been_usd, :cache_hit, :routed, "
                ":escalation_triggered, :status)"
            ),
            rows,
        )


# -- UC-35, UC-37 -----------------------------------------------------------


@pytest.mark.usefixtures("clean_all")
async def test_a_demonstrated_downgrade_is_recommended_with_a_dollar_figure(
    api_base: AsyncClient,
) -> None:
    await provision_account(api_base, "advisor@example.com")
    auth, project_id = await login(api_base, "advisor@example.com")
    user_id = await _user_id(project_id)

    # 300 requests already ran cheap without a single escalation; 700 are
    # still paying full price.
    await seed_ledger(project_id, user_id, routed=300, passthrough=700)

    written = await generate_recommendations()
    assert written >= 1

    response = await api_base.get(f"/advisor/recommendations?project_id={project_id}", headers=auth)
    assert response.status_code == 200, response.text

    downgrades = [r for r in response.json() if r["kind"] == "downgrade"]
    assert downgrades, response.json()

    rec = downgrades[0]
    assert rec["confidence"] == "high"
    assert rec["sample_size"] == 300
    # UC-37: 700 requests * $0.009 saved each.
    assert rec["projected_savings_usd"] == pytest.approx(6.3, rel=0.01)
    assert "gpt-4o-mini" in rec["title"]


@pytest.mark.usefixtures("clean_all")
async def test_a_thin_history_produces_no_downgrade_advice(api_base: AsyncClient) -> None:
    """Recommending off ten requests is guessing with a confidence score."""
    await provision_account(api_base, "thin@example.com")
    auth, project_id = await login(api_base, "thin@example.com")
    user_id = await _user_id(project_id)

    await seed_ledger(project_id, user_id, routed=10, passthrough=50)
    await generate_recommendations()

    response = await api_base.get(f"/advisor/recommendations?project_id={project_id}", headers=auth)
    assert [r for r in response.json() if r["kind"] == "downgrade"] == []


@pytest.mark.usefixtures("clean_all")
async def test_escalations_suppress_the_recommendation(api_base: AsyncClient) -> None:
    """The cheap tier having needed rescuing is evidence against it."""
    await provision_account(api_base, "escalated@example.com")
    auth, project_id = await login(api_base, "escalated@example.com")
    user_id = await _user_id(project_id)

    await seed_ledger(project_id, user_id, routed=300, passthrough=700, escalated=60)
    await generate_recommendations()

    response = await api_base.get(f"/advisor/recommendations?project_id={project_id}", headers=auth)
    assert [r for r in response.json() if r["kind"] == "downgrade"] == []


@pytest.mark.usefixtures("clean_all")
async def test_recommendations_are_replaced_not_accumulated(api_base: AsyncClient) -> None:
    """A recommendation is a statement about current usage."""
    await provision_account(api_base, "replace@example.com")
    auth, project_id = await login(api_base, "replace@example.com")
    user_id = await _user_id(project_id)

    await seed_ledger(project_id, user_id, routed=300, passthrough=700)

    for _ in range(3):
        await generate_recommendations()

    response = await api_base.get(f"/advisor/recommendations?project_id={project_id}", headers=auth)
    downgrades = [r for r in response.json() if r["kind"] == "downgrade"]
    assert len(downgrades) == 1, f"accumulated duplicates: {len(downgrades)}"


@pytest.mark.usefixtures("clean_all")
async def test_a_dismissed_recommendation_is_never_resurrected(
    api_base: AsyncClient,
) -> None:
    """Saying it again every morning is not persistence, it is nagging."""
    await provision_account(api_base, "dismiss@example.com")
    auth, project_id = await login(api_base, "dismiss@example.com")
    user_id = await _user_id(project_id)

    await seed_ledger(project_id, user_id, routed=300, passthrough=700)
    await generate_recommendations()

    listed = (
        await api_base.get(f"/advisor/recommendations?project_id={project_id}", headers=auth)
    ).json()
    target = next(r for r in listed if r["kind"] == "downgrade")

    dismissed = await api_base.post(
        f"/advisor/recommendations/{target['id']}/status",
        headers=auth,
        json={"status": "dismissed"},
    )
    assert dismissed.status_code == 200, dismissed.text
    assert dismissed.json()["status"] == "dismissed"

    await generate_recommendations()

    after = (
        await api_base.get(f"/advisor/recommendations?project_id={project_id}", headers=auth)
    ).json()
    assert [r for r in after if r["kind"] == "downgrade"] == []


@pytest.mark.usefixtures("clean_all")
async def test_recommendations_are_ordered_by_projected_saving(
    api_base: AsyncClient,
) -> None:
    await provision_account(api_base, "ordered@example.com")
    auth, project_id = await login(api_base, "ordered@example.com")
    user_id = await _user_id(project_id)

    await seed_ledger(project_id, user_id, routed=100, passthrough=100, endpoint="/small")
    await seed_ledger(
        project_id,
        user_id,
        routed=100,
        passthrough=900,
        endpoint="/large",
        full_cost=0.05,
    )
    await generate_recommendations()

    rows = (
        await api_base.get(f"/advisor/recommendations?project_id={project_id}", headers=auth)
    ).json()
    savings = [r["projected_savings_usd"] for r in rows]

    assert savings == sorted(savings, reverse=True)
    assert rows[0]["detail"]["endpoint"] == "/large"


# -- UC-36: break-even ------------------------------------------------------


@pytest.mark.usefixtures("clean_all")
async def test_breakeven_says_insufficient_data_for_a_quiet_project(
    api_base: AsyncClient,
) -> None:
    await provision_account(api_base, "quietbe@example.com")
    auth, project_id = await login(api_base, "quietbe@example.com")

    response = await api_base.get(f"/advisor/breakeven?project_id={project_id}", headers=auth)
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["recommendation"] == "insufficient_data"
    assert body["n_gpus"] == 0
    assert body["caveats"], "even the refusal explains itself"


@pytest.mark.usefixtures("clean_all")
async def test_breakeven_ships_its_caveats_in_the_payload(api_base: AsyncClient) -> None:
    """BUILD_SPEC §6.7: a bare "self-hosting is cheaper" is misleading.

    A caveat the frontend can forget to render is a caveat that will be
    forgotten, so it travels with the number.
    """
    await provision_account(api_base, "bigvolume@example.com")
    auth, project_id = await login(api_base, "bigvolume@example.com")
    user_id = await _user_id(project_id)

    await seed_ledger(
        project_id,
        user_id,
        routed=0,
        passthrough=600,
        full_cost=1.0,
        tokens_each=5000,
    )

    response = await api_base.get(f"/advisor/breakeven?project_id={project_id}", headers=auth)
    body = response.json()

    assert body["monthly_tokens"] > 0
    assert len(body["caveats"]) >= 5
    joined = " ".join(body["caveats"]).lower()
    assert "quality" in joined
    assert "idle" in joined


@pytest.mark.usefixtures("clean_all")
async def test_breakeven_compares_every_instance_type(api_base: AsyncClient) -> None:
    await provision_account(api_base, "options@example.com")
    auth, project_id = await login(api_base, "options@example.com")
    user_id = await _user_id(project_id)

    await seed_ledger(
        project_id, user_id, routed=0, passthrough=600, full_cost=1.0, tokens_each=5000
    )

    body = (await api_base.get(f"/advisor/breakeven?project_id={project_id}", headers=auth)).json()

    assert len(body["options"]) >= 3
    for option in body["options"]:
        assert option["n_gpus"] >= 1
        assert "recommendation" in option


# -- Isolation --------------------------------------------------------------


@pytest.mark.usefixtures("clean_all")
async def test_one_users_recommendations_are_invisible_to_another(
    api_base: AsyncClient,
) -> None:
    await provision_account(api_base, "rec-a@example.com")
    _, project_a = await login(api_base, "rec-a@example.com")
    user_a = await _user_id(project_a)
    await seed_ledger(project_a, user_a, routed=300, passthrough=700)
    await generate_recommendations()

    await provision_account(api_base, "rec-b@example.com")
    auth_b, project_b = await login(api_base, "rec-b@example.com")

    assert (
        await api_base.get(f"/advisor/recommendations?project_id={project_b}", headers=auth_b)
    ).json() == []

    stolen = await api_base.get(f"/advisor/recommendations?project_id={project_a}", headers=auth_b)
    assert stolen.status_code == 404


@pytest.mark.usefixtures("clean_all")
async def test_another_users_recommendation_cannot_be_dismissed(
    api_base: AsyncClient,
) -> None:
    await provision_account(api_base, "own@example.com")
    auth_a, project_a = await login(api_base, "own@example.com")
    user_a = await _user_id(project_a)
    await seed_ledger(project_a, user_a, routed=300, passthrough=700)
    await generate_recommendations()

    target = (
        await api_base.get(f"/advisor/recommendations?project_id={project_a}", headers=auth_a)
    ).json()[0]

    await provision_account(api_base, "other@example.com")
    auth_b, _ = await login(api_base, "other@example.com")

    response = await api_base.post(
        f"/advisor/recommendations/{target['id']}/status",
        headers=auth_b,
        json={"status": "dismissed"},
    )
    assert response.status_code == 404
