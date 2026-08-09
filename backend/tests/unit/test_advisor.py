"""Unit tests for the advisor core — P8, UC-35/36/37.

The break-even tests are organised around the four defects BUILD_SPEC §6.7
requires fixing, because each one produces a *confidently wrong* number rather
than an error, and a number is exactly what a user acts on.
"""

from __future__ import annotations

import pytest

from apicost.advisor.breakeven import (
    HOURS_PER_MONTH,
    MIN_MONTHLY_TOKENS,
    GpuOption,
    break_even_analysis,
)
from apicost.advisor.downgrade import (
    MIN_SAMPLE,
    DowngradeCandidate,
    recommend_downgrades,
)

GPU = GpuOption(name="A10G", cost_per_hour_usd=1.0, max_tokens_per_second=1000.0)

# One GPU at 50% utilisation: 1000 tok/s * 3600 * 730 * 0.5
CAPACITY = 1000.0 * 3600.0 * HOURS_PER_MONTH * 0.5
MONTHLY_GPU_COST = 1.0 * HOURS_PER_MONTH


# -- Fix 2: no advice below a meaningful volume ------------------------------


def test_a_user_with_no_traffic_is_not_told_to_buy_a_gpu() -> None:
    """The supplied formula gave n_gpus=0, so gpu_cost=0, so "self-host"."""
    result = break_even_analysis(0, 0.0, GPU)

    assert result.recommendation == "insufficient_data"
    assert result.n_gpus == 0
    assert result.monthly_saving_usd == 0.0


def test_a_trickle_of_traffic_is_also_insufficient() -> None:
    result = break_even_analysis(MIN_MONTHLY_TOKENS - 1, 0.000002, GPU)
    assert result.recommendation == "insufficient_data"


def test_the_insufficient_data_answer_still_explains_itself() -> None:
    result = break_even_analysis(0, 0.0, GPU)
    assert result.caveats
    assert any("not enough" in caveat for caveat in result.caveats)


# -- Fix 3: utilization ------------------------------------------------------


def test_utilization_changes_how_many_gpus_are_needed() -> None:
    """Assuming 100% of peak for 730 hours understates the GPU count."""
    tokens = int(CAPACITY * 1.5)

    realistic = break_even_analysis(tokens, 0.00001, GPU, utilization=0.5)
    optimistic = break_even_analysis(tokens, 0.00001, GPU, utilization=1.0)

    assert realistic.n_gpus > optimistic.n_gpus
    assert realistic.gpu_monthly_cost_usd > optimistic.gpu_monthly_cost_usd


def test_the_utilization_assumption_is_stated_in_the_payload() -> None:
    result = break_even_analysis(int(CAPACITY), 0.00001, GPU, utilization=0.5)
    assert any("50%" in caveat for caveat in result.caveats)


def test_utilization_is_clamped_to_something_sane() -> None:
    assert break_even_analysis(10_000_000, 0.00001, GPU, utilization=0.0).n_gpus >= 1
    assert break_even_analysis(10_000_000, 0.00001, GPU, utilization=5.0).n_gpus >= 1


# -- Fix 1: the step function ------------------------------------------------


def test_gpu_cost_is_a_step_function() -> None:
    """One token past capacity costs a whole second GPU."""
    just_under = break_even_analysis(int(CAPACITY) - 1000, 0.00001, GPU)
    just_over = break_even_analysis(int(CAPACITY) + 1000, 0.00001, GPU)

    assert just_under.n_gpus == 1
    assert just_over.n_gpus == 2
    assert just_over.gpu_monthly_cost_usd == pytest.approx(2 * MONTHLY_GPU_COST)


