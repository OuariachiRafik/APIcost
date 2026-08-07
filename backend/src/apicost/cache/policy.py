"""What may be cached, and for how long — UC-24.

Pure: no I/O, no ORM (CODEBASE_GUIDE §9). This module is the main safety
mechanism for the whole caching feature, and it deserves the scrutiny that
implies (§13): serving a stale answer to a question whose answer has changed is
the one way semantic caching can actively harm a user, and everything else in
`cache/` assumes this file said yes.

The bias is deliberately conservative. A false negative costs a few cents. A
false positive returns wrong data to somebody's production application.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "NO_CACHE_HEADER",
    "TEMPERATURE_CEILING",
    "CacheDecision",
    "is_cacheable",
    "normalize_prompt",
]

NO_CACHE_HEADER: Final = "X-APICost-No-Cache"

TEMPERATURE_CEILING: Final = 0.7
"""Above this the caller is explicitly asking for variety, and replaying one
answer defeats the thing they asked for (UC-24)."""

MAX_CACHEABLE_TOKENS_ESTIMATE: Final = 100_000
"""Very large prompts are rarely repeated and cost the most to embed and store."""

# Markers that a prompt's correct answer depends on *now*. Caching these is how
# a user ends up being told the wrong date by their own application.
#
# Deliberately broad. "current" catches "the current directory" as well as "the
# current president", and refusing the former costs a fraction of a cent —
# whereas caching the latter returns a confidently wrong answer to production
# traffic. The asymmetry justifies the false positives.
_TIME_SENSITIVE_PATTERNS: Final = re.compile(
    r"\b("
    r"today|tonight|right now|current|currently|as of now|"
    r"this (?:morning|afternoon|evening|week|month|year)|"
    r"yesterday|tomorrow|latest|most recent|up to date|breaking"
    r")\b",
    re.IGNORECASE,
)

# An embedded timestamp or date almost always means the prompt is templated
# with the current time, so no two are ever really the same request.
_TIMESTAMP_PATTERNS: Final = re.compile(
    r"("
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}"  # ISO 8601
    r"|\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}"  # ISO-ish with a space
    r"|\b\d{10,13}\b"  # unix seconds or millis
    r"|\b\d{2}:\d{2}:\d{2}\b"  # wall clock
    r")"
)

_WHITESPACE: Final = re.compile(r"\s+")

# Fields that vary between otherwise identical requests. Dropped before
# embedding so they cannot make two equivalent prompts look different.
_NON_DETERMINISTIC_FIELDS: Final = frozenset(
    {"user", "metadata", "stream", "stream_options", "n", "seed", "store"}
)


@dataclass(frozen=True)
class CacheDecision:
    """Whether a request may be cached, and why not when it may not."""

    cacheable: bool
    reason: str
    """A machine-readable code, surfaced on the request row so a user asking
    'why did this never cache?' gets an answer rather than a shrug."""

    def __bool__(self) -> bool:
        return self.cacheable


def is_cacheable(
    body: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    cache_enabled: bool = True,
    excluded_endpoints: frozenset[str] | None = None,
    endpoint: str = "chat/completions",
) -> CacheDecision:
    """Decide whether a request may be served from, or written to, the cache.

    Checks run cheapest-first, and the first refusal wins.
    """
    if not cache_enabled:
        return CacheDecision(False, "CACHE_DISABLED")

    # An explicit per-request opt-out always wins (UC-24).
    if headers:
        lowered = {key.lower(): value for key, value in headers.items()}
        marker = lowered.get(NO_CACHE_HEADER.lower(), "")
        if marker.strip().lower() in {"1", "true", "yes"}:
            return CacheDecision(False, "NO_CACHE_HEADER")

    if excluded_endpoints and endpoint in excluded_endpoints:
        return CacheDecision(False, "EXCLUDED_ENDPOINT")

    # Embeddings are logged passthrough only in v1 (CODEBASE_GUIDE §13).
    if not endpoint.startswith("chat/completions"):
        return CacheDecision(False, "ENDPOINT_NOT_CACHEABLE")

    temperature = body.get("temperature")
    if isinstance(temperature, int | float) and temperature > TEMPERATURE_CEILING:
        return CacheDecision(False, "HIGH_TEMPERATURE")

    # Sampling more than one completion means the caller wants variety.
    n = body.get("n")
    if isinstance(n, int) and n > 1:
        return CacheDecision(False, "MULTIPLE_COMPLETIONS")

    # Tool calls are side-effecting by nature: the model is being asked to
    # *do* something, and replaying the decision to do it is not the same as
    # replaying an answer.
    if body.get("tools") or body.get("functions") or body.get("tool_choice"):
        return CacheDecision(False, "TOOL_CALLS")

    if body.get("response_format", {}) and _is_streaming_json_schema(body):
        return CacheDecision(False, "STRUCTURED_OUTPUT")

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return CacheDecision(False, "NO_MESSAGES")

    combined = " ".join(
        str(message.get("content", "")) for message in messages if isinstance(message, dict)
    )

    if not combined.strip():
        return CacheDecision(False, "EMPTY_PROMPT")

    if len(combined) // 4 > MAX_CACHEABLE_TOKENS_ESTIMATE:
        return CacheDecision(False, "PROMPT_TOO_LARGE")

    if _TIMESTAMP_PATTERNS.search(combined):
        return CacheDecision(False, "CONTAINS_TIMESTAMP")

    if _TIME_SENSITIVE_PATTERNS.search(combined):
        return CacheDecision(False, "TIME_SENSITIVE")

    return CacheDecision(True, "CACHEABLE")


def _is_streaming_json_schema(body: dict[str, Any]) -> bool:
    """A strict JSON schema response is usually part of a pipeline.

    Replaying one is lower risk than replaying a tool call, but the schema may
    have changed between requests while the prompt did not, so treat it as
    non-cacheable rather than reason about it.
    """
    response_format = body.get("response_format")
    return isinstance(response_format, dict) and response_format.get("type") == "json_schema"


def normalize_prompt(body: dict[str, Any], *, ignore_system_prefixes: tuple[str, ...] = ()) -> str:
    """Reduce a request to the text that determines its answer.

    Two requests that differ only in ways that cannot change the answer must
    normalize to the same string, and two that differ in ways that *can* must
    not. Concretely (BUILD_SPEC §4 P4):

    * system-prompt boilerplate the user marked ignorable is dropped;
    * whitespace is collapsed;
    * non-deterministic fields are excluded entirely.

    The model is included, because the same question asked of a different model
    is a different request with a different answer.
    """
    parts: list[str] = []

    model = body.get("model")
    if isinstance(model, str):
        parts.append(f"model={model}")

    for field in sorted(set(body) - _NON_DETERMINISTIC_FIELDS):
        if field in {"messages", "model"}:
            continue
        value = body[field]
        if isinstance(value, str | int | float | bool):
            parts.append(f"{field}={value}")

    for message in body.get("messages", []):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role", ""))
        content = str(message.get("content", ""))

        if role == "system" and ignore_system_prefixes:
            stripped = content.strip()
            if any(stripped.startswith(prefix) for prefix in ignore_system_prefixes):
                continue

        parts.append(f"{role}: {_WHITESPACE.sub(' ', content).strip()}")

    return "\n".join(parts)
