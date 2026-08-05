"""Gemini adapter — translates to and from the canonical OpenAI shape.

Gemini's differences: messages are ``contents`` with ``parts``, the assistant
role is ``model``, generation settings live under ``generationConfig``, the
system prompt is ``systemInstruction``, and the API key goes in a query
parameter rather than a header.
"""

from __future__ import annotations

from typing import Any

from apicost.core.ids import new_id
from apicost.proxy.providers.base import Usage

__all__ = ["GeminiProvider"]


class GeminiProvider:
    name = "gemini"
    base_url = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, base_url: str | None = None) -> None:
        if base_url:
            self.base_url = base_url.rstrip("/")

    def normalize_request(self, body: dict[str, Any], model: str) -> dict[str, Any]:
        contents: list[dict[str, Any]] = []
        system_parts: list[str] = []

        for message in body.get("messages", []):
            role = message.get("role")
            text = message.get("content", "")
            if role == "system":
                system_parts.append(text)
                continue
            contents.append(
                {
                    "role": "model" if role == "assistant" else "user",
                    "parts": [{"text": text}],
                }
            )

        request: dict[str, Any] = {"contents": contents}

        generation: dict[str, Any] = {}
        if "temperature" in body:
            generation["temperature"] = body["temperature"]
        if "top_p" in body:
            generation["topP"] = body["top_p"]
        if "max_tokens" in body:
            generation["maxOutputTokens"] = body["max_tokens"]
        if generation:
            request["generationConfig"] = generation

        if system_parts:
            request["systemInstruction"] = {
                "parts": [{"text": "\n\n".join(p for p in system_parts if p)}]
            }

        return request

    def denormalize_response(self, body: dict[str, Any]) -> dict[str, Any]:
        candidates = body.get("candidates", [])
        text = ""
        finish_reason = None

        if candidates:
            first = candidates[0]
            text = "".join(
                part.get("text", "")
                for part in first.get("content", {}).get("parts", [])
                if isinstance(part, dict)
            )
            finish_reason = _finish_reason(first.get("finishReason"))

        usage = body.get("usageMetadata", {})
        tokens_in = usage.get("promptTokenCount", 0)
        tokens_out = usage.get("candidatesTokenCount", 0)

        return {
            "id": f"chatcmpl-{new_id()}",
            "object": "chat.completion",
            "created": 0,
            "model": body.get("modelVersion", ""),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": tokens_in,
                "completion_tokens": tokens_out,
                "total_tokens": tokens_in + tokens_out,
            },
        }

    def parse_usage(self, body: dict[str, Any]) -> Usage | None:
        usage = body.get("usageMetadata")
        if not isinstance(usage, dict):
            return None
        tokens_in = usage.get("promptTokenCount")
        if not isinstance(tokens_in, int):
            return None
        tokens_out = usage.get("candidatesTokenCount", 0)
        return Usage(
            tokens_in=tokens_in,
            tokens_out=tokens_out if isinstance(tokens_out, int) else 0,
        )

    def auth_headers(self, api_key: str) -> dict[str, str]:
        """Gemini takes the key as a header rather than a query parameter.

        Both are accepted by the API; the header form is used because a key in
        a URL ends up in access logs, proxies, and browser history.
        """
        return {"x-goog-api-key": api_key}

    def endpoint_url(self, path: str) -> str:
        if path.lstrip("/").startswith("chat/completions"):
            return f"{self.base_url}/models"
        return f"{self.base_url}/{path.lstrip('/')}"

    def to_sse(self, chunk: dict[str, Any]) -> dict[str, Any]:
        candidates = chunk.get("candidates", [])
        content = ""
        finish_reason = None

        if candidates:
            first = candidates[0]
            content = "".join(
                part.get("text", "")
                for part in first.get("content", {}).get("parts", [])
                if isinstance(part, dict)
            )
            finish_reason = _finish_reason(first.get("finishReason"))

        return {
            "id": "",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": chunk.get("modelVersion", ""),
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": content} if content else {},
                    "finish_reason": finish_reason,
                }
            ],
        }


def _finish_reason(reason: str | None) -> str | None:
    return {
        "STOP": "stop",
        "MAX_TOKENS": "length",
        "SAFETY": "content_filter",
        "RECITATION": "content_filter",
    }.get(reason or "")
