"""Plan definitions and limit arithmetic — BUILD_SPEC §4 P10.

Free up to a request-volume cap, paid above it. The plan table lives here as
data rather than in the database because it is not user-editable and because
the proxy consults it on every request — a Postgres read to learn what a plan
allows would put the billing system on the hot path (hard rule 7).

Pure: no I/O, no ORM, no Stripe. What a plan *is*, separate from what Stripe
says about it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "FREE_PLAN_ID",
    "PLANS",
    "Plan",
    "PlanVerdict",
    "check_plan_limit",
    "get_plan",
]

FREE_PLAN_ID = "free"


class PlanAction(StrEnum):
    ALLOW = "allow"
    WARN = "warn"
    """Over the soft line. Served, with an upgrade prompt in the headers."""

    BLOCK = "block"


@dataclass(frozen=True)
class Plan:
    id: str
    name: str
    monthly_request_limit: int
    price_usd: float
    stripe_price_id: str = ""

    @property
    def is_metered(self) -> bool:
        """Whether the limit is enforced at all. 0 means unlimited."""
        return self.monthly_request_limit > 0


PLANS: dict[str, Plan] = {
    "free": Plan(
        id="free",
        name="Free",
        monthly_request_limit=10_000,
        price_usd=0.0,
    ),
    "pro": Plan(
        id="pro",
        name="Pro",
        monthly_request_limit=250_000,
        price_usd=19.0,
        stripe_price_id="price_pro_monthly",
    ),
    "scale": Plan(
        id="scale",
        name="Scale",
        monthly_request_limit=0,  # unlimited
        price_usd=99.0,
        stripe_price_id="price_scale_monthly",
    ),
}


def get_plan(plan_id: str | None) -> Plan:
    """Resolve a plan id, falling back to free.

    Falls back rather than raising, and falls back to the *most restrictive*
    plan. An unrecognised id means our data is wrong; the safe reading of that
    is not to hand out unlimited capacity.
    """
    return PLANS.get(plan_id or FREE_PLAN_ID, PLANS[FREE_PLAN_ID])


WARN_AT_FRACTION = 0.8


@dataclass(frozen=True)
class PlanVerdict:
    action: PlanAction
    plan_id: str
    limit: int
    used: int
    reason: str = ""

    @property
    def blocked(self) -> bool:
        return self.action is PlanAction.BLOCK

    @property
    def fraction(self) -> float:
        if self.limit <= 0:
            return 0.0
        return self.used / self.limit

    @property
    def remaining(self) -> int:
        if self.limit <= 0:
            return -1
        return max(0, self.limit - self.used)


def check_plan_limit(plan: Plan, requests_this_month: int) -> PlanVerdict:
    """Decide what this plan allows at this volume.

    **Over the cap is a warning, not a block, on the free plan.** Cutting off a
    developer's application mid-month because they crossed a volume line is a
    way to lose them permanently — and this is a cost-optimization product, so
    the traffic being blocked is traffic they are already paying a provider for.
    They get told, loudly and in the response headers, and they keep working.

    A block exists in the vocabulary because a paid plan that has lapsed is a
    different situation, and because an abusive account needs an off switch.
    Neither is reached by simply being popular.
    """
    if not plan.is_metered:
        return PlanVerdict(PlanAction.ALLOW, plan.id, 0, requests_this_month, "UNLIMITED")

    if requests_this_month >= plan.monthly_request_limit:
        return PlanVerdict(
            PlanAction.WARN,
            plan.id,
            plan.monthly_request_limit,
            requests_this_month,
            "OVER_PLAN_LIMIT",
        )

    if requests_this_month >= plan.monthly_request_limit * WARN_AT_FRACTION:
        return PlanVerdict(
            PlanAction.WARN,
            plan.id,
            plan.monthly_request_limit,
            requests_this_month,
            "APPROACHING_PLAN_LIMIT",
        )

    return PlanVerdict(
        PlanAction.ALLOW,
        plan.id,
        plan.monthly_request_limit,
        requests_this_month,
        "WITHIN_PLAN",
    )