def test_break_even_is_none_exactly_where_the_naive_formula_invents_one() -> None:
    """The heart of fix 1, and the sharpest form of it.

    Solving the step function collapses: within step n the lines cross at
    `n * step_cost / cpt`, which is inside the step only if
    `step_cost / cpt <= capacity` — the n cancels. So self-hosting either wins
    from the first GPU or never wins at all.

    The naive formula's answer exceeds one GPU's capacity *exactly* when no
    break-even exists. Its one wrong case is the case where the honest answer
    is "never", and it instead reports a volume the user could aim for.
    """
    # GPU cost per token of capacity is 730 / 1.314e9 = 5.56e-7.
    cost_per_token = 2e-7  # cheaper than that, so a GPU can never pay off
    naive = MONTHLY_GPU_COST / cost_per_token

    assert naive > CAPACITY, "test premise: the naive answer exceeds one GPU"

    result = break_even_analysis(int(CAPACITY), cost_per_token, GPU)

    assert result.break_even_tokens is None, (
        f"reported a break-even of {result.break_even_tokens} where none exists; "
        f"the naive formula would have said {naive:.3e}"
    )
    assert result.recommendation == "api"


def test_a_real_break_even_is_actually_break_even() -> None:
    """When one is reported, self-hosting must genuinely win there."""
    cost_per_token = 2e-6  # dearer than the GPU's per-token capacity cost
    result = break_even_analysis(int(CAPACITY), cost_per_token, GPU)

    assert result.break_even_tokens is not None
    at = break_even_analysis(result.break_even_tokens, cost_per_token, GPU)
    assert at.gpu_monthly_cost_usd <= at.api_monthly_cost_usd, (
        f"break_even_tokens={result.break_even_tokens} is not break-even: "
        f"gpu ${at.gpu_monthly_cost_usd} vs api ${at.api_monthly_cost_usd}"
    )


def test_whether_a_break_even_exists_does_not_depend_on_volume() -> None:
    """The n-cancels property, asserted rather than assumed.

    A regression that reintroduced per-step search would likely make this vary.
    """
    cost_per_token = 2e-6
    answers = {
        break_even_analysis(int(CAPACITY * multiple), cost_per_token, GPU).break_even_tokens
        for multiple in (0.8, 1.0, 2.5, 10.0)
    }
    assert len(answers) == 1, f"break-even moved with volume: {answers}"


def test_the_reported_break_even_is_the_smallest_one() -> None:
    """Just below it the API must still win, or it is not the break-even."""
    cost_per_token = 0.000002
    result = break_even_analysis(int(CAPACITY), cost_per_token, GPU)
    assert result.break_even_tokens is not None

    below = break_even_analysis(int(result.break_even_tokens * 0.9), cost_per_token, GPU)
    assert below.recommendation == "api"


def test_capacity_is_reported_so_the_steps_can_be_drawn() -> None:
    result = break_even_analysis(int(CAPACITY), 0.00001, GPU)
    assert result.capacity_tokens_per_gpu == pytest.approx(CAPACITY)


def test_no_break_even_when_the_api_is_simply_cheaper() -> None:
    """A vanishingly cheap API never justifies a GPU at any volume.

    Every extra GPU adds a fixed cost, and if the API's marginal cost per token
    never catches it, there is no crossing to report. `None` is the honest
    answer; a number would imply a volume worth reaching for.
    """
    result = break_even_analysis(int(CAPACITY), 1e-12, GPU)
    assert result.break_even_tokens is None
    assert result.recommendation == "api"


# -- Fix 4: the caveats ------------------------------------------------------


def test_every_result_carries_the_caveats_in_the_payload() -> None:
    """Not left to the UI to remember (BUILD_SPEC §6.7)."""
    result = break_even_analysis(int(CAPACITY * 3), 0.0001, GPU)

    assert result.recommendation == "gpu"
    assert len(result.caveats) >= 5

    joined = " ".join(result.caveats).lower()
    for topic in ("time", "idle", "quality", "outage"):
        assert topic in joined, f"no caveat mentions {topic}"


