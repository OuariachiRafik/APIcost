"""Prompt to feature vector — BUILD_SPEC §4 P5.

Pure: no I/O, no ORM, no model (CODEBASE_GUIDE §9). That matters more here than
elsewhere, because these features are computed on every routed request inside a
20 ms budget, and because the training script and the serving path must extract
features *identically* — the easiest way to ship a broken classifier is to
train on one representation and serve on another.

The feature set is deliberately shallow and cheap. Anything requiring a model
call, a network hop, or a tokenizer belongs somewhere else: this runs while a
user's request is waiting.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Final

__all__ = [
    "FEATURE_NAMES",
    "PromptFeatures",
    "extract_features",
    "to_vector",
]

# Task-type markers. Crude on purpose — a keyword flag that is right 70% of the
# time is a useful feature, and the classifier learns how much to trust it.
_CODE_KEYWORDS: Final = re.compile(
    r"\b(function|class|def |import |return|const |var |SELECT |compile|"
    r"refactor|debug|stack ?trace|exception|traceback|npm|pip)\b",
    re.IGNORECASE,
)
_REASONING_KEYWORDS: Final = re.compile(
    r"\b(why|explain|prove|derive|analy[sz]e|compare|evaluate|trade-?off|"
    r"design|architect|strategy|implications?|reason(ing)?|step by step)\b",
    re.IGNORECASE,
)
_SIMPLE_KEYWORDS: Final = re.compile(
    r"\b(translate|summari[sz]e|list|extract|classify|categori[sz]e|rewrite|"
    r"rephrase|fix typos?|format|convert|tl;?dr)\b",
    re.IGNORECASE,
)
_CREATIVE_KEYWORDS: Final = re.compile(
    r"\b(write|compose|story|poem|draft|brainstorm|imagine|slogan|tagline)\b",
    re.IGNORECASE,
)

_CODE_FENCE: Final = re.compile(r"```")
_JSON_LIKE: Final = re.compile(r"(\{\s*\"|\[\s*\{)")
_XML_LIKE: Final = re.compile(r"<[a-zA-Z][^>]*>")
_QUESTION: Final = re.compile(r"\?\s*$")

# Requested-model tier. The caller's own choice is signal: someone asking for
# the strongest model may know something about the task that we do not.
_MODEL_TIERS: Final[dict[str, float]] = {
    "gpt-4o-mini": 0.0,
    "gpt-3.5-turbo": 0.0,
    "claude-3-5-haiku-20241022": 0.0,
    "gemini-1.5-flash": 0.0,
    "gpt-4o": 0.5,
    "claude-3-5-sonnet-20241022": 0.5,
    "gemini-1.5-pro": 0.5,
    "gpt-4-turbo": 1.0,
    "claude-3-opus-20240229": 1.0,
}

FEATURE_NAMES: Final = (
    "char_length",
    "word_count",
    "message_count",
    "has_system_prompt",
    "conversation_depth",
    "has_code_fence",
    "has_json",
    "has_xml",
    "code_keywords",
    "reasoning_keywords",
    "simple_keywords",
    "creative_keywords",
    "is_question",
    "requested_tier",
    "avg_word_length",
    "max_tokens_hint",
)


@dataclass(frozen=True)
class PromptFeatures:
    """One request, as numbers. Field order defines the vector order."""

    char_length: float
    word_count: float
    message_count: float
    has_system_prompt: float
    conversation_depth: float
    has_code_fence: float
    has_json: float
    has_xml: float
    code_keywords: float
    reasoning_keywords: float
    simple_keywords: float
    creative_keywords: float
    is_question: float
    requested_tier: float
    avg_word_length: float
    max_tokens_hint: float

    def as_dict(self) -> dict[str, float]:
        return asdict(self)


def _scale_length(value: int, ceiling: int) -> float:
    """Squash a count into ``[0, 1]``.

    Raw lengths span four orders of magnitude, and an unscaled feature would
    dominate a linear model purely by magnitude.
    """
    return min(1.0, value / ceiling)


def extract_features(body: dict[str, Any]) -> PromptFeatures:
    """Turn a chat-completions body into a feature vector.

    Must behave identically here and in ``routing/train.py``; they call this
    same function precisely so they cannot drift apart.
    """
    messages = body.get("messages")
    messages = messages if isinstance(messages, list) else []

    texts: list[str] = []
    has_system = False
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "system":
            has_system = True
        content = message.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            # Multi-part content: keep the text parts.
            texts.extend(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )

    combined = "\n".join(texts)
    words = combined.split()

    model = body.get("model")
    tier = _MODEL_TIERS.get(model, 0.5) if isinstance(model, str) else 0.5

    max_tokens = body.get("max_tokens")
    max_tokens_hint = (
        _scale_length(int(max_tokens), 4_000)
        if isinstance(max_tokens, int | float) and max_tokens > 0
        else 0.0
    )

    return PromptFeatures(
        char_length=_scale_length(len(combined), 8_000),
        word_count=_scale_length(len(words), 1_500),
        message_count=_scale_length(len(messages), 20),
        has_system_prompt=1.0 if has_system else 0.0,
        # A long back-and-forth usually means accumulated context that a weak
        # model will lose track of.
        conversation_depth=_scale_length(
            sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "assistant"),
            10,
        ),
        has_code_fence=1.0 if _CODE_FENCE.search(combined) else 0.0,
        has_json=1.0 if _JSON_LIKE.search(combined) else 0.0,
        has_xml=1.0 if _XML_LIKE.search(combined) else 0.0,
        code_keywords=min(1.0, len(_CODE_KEYWORDS.findall(combined)) / 5.0),
        reasoning_keywords=min(1.0, len(_REASONING_KEYWORDS.findall(combined)) / 5.0),
        simple_keywords=min(1.0, len(_SIMPLE_KEYWORDS.findall(combined)) / 3.0),
        creative_keywords=min(1.0, len(_CREATIVE_KEYWORDS.findall(combined)) / 3.0),
        is_question=1.0 if _QUESTION.search(combined.strip()) else 0.0,
        requested_tier=tier,
        avg_word_length=min(1.0, (sum(len(w) for w in words) / len(words) / 12.0))
        if words
        else 0.0,
        max_tokens_hint=max_tokens_hint,
    )


def to_vector(features: PromptFeatures) -> list[float]:
    """Feature vector in :data:`FEATURE_NAMES` order.

    The order is fixed by that tuple rather than by dataclass introspection at
    call time, so reordering fields cannot silently invalidate a trained
    artifact.
    """
    values = features.as_dict()
    return [values[name] for name in FEATURE_NAMES]
