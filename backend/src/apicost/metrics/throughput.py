"""Throughput series — step and cumulative tokens per second.

Used for per-request streaming throughput and for the aggregate tokens/sec on
the usage dashboard. Pure (CODEBASE_GUIDE §9).

BUILD_SPEC §6.6 asks for one change to the supplied implementation: return
``overall_tps`` as ``0.0`` rather than raising when the total elapsed time is
zero. The existing guard makes that unreachable today; it is defensive against
the guard being relaxed later.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["ThroughputSeries", "compute_throughput"]


@dataclass(frozen=True)
class ThroughputSeries:
    """Throughput measured two ways over the same intervals."""

    step_tps: list[float]
    """Rate within each interval — noisy, shows stalls."""

    cumulative_tps: list[float]
    """Rate from the start through each interval — smooth, shows the trend."""

    overall_tps: float
    total_tokens: int
    total_seconds: float


def compute_throughput(intervals: list[tuple[int, float]]) -> ThroughputSeries:
    """Build step and cumulative throughput series.

    Args:
        intervals: ``(tokens, seconds)`` per interval, in order.

    Raises:
        ValueError: Empty input, negative tokens, or non-positive duration.
    """
    if not intervals:
        raise ValueError("intervals must not be empty")

    for index, (tokens, seconds) in enumerate(intervals):
        if tokens < 0:
            raise ValueError(f"interval {index} has negative tokens: {tokens}")
        if seconds <= 0:
            raise ValueError(f"interval {index} has non-positive duration: {seconds}")

    step_tps: list[float] = []
    cumulative_tps: list[float] = []

    running_tokens = 0
    running_seconds = 0.0

    for tokens, seconds in intervals:
        running_tokens += tokens
        running_seconds += seconds
        step_tps.append(tokens / seconds)
        cumulative_tps.append(running_tokens / running_seconds)

    # Unreachable given the guard above; kept so relaxing that guard cannot
    # turn a reporting call into a ZeroDivisionError.
    overall = running_tokens / running_seconds if running_seconds > 0 else 0.0

    return ThroughputSeries(
        step_tps=step_tps,
        cumulative_tps=cumulative_tps,
        overall_tps=overall,
        total_tokens=running_tokens,
        total_seconds=running_seconds,
    )
