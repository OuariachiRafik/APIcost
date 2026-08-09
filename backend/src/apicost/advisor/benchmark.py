"""Anonymized peer benchmark — UC-39, BUILD_SPEC §4 P9.

"How does my cost per request compare to everyone else's?" is a genuinely
useful question and a genuinely dangerous feature. Every comparison against a
group is a small disclosure about that group, and enough small disclosures
reconstruct an individual.

Two rules, and the code is arranged so neither can be skipped:

1. **Never publish a cohort statistic below the minimum cohort size**
   (BUILD_SPEC §4 P9: ≥50 users). Below it the endpoint says so and returns no
   numbers at all — not a rounded number, not a wider band, nothing.
2. **Only ever aggregates.** No cohort member is identified, counted
   individually, or made distinguishable. The caller's own figures come from
   their own data; everything else is a percentile over the cohort.

The subtle one is the interaction between them. A cohort of exactly 50 where
the caller is the largest spender means the maximum is the caller's own number,
so publishing a max would tell them nothing new — but publishing it to the
*other* 49 discloses the caller's spend. So this reports percentiles from the
interior of the distribution (p25/p50/p75) and never the extremes.

Pure by CLAUDE.md §Style — the SQL lives in the router; this decides what may
be said.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "MIN_COHORT_SIZE",
    "CohortStats",
    "PeerComparison",
    "compare_to_peers",
]

MIN_COHORT_SIZE = 50
"""BUILD_SPEC §4 P9. Below this, no statistic is published.

This is k-anonymity with k=50. It is deliberately checked in one place and
returns early, so there is no code path that computes a number first and
decides whether to show it afterwards — the number is never computed."""


@dataclass(frozen=True)
class CohortStats:
    """Percentiles over the cohort. Interior only, never min or max."""

    size: int
    p25_cost_per_request: float
    p50_cost_per_request: float
    p75_cost_per_request: float


@dataclass(frozen=True)
class PeerComparison:
    available: bool
    reason: str
    your_cost_per_request: float
    your_requests: int

    cohort_size: int = 0
    cohort_p25: float = 0.0
    cohort_p50: float = 0.0
    cohort_p75: float = 0.0

    percentile_band: str = ""
    """Which quartile the caller falls in, as a band rather than an exact rank.

    An exact percentile is a finer disclosure than it looks: watched over time
    it moves as other accounts join and leave, and the movement leaks their
    magnitude. A band is what the user can act on anyway."""

    verdict: str = ""


def compare_to_peers(
    your_cost_per_request: float,
    your_requests: int,
    cohort: CohortStats | None,
    *,
    min_cohort_size: int = MIN_COHORT_SIZE,
) -> PeerComparison:
    """Compare one account against its cohort, or refuse to.

    ``cohort`` is ``None`` when the query found too few accounts to aggregate.
    """
    if your_requests <= 0:
        return PeerComparison(
            available=False,
            reason="NO_TRAFFIC",
            your_cost_per_request=0.0,
            your_requests=0,
        )

    if cohort is None or cohort.size < min_cohort_size:
        # No numbers. Not rounded, not banded, not "approximately" — the
        # comparison simply does not exist yet.
        return PeerComparison(
            available=False,
            reason="COHORT_TOO_SMALL",
            your_cost_per_request=your_cost_per_request,
            your_requests=your_requests,
            cohort_size=0,
        )

    band, verdict = _band(your_cost_per_request, cohort)

    return PeerComparison(
        available=True,
        reason="OK",
        your_cost_per_request=your_cost_per_request,
        your_requests=your_requests,
        cohort_size=cohort.size,
        cohort_p25=cohort.p25_cost_per_request,
        cohort_p50=cohort.p50_cost_per_request,
        cohort_p75=cohort.p75_cost_per_request,
        percentile_band=band,
        verdict=verdict,
    )


def _band(cost: float, cohort: CohortStats) -> tuple[str, str]:
    """Which quartile, and what it means in words.

    Cheaper is better here, so the wording is inverted relative to the band:
    being in the bottom quartile of cost is the good end.
    """
    if cost <= cohort.p25_cost_per_request:
        return "bottom_quartile", "Your cost per request is lower than most comparable accounts."
    if cost <= cohort.p50_cost_per_request:
        return "below_median", "Your cost per request is below the median for comparable accounts."
    if cost <= cohort.p75_cost_per_request:
        return "above_median", "Your cost per request is above the median for comparable accounts."
    return (
        "top_quartile",
        "Your cost per request is higher than most comparable accounts. "
        "Caching and routing are where that usually comes from.",
    )
