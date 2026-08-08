"""Fast-path spend-spike detection (UC-31, BUILD_SPEC §4 P6).

One window's spend rate against the project's own rolling baseline. Runs on
every closed window in the ledger drain, so it must stay cheap and pure.

Deliberately *per project*: there is no global "normal" spend rate. A hobby
project at $0.10/day and a production app at $400/day are both normal, and any
threshold that flags one leaves the other unprotected.

Pure by CLAUDE.md §Style — no I/O, no ORM.
"""

from __future__ import annotations

from dataclasses import dataclass

from apicost.stats.welford import WelfordState, stddev

__all__ = [
    "DEFAULT_MIN_OBSERVATIONS",
    "DEFAULT_Z_THRESHOLD",
    "SpikeVerdict",
    "score",
]

DEFAULT_Z_THRESHOLD: float = 3.0
"""BUILD_SPEC §4 P6. Three sigma on a roughly normal series is ~1 window in 740,
or about one false alarm every 12 hours of continuous traffic at one-minute
windows. Spend rates are right-skewed rather than normal, so the real rate is
lower — but this is why the absolute floor below exists."""

DEFAULT_MIN_OBSERVATIONS: int = 30
"""BUILD_SPEC §4 P6. Below this the standard deviation is too unstable to
divide by: two similar windows give a near-zero sigma, and the third normal
window scores infinitely anomalous. Cold start must be silent."""

MIN_ABSOLUTE_RATE_USD: float = 0.01
"""A window has to be worth at least a cent before it can be a spike.

Without this, a project that habitually spends $0.0001 per window has a
microscopic sigma, and $0.002 — still nothing — scores z=20. The user gets
paged about two tenths of a cent. Relative anomaly is necessary but not
sufficient; the alert has to also be worth waking up for."""


@dataclass(frozen=True)
class SpikeVerdict:
    """Why a window did or did not fire.

    Carries the numbers even when it does not fire. The alert email quotes them,
    and "your spend hit $4.10/min against a $0.12/min baseline" is actionable in
    a way that "anomaly detected" is not.
    """

    anomalous: bool
    z: float
    rate_usd: float
    baseline_mean: float
    baseline_stddev: float
    observations: int
    reason: str

    @property
    def multiple(self) -> float:
        """How many times the baseline this window was. For humans."""
        if self.baseline_mean <= 0:
            return 0.0
        return self.rate_usd / self.baseline_mean


def score(
    baseline: WelfordState,
    rate_usd: float,
    *,
    z_threshold: float = DEFAULT_Z_THRESHOLD,
    min_observations: int = DEFAULT_MIN_OBSERVATIONS,
    min_absolute_rate_usd: float = MIN_ABSOLUTE_RATE_USD,
) -> SpikeVerdict:
    """Score one closed window against the baseline it is not yet part of.

    The caller must pass the baseline **excluding** this window. Including it
    lets a large enough spike pull the mean up far enough to hide itself, which
    is precisely backwards.
    """
    sigma = stddev(baseline)

    def verdict(anomalous: bool, z: float, reason: str) -> SpikeVerdict:
        return SpikeVerdict(
            anomalous=anomalous,
            z=z,
            rate_usd=rate_usd,
            baseline_mean=baseline.mean,
            baseline_stddev=sigma,
            observations=baseline.count,
            reason=reason,
        )

    if baseline.count < min_observations:
        return verdict(False, 0.0, "COLD_START")

    if rate_usd < min_absolute_rate_usd:
        return verdict(False, 0.0, "BELOW_ABSOLUTE_FLOOR")

    if sigma <= 0.0:
        # A perfectly flat baseline: every window so far cost the same. Any
        # change is infinitely many sigmas, so fall back to a plain multiple.
        # This is common for scheduled jobs, and without it the first genuinely
        # different window either divides by zero or never fires.
        if rate_usd > baseline.mean * 2.0:
            return verdict(True, float("inf"), "FLAT_BASELINE_EXCEEDED")
        return verdict(False, 0.0, "FLAT_BASELINE")

    z = (rate_usd - baseline.mean) / sigma

    if z < z_threshold:
        return verdict(False, z, "WITHIN_THRESHOLD")

    return verdict(True, z, "SPEND_SPIKE")
