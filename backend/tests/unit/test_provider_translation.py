"""Provider format translation — the product's central promise.

"Swap your base URL and nothing changes" only holds if an OpenAI-shaped request
becomes a correct Anthropic or Gemini request and the answer comes back
OpenAI-shaped. None of that conversion had a single test: the e2e stub is
OpenAI-shaped, so it exercises a near-identity path and proves nothing about
the other two.

These are unit tests on purpose. The conversion is pure dict-to-dict, so it can
be checked exhaustively without a network, and the cases that matter are the
structural ones — where the system prompt goes, what the assistant role is
called, which fields are mandatory — not whether a request succeeds.
"""

from __future__ import annotations

from typing import Any

import pytest

from apicost.proxy.providers.anthropic import DEFAULT_MAX_TOKENS, AnthropicProvider
from apicost.proxy.providers.gemini import GeminiProvider
from apicost.proxy.providers.openai import OpenAIProvider

OPENAI_REQUEST: dict[str, Any] = {
    "model": "gpt-4o",
    "messages": [
        {"role": "system", "content": "You are terse."},
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi."},
        {"role": "user", "content": "Explain gravity."},
    ],
    "temperature": 0.4,
    "max_tokens": 256,
    "stream": False,
}


# -- Anthropic --------------------------------------------------------------


def test_anthropic_lifts_the_system_prompt_out_of_messages() -> None:
    """Anthropic rejects a `system` role inside `messages`.

    Leaving it there is a 400 on every request carrying a system prompt, which
    is most production traffic.
    """
    request = AnthropicProvider().normalize_request(OPENAI_REQUEST, "claude-3-5-haiku-20241022")

    assert request["system"] == "You are terse."
    assert all(m["role"] != "system" for m in request["messages"])
    assert [m["role"] for m in request["messages"]] == ["user", "assistant", "user"]


def test_anthropic_always_sends_max_tokens() -> None:
    """It is required by their API. OpenAI treats it as optional."""
    without = {k: v for k, v in OPENAI_REQUEST.items() if k != "max_tokens"}
    request = AnthropicProvider().normalize_request(without, "claude-3-5-haiku-20241022")

    assert request["max_tokens"] == DEFAULT_MAX_TOKENS

    kept = AnthropicProvider().normalize_request(OPENAI_REQUEST, "claude-3-5-haiku-20241022")
    assert kept["max_tokens"] == 256


def test_anthropic_merges_multiple_system_messages() -> None:
    body = {
        "messages": [
            {"role": "system", "content": "Be terse."},
            {"role": "system", "content": "Answer in English."},
            {"role": "user", "content": "Hi"},
        ]
    }
    request = AnthropicProvider().normalize_request(body, "claude-3-5-haiku-20241022")
    assert request["system"] == "Be terse.\n\nAnswer in English."


def test_anthropic_translates_stop_to_stop_sequences() -> None:
    single = AnthropicProvider().normalize_request(
        {"messages": [], "stop": "END"}, "claude-3-5-haiku-20241022"
    )
    assert single["stop_sequences"] == ["END"]

    several = AnthropicProvider().normalize_request(
        {"messages": [], "stop": ["A", "B"]}, "claude-3-5-haiku-20241022"
    )
    assert several["stop_sequences"] == ["A", "B"]


def test_anthropic_routes_chat_completions_to_messages() -> None:
    provider = AnthropicProvider(base_url="https://example.test/v1")
    assert provider.endpoint_url("/chat/completions").endswith("/messages")
    assert provider.endpoint_url("chat/completions").endswith("/messages")


def test_anthropic_uses_its_own_auth_header() -> None:
    """A Bearer token would be rejected; Anthropic wants x-api-key."""
    headers = AnthropicProvider().auth_headers("sk-ant-secret")
    assert headers["x-api-key"] == "sk-ant-secret"
    assert headers["anthropic-version"]
    assert "Authorization" not in headers


def test_anthropic_response_becomes_openai_shaped() -> None:
    """The caller's SDK parses this. Every field it reads must be present."""
    body = {
        "id": "msg_1",
        "model": "claude-3-5-haiku-20241022",
        "content": [{"type": "text", "text": "Gravity is "}, {"type": "text", "text": "a force."}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 12, "output_tokens": 7},
    }
    result = AnthropicProvider().denormalize_response(body)

    assert result["object"] == "chat.completion"
    assert result["choices"][0]["message"] == {
        "role": "assistant",
        "content": "Gravity is a force.",
    }
    assert result["choices"][0]["finish_reason"] == "stop"
    assert result["usage"] == {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19}


def test_anthropic_maps_its_stop_reasons_to_openai_ones() -> None:
    provider = AnthropicProvider()
    for anthropic_reason, expected in [
        ("end_turn", "stop"),
        ("max_tokens", "length"),
        ("stop_sequence", "stop"),
    ]:
        result = provider.denormalize_response({"content": [], "stop_reason": anthropic_reason})
        assert result["choices"][0]["finish_reason"] == expected, anthropic_reason


def test_anthropic_usage_is_parsed_and_bad_usage_is_not_invented() -> None:
    provider = AnthropicProvider()

    usage = provider.parse_usage({"usage": {"input_tokens": 5, "output_tokens": 3}})
    assert usage is not None
    assert (usage.tokens_in, usage.tokens_out) == (5, 3)

    # None means "estimate it", which is honest. Zero would be a silent lie
    # that shows up as free requests in the user's spend.
    assert provider.parse_usage({}) is None
    assert provider.parse_usage({"usage": "nonsense"}) is None
    assert provider.parse_usage({"usage": {"input_tokens": "12"}}) is None


