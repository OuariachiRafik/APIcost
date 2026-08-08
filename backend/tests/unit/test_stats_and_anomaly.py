"""Unit tests for the pure statistics and detection core — P6."""

from __future__ import annotations

import math

import pytest

from apicost.anomaly.forest import (
    PatternFeatures,
    _entropy,
    detect,
    features_from_rows,
)
from apicost.anomaly.zscore import (
    DEFAULT_MIN_OBSERVATIONS,
    MIN_ABSOLUTE_RATE_USD,
    score,
)
from apicost.budgets.enforcement import (
    BudgetAction,
    BudgetSpec,
    budget_counter_key,
    period_bucket,
)
from apicost.stats.rolling import RollingStats, observe, rolling_from_dict
from apicost.stats.welford import (
    WelfordState,
    from_dict,
    merge,
    stddev,
    update,
    variance,
)

# -- Welford ----------------------------------------------------------------


def _fold(values: list[float]) -> WelfordState:
    state = WelfordState()
    for value in values:
        state = update(state, value)
    return state


def test_welford_matches_the_textbook_definition() -> None:
    values = [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]
    state = _fold(values)

    expected_mean = sum(values) / len(values)
    expected_var = sum((v - expected_mean) ** 2 for v in values) / (len(values) - 1)

    assert state.count == 8
    assert state.mean == pytest.approx(expected_mean)
    assert variance(state) == pytest.approx(expected_var)


def test_welford_survives_the_case_that_breaks_the_naive_formula() -> None:
    """Large mean, tiny spread — where E[x^2] - E[x]^2 loses its digits.

    The naive form computes ~1e18 - 1e18 here and returns garbage, sometimes
    negative. This is the entire reason the module exists, so it is worth
    asserting rather than trusting.
    """
    values = [1e9 + i for i in range(100)]
    state = _fold(values)

    mean = sum(values) / len(values)
    expected = sum((v - mean) ** 2 for v in values) / (len(values) - 1)

    assert variance(state) == pytest.approx(expected, rel=1e-9)
    assert variance(state) > 0


def test_variance_is_zero_below_two_observations() -> None:
    assert variance(WelfordState()) == 0.0
    assert variance(update(WelfordState(), 5.0)) == 0.0


def test_stddev_never_raises_on_a_constant_series() -> None:
    state = _fold([3.0] * 50)
    assert stddev(state) == 0.0
    assert not math.isnan(stddev(state))


def test_merge_equals_folding_everything_in_one_pass() -> None:
    left = _fold([1.0, 2.0, 3.0, 4.0])
    right = _fold([10.0, 11.0, 12.0])
    combined = merge(left, right)
    single = _fold([1.0, 2.0, 3.0, 4.0, 10.0, 11.0, 12.0])

    assert combined.count == single.count
    assert combined.mean == pytest.approx(single.mean)
    assert variance(combined) == pytest.approx(variance(single))


def test_merge_with_an_empty_state_is_identity() -> None:
    state = _fold([1.0, 2.0, 3.0])
    assert merge(state, WelfordState()) == state
    assert merge(WelfordState(), state) == state


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"count": "not a number", "mean": 1.0, "m2": 1.0},
        {"count": -5, "mean": 1.0, "m2": 1.0},
        {"count": 3, "mean": float("nan"), "m2": 1.0},
        {"count": 3, "mean": 1.0, "m2": -2.0},
    ],
)
def test_a_corrupt_checkpoint_resets_rather_than_raising(raw: dict[str, object]) -> None:
    assert from_dict(raw) == WelfordState()


# -- Rolling windows --------------------------------------------------------


def test_the_first_request_opens_a_window_without_closing_one() -> None:
    result = observe(RollingStats(), cost_usd=0.01, at=1000.0)
    assert result.closed_rate is None
    assert result.stats.window_requests == 1
    assert result.stats.baseline.count == 0


def test_requests_inside_a_window_accumulate() -> None:
    stats = observe(RollingStats(), cost_usd=0.01, at=1000.0).stats
    stats = observe(stats, cost_usd=0.02, at=1030.0).stats

    assert stats.window_requests == 2
    assert stats.window_cost == pytest.approx(0.03)
    assert stats.baseline.count == 0


