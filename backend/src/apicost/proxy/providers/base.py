"""Provider protocol and the shared HTTP client.

The **OpenAI request/response shape is the canonical internal representation**
(BUILD_SPEC §6.2). The OpenAI adapter is therefore close to identity; the
Anthropic and Gemini adapters translate into it and back out.

Why that matters for correctness: the caller's SDK parses whatever we return,
so a response must be byte-compatible with what the provider it *thinks* it is
talking to would have sent. Our own metadata rides in headers, never in the
body (BUILD_SPEC §0.5).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import httpx

from apicost.config import Settings, get_settings
from apicost.core.errors import UpstreamError
from apicost.core.logging import get_logger

__all__ = [
    "Provider",
    "ProviderResponse",
    "Usage",
    "close_http_client",
    "get_http_client",
]

_logger = get_logger(__name__)

_client: httpx.AsyncClient | None = None


def get_http_client(settings: Settings | None = None) -> httpx.AsyncClient:
    """One pooled client per process (BUILD_SPEC §2).

    A client per request would pay TCP and TLS setup on every call, which is
    the single easiest way to blow the <100 ms overhead budget.
    """
    global _client
    if _client is None:
        cfg = settings or get_settings()
        _client = httpx.AsyncClient(
            http2=True,
            timeout=httpx.Timeout(cfg.provider_timeout_seconds, connect=5.0),
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
            follow_redirects=False,
        )
    return _client


async def close_http_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None


@dataclass(frozen=True)
class Usage:
    """Token accounting for one request."""

    tokens_in: int
    tokens_out: int
    estimated: bool = False
    """True when these came from estimation rather than the provider's usage
    block. Recorded on the ledger row so cost accuracy is never silently
    overstated (BUILD_SPEC §6.2)."""

    @property
    def total(self) -> int:
        return self.tokens_in + self.tokens_out


@dataclass
class ProviderResponse:
    """A non-streamed provider response, already translated to OpenAI shape."""

    status_code: int
    body: dict[str, Any]
    usage: Usage
    model: str
    headers: dict[str, str]


@runtime_checkable
class Provider(Protocol):
    """What every provider adapter implements."""

    name: str
    base_url: str

    def normalize_request(self, body: dict[str, Any], model: str) -> dict[str, Any]:
        """OpenAI-shaped request to this provider's native shape."""
        ...

    def denormalize_response(self, body: dict[str, Any]) -> dict[str, Any]:
        """This provider's native response to OpenAI shape."""
        ...

    def parse_usage(self, body: dict[str, Any]) -> Usage | None:
        """Token counts from a response body, or ``None`` if absent."""
        ...

    def auth_headers(self, api_key: str) -> dict[str, str]:
        """Headers carrying the caller's provider key."""
        ...

    def endpoint_url(self, path: str) -> str:
        """Absolute URL for an OpenAI-style path such as ``/chat/completions``."""
        ...

    def to_sse(self, chunk: dict[str, Any]) -> dict[str, Any]:
        """A native streaming chunk translated to an OpenAI-shaped SSE chunk."""
        ...


def estimate_tokens(text: str) -> int:
    """Rough token estimate for when a provider omits usage.

    Deliberately crude — roughly four characters per token, the widely used
    English approximation. Anything better means shipping a tokenizer per
    provider onto the hot path, which the latency budget cannot afford. Rows
    costed this way are flagged ``tokens_estimated`` precisely because this
    number should not be trusted to the dollar.
    """
    return max(1, len(text) // 4)


def raise_for_upstream(response: httpx.Response, provider: str) -> None:
    """Turn a transport-level failure into a typed error.

    Provider *application* errors (a 400 for a bad model, a 429) are passed
    back to the caller verbatim by the pipeline — the user's error handling
    should work exactly as it did before they installed us (CODEBASE_GUIDE
    §12). This is only for responses we cannot forward at all.
    """
    if response.status_code >= 500:
        _logger.warning("provider_server_error", provider=provider, status=response.status_code)
        raise UpstreamError(f"{provider} returned {response.status_code}")
