"""SSE parsing, teeing, and replay — proxy/streaming.py.

The property under test throughout: **the client's bytes are never altered and
never delayed.** Everything else here is bookkeeping that must not be able to
interfere with delivery.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest

from apicost.proxy.streaming import (
    DONE_SENTINEL,
    StreamCapture,
    iter_sse_events,
    replay_as_sse,
    sse_line,
    tee_stream,
)


def chunk(content: str, *, finish: str | None = None) -> dict[str, object]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "model": "gpt-4o",
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": finish}],
    }


async def as_stream(*payloads: bytes, split_at: int | None = None) -> AsyncIterator[bytes]:
    for payload in payloads:
        if split_at is not None and len(payload) > split_at:
            # Deliver mid-event to prove reassembly works.
            yield payload[:split_at]
            yield payload[split_at:]
        else:
            yield payload


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


async def test_parses_events() -> None:
    stream = as_stream(sse_line(chunk("Hello")), sse_line(chunk(" world")))
    events = [payload async for _raw, payload in iter_sse_events(stream)]

    assert len(events) == 2
    assert events[0] is not None
    assert events[0]["choices"][0]["delta"]["content"] == "Hello"


async def test_reassembles_events_split_across_network_reads() -> None:
    """TCP does not respect message boundaries; the parser must not either."""
    stream = as_stream(sse_line(chunk("Hello")), split_at=12)
    events = [payload async for _raw, payload in iter_sse_events(stream)]

    assert len(events) == 1
    assert events[0] is not None


async def test_done_sentinel_yields_no_payload() -> None:
    stream = as_stream(f"data: {DONE_SENTINEL}\n\n".encode())
    events = [(raw, payload) async for raw, payload in iter_sse_events(stream)]

    assert len(events) == 1
    assert events[0][1] is None


async def test_malformed_json_is_forwarded_not_dropped() -> None:
    """The client's SDK is a better judge of what it can handle than we are."""
    stream = as_stream(b"data: {not json at all}\n\n")
    events = [(raw, payload) async for raw, payload in iter_sse_events(stream)]

    assert len(events) == 1
    assert events[0][1] is None
    assert b"not json at all" in events[0][0]


# ---------------------------------------------------------------------------
# Teeing
# ---------------------------------------------------------------------------


async def test_tee_forwards_bytes_unaltered() -> None:
    frames = [sse_line(chunk("Hel")), sse_line(chunk("lo")), f"data: {DONE_SENTINEL}\n\n".encode()]
    capture = StreamCapture()

    forwarded = b"".join([piece async for piece in tee_stream(as_stream(*frames), capture)])

    assert forwarded == b"".join(frames), "the tee altered the client's bytes"


async def test_tee_records_timestamps_and_content() -> None:
    frames = [
        sse_line(chunk("Hello")),
        sse_line(chunk(" world", finish="stop")),
        f"data: {DONE_SENTINEL}\n\n".encode(),
    ]
    capture = StreamCapture()

    async for _ in tee_stream(as_stream(*frames), capture):
        pass

    assert capture.content_chunks == 2
    assert len(capture.chunk_timestamps) == 2
    assert capture.text_length == len("Hello") + len(" world")
    assert capture.finish_reason == "stop"
    assert capture.model == "gpt-4o"
    assert capture.completed


async def test_timestamps_are_monotonic() -> None:
    """compute_inference_metrics rejects out-of-order input, so this must hold."""
    frames = [sse_line(chunk(str(index))) for index in range(20)]
    capture = StreamCapture()

    async for _ in tee_stream(as_stream(*frames), capture):
        pass

    stamps = capture.chunk_timestamps
    assert stamps == sorted(stamps)


async def test_tee_captures_a_usage_block_when_present() -> None:
    payload = {**chunk(""), "usage": {"prompt_tokens": 11, "completion_tokens": 22}}
    capture = StreamCapture()

    async for _ in tee_stream(as_stream(sse_line(payload)), capture):
        pass

    assert capture.usage is not None
    assert capture.usage.tokens_in == 11
    assert capture.usage.tokens_out == 22
    assert capture.usage.estimated is False


async def test_stream_without_done_is_marked_incomplete() -> None:
    """A truncated stream is recorded as truncated, not as a clean finish."""
    capture = StreamCapture()

    async for _ in tee_stream(as_stream(sse_line(chunk("partial"))), capture):
        pass

    assert not capture.completed


async def test_on_chunk_failure_does_not_break_the_stream() -> None:
    """Observability must never break the thing it observes."""
    frames = [sse_line(chunk("a")), sse_line(chunk("b"))]
    capture = StreamCapture()

    def explode(_payload: dict[str, object]) -> None:
        raise RuntimeError("observer blew up")

    forwarded = b"".join(
        [piece async for piece in tee_stream(as_stream(*frames), capture, on_chunk=explode)]
    )

    assert forwarded == b"".join(frames)
    assert capture.content_chunks == 2


async def test_upstream_failure_marks_incomplete_and_propagates() -> None:
    async def failing() -> AsyncIterator[bytes]:
        yield sse_line(chunk("partial"))
        raise ConnectionError("upstream died")

    capture = StreamCapture()

    with pytest.raises(ConnectionError):
        async for _ in tee_stream(failing(), capture):
            pass

    assert not capture.completed


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


async def test_replay_reconstructs_the_full_text() -> None:
    body = {
        "id": "chatcmpl-cached",
        "model": "gpt-4o",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Hello there"},
                "finish_reason": "stop",
            }
        ],
    }

    frames = [frame async for frame in replay_as_sse(body, chunk_size=4)]
    joined = b"".join(frames)

    text = ""
    for frame in frames:
        line = frame.decode().removeprefix("data: ").strip()
        if line == DONE_SENTINEL:
            continue
        payload = json.loads(line)
        text += payload["choices"][0]["delta"].get("content", "")

    assert text == "Hello there"
    assert joined.endswith(f"data: {DONE_SENTINEL}\n\n".encode())


async def test_replay_is_indistinguishable_from_a_live_stream() -> None:
    """A cache hit must not be detectable by the client's parser (§4 P4)."""
    body = {
        "id": "chatcmpl-cached",
        "model": "gpt-4o",
        "choices": [{"index": 0, "message": {"content": "hi"}, "finish_reason": "stop"}],
    }

    frames = [frame async for frame in replay_as_sse(body)]

    for frame in frames[:-1]:
        payload = json.loads(frame.decode().removeprefix("data: ").strip())
        assert payload["object"] == "chat.completion.chunk"
        assert "delta" in payload["choices"][0]

    assert (
        json.loads(frames[-2].decode().removeprefix("data: ").strip())["choices"][0][
            "finish_reason"
        ]
        == "stop"
    )


async def test_replay_handles_an_empty_completion() -> None:
    body = {"id": "x", "model": "gpt-4o", "choices": []}
    frames = [frame async for frame in replay_as_sse(body)]
    assert frames[-1] == f"data: {DONE_SENTINEL}\n\n".encode()
