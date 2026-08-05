"""The metrics library — BUILD_SPEC §6.6.

Target is 100% coverage on ``metrics/`` (§9), with the edge cases §6.6 calls
out tested explicitly: too-few timestamps, zero elapsed time, out-of-order
input, mismatched stage lengths, empty input, and consistent percentile keys.
"""

from __future__ import annotations

import pytest

from apicost.metrics.inference import compute_inference_metrics
from apicost.metrics.latency import (
    DEFAULT_PERCENTILES,
    DIVERGENCE_THRESHOLD,
    decompose_latency,
    percentile,
)
from apicost.metrics.throughput import compute_throughput

# ---------------------------------------------------------------------------
# inference
# ---------------------------------------------------------------------------


def test_inference_metrics_basic() -> None:
    # Five chunks, 10 ms apart, starting 100 ms after dispatch.
    timestamps = [1.10, 1.11, 1.12, 1.13, 1.14]
    metrics = compute_inference_metrics(timestamps, request_start=1.00)

    assert metrics.ttft_ms == pytest.approx(100.0, abs=0.01)
    assert metrics.itl_ms == pytest.approx(10.0, abs=0.01)
    assert metrics.tps == pytest.approx(125.0, rel=0.01)  # 5 chunks / 0.04 s
    assert metrics.token_count == 5


def test_ttft_is_zero_without_a_request_start() -> None:
    metrics = compute_inference_metrics([2.0, 2.1])
    assert metrics.ttft_ms == 0.0


@pytest.mark.parametrize("timestamps", [[], [1.0]])
def test_too_few_timestamps_raises_value_error(timestamps: list[float]) -> None:
    """§6.6: ValueError, not the IndexError the supplied version raised."""
    with pytest.raises(ValueError, match="at least 2 timestamps"):
        compute_inference_metrics(timestamps)


def test_zero_elapsed_returns_zero_tps() -> None:
    """§6.6 asks us to pick one behaviour and document it. We return 0.0.

    Infinity is not a number any average or dashboard can use, and it poisons
    every aggregate it lands in. A cache replay legitimately produces this.
    """
    metrics = compute_inference_metrics([5.0, 5.0, 5.0])
    assert metrics.tps == 0.0
    assert metrics.itl_ms == 0.0


def test_out_of_order_timestamps_raise() -> None:
    """§6.6: silently negative ITL is worse than a loud failure."""
    with pytest.raises(ValueError, match="non-decreasing"):
        compute_inference_metrics([1.0, 1.2, 1.1])


def test_equal_adjacent_timestamps_are_allowed() -> None:
    """Non-decreasing, not strictly increasing — clock granularity is real."""
    metrics = compute_inference_metrics([1.0, 1.0, 1.1])
    assert metrics.itl_ms == pytest.approx(50.0, abs=0.01)


# ---------------------------------------------------------------------------
# throughput
# ---------------------------------------------------------------------------


def test_throughput_series() -> None:
    result = compute_throughput([(10, 1.0), (20, 1.0), (30, 2.0)])

    assert result.step_tps == pytest.approx([10.0, 20.0, 15.0])
    assert result.cumulative_tps == pytest.approx([10.0, 15.0, 15.0])
    assert result.total_tokens == 60
    assert result.overall_tps == pytest.approx(15.0)


def test_throughput_rejects_empty_input() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        compute_throughput([])


@pytest.mark.parametrize("intervals", [[(-1, 1.0)], [(10, 0.0)], [(10, -1.0)]])
def test_throughput_validation(intervals: list[tuple[int, float]]) -> None:
    with pytest.raises(ValueError):
        compute_throughput(intervals)


def test_zero_token_interval_is_allowed() -> None:
    """A stall is a real observation, not invalid input."""
    result = compute_throughput([(0, 1.0), (10, 1.0)])
    assert result.step_tps[0] == 0.0


# ---------------------------------------------------------------------------
# latency
# ---------------------------------------------------------------------------


