"""Server-sent events: parse, tee, and replay.

The constraint that shapes this whole file: **the tee must not buffer.** Most
LLM clients stream because the user is watching tokens appear, so holding a
chunk back to inspect it converts our observability into their perceived
latency. Every function here yields the bytes downstream *first* and records
afterwards.

Three jobs:

* :func:`iter_sse_events` — parse an SSE byte stream into events.
* :func:`tee_stream` — pass bytes through untouched while capturing chunk
  arrival times and token counts (BUILD_SPEC §4 P2).
* :func:`replay_as_sse` — re-chunk a complete response body back into SSE, for
  when a cache hit has to answer a ``stream: true`` request (§4 P4).
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from apicost.core.logging import get_logger
from apicost.proxy.providers.base import Usage

__all__ = [
    "DONE_SENTINEL",
    "StreamCapture",
    "iter_sse_events",
    "replay_as_sse",
    "sse_line",
    "tee_stream",
]

DONE_SENTINEL = "[DONE]"

_logger = get_logger(__name__)


def sse_line(payload: dict[str, Any]) -> bytes:
    """Encode one JSON payload as an SSE ``data:`` frame."""
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n".encode()


@dataclass
class StreamCapture:
    """What the tee observed, available once the stream completes."""

    chunk_timestamps: list[float] = field(default_factory=list)
    """Arrival times in seconds from ``time.perf_counter()`` — the monotonic
    clock BUILD_SPEC §6.6 requires at the capture site."""

    content_chunks: int = 0
    usage: Usage | None = None
    finish_reason: str | None = None
    model: str | None = None
    text_length: int = 0
    text: str = ""
    """The assembled completion. Needed to cache a streamed response, which is
    otherwise never held in one piece (§4 P4)."""
    completed: bool = False
    """False when the stream ended without a ``[DONE]`` — a disconnect or an
    upstream error mid-flight. Such rows are still ledgered, flagged."""

    @property
    def first_token_at(self) -> float | None:
        return self.chunk_timestamps[0] if self.chunk_timestamps else None


async def iter_sse_events(
    stream: AsyncIterator[bytes],
) -> AsyncIterator[tuple[bytes, dict[str, Any] | None]]:
    """Yield ``(raw_bytes, parsed_payload)`` for each SSE event.

    ``parsed_payload`` is ``None`` for the ``[DONE]`` sentinel, for comments,
    and for anything that fails to parse — a malformed chunk is forwarded
    verbatim rather than dropped, because the client's SDK is a better judge of
    what it can handle than we are.
    """
    buffer = b""

    async for piece in stream:
        buffer += piece

        # SSE events are separated by a blank line. Split on that, keeping any
        # partial trailing event in the buffer.
        while b"\n\n" in buffer:
            raw, buffer = buffer.split(b"\n\n", 1)
            raw_event = raw + b"\n\n"

            payload: dict[str, Any] | None = None
            for line in raw.split(b"\n"):
                if not line.startswith(b"data:"):
                    continue
                data = line[len(b"data:") :].strip()
                if data == DONE_SENTINEL.encode():
                    break
                try:
                    decoded = json.loads(data)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    break
                if isinstance(decoded, dict):
                    payload = decoded
                break

            yield raw_event, payload

    if buffer:
        yield buffer, None


async def tee_stream(
    stream: AsyncIterator[bytes],
    capture: StreamCapture,
    *,
    on_chunk: Callable[[dict[str, Any]], None] | None = None,
) -> AsyncIterator[bytes]:
    """Forward an SSE stream byte-for-byte while recording what went past.

    The client sees exactly what the provider sent, in the order it was sent,
    with no added buffering: each event is yielded before it is inspected.
    """
    try:
        async for raw_event, payload in iter_sse_events(stream):
            # Downstream first. Everything after this line is bookkeeping.
            yield raw_event

            if raw_event.strip().endswith(DONE_SENTINEL.encode()):
                capture.completed = True
                continue

            if payload is None:
                continue

            capture.chunk_timestamps.append(time.perf_counter())
            _absorb(capture, payload)

            if on_chunk is not None:
                try:
                    on_chunk(payload)
                except Exception:
                    # Observability must never break the stream it observes.
                    _logger.warning("stream_on_chunk_failed", subsystem="streaming")
    except Exception:
        # The stream broke mid-flight. The client sees a truncated response,
        # which is what actually happened; we record it rather than pretending
        # the request completed.
        capture.completed = False
        _logger.warning("stream_interrupted", subsystem="streaming", exc_info=True)
        raise


def _absorb(capture: StreamCapture, payload: dict[str, Any]) -> None:
    """Pull the fields worth recording out of one chunk."""
    if capture.model is None and isinstance(payload.get("model"), str):
        capture.model = payload["model"]

    # Some providers emit a usage block on the final chunk when asked to.
    usage = payload.get("usage")
    if isinstance(usage, dict):
        tokens_in = usage.get("prompt_tokens")
        tokens_out = usage.get("completion_tokens", 0)
        if isinstance(tokens_in, int):
            capture.usage = Usage(
                tokens_in=tokens_in,
                tokens_out=tokens_out if isinstance(tokens_out, int) else 0,
            )

    for choice in payload.get("choices", []):
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta")
        if isinstance(delta, dict):
            content = delta.get("content")
            if isinstance(content, str) and content:
                capture.content_chunks += 1
                capture.text_length += len(content)
                capture.text += content
        if choice.get("finish_reason"):
            capture.finish_reason = choice["finish_reason"]


async def replay_as_sse(body: dict[str, Any], *, chunk_size: int = 24) -> AsyncIterator[bytes]:
    """Re-chunk a complete response into an SSE stream.

    Used when a cached response answers a request that asked to stream (§4 P4).
    The client must not be able to tell the difference, so the frames carry the
    ``chat.completion.chunk`` object type and terminate with ``[DONE]`` exactly
    as a live stream would.
    """
    content = ""
    finish_reason = "stop"
    choices = body.get("choices", [])
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message", {})
        if isinstance(message, dict):
            content = message.get("content") or ""
        finish_reason = choices[0].get("finish_reason") or "stop"

    template: dict[str, Any] = {
        "id": body.get("id", ""),
        "object": "chat.completion.chunk",
        "created": body.get("created", 0),
        "model": body.get("model", ""),
    }

    # An initial chunk carrying the role, matching what providers send.
    yield sse_line(
        {
            **template,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
    )

    for start in range(0, len(content), chunk_size):
        piece = content[start : start + chunk_size]
        yield sse_line(
            {
                **template,
                "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
            }
        )

    yield sse_line(
        {**template, "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]}
    )
    yield f"data: {DONE_SENTINEL}\n\n".encode()
