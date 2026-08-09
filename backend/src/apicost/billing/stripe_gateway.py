"""Stripe integration — checkout, webhooks, subscription state.

Two things here are security boundaries rather than features:

**Webhook signature verification.** `POST /billing/webhook` is a public,
unauthenticated endpoint that changes what a user is allowed to do. Without a
verified signature, anyone who learns the URL can upgrade themselves to the
unlimited plan with a curl command. Verification is mandatory and there is no
configuration that turns it off — a missing secret makes the endpoint fail
closed rather than trust its input.

**Idempotency.** Stripe retries on any non-2xx and on its own schedule, so
every handler must be safe to run twice. The mechanism is a primary-key insert
on Stripe's own event id: a duplicate delivery fails the insert, which is how
we know to skip it. A SELECT-then-INSERT would leave a window where two
concurrent deliveries both see nothing and both apply.

Nothing here logs an event body. Stripe payloads carry customer emails, card
metadata and billing addresses (hard rule 3).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from apicost.billing.plans import FREE_PLAN_ID, PLANS
from apicost.config import Settings, get_settings
from apicost.core.errors import APICostError
from apicost.core.logging import get_logger
from apicost.db.session import get_admin_engine

__all__ = [
    "HANDLED_EVENTS",
    "StripeConfigurationError",
    "StripeSignatureError",
    "WebhookResult",
    "create_checkout_session",
    "handle_webhook",
    "verify_signature",
]

_logger = get_logger(__name__)


class StripeSignatureError(APICostError):
    """The webhook could not be proven to come from Stripe."""

    status_code = 400
    title = "Invalid Signature"


class StripeConfigurationError(APICostError):
    """Billing is not configured on this deployment."""

    status_code = 503
    title = "Billing Unavailable"


HANDLED_EVENTS = frozenset(
    {
        "checkout.session.completed",
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
        "invoice.payment_failed",
    }
)
"""Everything else is acknowledged and ignored.