def test_percentile_matches_numpy_linear_interpolation() -> None:
    """Values checked against numpy's default method."""
    values = [1.0, 2.0, 3.0, 4.0]
    assert percentile(values, 0) == 1.0
    assert percentile(values, 50) == pytest.approx(2.5)
    assert percentile(values, 100) == 4.0
    assert percentile(values, 75) == pytest.approx(3.25)


def test_percentile_of_a_single_value() -> None:
    assert percentile([7.0], 95) == 7.0


def test_percentile_rejects_empty_and_out_of_range() -> None:
    with pytest.raises(ValueError):
        percentile([], 50)
    with pytest.raises(ValueError):
        percentile([1.0], 101)


def test_decompose_identifies_the_bottleneck() -> None:
    result = decompose_latency(
        {
            "auth": [1.0, 1.0, 1.0],
            "cache_lookup": [20.0, 22.0, 21.0],
            "provider": [5.0, 5.0, 5.0],
        }
    )

    assert result.bottleneck == "cache_lookup"
    assert result.sample_count == 3
    assert result.summed_mean_ms == pytest.approx(1.0 + 21.0 + 5.0)
    assert result.stages["cache_lookup"].share_of_total == pytest.approx(21 / 27, rel=1e-3)


def test_percentile_keys_are_strings_everywhere() -> None:
    """§6.6: the supplied version mixed raw numbers and 'p50' strings.

    The frontend indexes these; two key styles in one payload is a bug waiting
    for whoever writes the chart.
    """
    result = decompose_latency({"auth": [1.0, 2.0, 3.0]}, measured_total=[10.0, 11.0, 12.0])

    expected = {f"p{q:g}" for q in DEFAULT_PERCENTILES}
    assert set(result.stages["auth"].percentiles) == expected
    assert set(result.measured_percentiles) == expected
    assert all(isinstance(key, str) for key in result.measured_percentiles)


def test_mismatched_stage_lengths_are_rejected_by_name() -> None:
    """§6.6: numpy would broadcast or raise from inside a ufunc."""
    with pytest.raises(ValueError, match="same sample count"):
        decompose_latency({"auth": [1.0, 2.0], "provider": [1.0]})


def test_empty_input_is_handled_explicitly() -> None:
    with pytest.raises(ValueError, match="at least one stage"):
        decompose_latency({})
    with pytest.raises(ValueError, match="no samples"):
        decompose_latency({"auth": []})


def test_measured_total_length_is_validated() -> None:
    with pytest.raises(ValueError, match="measured_total"):
        decompose_latency({"auth": [1.0, 2.0]}, measured_total=[1.0])


def test_divergence_flags_overlapping_stages() -> None:
    """Summing stage means assumes they are sequential and disjoint (§6.6).

    When the cache lookup and routing eventually run concurrently, the sum will
    exceed the measured wall time — and this is the flag that says so instead
    of quietly reporting a wrong total.
    """
    result = decompose_latency(
        {"cache_lookup": [20.0, 20.0], "routing": [20.0, 20.0]},
        measured_total=[22.0, 22.0],  # they overlapped
    )

    assert result.summed_mean_ms == pytest.approx(40.0)
    assert result.measured_mean_ms == pytest.approx(22.0)
    assert result.divergence == pytest.approx(18 / 22, rel=1e-3)
    assert result.diverged


def test_no_divergence_when_stages_are_sequential() -> None:
    result = decompose_latency(
        {"auth": [2.0, 2.0], "provider": [18.0, 18.0]},
        measured_total=[20.0, 20.0],
    )
    assert result.divergence == pytest.approx(0.0, abs=1e-9)
    assert not result.diverged
    assert result.divergence is not None
    assert result.divergence < DIVERGENCE_THRESHOLD


def test_divergence_is_absent_without_a_measured_total() -> None:
    result = decompose_latency({"auth": [1.0]})
    assert result.measured_mean_ms is None
    assert result.divergence is None
    assert not result.diverged
