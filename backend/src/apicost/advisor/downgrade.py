"""Cheaper-tier recommendations from the user's own history — UC-35, UC-37.

The claim this makes is narrow on purpose: *on this endpoint, requests that
were routed to the cheap tier came back good enough that escalation never
fired.* That is evidence from the user's own traffic, not a model's opinion
about their prompts.

Which means the recommendation is only available where routing has already run
and produced a record. A project that has never enabled routing gets no
downgrade advice, and that is the honest answer — we would be guessing.

Confidence comes from sample size, because the failure mode here is
recommending a downgrade off six lucky requests. Every recommendation carries
its observed sample and a projected dollar impact (UC-37) so the user can weigh
it rather than take it on faith.

Pure — no I/O, no ORM. The nightly job supplies aggregates.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "MIN_SAMPLE",
    "DowngradeCandidate",
    "DowngradeRecommendation",
    "recommend_downgrades",
]

MIN_SAMPLE = 30
"""Below 30 cheap-tier requests on an endpoint there is nothing to conclude.
Six successes in a row is a coin landing heads six times."""

HIGH_CONFIDENCE_SAMPLE = 200
MEDIUM_CONFIDENCE_SAMPLE = 75

MAX_ESCALATION_RATE = 0.02
"""If more than 2% of cheap-tier requests on an endpoint had to be escalated,
the cheap tier is not reliably good enough there. Escalation already cost the
user two calls for those; recommending more of them would compound it."""


@dataclass(frozen=True)
class DowngradeCandidate:
    """One endpoint/model pair as observed in the ledger."""

    endpoint: str
    model_requested: str
    cheap_model: str

    total_requests: int
    """All requests on this endpoint for this requested model."""

    cheap_requests: int
    """Of those, how many actually ran on the cheap tier."""

    escalated_requests: int
    """Of the cheap ones, how many had to be retried on the strong model."""

    avg_cost_requested_usd: float
    avg_cost_cheap_usd: float

    @property
    def escalation_rate(self) -> float:
        if self.cheap_requests <= 0:
            return 1.0
        return self.escalated_requests / self.cheap_requests

    @property
    def not_yet_downgraded(self) -> int:
        """Requests still paying full price that this would move."""
        return max(0, self.total_requests - self.cheap_requests)


@dataclass(frozen=True)
class DowngradeRecommendation:
    endpoint: str
    from_model: str
    to_model: str
    confidence: str
    """``high`` | ``medium`` | ``low``."""

    sample_size: int
    escalation_rate: float
    projected_savings_usd: float
    """UC-37. Monthly, over the same traffic that was observed."""

    rationale: str


def recommend_downgrades(
    candidates: list[DowngradeCandidate],
    *,
    min_sample: int = MIN_SAMPLE,
    max_escalation_rate: float = MAX_ESCALATION_RATE,
) -> list[DowngradeRecommendation]:
    """Turn observed history into recommendations, best saving first."""
    recommendations: list[DowngradeRecommendation] = []

    for candidate in candidates:
        if candidate.cheap_requests < min_sample:
            continue
        if candidate.escalation_rate > max_escalation_rate:
            continue
        if candidate.not_yet_downgraded <= 0:
            # Everything already runs cheap. Nothing to recommend, and telling
            # the user to do what they are already doing costs their trust in
            # every other recommendation on the page.
            continue

        per_request_saving = candidate.avg_cost_requested_usd - candidate.avg_cost_cheap_usd
        if per_request_saving <= 0:
            continue

        projected = per_request_saving * candidate.not_yet_downgraded

        recommendations.append(
            DowngradeRecommendation(
                endpoint=candidate.endpoint,
                from_model=candidate.model_requested,
                to_model=candidate.cheap_model,
                confidence=_confidence(candidate.cheap_requests),
                sample_size=candidate.cheap_requests,
                escalation_rate=round(candidate.escalation_rate, 4),
                projected_savings_usd=round(projected, 6),
                rationale=(
                    f"{candidate.cheap_requests:,} requests on {candidate.endpoint} already ran "
                    f"on {candidate.cheap_model}, and {_escalation_phrase(candidate)}. "
                    f"{candidate.not_yet_downgraded:,} requests are still using "
                    f"{candidate.model_requested}."
                ),
            )
        )

    return sorted(recommendations, key=lambda r: -r.projected_savings_usd)


def _confidence(sample: int) -> str:
    if sample >= HIGH_CONFIDENCE_SAMPLE:
        return "high"
    if sample >= MEDIUM_CONFIDENCE_SAMPLE:
        return "medium"
    return "low"


def _escalation_phrase(candidate: DowngradeCandidate) -> str:
    if candidate.escalated_requests == 0:
        return "none needed escalating to a stronger model"
    return f"{candidate.escalated_requests} needed escalating ({candidate.escalation_rate:.1%})"
