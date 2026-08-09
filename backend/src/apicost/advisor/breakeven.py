"""Self-hosting vs pay-per-token — UC-36, BUILD_SPEC §6.7.

At the user's actual monthly volume, would a dedicated GPU deployment cost less
than the API? The arithmetic is easy. Being *honest* about it is the hard part,
and most of this module is the honesty.

BUILD_SPEC §6.7 lists four defects in the supplied formula. All four are fixed
here and each is marked at the point it is fixed:

1. `break_even_tokens` ignored `n_gpus`, so it was wrong above one GPU's
   capacity. GPU cost is a **step function** — you buy whole GPUs — so the true
   break-even is piecewise.
2. `n_gpus` was 0 at zero volume, giving a GPU cost of 0 and recommending
   self-hosting to someone with no traffic.
3. It assumed a GPU sustains peak throughput for all 730 hours of a month.
4. It reported a number with none of the caveats that decide whether the number
   means anything.

Pure by CLAUDE.md §Style: no I/O, no ORM. Volume and blended price come from
the ledger; the GPU price table is passed in.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

__all__ = [
    "HOURS_PER_MONTH",
    "MIN_MONTHLY_TOKENS",
    "BreakEvenResult",
    "GpuOption",
    "break_even_analysis",
]

HOURS_PER_MONTH = 730.0
"""365 * 24 / 12. Not 720 — a 30-day month understates monthly GPU cost by 1.4%,
which is the wrong direction for a recommendation that costs the user money if
it is optimistic."""

MIN_MONTHLY_TOKENS = 1_000_000
"""**Fix 2.** Below a million tokens a month the API bill is a few dollars and
no GPU recommendation is meaningful. The supplied formula computed `n_gpus = 0`
at zero volume, hence a GPU cost of $0, hence "self-host" — advice that is both
wrong and confidently stated."""

DEFAULT_UTILIZATION = 0.5
"""**Fix 3.** The fraction of peak throughput a GPU actually sustains.

Assuming `max_tokens_per_second` for all 730 hours means assuming traffic
arrives perfectly evenly and the model never idles between requests. Real
traffic is diurnal and bursty. 0.5 is still generous for a solo developer whose
load follows their users' working day."""


@dataclass(frozen=True)
class GpuOption:
    """One instance type from the maintained price table."""

    name: str
    cost_per_hour_usd: float
    max_tokens_per_second: float

    def monthly_cost(self, n_gpus: int) -> float:
        return n_gpus * self.cost_per_hour_usd * HOURS_PER_MONTH

    def capacity_tokens_per_month(self, utilization: float) -> float:
        """Tokens one GPU can serve in a month at a realistic duty cycle."""
        return self.max_tokens_per_second * 3600.0 * HOURS_PER_MONTH * utilization


@dataclass(frozen=True)
class BreakEvenResult:
    recommendation: str
    """``api`` | ``gpu`` | ``insufficient_data``."""

    monthly_tokens: int
    api_monthly_cost_usd: float
    gpu_monthly_cost_usd: float
    n_gpus: int
    gpu_option: str

    break_even_tokens: int | None
    """Monthly volume at which self-hosting starts to win, accounting for the
    step function. ``None`` when no volume makes it win at this price."""

    capacity_tokens_per_gpu: float
    """So the dashboard can draw the steps rather than a straight line."""

    monthly_saving_usd: float
    """Positive means self-hosting is cheaper. Can be negative."""

    caveats: list[str] = field(default_factory=list)
    """**Fix 4.** Shipped *in the payload*, not left to the UI to remember."""


CAVEATS = [
    "This compares infrastructure cost only. It does not price your time: "
    "deploying, monitoring, patching, and being on call for a GPU is ongoing work.",
    "A dedicated GPU bills continuously, including while idle overnight and at "
    "weekends. The API bills only for what you use.",
    "An open-weights model you host is not the model you are calling today. "
    "Output quality will differ, and the difference may matter more than the cost.",
    "You lose the provider's availability guarantees. If your GPU host has an "
    "outage, you have the outage.",
    "Cold starts, model loading, and failover capacity are not in this number.",
    "Throughput is assumed at {utilization:.0%} of peak. If your traffic is "
    "burstier than that, you will need more GPUs than shown.",
]


