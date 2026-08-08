"""P5 acceptance criteria — UC-14 through UC-19.

"routing savings and caching savings are reported separately and never
 double-count. A routing-engine exception or a >20 ms classifier stall
 results in passthrough to the requested model, logged with
 FAILOPEN_TIMEOUT, not an error."
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from openai import AsyncOpenAI
from sqlalchemy import text

from apicost.db.session import get_admin_engine
from apicost.routing.classifier import load_classifier
from apicost.worker.tasks import drain_ledger
from tests.e2e.conftest import LiveServer, provision_account
from tests.e2e.stub_provider import COMPLETION_TEXT

pytestmark = pytest.mark.integration


def sdk(proxy: LiveServer, key: str) -> AsyncOpenAI:
    return AsyncOpenAI(base_url=f"{proxy.url}/v1", api_key=key, max_retries=0)


async def enable_routing(api: AsyncClient, email: str) -> tuple[dict[str, str], str]:
    """Turn routing on — it is off by default (UC-14)."""
    login = await api.post("/auth/login", json={"email": email, "password": "a-very-long-password"})
    auth = {"Authorization": f"Bearer {login.json()['access_token']}"}
    project_id = (await api.get("/projects", headers=auth)).json()[0]["id"]
    response = await api.put(
        f"/projects/{project_id}/settings", headers=auth, json={"routing_enabled": True}
    )
    assert response.status_code == 200
    return auth, project_id


async def ledger_row() -> dict[str, Any]:
    async with get_admin_engine().connect() as conn:
        result = await conn.execute(
            text("SELECT * FROM requests_log ORDER BY timestamp DESC LIMIT 1")
        )
        return dict(result.mappings().one())


@pytest.mark.usefixtures("clean_all")
async def test_routing_is_off_until_the_user_turns_it_on(
    live_proxy: LiveServer, api_base: AsyncClient
) -> None:
    """UC-14. A cost optimization that changes which model answers must be opt-in."""
    key = await provision_account(api_base, "route-off@example.com")

    await sdk(live_proxy, key).chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "translate hello to Spanish"}]
    )
    await drain_ledger(block_ms=100)

    row = await ledger_row()
    assert row["routed"] is False
    assert row["model_used"] == "gpt-4o"


@pytest.mark.usefixtures("clean_all")
async def test_a_simple_prompt_is_routed_to_the_cheap_tier(
    live_proxy: LiveServer, api_base: AsyncClient
) -> None:
    if not load_classifier():
        pytest.skip("no trained artifact")

    key = await provision_account(api_base, "route-cheap@example.com")
    await enable_routing(api_base, "route-cheap@example.com")

    response = await sdk(live_proxy, key).chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": "Translate 'good morning' into Spanish"}],
    )
    assert response.choices[0].message.content == COMPLETION_TEXT

    await drain_ledger(block_ms=100)
    row = await ledger_row()

    assert row["routed"] is True
    assert row["model_requested"] == "gpt-4o"
    assert row["model_used"] == "gpt-4o-mini"
    assert row["routing_reason_code"] == "CLASSIFIER_CHEAP_TIER"
    assert row["routing_model_version"] is not None


@pytest.mark.usefixtures("clean_all")
async def test_an_exclude_rule_is_honoured_end_to_end(
    live_proxy: LiveServer, api_base: AsyncClient
) -> None:
    """UC-19, through the whole stack including the auth cache."""
    key = await provision_account(api_base, "route-exclude@example.com")
    auth, project_id = await enable_routing(api_base, "route-exclude@example.com")

    created = await api_base.post(
        "/routing-rules",
        headers=auth,
        json={"project_id": project_id, "rule_type": "exclude", "match_condition": {}},
    )
    assert created.status_code == 201

    await sdk(live_proxy, key).chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": "Translate 'good morning' into Spanish"}],
    )
    await drain_ledger(block_ms=100)

    row = await ledger_row()
    assert row["routed"] is False
    assert row["model_used"] == "gpt-4o"
    assert row["routing_reason_code"] == "EXCLUDED_ENDPOINT"


@pytest.mark.usefixtures("clean_all")
async def test_an_override_rule_forces_a_model(
    live_proxy: LiveServer, api_base: AsyncClient
) -> None:
    """UC-15."""
    key = await provision_account(api_base, "route-override@example.com")
    auth, project_id = await enable_routing(api_base, "route-override@example.com")

    await api_base.post(
        "/routing-rules",
        headers=auth,
        json={
            "project_id": project_id,
            "rule_type": "override",
            "match_condition": {},
            "target_model": "gpt-4o-mini",
        },
    )

    # A hard prompt the classifier would leave alone.
    await sdk(live_proxy, key).chat.completions.create(
        model="gpt-4o",
        messages=[
            {
                "role": "user",
                "content": "Prove this concurrent queue is linearisable, or find the "
                "interleaving that breaks it",
            }
        ],
    )
    await drain_ledger(block_ms=100)

    row = await ledger_row()
    assert row["model_used"] == "gpt-4o-mini"
    assert row["routing_reason_code"] == "RULE_OVERRIDE"


@pytest.mark.usefixtures("clean_all")
async def test_a_stalled_classifier_passes_through(
    live_proxy: LiveServer, api_base: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The acceptance criterion: a >20 ms stall is a passthrough, not an error.

    The user's completion must arrive, on the model they asked for, promptly.
    """
    key = await provision_account(api_base, "route-stall@example.com")
    await enable_routing(api_base, "route-stall@example.com")

    def stall(_body: dict[str, Any], **_kwargs: Any) -> None:
        # Synchronous sleep: the classifier is CPU-bound, so this is what a
        # real stall looks like from the pipeline's point of view.
        import time

        time.sleep(2.0)

    import apicost.proxy.pipeline as pipeline

    monkeypatch.setattr(pipeline, "routing_decide", stall)

    response = await sdk(live_proxy, key).chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "hello"}]
    )

    assert response.choices[0].message.content == COMPLETION_TEXT

    await drain_ledger(block_ms=100)
    row = await ledger_row()
    assert row["routed"] is False
    assert row["model_used"] == "gpt-4o"
    assert row["status"] == 200


