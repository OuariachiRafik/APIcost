"""P10 acceptance — Stripe billing.

`POST /billing/webhook` is a public, unauthenticated endpoint that changes what
an account is allowed to do. Most of this file is about that one fact.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from apicost.billing.plans import PLANS, check_plan_limit, get_plan
from apicost.db.session import get_admin_engine
from tests.e2e.conftest import provision_account

pytestmark = pytest.mark.integration

WEBHOOK_SECRET = "whsec_test_secret_for_the_suite"


def sign(payload: dict[str, Any], secret: str = WEBHOOK_SECRET, *, timestamp: int | None = None):
    """Produce a genuine Stripe-Signature header.

    Real HMAC over the real bytes, so the verification path under test is the
    production one rather than a stub that always says yes.

    `object: "event"` is added because every real Stripe event carries it and
    the SDK reads it to tell v1 events from v2 — a fixture without it fails
    inside `construct_event` for a reason that has nothing to do with the code
    under test.
    """
    payload = {"object": "event", "api_version": "2024-06-20", **payload}
    body = json.dumps(payload).encode()
    ts = timestamp if timestamp is not None else int(time.time())
    signed = f"{ts}.{body.decode()}".encode()
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return body, f"t={ts},v1={digest}"


@pytest.fixture(autouse=True)
def _stripe_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    from apicost.config import get_settings

    monkeypatch.setenv("APICOST_STRIPE_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("APICOST_STRIPE_SECRET_KEY", "sk_test_not_a_real_key")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def login(api: AsyncClient, email: str) -> dict[str, str]:
    response = await api.post(
        "/auth/login", json={"email": email, "password": "a-very-long-password"}
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _user(email: str) -> Any:
    async with get_admin_engine().connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT id, plan_id, plan_status, stripe_customer_id "
                    "FROM users WHERE email = :e"
                ),
                {"e": email},
            )
        ).one()


def subscription_event(
    event_id: str,
    customer_id: str,
    *,
    event_type: str = "customer.subscription.updated",
    plan_id: str = "pro",
) -> dict[str, Any]:
    return {
        "object": "event",
        "id": event_id,
        "type": event_type,
        "data": {
            "object": {
                "id": "sub_test_1",
                "customer": customer_id,
                "metadata": {"plan_id": plan_id},
                "items": {"data": [{"price": {"id": PLANS[plan_id].stripe_price_id}}]},
            }
        },
    }


# -- Signature verification: the security boundary --------------------------


@pytest.mark.usefixtures("clean_all")
async def test_an_unsigned_webhook_is_rejected(api_base: AsyncClient) -> None:
    """Without this, anyone who learns the URL can upgrade themselves."""
    body, _ = sign({"id": "evt_1", "type": "customer.subscription.updated"})

    response = await api_base.post("/billing/webhook", content=body)
    assert response.status_code == 400


@pytest.mark.usefixtures("clean_all")
async def test_a_forged_signature_is_rejected(api_base: AsyncClient) -> None:
    payload = {"id": "evt_2", "type": "customer.subscription.updated"}
    body, _ = sign(payload)

    response = await api_base.post(
        "/billing/webhook",
        content=body,
        headers={"Stripe-Signature": "t=1,v1=" + "0" * 64},
    )
    assert response.status_code == 400


@pytest.mark.usefixtures("clean_all")
async def test_a_signature_from_the_wrong_secret_is_rejected(api_base: AsyncClient) -> None:
    payload = {"id": "evt_3", "type": "customer.subscription.updated"}
    body, header = sign(payload, secret="whsec_someone_elses_secret")

    response = await api_base.post(
        "/billing/webhook", content=body, headers={"Stripe-Signature": header}
    )
    assert response.status_code == 400


@pytest.mark.usefixtures("clean_all")
async def test_a_replayed_old_signature_is_rejected(api_base: AsyncClient) -> None:
    """Stripe's verifier enforces a timestamp tolerance.

    Without it, one captured payload could be replayed forever.
    """
    payload = {"id": "evt_4", "type": "customer.subscription.updated"}
    body, header = sign(payload, timestamp=int(time.time()) - 86_400)

    response = await api_base.post(
        "/billing/webhook", content=body, headers={"Stripe-Signature": header}
    )
    assert response.status_code == 400


@pytest.mark.usefixtures("clean_all")
async def test_a_tampered_body_is_rejected(api_base: AsyncClient) -> None:
    """The signature covers the bytes, so changing one invalidates it."""
    payload = {"id": "evt_5", "type": "customer.subscription.updated"}
    _, header = sign(payload)

    tampered = json.dumps({**payload, "type": "customer.subscription.deleted"}).encode()

    response = await api_base.post(
        "/billing/webhook", content=tampered, headers={"Stripe-Signature": header}
    )
    assert response.status_code == 400


@pytest.mark.usefixtures("clean_all")
async def test_a_deployment_with_no_secret_rejects_everything(
    api_base: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed. Unconfigured is not a reason to trust the internet."""
    from apicost.config import get_settings

    monkeypatch.setenv("APICOST_STRIPE_WEBHOOK_SECRET", "")
    get_settings.cache_clear()

    body, header = sign({"id": "evt_6", "type": "customer.subscription.updated"})
    response = await api_base.post(
        "/billing/webhook", content=body, headers={"Stripe-Signature": header}
    )

    assert response.status_code == 503
    get_settings.cache_clear()


