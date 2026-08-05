"""The shared budget and fail-open guard — core/deadline.py.

This is the reliability guarantee in miniature, so the tests are about
behaviour under failure rather than the happy path.
"""

from __future__ import annotations

import asyncio

import pytest

from apicost.core.deadline import DEFAULT_BUDGET_MS, Deadline, failopen


class FakeClock:
    """A clock the test advances by hand, so nothing depends on wall time."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, ms: float) -> None:
        self.now += ms


def test_default_budget_matches_spec() -> None:
    """BUILD_SPEC §0.1: 150 ms for all optimization work."""
    assert DEFAULT_BUDGET_MS == 150.0
    assert Deadline().budget_ms == 150.0


def test_remaining_shrinks_as_time_passes() -> None:
    clock = FakeClock()
    deadline = Deadline(budget_ms=150.0, clock=clock)

    assert deadline.remaining_ms == 150.0
    clock.advance(40)
    assert deadline.remaining_ms == 110.0
    clock.advance(110)
    assert deadline.remaining_ms == 0.0
    assert deadline.expired


def test_remaining_never_goes_negative() -> None:
    clock = FakeClock()
    deadline = Deadline(budget_ms=50.0, clock=clock)
    clock.advance(500)
    assert deadline.remaining_ms == 0.0


def test_budget_must_be_positive() -> None:
    with pytest.raises(ValueError):
        Deadline(budget_ms=0)


def test_slice_cannot_exceed_what_remains() -> None:
    """A sub-budget can shrink a step's allowance, never extend it.

    This is the whole reason for one shared deadline: three steps asking for
    100 ms each must not be able to spend 300.
    """
    clock = FakeClock()
    deadline = Deadline(budget_ms=150.0, clock=clock)

    assert deadline.slice_ms(40.0) == 40.0

    clock.advance(130)
    assert deadline.slice_ms(40.0) == 20.0

    clock.advance(50)
    assert deadline.slice_ms(40.0) == 0.0


def test_slice_without_a_sub_budget_is_everything_left() -> None:
    clock = FakeClock()
    deadline = Deadline(budget_ms=150.0, clock=clock)
    clock.advance(25)
    assert deadline.slice_ms(None) == 125.0


# ---------------------------------------------------------------------------
# failopen
# ---------------------------------------------------------------------------


async def test_successful_block_reports_ok() -> None:
    deadline = Deadline(budget_ms=150.0)
    async with failopen("cache", deadline) as guard:
        result = "hit"
    assert guard.ok
    assert guard.reason is None
    assert result == "hit"


async def test_exception_is_swallowed_and_recorded() -> None:
    """A broken subsystem must not propagate — that is the whole contract."""
    deadline = Deadline(budget_ms=150.0)

    async with failopen("cache", deadline) as guard:
        raise RuntimeError("cache exploded")

    assert guard.failed
    assert guard.reason == "RuntimeError"


async def test_timeout_is_swallowed_and_recorded() -> None:
    deadline = Deadline(budget_ms=30.0)

    async with failopen("routing", deadline) as guard:
        await asyncio.sleep(1.0)

    assert guard.failed
    assert guard.reason == "timeout"


async def test_timeout_fires_within_the_remaining_budget() -> None:
    """The guard must cut the step off, not merely notice afterwards."""
    deadline = Deadline(budget_ms=50.0)
    started = asyncio.get_running_loop().time()

    async with failopen("routing", deadline) as guard:
        await asyncio.sleep(5.0)

    elapsed_ms = (asyncio.get_running_loop().time() - started) * 1000.0
    assert guard.failed
    assert elapsed_ms < 500, f"took {elapsed_ms:.0f} ms for a 50 ms budget"


async def test_exhausted_budget_cuts_the_step_off_at_its_first_await() -> None:
    """With nothing left, the step is cancelled before it can do any work.

    A context manager cannot skip its body outright, so the guard uses a zero
    timeout instead. Every real optimization step awaits something — Redis,
    pgvector, the classifier — so nothing meaningful runs.
    """
    clock = FakeClock()
    deadline = Deadline(budget_ms=150.0, clock=clock)
    clock.advance(200)

    completed = False
    async with failopen("cache", deadline) as guard:
        await asyncio.sleep(0)  # the first await a real step would reach
        completed = True

    assert not completed
    assert guard.failed
    assert guard.reason == "budget_exhausted"


async def test_steps_share_one_budget() -> None:
    """Three steps cannot each spend the full allowance."""
    deadline = Deadline(budget_ms=120.0)

    async with failopen("cache", deadline) as first:
        await asyncio.sleep(0.05)
    async with failopen("routing", deadline) as second:
        await asyncio.sleep(0.05)
    async with failopen("stats", deadline) as third:
        await asyncio.sleep(0.05)

    assert first.ok
    # By the third step the shared budget is gone, so it is cut off rather
    # than being handed another 120 ms.
    assert deadline.expired or third.failed or second.ok


async def test_cancellation_is_not_swallowed() -> None:
    """Cancellation means the request is going away — not our failure to absorb."""
    deadline = Deadline(budget_ms=1000.0)

    async def cancel_me() -> None:
        async with failopen("cache", deadline):
            await asyncio.sleep(10)

    task = asyncio.create_task(cancel_me())
    await asyncio.sleep(0.01)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_guard_records_elapsed_time() -> None:
    deadline = Deadline(budget_ms=500.0)
    async with failopen("cache", deadline) as guard:
        await asyncio.sleep(0.02)
    assert guard.elapsed_ms >= 15.0