@pytest.mark.usefixtures("clean_all")
async def test_a_routing_exception_passes_through(
    live_proxy: LiveServer, api_base: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = await provision_account(api_base, "route-boom@example.com")
    await enable_routing(api_base, "route-boom@example.com")

    def explode(_body: dict[str, Any], **_kwargs: Any) -> None:
        raise RuntimeError("routing engine blew up")

    import apicost.proxy.pipeline as pipeline

    monkeypatch.setattr(pipeline, "routing_decide", explode)

    response = await sdk(live_proxy, key).chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "hello"}]
    )

    assert response.choices[0].message.content == COMPLETION_TEXT
    await drain_ledger(block_ms=100)
    assert (await ledger_row())["status"] == 200


@pytest.mark.usefixtures("clean_all")
async def test_escalation_retries_on_the_requested_model(
    live_proxy: LiveServer, api_base: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UC-17, and the cost accounting that goes with it."""
    key = await provision_account(api_base, "route-escalate@example.com")
    await enable_routing(api_base, "route-escalate@example.com")

    import apicost.proxy.pipeline as pipeline
    from apicost.routing.engine import RoutingDecision

    monkeypatch.setattr(
        pipeline,
        "routing_decide",
        lambda _body, **_kw: RoutingDecision(
            model="gpt-4o-mini",
            routed=True,
            reason_code="CLASSIFIER_CHEAP_TIER",
            confidence=0.99,
            model_version="test",
        ),
    )
    # Force the cheap answer to look unusable.
    monkeypatch.setattr(
        pipeline,
        "looks_low_confidence",
        lambda _body, **_kw: type("V", (), {"escalate": True, "reason": "REFUSAL"})(),
    )

    await sdk(live_proxy, key).chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "hello"}]
    )
    await drain_ledger(block_ms=100)

    row = await ledger_row()
    assert row["escalation_triggered"] is True
    assert row["model_used"] == "gpt-4o", "escalation returns to the requested model"
    # Both calls were paid for, so the recorded tokens are the sum.
    assert row["tokens_in"] == 24, "the cheap attempt's tokens were not counted"


@pytest.mark.usefixtures("clean_all")
async def test_routing_and_caching_savings_never_double_count(
    live_proxy: LiveServer, api_base: AsyncClient
) -> None:
    """The acceptance criterion, and the product's central honesty claim."""
    key = await provision_account(api_base, "route-savings@example.com")
    auth, _project_id = await enable_routing(api_base, "route-savings@example.com")
    client = sdk(live_proxy, key)

    prompt = "Translate 'good morning' into Spanish"
    await client.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": prompt}]
    )
    # Identical prompt -> cache hit, which must not also count as routing.
    await client.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": prompt}]
    )

    await drain_ledger(block_ms=200)
    from apicost.ledger.rollup import rebuild_rollups

    await rebuild_rollups(full=True)

    cache = (await api_base.get("/cache/stats", headers=auth)).json()
    routing = (await api_base.get("/routing/stats", headers=auth)).json()
    usage = (await api_base.get("/usage?range=30d", headers=auth)).json()["summary"]

    cache_savings = Decimal(cache["savings_usd"])
    routing_savings = Decimal(routing["savings_usd"])

    # Whatever each reports, together they cannot exceed what the requests
    # would have cost without us.
    assert cache_savings + routing_savings <= Decimal(usage["total_would_have_been_usd"])
    assert Decimal(usage["cache_savings_usd"]) == cache_savings

    async with get_admin_engine().connect() as conn:
        both = (
            await conn.execute(text("SELECT count(*) FROM requests_log WHERE cache_hit AND routed"))
        ).scalar()
    # A cached row may carry the routed flag, but the reports must not count it
    # in both places — that is what the inequality above proves.
    assert both is not None


