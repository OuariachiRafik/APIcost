"""Billing — BUILD_SPEC §4 P10."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

from apicost.api.deps import CurrentUser, DbSession
from apicost.billing.plans import PLANS, check_plan_limit, get_plan
from apicost.billing.stripe_gateway import (
    StripeSignatureError,
    create_checkout_session,
    handle_webhook,
    verify_signature,
)
from apicost.billing.usage import monthly_request_count
from apicost.config import get_settings
from apicost.core.logging import get_logger

router = APIRouter(tags=["billing"])

_logger = get_logger(__name__)


class PlanOption(BaseModel):
    id: str
    name: str
    monthly_request_limit: int
    price_usd: float
    purchasable: bool


class PlanResponse(BaseModel):
    plan_id: str
    plan_name: str
    plan_status: str
    monthly_request_limit: int
    requests_this_month: int
    fraction_used: float
    remaining: int
    action: str
    renews_at: datetime | None
    available_plans: list[PlanOption]


class CheckoutRequest(BaseModel):
    plan_id: str = Field(min_length=1)


class CheckoutResponse(BaseModel):
    checkout_url: str


@router.get("/billing/plan")
async def get_billing_plan(user: CurrentUser, session: DbSession) -> PlanResponse:
    """The caller's plan and where they stand against it."""
    row = (
        await session.execute(
            text("SELECT plan_id, plan_status, plan_renews_at FROM users WHERE id = :id"),
            {"id": user.id},
        )
    ).one()

    plan = get_plan(str(row.plan_id))
    used = await monthly_request_count(user.id)
    verdict = check_plan_limit(plan, used)

    return PlanResponse(
        plan_id=plan.id,
        plan_name=plan.name,
        plan_status=str(row.plan_status),
        monthly_request_limit=plan.monthly_request_limit,
        requests_this_month=used,
        fraction_used=round(verdict.fraction, 4),
        remaining=verdict.remaining,
        action=verdict.action.value,
        renews_at=row.plan_renews_at,
        available_plans=[
            PlanOption(
                id=option.id,
                name=option.name,
                monthly_request_limit=option.monthly_request_limit,
                price_usd=option.price_usd,
                purchasable=bool(option.stripe_price_id),
            )
            for option in PLANS.values()
        ],
    )


@router.post("/billing/checkout-session")
async def checkout_session(payload: CheckoutRequest, user: CurrentUser) -> CheckoutResponse:
    """Start a Stripe Checkout session for a plan change."""
    url = await create_checkout_session(user.id, user.email, payload.plan_id)
    return CheckoutResponse(checkout_url=url)


@router.post("/billing/webhook")
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(default="", alias="Stripe-Signature"),
) -> dict[str, Any]:
    """Apply a Stripe event — signature-verified and idempotent.

    Unauthenticated, because Stripe cannot hold a session. The signature *is*
    the authentication, so it is verified before the payload is parsed as
    anything meaningful, and a deployment with no webhook secret rejects
    everything rather than trusting it.

    Reads the raw body rather than a parsed model: the signature covers the
    exact bytes Stripe sent, and re-serialising a parsed body changes them.
    """
    body = await request.body()

    if not stripe_signature:
        raise StripeSignatureError("Missing Stripe-Signature header")

    event = verify_signature(body, stripe_signature, get_settings())
    result = await handle_webhook(event)

    _logger.info(
        "stripe_webhook_processed",
        subsystem="billing",
        event_type=result.event_type,
        applied=result.applied,
        reason=result.reason,
    )

    # Always 200 once verified. A 4xx or 5xx makes Stripe retry, and retrying
    # an event we have deliberately ignored is a queue that never drains.
    return {"received": True, "applied": result.applied, "reason": result.reason}
