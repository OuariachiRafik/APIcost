"""Stage-by-stage latency decomposition.

Powers the NFR harness that proves the <100 ms cache-miss and <30 ms cache-hit
targets (BUILD_SPEC §5), and an admin-only dashboard view. Pure
(CODEBASE_GUIDE §9).

Percentiles are computed here rather than with numpy so ``metrics/`` stays
dependency-free and importable from the proxy hot path without pulling the
``ml`` dependency group onto the data plane. The implementation matches
numpy's default linear interpolation, and there is a test pinning that.

The fixes BUILD_SPEC §6.6 requires of the supplied implementation:

* mismatched stage array lengths are rejected with a clear message instead of
  broadcasting or failing deep inside numpy;
* empty input is handled explicitly;
* percentile keys are ``"p50"`` strings everywhere — the supplied version used
  raw numbers for the end-to-end series and strings per stage, which would
  have bitten the frontend;
* a *measured* end-to-end series is reported alongside the sum of stages,
  with a divergence flag. Summing stage means assumes stages are sequential
  and non-overlapping, and the cache lookup and routing decision may run
  concurrently later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

__all__ = [
    "DEFAULT_PERCENTILES",
    "DIVERGENCE_THRESHOLD",
    "LatencyDecomposition",
    "StageStats",
    "StageTimer",
    "decompose_latency",
    "percentile",
]

DEFAULT_PERCENTILES: Final = (50.0, 95.0, 99.0)

DIVERGENCE_THRESHOLD: Final = 0.10
"""Flag when summed stages and measured end-to-end differ by more than 10%."""

PIPELINE_STAGES: Final = (
    "auth",
    "budget_check",
    "embed",
    "cache_lookup",
    "routing",
    "key_decrypt",
    "provider",
    "serialize",
)
"""The stages BUILD_SPEC §6.6 asks to instrument, in pipeline order."""


def percentile(values: list[float], q: float) -> float:
    """Linear-interpolated percentile, matching numpy's default method.

    Args:
        values: Samples. Not required to be sorted.
        q: Percentile in ``[0, 100]``.
    """
    if not values:
        raise ValueError("cannot take a percentile of an empty sequence")
    if not 0.0 <= q <= 100.0:
        raise ValueError(f"percentile must be within [0, 100], got {q}")

    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]

    position = (len(ordered) - 1) * (q / 100.0)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _percentile_map(values: list[float], percentiles: tuple[float, ...]) -> dict[str, float]:
    """Percentiles keyed as ``"p50"``, ``"p95"``, ... — one key style, everywhere."""
    return {f"p{q:g}": percentile(values, q) for q in percentiles}


@dataclass(frozen=True)
class StageStats:
    """Per-stage summary."""

    name: str
    mean_ms: float
    percentiles: dict[str, float]
    share_of_total: float
    """Fraction of the summed mean, in ``[0, 1]``."""


@dataclass(frozen=True)
class LatencyDecomposition:
    """Where the time went, and whether the accounting adds up."""

    sample_count: int
    stages: dict[str, StageStats]
    summed_mean_ms: float
    """Sum of per-stage means. Assumes stages are sequential and disjoint."""

    measured_mean_ms: float | None
    measured_percentiles: dict[str, float] = field(default_factory=dict)
    bottleneck: str | None = None
    divergence: float | None = None
    """Relative gap between summed and measured means, when both are known."""

    diverged: bool = False
    """True when the gap exceeds :data:`DIVERGENCE_THRESHOLD` — the signal that
    stages overlap, or that something on the path is not instrumented."""


def decompose_latency(
    stage_latencies: dict[str, list[float]],
    *,
    measured_total: list[float] | None = None,
    percentiles: tuple[float, ...] = DEFAULT_PERCENTILES,
) -> LatencyDecomposition:
    """Break a batch of requests down by pipeline stage.

    Args:
        stage_latencies: Stage name to per-request durations in ms. Every
            array must be the same length — element *i* of each is the same
            request.
        measured_total: End-to-end duration per request, measured rather than
            summed. Same length as the stage arrays.
        percentiles: Which percentiles to report.

    Raises:
        ValueError: No stages, empty arrays, or mismatched lengths.
    """
    if not stage_latencies:
        raise ValueError("stage_latencies must contain at least one stage")

    lengths = {name: len(values) for name, values in stage_latencies.items()}
    distinct = set(lengths.values())

    if distinct == {0}:
        raise ValueError("stage_latencies contains no samples")

    if len(distinct) > 1:
        # Left to numpy this either broadcasts into a wrong answer or raises
        # from inside a ufunc with a message that names no stage.
        detail = ", ".join(f"{name}={count}" for name, count in sorted(lengths.items()))
        raise ValueError(f"all stages must have the same sample count; got {detail}")

    sample_count = distinct.pop()

    if measured_total is not None and len(measured_total) != sample_count:
        raise ValueError(
            f"measured_total has {len(measured_total)} samples but stages have {sample_count}"
        )

    means = {name: sum(values) / len(values) for name, values in stage_latencies.items()}
    summed_mean = sum(means.values())

    stages = {
        name: StageStats(
            name=name,
            mean_ms=means[name],
            percentiles=_percentile_map(values, percentiles),
            share_of_total=(means[name] / summed_mean) if summed_mean > 0 else 0.0,
        )
        for name, values in stage_latencies.items()
    }

    bottleneck = max(means, key=lambda name: means[name]) if means else None

    measured_mean: float | None = None
    measured_percentiles: dict[str, float] = {}
    divergence: float | None = None
    diverged = False

    if measured_total is not None:
        measured_mean = sum(measured_total) / len(measured_total)
        measured_percentiles = _percentile_map(measured_total, percentiles)
        if measured_mean > 0:
            divergence = abs(summed_mean - measured_mean) / measured_mean
            diverged = divergence > DIVERGENCE_THRESHOLD

    return LatencyDecomposition(
        sample_count=sample_count,
        stages=stages,
        summed_mean_ms=summed_mean,
        measured_mean_ms=measured_mean,
        measured_percentiles=measured_percentiles,
        bottleneck=bottleneck,
        divergence=divergence,
        diverged=diverged,
    )


@dataclass
class StageTimer:
    """Accumulates per-stage durations for one request.

    Pure bookkeeping over ``time.perf_counter`` — no I/O, so this stays inside
    the pure-function core. The pipeline feeds it, the log line reports it, and
    :func:`decompose_latency` aggregates a batch of them.

    This exists because guessing at where request time goes is how a 30 ms
    budget quietly becomes 40. Stages are the ones named in BUILD_SPEC §6.6.
    """

    stages: dict[str, float] = field(default_factory=dict)
    _started: float = 0.0

    def start(self, clock: float) -> None:
        self._started = clock

    def mark(self, stage: str, clock: float) -> None:
        """Record time elapsed since the previous mark as ``stage``."""
        self.stages[stage] = (clock - self._started) * 1000.0
        self._started = clock

    def total_ms(self) -> float:
        return sum(self.stages.values())

    def as_log_fields(self) -> dict[str, float]:
        return {f"t_{name}": round(value, 2) for name, value in self.stages.items()}
