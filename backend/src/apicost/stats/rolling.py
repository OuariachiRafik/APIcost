"""Windowed spend-rate state per project (BUILD_SPEC §6.5).

The unit of observation for anomaly detection is a **window's spend rate**, not
an individual request. One expensive request is not an anomaly; a sustained
jump in spend per minute is. So the ledger feeds requests into an open window,
and when a window closes it becomes exactly one observation in the project's
Welford baseline.

Pure by CLAUDE.md §Style. This module decides *what* the state is and how it
advances; ``anomaly/store.py`` decides where it lives. See ADR 0008 — BUILD_SPEC
§3 describes Redis checkpointing here, and that I/O was moved out to keep
``apicost.stats`` pure and under ``mypy --strict``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

from apicost.stats.welford import WelfordState, from_dict, update

__all__ = [
    "DEFAULT_WINDOW_SECONDS",
    "RollingStats",
    "observe",
    "rolling_from_dict",
]

DEFAULT_WINDOW_SECONDS: int = 60
"""One minute. Short enough that a runaway loop is caught inside the two-minute
acceptance bound (BUILD_SPEC §4 P6), long enough that a handful of requests
arriving together is not its own window."""


@dataclass(frozen=True)
class RollingStats:
    """A project's baseline, plus the window currently being filled.

    ``baseline`` counts *windows*, not requests. A project at 5 requests/minute
    reaches the 30-observation minimum in 30 minutes regardless of its volume,
    which is the point: the guard is about having seen enough of the project's
    rhythm, not enough of its traffic.
    """

    baseline: WelfordState = field(default_factory=WelfordState)
    window_started_at: float = 0.0
    window_cost: float = 0.0
    window_requests: int = 0

    @property
    def window_rate(self) -> float:
        """Cost per minute in the open window, extrapolated from elapsed time.

        Unused so far — windows are only scored once closed, where the elapsed
        time is known to be the full window. Kept because the obvious next
        feature is scoring a window early, and doing that without this property
        means someone divides by ``DEFAULT_WINDOW_SECONDS`` on a window that has
        been open for four seconds and gets a 15x reading.
        """
        return self.window_cost

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline": self.baseline.to_dict(),
            "window_started_at": self.window_started_at,
            "window_cost": self.window_cost,
            "window_requests": self.window_requests,
        }


@dataclass(frozen=True)
class Observation:
    """The result of folding one request in.

    ``closed_rate`` is set only on the request that pushed the clock past the
    window boundary, and it carries the rate of the window that just ended —
    the one and only value that should be scored for anomalies.
    """

    stats: RollingStats
    closed_rate: float | None = None
    closed_requests: int = 0


def observe(
    stats: RollingStats,
    *,
    cost_usd: Decimal | float,
    at: float,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
) -> Observation:
    """Fold one request in, closing and scoring the window if it has elapsed.

    ``at`` is a wall-clock epoch second, taken from the ledger row rather than
    from now(): the worker drains asynchronously and may be minutes behind, and
    bucketing a backlog by processing time would smear a real spike flat.
    """
    cost = float(cost_usd)

    if stats.window_started_at == 0.0:
        # First request this project has ever made. Open a window at its
        # timestamp; do not treat the empty state as a window that closed.
        return Observation(
            stats=replace(
                stats,
                window_started_at=at,
                window_cost=cost,
                window_requests=1,
            )
        )

    elapsed = at - stats.window_started_at

    if elapsed < window_seconds:
        return Observation(
            stats=replace(
                stats,
                window_cost=stats.window_cost + cost,
                window_requests=stats.window_requests + 1,
            )
        )

    closed_rate = stats.window_cost
    closed_requests = stats.window_requests

    # A gap longer than one window means the project went quiet. Those silent
    # windows are deliberately NOT recorded as zero-cost observations: doing so
    # would drag the mean toward zero and the variance up, so a project that is
    # idle overnight would wake up unable to distinguish morning traffic from
    # an attack. Only windows that actually contained requests count.
    baseline = update(stats.baseline, closed_rate)

    return Observation(
        stats=RollingStats(
            baseline=baseline,
            window_started_at=at,
            window_cost=cost,
            window_requests=1,
        ),
        closed_rate=closed_rate,
        closed_requests=closed_requests,
    )


def rolling_from_dict(raw: dict[str, Any] | None) -> RollingStats:
    """Rebuild from a checkpoint, tolerating anything malformed."""
    if not isinstance(raw, dict):
        return RollingStats()

    baseline_raw = raw.get("baseline")
    baseline = from_dict(baseline_raw) if isinstance(baseline_raw, dict) else WelfordState()

    try:
        started = float(raw.get("window_started_at", 0.0))
        cost = float(raw.get("window_cost", 0.0))
        requests = int(raw.get("window_requests", 0))
    except (TypeError, ValueError):
        return RollingStats(baseline=baseline)

    if started < 0 or cost < 0 or requests < 0:
        return RollingStats(baseline=baseline)

    return RollingStats(
        baseline=baseline,
        window_started_at=started,
        window_cost=cost,
        window_requests=requests,
    )