def test_crossing_the_boundary_closes_the_window_into_the_baseline() -> None:
    stats = observe(RollingStats(), cost_usd=0.05, at=1000.0).stats
    result = observe(stats, cost_usd=0.01, at=1061.0)

    assert result.closed_rate == pytest.approx(0.05)
    assert result.closed_requests == 1
    assert result.stats.baseline.count == 1
    assert result.stats.window_cost == pytest.approx(0.01)


def test_idle_gaps_do_not_become_zero_observations() -> None:
    """A project quiet overnight must not have its baseline dragged to zero.

    If silence counted, a morning's normal traffic would look like a spike.
    """
    stats = observe(RollingStats(), cost_usd=0.05, at=1000.0).stats
    result = observe(stats, cost_usd=0.05, at=1000.0 + 86_400)

    assert result.stats.baseline.count == 1, "one closed window, not 1440 empty ones"


def test_rolling_state_round_trips_through_a_checkpoint() -> None:
    stats = observe(RollingStats(), cost_usd=0.05, at=1000.0).stats
    stats = observe(stats, cost_usd=0.02, at=1061.0).stats

    assert rolling_from_dict(stats.to_dict()) == stats


@pytest.mark.parametrize("raw", [None, {}, {"baseline": "nonsense"}, {"window_cost": -1}])
def test_a_corrupt_rolling_checkpoint_resets(raw: object) -> None:
    rebuilt = rolling_from_dict(raw)  # type: ignore[arg-type]
    assert rebuilt.window_cost == 0.0


# -- z-score ----------------------------------------------------------------


def _baseline(mean: float, count: int = 60, spread: float = 0.01) -> WelfordState:
    state = WelfordState()
    for index in range(count):
        state = update(state, mean + (spread if index % 2 else -spread))
    return state


def test_cold_start_never_fires() -> None:
    baseline = _baseline(0.10, count=DEFAULT_MIN_OBSERVATIONS - 1)
    verdict = score(baseline, 50.0)

    assert not verdict.anomalous
    assert verdict.reason == "COLD_START"


def test_a_clear_spike_fires() -> None:
    verdict = score(_baseline(0.10), 5.0)

    assert verdict.anomalous
    assert verdict.reason == "SPEND_SPIKE"
    assert verdict.z > 3.0
    assert verdict.multiple == pytest.approx(50.0, rel=0.05)


def test_normal_traffic_does_not_fire() -> None:
    verdict = score(_baseline(0.10), 0.105)
    assert not verdict.anomalous
    assert verdict.reason == "WITHIN_THRESHOLD"


def test_a_trivial_absolute_amount_never_fires_however_relatively_large() -> None:
    """The guard that stops us paging someone about two tenths of a cent."""
    baseline = _baseline(0.00001, spread=0.000001)
    verdict = score(baseline, 0.002)

    assert verdict.rate_usd < MIN_ABSOLUTE_RATE_USD
    assert not verdict.anomalous
    assert verdict.reason == "BELOW_ABSOLUTE_FLOOR"


def test_a_perfectly_flat_baseline_still_detects_a_real_jump() -> None:
    """Scheduled jobs produce zero variance; sigma is 0 and z is undefined."""
    flat = WelfordState()
    for _ in range(60):
        flat = update(flat, 0.50)

    assert score(flat, 0.55).anomalous is False
    fired = score(flat, 5.0)
    assert fired.anomalous
    assert fired.reason == "FLAT_BASELINE_EXCEEDED"


def test_a_drop_in_spend_is_not_an_anomaly() -> None:
    assert not score(_baseline(1.0), 0.0001).anomalous


# -- IsolationForest --------------------------------------------------------


def test_entropy_is_zero_for_a_single_category() -> None:
    assert _entropy([10]) == 0.0
    assert _entropy([]) == 0.0
    assert _entropy([5, 5]) == pytest.approx(1.0)


def test_features_summarise_a_window() -> None:
    rows = [
        {"model_used": "gpt-4o", "endpoint": "/v1/chat", "cost_usd": 0.01, "prompt_hash": "a"},
        {"model_used": "gpt-4o", "endpoint": "/v1/chat", "cost_usd": 0.01, "prompt_hash": "a"},
        {"model_used": "gpt-4o", "endpoint": "/v1/chat", "cost_usd": 0.02, "prompt_hash": "b"},
    ]
    features = features_from_rows(rows, window_minutes=5)

    assert features.request_rate == pytest.approx(0.6)
    assert features.cost_rate == pytest.approx(0.008)
    assert features.model_entropy == 0.0
    assert features.unique_prompt_ratio == pytest.approx(2 / 3)


