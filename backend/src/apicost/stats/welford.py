"""Welford's online mean and variance (BUILD_SPEC §6.5).

Why not just keep a sum and a sum of squares: the naive form computes variance
as ``E[x^2] - E[x]^2``, subtracting two large and nearly equal numbers. For spend
rates — small values with a large mean — that cancellation eats most of the
significant digits, and it can return a *negative* variance, which then raises
on the square root. Welford never forms those large intermediates.

The property that matters here is O(1) per observation with no history. We
update these on every ledger record; recomputing over a rolling window on each
one would put the cost of anomaly detection on the volume of traffic being
watched, which is backwards.

Pure by CLAUDE.md §Style: no I/O, no ORM. Persistence lives in
``anomaly/store.py``; see ADR 0008.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

__all__ = [
    "WelfordState",
    "from_dict",
    "merge",
    "stddev",
    "update",
    "variance",
]


@dataclass(frozen=True)
class WelfordState:
    """Accumulated count, mean, and sum of squared deviations.

    Frozen: every operation returns a new state. An anomaly check that
    accidentally mutated the baseline it was comparing against would drift the
    baseline toward the anomaly and stop firing — the failure would look like
    "alerts tapered off", which nobody investigates.
    """

    count: int = 0
    mean: float = 0.0
    m2: float = 0.0
    """Sum of squared deviations from the running mean. Not a variance."""

    def to_dict(self) -> dict[str, float | int]:
        return {"count": self.count, "mean": self.mean, "m2": self.m2}


def update(state: WelfordState, value: float) -> WelfordState:
    """Fold one observation in. O(1), exact."""
    count = state.count + 1
    delta = value - state.mean
    mean = state.mean + delta / count
    # delta uses the OLD mean and delta2 the NEW one. That asymmetry is the
    # algorithm, not a typo — their product is what keeps m2 exact.
    delta2 = value - mean
    return WelfordState(count=count, mean=mean, m2=state.m2 + delta * delta2)


def merge(a: WelfordState, b: WelfordState) -> WelfordState:
    """Combine two independently accumulated states (Chan's parallel form).

    Needed because the worker updates a state per project while draining a
    batch, and the checkpoint in Redis may have advanced underneath it.
    """
    if a.count == 0:
        return b
    if b.count == 0:
        return a

    count = a.count + b.count
    delta = b.mean - a.mean
    mean = a.mean + delta * b.count / count
    m2 = a.m2 + b.m2 + delta * delta * a.count * b.count / count
    return WelfordState(count=count, mean=mean, m2=m2)


def variance(state: WelfordState) -> float:
    """Sample variance (Bessel-corrected). 0.0 below two observations.

    Sample rather than population: the observations are a window of a project's
    traffic, not its entire lifetime, and the population form biases the
    variance *down* on small samples — which would make the z-score larger and
    fire spurious alerts exactly when the least is known.
    """
    if state.count < 2:
        return 0.0
    return state.m2 / (state.count - 1)


def stddev(state: WelfordState) -> float:
    """Sample standard deviation. Never negative, never NaN."""
    # max() guards the floating-point case where m2 lands fractionally below
    # zero on a constant series; sqrt of -1e-18 raises, and this runs on the
    # alerting path.
    return math.sqrt(max(0.0, variance(state)))


def from_dict(raw: dict[str, Any]) -> WelfordState:
    """Rebuild from a checkpoint, tolerating anything malformed.

    Returns a zero state rather than raising: a corrupt checkpoint should cost
    a project its baseline history, which rebuilds, not its alerting.
    """
    try:
        count = int(raw["count"])
        mean = float(raw["mean"])
        m2 = float(raw["m2"])
    except (KeyError, TypeError, ValueError):
        return WelfordState()

    if count < 0 or m2 < 0 or not math.isfinite(mean) or not math.isfinite(m2):
        return WelfordState()
    return WelfordState(count=count, mean=mean, m2=m2)