Returning 2xx for an event we do not handle is deliberate: a 4xx would make
Stripe retry it forever, and an event we have no opinion about is not an
error."""


@dataclass(frozen=True)
class WebhookResult:
    event_id: str
    event_type: str
    applied: bool
    reason: str


def verify_signature(payload: bytes, signature_header: str, settings: Settings) -> dict[str, Any]:
    """Verify and parse a webhook. Raises rather than returning on failure.

    Delegates to Stripe's own verifier, which does the constant-time HMAC
    comparison and the timestamp-tolerance check that stops a captured payload
    being replayed indefinitely.
    """
    secret = settings.stripe_webhook_secret.get_secret_value()
    if not secret:
        # Fail closed. An endpoint that mutates plans must never accept
        # unverified input, and "the secret wasn't configured" is not a reason
        # to start trusting the internet.
        _logger.error("stripe_webhook_secret_missing", subsystem="billing")
        raise StripeConfigurationError("Billing webhooks are not configured")

    import stripe

    try:
        stripe.Webhook.construct_event(payload, signature_header, secret)
    except ValueError as exc:
        raise StripeSignatureError("Malformed webhook payload") from exc
    except stripe.SignatureVerificationError as exc:
        # Logged without the payload or the header. A rejected signature is
        # worth knowing about; its contents are not ours to record.
        _logger.warning("stripe_signature_rejected", subsystem="billing")
        raise StripeSignatureError("Webhook signature verification failed") from exc

    # Parse the *verified* bytes ourselves rather than using the StripeObject
    # the SDK returns. StripeObject overrides attribute access and carries its
    # own `object` field naming the resource type, which collides with the
    # `data.object` path every handler here walks — `dict()` only flattens the
    # top level, so nested values keep those semantics.
    #
    # This is not a second parse of untrusted input: construct_event has
    # already proven these exact bytes came from Stripe.
    return json.loads(payload)


async def handle_webhook(event: dict[str, Any]) -> WebhookResult:
    """Apply one verified event, exactly once."""
    event_id = str(event.get("id") or "")
    event_type = str(event.get("type") or "")

    if not event_id:
        raise StripeSignatureError("Webhook has no event id")

    if event_type not in HANDLED_EVENTS:
        return WebhookResult(event_id, event_type, False, "UNHANDLED_EVENT_TYPE")

    payload_hash = hashlib.sha256(repr(sorted(event.items())).encode()).hexdigest()

    claimed = await _claim(event_id, event_type, payload_hash)
    if not claimed:
        _logger.info(
            "stripe_webhook_duplicate",
            subsystem="billing",
            event_id=event_id,
            event_type=event_type,
        )
        return WebhookResult(event_id, event_type, False, "ALREADY_PROCESSED")

    try:
        applied = await _apply(event_type, event)
    except Exception:
        # Release the claim so Stripe's retry can try again. Leaving it would
        # make a transient database error permanently swallow a subscription
        # change — the user pays and never gets upgraded.
        await _release(event_id)
        raise

    return WebhookResult(event_id, event_type, applied, "APPLIED" if applied else "NO_MATCH")


async def _claim(event_id: str, event_type: str, payload_hash: str) -> bool:
    """Insert the event id, returning False if it was already there."""
    async with get_admin_engine().begin() as conn:
        result = await conn.execute(
            text(
                "INSERT INTO billing_events (id, event_type, payload_hash) "
                "VALUES (:id, :type, :hash) ON CONFLICT (id) DO NOTHING RETURNING id"
            ),
            {"id": event_id, "type": event_type, "hash": payload_hash},
        )
        return result.first() is not None


async def _release(event_id: str) -> None:
    try:
        async with get_admin_engine().begin() as conn:
            await conn.execute(text("DELETE FROM billing_events WHERE id = :id"), {"id": event_id})
    except Exception:
        _logger.warning("stripe_claim_release_failed", subsystem="billing", event_id=event_id)


async def _apply(event_type: str, event: dict[str, Any]) -> bool:
    obj = event.get("data", {}).get("object", {})
    if not isinstance(obj, dict):
        return False

    if event_type == "customer.subscription.deleted":
        return await _downgrade(str(obj.get("customer") or ""))

    if event_type == "invoice.payment_failed":
        # Not a downgrade. A failed payment may be a expired card that the user
        # fixes in an hour, and Stripe retries on its own dunning schedule.
        # Marking the status is enough for the dashboard to prompt them.
        return await _set_status(str(obj.get("customer") or ""), "past_due")

    customer_id = str(obj.get("customer") or "")
    plan_id = _plan_from_object(obj)

    if event_type == "checkout.session.completed":
        # The session carries our user id in metadata; the subscription events
        # that follow only carry the customer, which is why this one is where
        # the customer id gets attached to the account.
        user_id = str(obj.get("metadata", {}).get("user_id") or "")
        if user_id and customer_id:
            return await _link_customer(user_id, customer_id, obj)

    if customer_id and plan_id:
        return await _set_plan(customer_id, plan_id, obj)

    return False


def _plan_from_object(obj: dict[str, Any]) -> str | None:
    """Map a Stripe price id back to one of our plans.

    Falls back to metadata, because a checkout session does not expand its
    line items by default and re-fetching from Stripe inside a webhook adds a
    network call to a handler that must answer quickly.
    """
    metadata_plan = obj.get("metadata", {}).get("plan_id")
    if isinstance(metadata_plan, str) and metadata_plan in PLANS:
        return metadata_plan

    items = obj.get("items", {}).get("data", [])
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            price_id = str(item.get("price", {}).get("id") or "")
            for plan in PLANS.values():
                if plan.stripe_price_id and plan.stripe_price_id == price_id:
                    return plan.id
    return None


async def _link_customer(user_id: str, customer_id: str, obj: dict[str, Any]) -> bool:
    plan_id = _plan_from_object(obj) or FREE_PLAN_ID
    async with get_admin_engine().begin() as conn:
        result = await conn.execute(
            text(
                "UPDATE users SET stripe_customer_id = :customer, plan_id = :plan, "
                "plan_status = 'active', stripe_subscription_id = :subscription "
                "WHERE id = :user_id RETURNING id"
            ),
            {
                "customer": customer_id,
                "plan": plan_id,
                "subscription": str(obj.get("subscription") or "") or None,
                "user_id": user_id,
            },
        )
        return result.first() is not None


async def _set_plan(customer_id: str, plan_id: str, obj: dict[str, Any]) -> bool:
    renews_at = obj.get("current_period_end")
    async with get_admin_engine().begin() as conn:
        result = await conn.execute(
            text(
                "UPDATE users SET plan_id = :plan, plan_status = 'active', "
                "stripe_subscription_id = COALESCE(:subscription, stripe_subscription_id), "
                "plan_renews_at = :renews "
                "WHERE stripe_customer_id = :customer RETURNING id"
            ),
            {
                "plan": plan_id,
                "customer": customer_id,
                "subscription": str(obj.get("id") or "") or None,
                "renews": datetime.fromtimestamp(renews_at, UTC)
                if isinstance(renews_at, int | float)
                else None,
            },
        )
        return result.first() is not None


async def _downgrade(customer_id: str) -> bool:
    """Back to free. Their data and their keys are untouched.

    A cancelled subscription is not a deleted account, and treating it as one
    is how a product loses a customer who meant to come back next quarter.
    """
    async with get_admin_engine().begin() as conn:
        result = await conn.execute(
            text(
                "UPDATE users SET plan_id = :free, plan_status = 'cancelled', "
                "stripe_subscription_id = NULL, plan_renews_at = NULL "
                "WHERE stripe_customer_id = :customer RETURNING id"
            ),
            {"free": FREE_PLAN_ID, "customer": customer_id},
        )
        return result.first() is not None


async def _set_status(customer_id: str, status: str) -> bool:
    async with get_admin_engine().begin() as conn:
        result = await conn.execute(
            text(
                "UPDATE users SET plan_status = :status WHERE stripe_customer_id = :customer "
                "RETURNING id"
            ),
            {"status": status, "customer": customer_id},
        )
        return result.first() is not None


async def create_checkout_session(
    user_id: str,
    email: str,
    plan_id: str,
    *,
    settings: Settings | None = None,
) -> str:
    """Start a Stripe Checkout session and return its URL."""
    cfg = settings or get_settings()
    api_key = cfg.stripe_secret_key.get_secret_value()

    if not api_key:
        raise StripeConfigurationError("Billing is not configured on this deployment")

    plan = PLANS.get(plan_id)
    if plan is None or not plan.stripe_price_id:
        raise StripeConfigurationError(f"Plan {plan_id!r} is not purchasable")

    import stripe

    stripe.api_key = api_key

    session = await stripe.checkout.Session.create_async(
        mode="subscription",
        line_items=[{"price": plan.stripe_price_id, "quantity": 1}],
        customer_email=email,
        success_url=f"{cfg.web_base_url}/billing?status=success",
        cancel_url=f"{cfg.web_base_url}/billing?status=cancelled",
        # Carried through to the webhook. The subscription events that follow
        # identify the customer but not our user, so this is the only place the
        # two are tied together.
        metadata={"user_id": user_id, "plan_id": plan_id},
        subscription_data={"metadata": {"user_id": user_id, "plan_id": plan_id}},
    )

    url = session.get("url")
    if not isinstance(url, str):
        raise StripeConfigurationError("Stripe did not return a checkout URL")

    _logger.info("checkout_session_created", subsystem="billing", plan_id=plan_id)
    return url