def test_features_tolerate_missing_and_malformed_fields() -> None:
    features = features_from_rows([{"cost_usd": "junk"}], window_minutes=5)
    assert features.cost_rate == 0.0
    assert features.request_rate == pytest.approx(0.2)


def test_the_forest_is_silent_until_it_has_history() -> None:
    verdict = detect([PatternFeatures(1, 1, 1, 1, 1)], PatternFeatures(99, 99, 9, 9, 1))
    assert not verdict.anomalous
    assert verdict.reason == "COLD_START"


def test_a_leaked_key_pattern_is_flagged_at_normal_volume() -> None:
    """UC-32's actual scenario: same request rate, different shape.

    Spend and volume are ordinary — the z-score path would see nothing. What
    changed is the model mix, the endpoint spread, and prompts that never
    repeat.
    """
    history = [
        PatternFeatures(
            request_rate=10.0 + (i % 3) * 0.2,
            cost_rate=0.05,
            model_entropy=0.1,
            endpoint_entropy=0.0,
            unique_prompt_ratio=0.2,
        )
        for i in range(40)
    ]
    leaked = PatternFeatures(
        request_rate=10.1,
        cost_rate=0.05,
        model_entropy=2.4,
        endpoint_entropy=1.9,
        unique_prompt_ratio=1.0,
    )

    verdict = detect(history, leaked)

    assert verdict.anomalous, f"score {verdict.score}, reason {verdict.reason}"
    assert verdict.contributors


def test_ordinary_traffic_does_not_trip_the_forest() -> None:
    history = [PatternFeatures(10.0 + (i % 5) * 0.1, 0.05, 0.1, 0.0, 0.2) for i in range(40)]
    verdict = detect(history, PatternFeatures(10.2, 0.051, 0.1, 0.0, 0.22))
    assert not verdict.anomalous


def test_a_tiny_move_in_a_constant_feature_does_not_fire() -> None:
    """Regression: the forest alone flagged this at -0.34, same as a real leak.

    A well-behaved project holds most of these features perfectly constant, so
    the scored window is the only point that differs on them at all and the
    forest isolates it in one split — whether the move was 2% or 24x. Without
    the magnitude gate this fired on ordinary noise.
    """
    history = [PatternFeatures(10.0 + (i % 5) * 0.1, 0.05, 0.1, 0.0, 0.2) for i in range(40)]
    verdict = detect(history, PatternFeatures(10.2, 0.0501, 0.1, 0.0, 0.201))

    assert not verdict.anomalous
    assert verdict.reason == "WITHIN_NORMAL_MAGNITUDE"
    assert verdict.score < 0, "the forest did call it unique; the gate is what stopped it"


def test_the_forest_never_raises_on_garbage() -> None:
    history = [PatternFeatures(float("nan"), 1, 1, 1, 1) for _ in range(40)]
    verdict = detect(history, PatternFeatures(1, 1, 1, 1, 1))
    assert not verdict.anomalous
    assert verdict.reason in {"NON_FINITE_FEATURES", "DETECTOR_FAILED"}


# -- Budget primitives ------------------------------------------------------


def test_period_buckets_roll_over_on_their_own() -> None:
    from datetime import UTC, datetime

    late = datetime(2026, 8, 8, 23, 59, tzinfo=UTC)
    early = datetime(2026, 8, 9, 0, 1, tzinfo=UTC)

    assert period_bucket("daily", late) != period_bucket("daily", early)
    assert period_bucket("monthly", late) == period_bucket("monthly", early)


def test_the_counter_key_embeds_the_period_instance() -> None:
    from datetime import UTC, datetime

    key = budget_counter_key("proj_1", "daily", datetime(2026, 8, 8, tzinfo=UTC))
    assert key.endswith("proj_1:daily:2026-08-08")


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"period": "hourly", "limit_usd": 5, "action": "hard_stop"},
        {"period": "daily", "limit_usd": 0, "action": "hard_stop"},
        {"period": "daily", "limit_usd": 5, "action": "explode"},
    ],
)
def test_a_malformed_budget_is_discarded_not_guessed_at(raw: dict[str, object]) -> None:
    assert BudgetSpec.from_raw(raw) is None


def test_a_valid_budget_parses() -> None:
    spec = BudgetSpec.from_raw({"period": "daily", "limit_usd": 5.0, "action": "hard_stop"})
    assert spec is not None
    assert spec.action is BudgetAction.HARD_STOP
