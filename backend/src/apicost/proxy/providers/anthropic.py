"""Anthropic adapter — translates to and from the canonical OpenAI shape.

The differences that matter:

* the system prompt is a top-level ``system`` field, not a message with
  ``role: "system"``;
* ``max_tokens`` is required;
* usage is ``input_tokens`` / ``output_tokens``;
* streaming is a typed event sequence (``content_block_delta`` and friends)
  rather than OpenAI's uniform chunk shape.
"""

from __future__ import annotations

from typing import Any

from apicost.core.ids import new_id
from apicost.proxy.providers.base import Usage

__all__ = ["AnthropicProvider"]

DEFAULT_MAX_TOKENS = 4096
"""Anthropic requires max_tokens; OpenAI does not. Callers who omit it expect
the provider default, so we supply one rather than rejecting the request."""


class AnthropicProvider:
    name = "anthropic"
    base_url = "https://api.anthropic.com/v1"
    api_version = "2023-06-01"

    def __init__(self, base_url: str | None = None) -> None:
        if base_url:
            self.base_url = base_url.rstrip("/")

    def normalize_request(self, body: dict[str, Any], model: str) -> dict[str, Any]:
        messages = body.get("messages", [])

        system_parts = [m.get("content", "") for m in messages if m.get("role") == "system"]
        conversation = [m for m in messages if m.get("role") != "system"]

        request: dict[str, Any] = {
            "model": model,
            "messages": conversation,
            "max_tokens": body.get("max_tokens", DEFAULT_MAX_TOKENS),
        }
        if system_parts:
            request["system"] = "\n\n".join(part for part in system_parts if part)
        for passthrough in ("temperature", "top_p", "stop_sequences", "stream"):
            if passthrough in body:
                request[passthrough] = body[passthrough]
        if "stop" in body and "stop_sequences" not in request:
            stop = body["stop"]
            request["stop_sequences"] = [stop] if isinstance(stop, str) else stop

        return request

    def denormalize_response(self, body: dict[str, Any]) -> dict[str, Any]:
        text = "".join(
            block.get("text", "")
            for block in body.get("content", [])
            if isinstance(block, dict) and block.get("type") == "text"
        )

        usage = body.get("usage", {})
        tokens_in = usage.get("input_tokens", 0)
        tokens_out = usage.get("output_tokens", 0)

        return {
            "id": body.get("id", f"chatcmpl-{new_id()}"),
            "object": "chat.completion",
            "created": 0,
            "model": body.get("model", ""),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": _finish_reason(body.get("stop_reason")),
                }
            ],
            "usage": {
                "prompt_tokens": tokens_in,
                "completion_tokens": tokens_out,
                "total_tokens": tokens_in + tokens_out,
            },
        }

    def parse_usage(self, body: dict[str, Any]) -> Usage | None:
        usage = body.get("usage")
        if not isinstance(usage, dict):
            return None
        tokens_in = usage.get("input_tokens")
        if not isinstance(tokens_in, int):
            return None
        tokens_out = usage.get("output_tokens", 0)
        return Usage(
            tokens_in=tokens_in,
            tokens_out=tokens_out if isinstance(tokens_out, int) else 0,
        )

    def auth_headers(self, api_key: str) -> dict[str, str]:
        return {"x-api-key": api_key, "anthropic-version": self.api_version}

    def endpoint_url(self, path: str) -> str:
        # OpenAI's /chat/completions is Anthropic's /messages.
        if path.lstrip("/").startswith("chat/completions"):
            return f"{self.base_url}/messages"
        return f"{self.base_url}/{path.lstrip('/')}"

    def to_sse(self, chunk: dict[str, Any]) -> dict[str, Any]:
        """One Anthropic stream event as an OpenAI-shaped chunk.

        Only ``content_block_delta`` carries text; the other event types map to
        an empty delta, which a client can safely ignore.
        """
        event_type = chunk.get("type")
        content = ""
        finish_reason = None

        if event_type == "content_block_delta":
            content = chunk.get("delta", {}).get("text", "")
        elif event_type == "message_delta":
            finish_reason = _finish_reason(chunk.get("delta", {}).get("stop_reason"))

        return {
            "id": chunk.get("message", {}).get("id", ""),
            "object": "chat.completion.chunk",
            "created": 0,
            "model": chunk.get("message", {}).get("model", ""),
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": content} if content else {},
                    "finish_reason": finish_reason,
                }
            ],
        }


def _finish_reason(stop_reason: str | None) -> str | None:
    """Anthropic stop reasons in OpenAI's vocabulary."""
    return {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "max_tokens": "length",
        "tool_use": "tool_calls",
    }.get(stop_reason or "")
