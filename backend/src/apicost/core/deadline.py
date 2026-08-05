"""The shared time budget and the fail-open guard.

Two ideas, and the whole reliability story rests on them (BUILD_SPEC §0.1,
CODEBASE_GUIDE §8.1-8.2):

*One budget, not many.* A single :class:`Deadline` is created per request and
threaded through every optimization step. Per-step timeouts are the obvious
alternative and they are wrong: three steps with 100 ms timeouts each can spend
300 ms, and the ceiling the product promises is 150 ms total. Each step gets
whatever is left, not a fresh allowance.

*Failure means passthrough, never an error.* :func:`failopen` swallows
exceptions and budget overruns, records what happened, and lets the pipeline
continue with the original request. A broken cache must never break somebody's
production application.

The only deliberate exception in the entire system is a ``hard_stop`` budget,
which fails closed — and that check runs *before* the deadline is created.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Final

from apicost.core.logging import get_logger

__all__ = [
    "DEFAULT_BUDGET_MS",
    "Deadline",
    "FailOpenGuard",
    "failopen",
]

DEFAULT_BUDGET_MS: Final = 150.0

_logger = get_logger(__name__)

Clock = Callable[[], float]


def _monotonic_ms() -> float:
    """Milliseconds from a monotonic clock.

    ``perf_counter`` rather than ``time.time``: the wall clock can step
    backwards (NTP, DST, a VM resuming) and a negative elapsed time turns a
    budget check into an unbounded wait.
    """
    return time.perf_counter() * 1000.0


@dataclass
class Deadline:
    """A shrinking time budget shared by every optimization step."""

    budget_ms: float = DEFAULT_BUDGET_MS
    clock: Clock = field(default=_monotonic_ms, repr=False)
    started_ms: float = field(init=False)

    def __post_init__(self) -> None:
        if self.budget_ms <= 0:
            raise ValueError("budget_ms must be positive")
        self.started_ms = self.clock()

    @property
    def elapsed_ms(self) -> float:
        return self.clock() - self.started_ms

    @property
    def remaining_ms(self) -> float:
        """Budget left, never negative."""
        return max(0.0, self.budget_ms - self.elapsed_ms)

    @property
    def expired(self) -> bool:
        return self.remaining_ms <= 0.0

    def slice_ms(self, requested_ms: float | None) -> float:
        """The smaller of a step's own sub-budget and what is actually left.

        ``cache/embeddings.py`` wants 40 ms (§4 P4), but if only 12 ms remain
        it gets 12. A sub-budget can shrink the allowance, never extend it.
        """
        if requested_ms is None:
            return self.remaining_ms
        return min(requested_ms, self.remaining_ms)


@dataclass
class FailOpenGuard:
    """What happened inside a :func:`failopen` block."""

    subsystem: str
    failed: bool = False
    reason: str | None = None
    elapsed_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.failed

    def value_or(self, value: object, fallback: object) -> object:
        """Return ``value`` when the block succeeded, else ``fallback``."""
        return fallback if self.failed else value


@asynccontextmanager
async def failopen(
    subsystem: str,
    deadline: Deadline,
    *,
    budget_ms: float | None = None,
) -> AsyncIterator[FailOpenGuard]:
    """Run an optimization step under the shared budget, swallowing failure.

    On timeout or exception the guard is marked failed, the event is logged
    with ``subsystem=<name>``, and control returns to the caller normally. The
    caller is responsible for treating a failed guard as "no result" — which in
    practice means passing the original request through unchanged.

    Callers must not use the value produced inside the block without checking
    ``guard.ok`` first; that is the one thing this helper cannot enforce.
    """
    guard = FailOpenGuard(subsystem=subsystem)
    started = deadline.clock()
    allowance = deadline.slice_ms(budget_ms)
    exhausted = allowance <= 0.0

    # An exhausted budget still enters the block — a context manager cannot
    # skip its body — but with a zero timeout, so the step is cut off at its
    # first await. Every optimization step awaits (Redis, pgvector, the
    # classifier), so in practice no work happens. Callers must still check
    # ``guard.ok`` before using anything produced inside.
    try:
        async with asyncio.timeout(allowance / 1000.0):
            yield guard
    except TimeoutError:
        guard.failed = True
        guard.reason = "budget_exhausted" if exhausted else "timeout"
        _logger.warning(
            "failopen_timeout",
            subsystem=subsystem,
            reason=guard.reason,
            allowance_ms=round(allowance, 2),
            deadline_elapsed_ms=round(deadline.elapsed_ms, 2),
        )
    except asyncio.CancelledError:
        # Not our failure to swallow — the request itself is going away.
        raise
    except Exception as exc:
        guard.failed = True
        guard.reason = type(exc).__name__
        _logger.warning(
            "failopen_error",
            subsystem=subsystem,
            error_type=type(exc).__name__,
            exc_info=exc,
        )
    finally:
        guard.elapsed_ms = deadline.clock() - started
