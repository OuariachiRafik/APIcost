"""Streaming inference metrics — TTFT, inter-token latency, tokens/sec.

Fed by the chunk timestamps captured in ``proxy/streaming.py``. Pure: no I/O,
no ORM, no framework (CODEBASE_GUIDE §9).

The fixes BUILD_SPEC §6.6 requires of the supplied implementation are applied
here and each is covered by a test:

* fewer than two timestamps raises ``ValueError`` rather than ``IndexError``;
* zero elapsed time returns ``0.0`` TPS rather than dividing by zero;
* out-of-order timestamps raise rather than silently producing negative ITL;
* timestamps are captured with ``time.perf_counter()``, never ``time.time()``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

__all__ = ["InferenceMetrics", "compute_inference_metrics"]

MIN_TIMESTAMPS: Final = 2


@dataclass(frozen=True)
class InferenceMetrics:
    """Per-request streaming metrics, all times in milliseconds."""

    ttft_ms: float
    """Time to first token: request start to the first content chunk."""

    itl_ms: float
    """Mean inter-token latency across the streamed chunks."""

    tps: float
    """Tokens per second over the streaming window."""

    token_count: int
    total_ms: float


def compute_inference_metrics(
    timestamps: list[float], *, request_start: float | None = None
) -> InferenceMetrics:
    """Compute TTFT, ITL, and TPS from chunk arrival times.

    Args:
        timestamps: Chunk arrival times in **seconds** from
            ``time.perf_counter()``, in arrival order. Must be
            non-decreasing and contain at least two entries.
        request_start: When the request was dispatched, same clock. TTFT is
            measured from here when given; otherwise from the first chunk,
            which makes TTFT ``0.0``.

    Raises:
        ValueError: Fewer than two timestamps, or they are not monotonic.
    """
    if len(timestamps) < MIN_TIMESTAMPS:
        # The supplied version indexed timestamps[1] and raised IndexError,
        # which reads like a bug in the metrics code rather than a caller
        # passing a stream that produced one chunk.
        raise ValueError(
            f"need at least {MIN_TIMESTAMPS} timestamps to compute inter-token "
            f"latency, got {len(timestamps)}"
        )

    for index in range(1, len(timestamps)):
        if timestamps[index] < timestamps[index - 1]:
            # Without this an out-of-order stream yields a negative ITL, which
            # then propagates into the dashboard as a plausible-looking number.
            raise ValueError(
                f"timestamps must be non-decreasing; index {index} "
                f"({timestamps[index]}) precedes index {index - 1} "
                f"({timestamps[index - 1]})"
            )

    first, last = timestamps[0], timestamps[-1]
    elapsed_s = last - first

    ttft_s = first - request_start if request_start is not None else 0.0

    intervals = len(timestamps) - 1
    itl_s = elapsed_s / intervals if intervals else 0.0

    # Zero elapsed time happens when every chunk arrives inside one clock tick,
    # which is normal for a cache replay. Report 0.0 rather than infinity: an
    # infinite rate is not a number any dashboard or aggregate can use, and it
    # poisons every average it lands in.
    tps = (len(timestamps) / elapsed_s) if elapsed_s > 0 else 0.0

    return InferenceMetrics(
        ttft_ms=ttft_s * 1000.0,
        itl_ms=itl_s * 1000.0,
        tps=tps,
        token_count=len(timestamps),
        total_ms=elapsed_s * 1000.0,
    )