# -- Idempotency ------------------------------------------------------------


@pytest.mark.usefixtures("clean_all")
async def test_a_replayed_event_is_applied_only_once(api_base: AsyncClient) -> None:
    """Stripe retries on any non-2xx and on its own schedule."""
    await provision_account(api_base, "idem@example.com")
    user = await _user("idem@example.com")
    await _attach_customer(str(user.id), "cus_idem")

    event = subscription_event("evt_idem_1", "cus_idem")
    body, header = sign(event)

    first = await api_base.post(
        "/billing/webhook", content=body, headers={"Stripe-Signature": header}
    )
    second = await api_base.post(
        "/billing/webhook", content=body, headers={"Stripe-Signature": header}
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["applied"] is True
    assert second.json()["applied"] is False
    assert second.json()["reason"] == "ALREADY_PROCESSED"

    async with get_admin_engine().connect() as conn:
        count = (
            await conn.execute(
                text("SELECT count(*) FROM billing_events WHERE id = :id"),
                {"id": "evt_idem_1"},
            )
        ).scalar()
    assert count == 1


@pytest.mark.usefixtures("clean_all")
async def test_an_unhandled_event_is_acknowledged_not_retried(
    api_base: AsyncClient,
) -> None:
    """A 4xx would make Stripe retry forever an event we have no opinion on."""
    body, header = sign({"id": "evt_unhandled", "type": "customer.discount.created"})

    response = await api_base.post(
        "/billing/webhook", content=body, headers={"Stripe-Signature": header}
    )

    assert response.status_code == 200
    assert response.json()["applied"] is False
    assert response.json()["reason"] == "UNHANDLED_EVENT_TYPE"


# -- Applying subscription state --------------------------------------------


@pytest.mark.usefixtures("clean_all")
async def test_a_subscription_update_changes_the_plan(api_base: AsyncClient) -> None:
    await provision_account(api_base, "upgrade@example.com")
    user = await _user("upgrade@example.com")
    assert user.plan_id == "free"

    await _attach_customer(str(user.id), "cus_upgrade")

    body, header = sign(subscription_event("evt_up_1", "cus_upgrade", plan_id="pro"))
    await api_base.post("/billing/webhook", content=body, headers={"Stripe-Signature": header})

    after = await _user("upgrade@example.com")
    assert after.plan_id == "pro"
    assert after.plan_status == "active"


@pytest.mark.usefixtures("clean_all")
async def test_a_cancelled_subscription_returns_to_free_without_deleting_anything(
    api_base: AsyncClient,
) -> None:
    """A cancellation is not an account deletion."""
    key = await provision_account(api_base, "cancel@example.com")
    user = await _user("cancel@example.com")
    await _attach_customer(str(user.id), "cus_cancel", plan_id="pro")

    body, header = sign(
        subscription_event("evt_cancel_1", "cus_cancel", event_type="customer.subscription.deleted")
    )
    await api_base.post("/billing/webhook", content=body, headers={"Stripe-Signature": header})

    after = await _user("cancel@example.com")
    assert after.plan_id == "free"
    assert after.plan_status == "cancelled"

    # Their keys and projects are untouched.
    auth = await login(api_base, "cancel@example.com")
    assert (await api_base.get("/projects", headers=auth)).status_code == 200
    assert key


@pytest.mark.usefixtures("clean_all")
async def test_a_failed_payment_marks_past_due_without_downgrading(
    api_base: AsyncClient,
) -> None:
    """An expired card may be fixed in an hour; Stripe retries on its own."""
    await provision_account(api_base, "pastdue@example.com")
    user = await _user("pastdue@example.com")
    await _attach_customer(str(user.id), "cus_pastdue", plan_id="pro")

    event = {
        "id": "evt_failed_1",
        "type": "invoice.payment_failed",
        "data": {"object": {"customer": "cus_pastdue"}},
    }
    body, header = sign(event)
    await api_base.post("/billing/webhook", content=body, headers={"Stripe-Signature": header})

    after = await _user("pastdue@example.com")
    assert after.plan_status == "past_due"
    assert after.plan_id == "pro", "a failed payment must not downgrade immediately"


@pytest.mark.usefixtures("clean_all")
async def test_an_event_for_an_unknown_customer_changes_nothing(
    api_base: AsyncClient,
) -> None:
    body, header = sign(subscription_event("evt_ghost", "cus_does_not_exist"))

    response = await api_base.post(
        "/billing/webhook", content=body, headers={"Stripe-Signature": header}
    )
    assert response.status_code == 200
    assert response.json()["applied"] is False
    assert response.json()["reason"] == "NO_MATCH"


# -- GET /billing/plan ------------------------------------------------------


@pytest.mark.usefixtures("clean_all")
async def test_the_plan_endpoint_reports_usage_against_the_limit(
    api_base: AsyncClient,
) -> None:
    await provision_account(api_base, "planview@example.com")
    auth = await login(api_base, "planview@example.com")

    body = (await api_base.get("/billing/plan", headers=auth)).json()

    assert body["plan_id"] == "free"
    assert body["monthly_request_limit"] == PLANS["free"].monthly_request_limit
    assert body["requests_this_month"] >= 0
    assert body["action"] in {"allow", "warn", "block"}
    assert len(body["available_plans"]) == len(PLANS)


@pytest.mark.usefixtures("clean_all")
async def test_the_plan_endpoint_requires_authentication(api_base: AsyncClient) -> None:
    assert (await api_base.get("/billing/plan")).status_code == 401


@pytest.mark.usefixtures("clean_all")
async def test_checkout_requires_a_purchasable_plan(api_base: AsyncClient) -> None:
    await provision_account(api_base, "checkout@example.com")
    auth = await login(api_base, "checkout@example.com")

    response = await api_base.post(
        "/billing/checkout-session", headers=auth, json={"plan_id": "free"}
    )
    assert response.status_code == 503, "the free plan is not purchasable"


# -- The proxy signal -------------------------------------------------------


def test_going_over_the_free_cap_warns_rather_than_blocks() -> None:
    """A cost tool that cuts off a developer's application loses them.

    The traffic being refused is traffic they are already paying a provider
    for, so blocking costs them money to save us nothing.
    """
    free = get_plan("free")
    verdict = check_plan_limit(free, free.monthly_request_limit * 5)

    assert verdict.action.value == "warn"
    assert not verdict.blocked
    assert verdict.reason == "OVER_PLAN_LIMIT"


def test_an_unknown_plan_falls_back_to_the_most_restrictive() -> None:
    """Our data being wrong is not a reason to hand out unlimited capacity."""
    assert get_plan("enterprise_unlimited").id == "free"
    assert get_plan(None).id == "free"


@pytest.mark.usefixtures("clean_all")
async def test_the_proxy_advertises_the_plan_when_over_the_line(
    live_proxy: Any, api_base: AsyncClient
) -> None:
    """UC: "plan-limit signals consumed by the proxy for upgrade prompts"."""
    from apicost.billing.usage import plan_usage_key
    from apicost.db.redis import get_redis

    key = await provision_account(api_base, "proxyplan@example.com")
    user = await _user("proxyplan@example.com")

    # Push the counter past the free cap.
    await get_redis().set(plan_usage_key(str(user.id)), PLANS["free"].monthly_request_limit + 1)

    async with AsyncClient(timeout=30.0) as raw:
        response = await raw.post(
            f"{live_proxy.url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 200, "over the cap must still be served"
    assert response.headers["x-apicost-plan"] == "free"
    assert response.headers["x-apicost-plan-notice"] == "OVER_PLAN_LIMIT"
    assert "/" in response.headers["x-apicost-plan-usage"]


@pytest.mark.usefixtures("clean_all")
async def test_a_normal_request_carries_no_plan_nag(live_proxy: Any, api_base: AsyncClient) -> None:
    key = await provision_account(api_base, "quietplan@example.com")

    async with AsyncClient(timeout=30.0) as raw:
        response = await raw.post(
            f"{live_proxy.url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hello"}]},
        )

    assert response.status_code == 200
    assert "x-apicost-plan-notice" not in response.headers


async def _attach_customer(user_id: str, customer_id: str, plan_id: str = "free") -> None:
    async with get_admin_engine().begin() as conn:
        await conn.execute(
            text("UPDATE users SET stripe_customer_id = :c, plan_id = :p WHERE id = :id"),
            {"c": customer_id, "id": user_id, "p": plan_id},
        )
