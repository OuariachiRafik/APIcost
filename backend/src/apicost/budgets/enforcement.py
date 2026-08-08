"""Budget enforcement on the proxy hot path — UC-29, UC-30.

**This is the one place in the system where fail-open does not apply**
(CLAUDE.md hard rule 1, BUILD_SPEC §4 P6). Everywhere else, a broken
optimization means the request goes to the provider unchanged. Here, a request
that goes to the provider unchanged is a request the user is billed for, and a
user who set a hard stop asked us to *not do that*. So an unreadable budget
state blocks — but only for projects that chose ``hard_stop``, and it is logged
loudly, because silently refusing traffic is its own kind of outage.

The check is Redis-only. Spend counters live at
``apicost:budget:{project_id}:{period}:{bucket}`` as INCRBYFLOAT values, written
by the proxy immediately after each request rather than by the worker draining
the ledger. That matters for the acceptance criterion: a hard stop must engage
within *one request* of the threshold, and a worker that runs every five seconds
would let hundreds through at production rates.

The counter is therefore an optimistic view — it can drift from the ledger if a
process dies between forwarding and incrementing. ``budgets/reconcile.py``
repairs it from Postgres, which is the authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from redis.asyncio import Redis

from apicost.core.logging import get_logger

__all__ = [
    "BUDGET_KEY_PREFIX",
    "BudgetAction",
    "BudgetDecision",
    "BudgetVerdict",
    "budget_counter_key",
    "check_budgets",
    "period_bucket",
    "record_spend",
]

_logger = get_logger(__name__)

BUDGET_KEY_PREFIX = "apicost:budget:"

WARN_AT_FRACTION = 0.8
"""Warn at 80% of a budget. A limit you only hear about once you have hit it is
a bill, not a budget."""


class BudgetAction(StrEnum):
    ALERT_ONLY = "alert_only"
    SOFT_THROTTLE = "soft_throttle"
    HARD_STOP = "hard_stop"


class BudgetDecision(StrEnum):
    ALLOW = "allow"
    THROTTLE = "throttle"
    """Serve, but force the cheapest equivalent model (UC-30, "throttle further
    requests"). Degrading is friendlier than refusing for a user who wanted a
    ceiling rather than a wall."""

    BLOCK = "block"


@dataclass(frozen=True)
class BudgetVerdict:
    decision: BudgetDecision = BudgetDecision.ALLOW
    reason: str = "NO_BUDGET"
    period: str | None = None
    limit_usd: float = 0.0
    spent_usd: float = 0.0
    warn: bool = False
    """Crossed the warning fraction but not the limit."""

    degraded: bool = False
    """The verdict was reached without readable state. Only ever set with
    BLOCK, and only for hard_stop projects."""

    @property
    def blocked(self) -> bool:
        return self.decision is BudgetDecision.BLOCK

    @property
    def fraction(self) -> float:
        if self.limit_usd <= 0:
            return 0.0
        return self.spent_usd / self.limit_usd


@dataclass(frozen=True)
class BudgetSpec:
    """A budget as carried in the cached auth resolution.

    Deliberately a plain value object, not the ORM model: this is read on every
    proxied request and must never imply a database query (hard rule 7).
    """

    period: str
    limit_usd: float
    action: BudgetAction

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> BudgetSpec | None:
        try:
            period = str(raw["period"])
            limit_usd = float(raw["limit_usd"])
            action = BudgetAction(str(raw.get("action", "alert_only")))
        except (KeyError, TypeError, ValueError):
            return None
        if period not in ("daily", "weekly", "monthly") or limit_usd <= 0:
            return None
        return cls(period=period, limit_usd=limit_usd, action=action)


@dataclass
class _Counters:
    values: dict[str, float] = field(default_factory=dict)


def period_bucket(period: str, at: datetime | None = None) -> str:
    """The identifier of the current period instance.

    UTC throughout. A budget boundary that moved with the user's timezone would
    need a per-user offset on the hot path, and daylight saving would give some
    users a 23-hour or 25-hour "day" twice a year — spend limits should not
    have leap hours.
    """
    now = at or datetime.now(UTC)
    if period == "daily":
        return now.strftime("%Y-%m-%d")
    if period == "weekly":
        # ISO week: a fixed 7 days that never straddles a month boundary
        # ambiguously, unlike "week of the month".
        iso = now.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    if period == "monthly":
        return now.strftime("%Y-%m")
    return "unknown"


def budget_counter_key(project_id: str, period: str, at: datetime | None = None) -> str:
    """Redis key for one project's spend in the current period instance.

    The bucket is part of the key rather than a field, so period rollover is
    free: at midnight the daily key simply changes and the new one starts at
    zero. No scheduled reset job to fail to run.
    """
    return f"{BUDGET_KEY_PREFIX}{project_id}:{period}:{period_bucket(period, at)}"


_TTL_SECONDS = {
    "daily": 60 * 60 * 24 * 2,
    "weekly": 60 * 60 * 24 * 9,
    "monthly": 60 * 60 * 24 * 40,
}
"""Each counter outlives its period by a margin, then expires on its own. The
margin covers a late-arriving ledger reconciliation; the expiry keeps Redis from
accumulating a key per project per day forever."""


async def check_budgets(
    redis: Redis,
    project_id: str,
    budgets: list[BudgetSpec],
    *,
    at: datetime | None = None,
) -> BudgetVerdict:
    """Decide whether this request may proceed. Redis only, never Postgres.

    Returns the *most restrictive* verdict across all of a project's budgets:
    a project with a daily alert and a monthly hard stop is stopped when the
    month is exhausted, regardless of where the day stands.
    """
    if not budgets:
        return BudgetVerdict()

    keys = [budget_counter_key(project_id, b.period, at) for b in budgets]

    try:
        raw = await redis.mget(keys)
    except Exception as exc:
        return _unreadable(project_id, budgets, exc)

    verdicts: list[BudgetVerdict] = []
    for budget, value in zip(budgets, raw, strict=True):
        spent = _as_float(value)
        verdicts.append(_verdict_for(budget, spent))

    # BLOCK > THROTTLE > ALLOW, and among equals the one furthest over.
    return max(verdicts, key=_severity)


def _severity(verdict: BudgetVerdict) -> tuple[int, float]:
    order = {BudgetDecision.ALLOW: 0, BudgetDecision.THROTTLE: 1, BudgetDecision.BLOCK: 2}
    return (order[verdict.decision], verdict.fraction)


def _verdict_for(budget: BudgetSpec, spent: float) -> BudgetVerdict:
    over = spent >= budget.limit_usd
    warn = not over and spent >= budget.limit_usd * WARN_AT_FRACTION

    if not over:
        return BudgetVerdict(
            decision=BudgetDecision.ALLOW,
            reason="WITHIN_BUDGET",
            period=budget.period,
            limit_usd=budget.limit_usd,
            spent_usd=spent,
            warn=warn,
        )

    decision = {
        BudgetAction.ALERT_ONLY: BudgetDecision.ALLOW,
        BudgetAction.SOFT_THROTTLE: BudgetDecision.THROTTLE,
        BudgetAction.HARD_STOP: BudgetDecision.BLOCK,
    }[budget.action]

    return BudgetVerdict(
        decision=decision,
        reason=f"BUDGET_EXCEEDED_{budget.action.value.upper()}",
        period=budget.period,
        limit_usd=budget.limit_usd,
        spent_usd=spent,
    )


def _unreadable(project_id: str, budgets: list[BudgetSpec], exc: Exception) -> BudgetVerdict:
    """Redis would not answer. Fail closed for hard_stop only.

    Loudly, per BUILD_SPEC §4 P6: this refuses a paying customer's traffic, and
    it must never be something an operator has to infer from a graph. The
    exception is logged by class and message only — it can carry a connection
    string, and hard rule 3 has no exceptions.
    """
    hard = [b for b in budgets if b.action is BudgetAction.HARD_STOP]

    if not hard:
        _logger.warning(
            "budget_state_unreadable_passthrough",
            subsystem="budgets",
            project_id=project_id,
            error_type=type(exc).__name__,
        )
        return BudgetVerdict(decision=BudgetDecision.ALLOW, reason="BUDGET_UNREADABLE_PASSTHROUGH")

    _logger.error(
        "budget_state_unreadable_failing_closed",
        subsystem="budgets",
        project_id=project_id,
        error_type=type(exc).__name__,
        periods=[b.period for b in hard],
    )
    return BudgetVerdict(
        decision=BudgetDecision.BLOCK,
        reason="BUDGET_UNREADABLE_FAIL_CLOSED",
        period=hard[0].period,
        limit_usd=hard[0].limit_usd,
        degraded=True,
    )


async def record_spend(
    redis: Redis,
    project_id: str,
    cost_usd: Decimal | float,
    periods: list[str],
    *,
    at: datetime | None = None,
) -> None:
    """Add one request's cost to every active period counter.

    Never raises. A failure here loses accuracy in the counter, which the
    reconciler repairs; letting it propagate would fail a request the user has
    already been charged for by the provider.
    """
    cost = float(cost_usd)
    if cost <= 0 or not periods:
        return

    try:
        pipe = redis.pipeline()
        for period in periods:
            key = budget_counter_key(project_id, period, at)
            pipe.incrbyfloat(key, cost)
            # Refreshed on every write rather than set once on creation: an
            # EXPIRE that raced a rollover could otherwise leave a live counter
            # with no TTL, and it would outlive its period silently.
            pipe.expire(key, _TTL_SECONDS.get(period, _TTL_SECONDS["monthly"]))
        await pipe.execute()
    except Exception as exc:
        _logger.warning(
            "budget_counter_write_failed",
            subsystem="budgets",
            project_id=project_id,
            error_type=type(exc).__name__,
        )


def _as_float(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
