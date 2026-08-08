"""Low-confidence detection — UC-17.

After a cheap-tier answer, decide whether to retry once on a stronger model.
Pure: it inspects a response body and says yes or no.

The economics govern the tuning. An escalation costs *both* calls, so a false
positive is worse than merely wasteful — it makes routing look unprofitable in
the savings report (and honestly so, per CODEBASE_GUIDE §12). The signals here
are therefore ones that indicate a genuinely unusable answer, not merely a
short one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "MIN_USEFUL_CHARS",
    "EscalationDecision",
    "looks_low_confidence",
]

MIN_USEFUL_CHARS: Final = 24
"""Below this an answer is too short to be a real one — but see the caveat in
:func:`looks_low_confidence` about legitimately terse replies."""

# Phrasing a model uses when it is declining or unsure. Deliberately narrow:
# "I cannot" appears in plenty of perfectly good answers ("you cannot divide by
# zero"), so the patterns are anchored to first-person statements about the
# model's own ability.
_UNCERTAINTY: Final = re.compile(
    r"("
    r"^\s*(i'm sorry|i am sorry|sorry,)"
    r"|i (?:cannot|can't|am unable to|don't know how to) (?:help|assist|answer|provide|complete)"
    r"|i (?:do not|don't) have (?:enough|sufficient) (?:information|context)"
    r"|as an ai(?: language)? model"
    r"|i'?m not (?:sure|certain) (?:what|how|if)"
    r"|could you (?:please )?(?:clarify|rephrase|provide more)"
    r")",
    re.IGNORECASE,
)

_REFUSAL: Final = re.compile(
    r"(i (?:cannot|can't|won't) (?:comply|do that)|against my guidelines)", re.IGNORECASE
)


@dataclass(frozen=True)
class EscalationDecision:
    escalate: bool
    reason: str

    def __bool__(self) -> bool:
        return self.escalate


def _completion_text(body: dict[str, Any]) -> str:
    parts: list[str] = []
    for choice in body.get("choices", []):
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                parts.append(content)
    return "\n".join(parts)


def _finish_reason(body: dict[str, Any]) -> str | None:
    choices = body.get("choices", [])
    if choices and isinstance(choices[0], dict):
        reason = choices[0].get("finish_reason")
        return str(reason) if reason else None
    return None


def looks_low_confidence(
    response_body: dict[str, Any],
    *,
    request_body: dict[str, Any] | None = None,
    quality_critical: bool = False,
) -> EscalationDecision:
    """Whether a cheap-tier answer warrants one retry on a stronger model.

    Args:
        response_body: The completion, in OpenAI shape.
        request_body: The original request, used to tell whether structured
            output was asked for.
        quality_critical: The endpoint is flagged as such by the user, which
            lowers the bar for escalating.
    """
    text = _completion_text(response_body)
    stripped = text.strip()

    if not stripped:
        return EscalationDecision(True, "EMPTY_RESPONSE")

    # Truncated JSON when JSON was requested is unambiguous: the caller is
    # going to try to parse this and fail.
    if request_body is not None and _json_was_requested(request_body):
        try:
            json.loads(stripped)
        except json.JSONDecodeError:
            return EscalationDecision(True, "MALFORMED_JSON")

    if _finish_reason(response_body) == "length":
        # Cut off mid-answer. A stronger model will not necessarily be
        # shorter, but the caller asked for a complete answer.
        return EscalationDecision(True, "TRUNCATED")

    if _REFUSAL.search(stripped):
        return EscalationDecision(True, "REFUSAL")

    if _UNCERTAINTY.search(stripped):
        return EscalationDecision(True, "UNCERTAIN_PHRASING")

    # Length last, and only when the endpoint is flagged quality-critical.
    # Plenty of good answers are three words ("Paris.", "42", "yes — because
    # ..."), and escalating those would burn two calls to improve nothing.
    if quality_critical and len(stripped) < MIN_USEFUL_CHARS:
        return EscalationDecision(True, "SHORT_RESPONSE")

    return EscalationDecision(False, "CONFIDENT")


def _json_was_requested(request_body: dict[str, Any]) -> bool:
    response_format = request_body.get("response_format")
    if isinstance(response_format, dict):
        return str(response_format.get("type", "")).startswith("json")
    return False
