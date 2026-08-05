"""OpenAI adapter — the canonical shape.

Normalization and denormalization are identity here by definition: the OpenAI
request/response shape *is* our internal representation (BUILD_SPEC §6.2). The
adapter exists so the pipeline has one uniform interface, not because there is
translation to do.
"""

from __future__ import annotations

from typing import Any

from apicost.proxy.providers.base import Usage

__all__ = ["OpenAIProvider"]


class OpenAIProvider:
    name = "openai"
    base_url = "https://api.openai.com/v1"

    def __init__(self, base_url: str | None = None) -> None:
        # Overridable so the e2e suite can point at a stub provider.
        if base_url:
            self.base_url = base_url.rstrip("/")

    def normalize_request(self, body: dict[str, Any], model: str) -> dict[str, Any]:
        """Identity, apart from the model the router chose."""
        if body.get("model") == model:
            return body
        return {**body, "model": model}

    def denormalize_response(self, body: dict[str, Any]) -> dict[str, Any]:
        return body

    def parse_usage(self, body: dict[str, Any]) -> Usage | None:
        usage = body.get("usage")
        if not isinstance(usage, dict):
            return None

        tokens_in = usage.get("prompt_tokens")
        tokens_out = usage.get("completion_tokens", 0)
        if not isinstance(tokens_in, int):
            return None

        return Usage(
            tokens_in=tokens_in,
            tokens_out=tokens_out if isinstance(tokens_out, int) else 0,
            estimated=False,
        )

    def auth_headers(self, api_key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {api_key}"}

    def endpoint_url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def to_sse(self, chunk: dict[str, Any]) -> dict[str, Any]:
        return chunk
