"""Prompt embeddings — `BAAI/bge-small-en-v1.5` via fastembed, in-process.

384 dimensions, CPU, no network hop on the hot path (BUILD_SPEC §2).

Two things here are load-bearing for the proxy's latency, and both are easy to
get wrong:

**fastembed is synchronous CPU work.** Calling it directly from an async
handler blocks the event loop for the duration — not just this request, but
every other request the process is serving. So embedding runs in a thread, and
the GIL is released inside ONNX for the actual inference.

**Model load takes seconds.** It happens once, at startup, before the process
accepts traffic. A lazy first-request load would put multi-second latency on
one unlucky user's request and blow the deadline for everyone queued behind it.

Everything here is best-effort: :func:`embed` returns ``None`` rather than
raising, and a ``None`` means the request proceeds as a cache miss. A broken
embedder must never break a completion (hard rule 1).
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import TYPE_CHECKING, Any, Final

from apicost.config import Settings, get_settings
from apicost.core.logging import get_logger

if TYPE_CHECKING:
    pass

__all__ = [
    "EMBEDDING_DIMENSIONS",
    "MODEL_NAME",
    "embed",
    "embedding_is_ready",
    "shutdown_embedder",
    "to_pgvector",
    "warm_embedder",
]

MODEL_NAME: Final = "BAAI/bge-small-en-v1.5"
EMBEDDING_DIMENSIONS: Final = 384

_logger = get_logger(__name__)

_model: Any | None = None
_model_lock = threading.Lock()
_load_failed = False


def _load_model() -> Any | None:
    """Load the ONNX model. Synchronous and slow; called once."""
    global _model, _load_failed

    with _model_lock:
        if _model is not None:
            return _model
        if _load_failed:
            return None

        try:
            from fastembed import TextEmbedding

            started = time.perf_counter()
            _model = TextEmbedding(model_name=MODEL_NAME)
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            _logger.info("embedder_loaded", model=MODEL_NAME, load_ms=round(elapsed_ms, 1))
            return _model
        except Exception:
            # Missing `ml` dependency group, no disk space for the model, no
            # network to fetch it on first run. Caching is simply off; the
            # proxy keeps working.
            _load_failed = True
            _logger.warning(
                "embedder_unavailable", model=MODEL_NAME, subsystem="cache", exc_info=True
            )
            return None


async def warm_embedder(settings: Settings | None = None) -> bool:
    """Load and exercise the model at startup.

    Returns whether the embedder is usable. Called from the proxy's lifespan so
    the cost is paid before the process serves anyone.

    The model is also *run* once, not merely loaded: the first inference does
    lazy allocation inside ONNX, and leaving that for a real request would put
    it on the critical path exactly once, unpredictably.
    """
    del settings  # accepted for symmetry with the other startup hooks

    def _warm() -> bool:
        model = _load_model()
        if model is None:
            return False
        list(model.embed(["warmup"]))
        return True

    try:
        ready = await asyncio.to_thread(_warm)
    except Exception:
        _logger.warning("embedder_warmup_failed", subsystem="cache", exc_info=True)
        return False

    if ready:
        _logger.info("embedder_ready", model=MODEL_NAME, dimensions=EMBEDDING_DIMENSIONS)
    return ready


def embedding_is_ready() -> bool:
    """Whether the model is loaded and usable, without attempting a load."""
    return _model is not None


def _embed_sync(text: str) -> list[float] | None:
    model = _load_model()
    if model is None:
        return None
    vectors = list(model.embed([text]))
    if not vectors:
        return None
    return [float(value) for value in vectors[0]]


async def embed(text: str, *, budget_ms: float | None = None) -> list[float] | None:
    """Embed one prompt, or return ``None``.

    Args:
        text: The normalized prompt (see ``cache/policy.py``).
        budget_ms: Ceiling for this call. Defaults to the configured embedding
            budget. Overrunning it returns ``None`` and the request proceeds as
            a miss (BUILD_SPEC §4 P4).

    Returns ``None`` on timeout, on any failure, and when the model could not
    be loaded. Never raises.
    """
    if not text.strip():
        return None

    allowance = budget_ms if budget_ms is not None else get_settings().embedding_budget_ms

    try:
        async with asyncio.timeout(allowance / 1000.0):
            # to_thread, not inline: fastembed is synchronous CPU work, and
            # running it on the event loop would stall every other request in
            # this process for the duration.
            return await asyncio.to_thread(_embed_sync, text)
    except TimeoutError:
        _logger.warning("embedding_budget_exceeded", subsystem="cache", budget_ms=allowance)
        return None
    except Exception:
        _logger.warning("embedding_failed", subsystem="cache", exc_info=True)
        return None


def to_pgvector(vector: list[float]) -> str:
    """Render a vector in the literal form pgvector parses.

    A plain Python list would be adapted as an array, not a vector, and the
    cast would fail at query time rather than here.
    """
    return "[" + ",".join(f"{value:.7g}" for value in vector) + "]"


async def shutdown_embedder() -> None:
    """Drop the model. Called from app shutdown and by tests."""
    global _model, _load_failed
    _model = None
    _load_failed = False