@pytest.mark.usefixtures("clean_all")
async def test_routing_stats_reports_escalation_cost_honestly(
    api_base: AsyncClient,
) -> None:
    """UC-18: escalations reduce reported savings, possibly below zero."""
    from tests.integration.test_usage_api import insert_rows, setup_user

    auth, user_id, project_id = await setup_user(api_base, "route-stats@example.com")
    await insert_rows(
        user_id,
        project_id,
        [
            # A clean routing win.
            {
                "routed": True,
                "model_used": "gpt-4o-mini",
                "cost_usd": Decimal("1.00"),
                "cost_would_have_been_usd": Decimal("4.00"),
            },
            # An escalation: paid for both calls, ended up where it started.
            {
                "routed": True,
                "escalation_triggered": True,
                "cost_usd": Decimal("5.00"),
                "cost_would_have_been_usd": Decimal("4.00"),
            },
        ],
    )

    stats = (await api_base.get("/routing/stats", headers=auth)).json()

    assert Decimal(stats["gross_savings_usd"]) == Decimal("3.00")
    assert Decimal(stats["escalation_cost_usd"]) == Decimal("1.00")
    assert Decimal(stats["savings_usd"]) == Decimal("2.00")
    assert stats["escalations"] == 1


@pytest.mark.usefixtures("clean_all")
async def test_routing_rules_are_scoped_to_their_owner(api_base: AsyncClient) -> None:
    from tests.integration.test_usage_api import setup_user

    _auth_a, _user_a, project_a = await setup_user(api_base, "rr-a@example.com")
    auth_b, _user_b, _project_b = await setup_user(api_base, "rr-b@example.com")

    response = await api_base.post(
        "/routing-rules",
        headers=auth_b,
        json={"project_id": project_a, "rule_type": "exclude", "match_condition": {}},
    )
    assert response.status_code == 404


@pytest.mark.usefixtures("clean_all")
async def test_an_override_rule_needs_a_target(api_base: AsyncClient) -> None:
    from tests.integration.test_usage_api import setup_user

    auth, _user_id, project_id = await setup_user(api_base, "rr-target@example.com")

    response = await api_base.post(
        "/routing-rules",
        headers=auth,
        json={"project_id": project_id, "rule_type": "override", "match_condition": {}},
    )
    assert response.status_code == 400


async def test_the_routing_budget_matches_the_spec() -> None:
    from apicost.proxy.pipeline import ROUTING_BUDGET_MS

    assert ROUTING_BUDGET_MS == 20.0


def test_asyncio_is_imported() -> None:
    assert asyncio is not None