def test_a_favourable_result_is_still_caveated() -> None:
    result = break_even_analysis(int(CAPACITY * 10), 0.001, GPU)
    assert result.recommendation == "gpu"
    assert result.monthly_saving_usd > 0
    assert result.caveats


# -- Sanity ------------------------------------------------------------------


def test_the_recommendation_matches_the_arithmetic() -> None:
    cheap_api = break_even_analysis(10_000_000, 0.0000001, GPU)
    assert cheap_api.recommendation == "api"
    assert cheap_api.monthly_saving_usd < 0

    dear_api = break_even_analysis(10_000_000, 0.001, GPU)
    assert dear_api.recommendation == "gpu"
    assert dear_api.monthly_saving_usd > 0


def test_hours_per_month_is_the_average_not_thirty_days() -> None:
    assert pytest.approx(730.0) == HOURS_PER_MONTH


# -- UC-35 / UC-37: downgrades ----------------------------------------------


def _candidate(**overrides: object) -> DowngradeCandidate:
    base = {
        "endpoint": "/v1/chat/completions",
        "model_requested": "gpt-4o",
        "cheap_model": "gpt-4o-mini",
        "total_requests": 1000,
        "cheap_requests": 400,
        "escalated_requests": 0,
        "avg_cost_requested_usd": 0.01,
        "avg_cost_cheap_usd": 0.001,
    }
    base.update(overrides)
    return DowngradeCandidate(**base)  # type: ignore[arg-type]


def test_a_well_evidenced_downgrade_is_recommended() -> None:
    [rec] = recommend_downgrades([_candidate()])

    assert rec.from_model == "gpt-4o"
    assert rec.to_model == "gpt-4o-mini"
    assert rec.confidence == "high"
    assert rec.sample_size == 400
    # 600 requests still on the dear model, saving $0.009 each.
    assert rec.projected_savings_usd == pytest.approx(5.4)


def test_a_small_sample_is_not_recommended_however_good_it_looks() -> None:
    """Six successes in a row is a coin landing heads six times."""
    assert recommend_downgrades([_candidate(cheap_requests=MIN_SAMPLE - 1)]) == []


def test_a_high_escalation_rate_blocks_the_recommendation() -> None:
    """Escalation means the cheap tier was not good enough."""
    assert recommend_downgrades([_candidate(escalated_requests=50)]) == []


def test_confidence_tracks_sample_size() -> None:
    low = recommend_downgrades([_candidate(cheap_requests=40)])[0]
    medium = recommend_downgrades([_candidate(cheap_requests=100)])[0]
    high = recommend_downgrades([_candidate(cheap_requests=500)])[0]

    assert (low.confidence, medium.confidence, high.confidence) == ("low", "medium", "high")


def test_nothing_is_recommended_when_it_is_already_being_done() -> None:
    assert recommend_downgrades([_candidate(total_requests=400, cheap_requests=400)]) == []


def test_a_downgrade_that_saves_nothing_is_not_recommended() -> None:
    assert recommend_downgrades([_candidate(avg_cost_cheap_usd=0.01)]) == []
    assert recommend_downgrades([_candidate(avg_cost_cheap_usd=0.05)]) == []


def test_recommendations_are_ordered_by_projected_saving() -> None:
    """UC-37: the number is the point, so it decides the order."""
    small = _candidate(endpoint="/small", avg_cost_requested_usd=0.002)
    large = _candidate(endpoint="/large", avg_cost_requested_usd=0.10)

    ordered = recommend_downgrades([small, large])
    assert [r.endpoint for r in ordered] == ["/large", "/small"]
    assert ordered[0].projected_savings_usd > ordered[1].projected_savings_usd


def test_the_rationale_quotes_the_evidence() -> None:
    [rec] = recommend_downgrades([_candidate()])

    assert "400" in rec.rationale
    assert "gpt-4o-mini" in rec.rationale
    assert "600" in rec.rationale


def test_no_candidates_means_no_recommendations() -> None:
    assert recommend_downgrades([]) == []