def _render_caveats(utilization: float) -> list[str]:
    return [caveat.format(utilization=utilization) for caveat in CAVEATS]


def break_even_analysis(
    monthly_tokens: int,
    cost_per_token_usd: float,
    gpu: GpuOption,
    *,
    utilization: float = DEFAULT_UTILIZATION,
    min_monthly_tokens: int = MIN_MONTHLY_TOKENS,
) -> BreakEvenResult:
    """Compare the user's API spend against a self-hosted deployment.

    ``cost_per_token_usd`` is the *blended* rate from the ledger — total spend
    over total tokens — so it already reflects the model mix actually used
    rather than a list price for one model.
    """
    utilization = min(max(utilization, 0.01), 1.0)
    capacity = gpu.capacity_tokens_per_month(utilization)

    # -- Fix 2: refuse to advise below a meaningful volume -------------------
    if monthly_tokens < min_monthly_tokens or cost_per_token_usd <= 0:
        return BreakEvenResult(
            recommendation="insufficient_data",
            monthly_tokens=monthly_tokens,
            api_monthly_cost_usd=round(monthly_tokens * max(cost_per_token_usd, 0.0), 2),
            gpu_monthly_cost_usd=0.0,
            n_gpus=0,
            gpu_option=gpu.name,
            break_even_tokens=_break_even_tokens(gpu, cost_per_token_usd, capacity),
            capacity_tokens_per_gpu=capacity,
            monthly_saving_usd=0.0,
            caveats=[
                f"Below {min_monthly_tokens:,} tokens a month there is not enough "
                "volume for a self-hosting comparison to mean anything.",
                *_render_caveats(utilization),
            ],
        )

    # You buy whole GPUs. Serving 1.01 GPUs' worth of traffic costs two.
    n_gpus = max(1, math.ceil(monthly_tokens / capacity))

    api_cost = monthly_tokens * cost_per_token_usd
    gpu_cost = gpu.monthly_cost(n_gpus)

    return BreakEvenResult(
        recommendation="gpu" if gpu_cost < api_cost else "api",
        monthly_tokens=monthly_tokens,
        api_monthly_cost_usd=round(api_cost, 2),
        gpu_monthly_cost_usd=round(gpu_cost, 2),
        n_gpus=n_gpus,
        gpu_option=gpu.name,
        break_even_tokens=_break_even_tokens(gpu, cost_per_token_usd, capacity),
        capacity_tokens_per_gpu=capacity,
        monthly_saving_usd=round(api_cost - gpu_cost, 2),
        caveats=_render_caveats(utilization),
    )


def _break_even_tokens(gpu: GpuOption, cost_per_token_usd: float, capacity: float) -> int | None:
    """**Fix 1.** The smallest volume at which self-hosting wins, step-aware.

    The supplied formula was ``one GPU's monthly cost / cost_per_token``, which
    silently assumes one GPU serves any volume. It does not: past `capacity`
    you buy another and the cost jumps.

    Solving it properly turns out to collapse. Inside step ``n`` the GPU cost is
    flat at ``n * step_cost`` while the API cost rises linearly, so they cross
    at ``n * step_cost / cost_per_token``. That crossing is real only if it
    falls inside the step, i.e. ``n * step_cost / cpt <= n * capacity`` — and
    **the n cancels**. Whether a crossing exists does not depend on how many
    GPUs you buy, because cost and capacity both scale linearly with them. It
    reduces to a comparison of two per-token prices:

        GPU cost per token of capacity  =  step_cost / capacity
        API cost per token              =  cost_per_token

    So either self-hosting wins from the first GPU, or it never wins at all.

    That is precisely where the naive formula does its damage. Its answer
    exceeds one GPU's capacity exactly when ``step_cost / cpt > capacity`` —
    which is the condition for there being **no break-even**. The one case it
    gets wrong is the case where it should have said "never", and instead it
    confidently reports a volume the user could aim for.
    """
    if cost_per_token_usd <= 0 or capacity <= 0:
        return None

    step_cost = gpu.monthly_cost(1)
    gpu_cost_per_token = step_cost / capacity

    if cost_per_token_usd <= gpu_cost_per_token:
        return None

    return math.ceil(step_cost / cost_per_token_usd)