def test_anthropic_stream_events_become_openai_chunks() -> None:
    provider = AnthropicProvider()

    text_chunk = provider.to_sse(
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hel"}}
    )
    assert text_chunk["object"] == "chat.completion.chunk"
    assert text_chunk["choices"][0]["delta"]["content"] == "Hel"

    # Anthropic emits several event types that carry no text. They must become
    # harmless empty deltas, not crashes and not stray empty strings.
    for event in ("message_start", "content_block_start", "ping", "content_block_stop"):
        chunk = provider.to_sse({"type": event})
        assert chunk["choices"][0]["delta"] == {}

    final = provider.to_sse({"type": "message_delta", "delta": {"stop_reason": "end_turn"}})
    assert final["choices"][0]["finish_reason"] == "stop"


# -- Gemini -----------------------------------------------------------------


def test_gemini_renames_messages_and_the_assistant_role() -> None:
    """Gemini calls them `contents`, and the assistant is `model`."""
    request = GeminiProvider().normalize_request(OPENAI_REQUEST, "gemini-1.5-flash")

    assert "messages" not in request
    assert [c["role"] for c in request["contents"]] == ["user", "model", "user"]
    assert request["contents"][0]["parts"] == [{"text": "Hello"}]


def test_gemini_lifts_the_system_prompt_to_system_instruction() -> None:
    request = GeminiProvider().normalize_request(OPENAI_REQUEST, "gemini-1.5-flash")

    assert request["systemInstruction"]["parts"] == [{"text": "You are terse."}]
    assert all(c["role"] != "system" for c in request["contents"])


def test_gemini_moves_generation_settings_under_generation_config() -> None:
    request = GeminiProvider().normalize_request(OPENAI_REQUEST, "gemini-1.5-flash")

    assert "temperature" not in request
    assert request["generationConfig"]["temperature"] == 0.4


def test_gemini_uses_its_own_auth_header() -> None:
    headers = GeminiProvider().auth_headers("AIzaSecret")
    assert headers["x-goog-api-key"] == "AIzaSecret"
    assert "Authorization" not in headers


def test_gemini_response_becomes_openai_shaped() -> None:
    body = {
        "candidates": [
            {
                "content": {"parts": [{"text": "Gravity "}, {"text": "bends spacetime."}]},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 4},
    }
    result = GeminiProvider().denormalize_response(body)

    assert result["object"] == "chat.completion"
    assert result["choices"][0]["message"]["content"] == "Gravity bends spacetime."
    assert result["choices"][0]["message"]["role"] == "assistant"
    assert result["usage"]["prompt_tokens"] == 9
    assert result["usage"]["completion_tokens"] == 4


def test_gemini_usage_is_parsed_and_bad_usage_is_not_invented() -> None:
    provider = GeminiProvider()

    usage = provider.parse_usage(
        {"usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 4}}
    )
    assert usage is not None
    assert (usage.tokens_in, usage.tokens_out) == (9, 4)

    assert provider.parse_usage({}) is None
    assert provider.parse_usage({"usageMetadata": "nonsense"}) is None


# -- Shared contract --------------------------------------------------------


@pytest.mark.parametrize(
    "provider", [OpenAIProvider(), AnthropicProvider(), GeminiProvider()], ids=lambda p: p.name
)
def test_every_provider_survives_an_empty_or_malformed_body(provider: Any) -> None:
    """A provider that raises here turns a bad request into a 500."""
    for body in ({}, {"messages": []}, {"messages": [{"role": "user"}]}):
        provider.normalize_request(body, "some-model")

    for body in ({}, {"choices": []}, {"content": []}, {"candidates": []}):
        result = provider.denormalize_response(body)
        assert isinstance(result, dict)


@pytest.mark.parametrize(
    "provider", [OpenAIProvider(), AnthropicProvider(), GeminiProvider()], ids=lambda p: p.name
)
def test_every_denormalized_response_has_the_fields_an_sdk_reads(provider: Any) -> None:
    """Hard rule 6 in practice: the caller's SDK must not notice us."""
    samples = {
        "openai": {
            "id": "x",
            "object": "chat.completion",
            "model": "gpt-4o",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hi"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
        "anthropic": {
            "id": "msg_1",
            "model": "claude-3-5-haiku-20241022",
            "content": [{"type": "text", "text": "hi"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
        "gemini": {
            "candidates": [{"content": {"parts": [{"text": "hi"}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
        },
    }
    result = provider.denormalize_response(samples[provider.name])

    assert result["object"] == "chat.completion"
    assert isinstance(result["choices"], list) and result["choices"]

    choice = result["choices"][0]
    assert choice["index"] == 0
    assert choice["message"]["role"] == "assistant"
    assert isinstance(choice["message"]["content"], str)
    assert "finish_reason" in choice

    usage = result["usage"]
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]


@pytest.mark.parametrize(
    "provider", [OpenAIProvider(), AnthropicProvider(), GeminiProvider()], ids=lambda p: p.name
)
def test_no_provider_echoes_the_api_key_into_a_request_body(provider: Any) -> None:
    """Keys belong in headers. A key in a body ends up in a cached response."""
    request = provider.normalize_request(OPENAI_REQUEST, "some-model")
    assert "sk-" not in str(request)
    assert "api_key" not in request
