"""Long-context detection and compression suggestions — UC-26, UC-27.

Two questions about one request, both answered without calling anything:

1. **Is this conversation carrying weight it does not need?** A chat loop that
   resends its whole history pays for every earlier message on every turn, and
   the cost grows quadratically with the length of the conversation. That is
   the single most common way a working application quietly becomes expensive.

2. **What would a trimmed version look like, and what would it save?**

Size alone is a bad signal and this module deliberately does not use it alone.
A long history that is *all* relevant — a document being edited, a debugging
session — is long for a reason, and warning about it trains the user to ignore
warnings. What earns a warning is length **plus** early messages that have
little to do with what is being asked now.

**Advisory only** (BUILD_SPEC §4 P7). Nothing here rewrites a request. v1
shows the user a suggestion and a number and lets them decide; silently
altering a prompt would change model output in ways the user cannot predict
and did not ask for.

Pure — no I/O, no ORM. Cheap enough to run on the proxy path inside the shared
deadline: lexical overlap over token sets, no embeddings, no model calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "DEFAULT_CONTEXT_TOKEN_THRESHOLD",
    "DEFAULT_RELEVANCE_THRESHOLD",
    "CompressionSuggestion",
    "ContextVerdict",
    "analyse_context",
    "suggest_compression",
]

DEFAULT_CONTEXT_TOKEN_THRESHOLD = 2000
"""Below this a conversation is not worth commenting on. Trimming 400 tokens
saves a fraction of a cent and costs the user their attention."""

DEFAULT_RELEVANCE_THRESHOLD = 0.08
"""Jaccard overlap between an early message and the current question, below
which that message is treated as stale.

Deliberately low. Overlap on raw tokens understates real relevance — the same
idea gets restated in different words — so a permissive floor keeps this from
flagging history that a human would call related. False *quiet* is much cheaper
here than false noise."""

MIN_MESSAGES_FOR_WARNING = 6
"""Fewer turns than this and there is nothing meaningful to trim: the system
prompt and the last exchange are usually all of it."""

_WORD = re.compile(r"[a-z0-9']+")

_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "if",
        "then",
        "than",
        "that",
        "this",
        "these",
        "those",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "do",
        "does",
        "did",
        "doing",
        "have",
        "has",
        "had",
        "having",
        "i",
        "you",
        "he",
        "she",
        "it",
        "we",
        "they",
        "me",
        "him",
        "her",
        "them",
        "my",
        "your",
        "his",
        "its",
        "our",
        "their",
        "to",
        "of",
        "in",
        "on",
        "at",
        "by",
        "for",
        "with",
        "from",
        "as",
        "into",
        "about",
        "over",
        "after",
        "before",
        "between",
        "not",
        "no",
        "nor",
        "so",
        "such",
        "can",
        "could",
        "should",
        "would",
        "may",
        "might",
        "must",
        "will",
        "just",
        "also",
        "very",
        "please",
        "what",
        "which",
        "who",
        "whom",
        "when",
        "where",
        "why",
        "how",
    ]
)


def _tokens(text: str) -> set[str]:
    """Content words, lowercased. Stopwords removed.

    Without the stopword filter every pair of English sentences overlaps on
    "the" and "is", and the relevance score compresses into a narrow band where
    no threshold separates related from unrelated.
    """
    return {word for word in _WORD.findall(text.lower()) if word not in _STOPWORDS}


def estimate_tokens(text: str) -> int:
    """Rough token count: ~4 characters per token.

    The same approximation the ledger uses when a provider does not report
    usage. It is wrong in the third digit and that is fine — every number this
    module produces is a comparison between two counts made the same way, so
    the bias cancels.
    """
    return max(1, len(text) // 4)


def _message_text(message: Any) -> str:
    """Extract text from a chat message, tolerating the content-parts form."""
    if not isinstance(message, dict):
        return ""

    content = message.get("content")
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        # OpenAI's multimodal form: [{"type": "text", "text": ...}, ...]
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return " ".join(parts)

    return ""


@dataclass(frozen=True)
class StaleMessage:
    index: int
    role: str
    tokens: int
    relevance: float


@dataclass(frozen=True)
class ContextVerdict:
    """What the analysis found. Carries its numbers whether or not it warns."""

    warn: bool = False
    reason: str = "OK"
    total_tokens: int = 0
    message_count: int = 0
    stale: list[StaleMessage] = field(default_factory=list)
    reclaimable_tokens: int = 0
    """Tokens in messages judged stale. An upper bound on what trimming saves,
    not a promise — the user may know those messages matter."""

    @property
    def reclaimable_fraction(self) -> float:
        if self.total_tokens <= 0:
            return 0.0
        return self.reclaimable_tokens / self.total_tokens


def analyse_context(
    body: dict[str, Any],
    *,
    token_threshold: int = DEFAULT_CONTEXT_TOKEN_THRESHOLD,
    relevance_threshold: float = DEFAULT_RELEVANCE_THRESHOLD,
) -> ContextVerdict:
    """Decide whether this request is carrying stale conversation history.

    Never raises. This runs on the request path and a malformed body must cost
    the user nothing but the advice they did not get.
    """
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return ContextVerdict(reason="NOT_A_CONVERSATION")

    texts = [_message_text(m) for m in messages]
    token_counts = [estimate_tokens(t) if t else 0 for t in texts]
    total = sum(token_counts)

    if total < token_threshold:
        return ContextVerdict(
            reason="BELOW_THRESHOLD",
            total_tokens=total,
            message_count=len(messages),
        )

    if len(messages) < MIN_MESSAGES_FOR_WARNING:
        # Long, but not a conversation — one big document or system prompt.
        # There is no "earlier history" to trim, so the honest answer is that
        # we have no advice, not that the user should shorten their document.
        return ContextVerdict(
            reason="FEW_MESSAGES",
            total_tokens=total,
            message_count=len(messages),
        )

    current = _current_question(messages, texts)
    if not current:
        return ContextVerdict(
            reason="NO_CURRENT_QUESTION",
            total_tokens=total,
            message_count=len(messages),
        )

    stale: list[StaleMessage] = []
    # The final exchange is never stale — it is what is being answered. The
    # system prompt is never stale either: it sets behaviour rather than
    # supplying facts, so lexical overlap says nothing useful about it, and
    # advising someone to drop it would break their application.
    for index, message in enumerate(messages[:-2]):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role", ""))
        if role == "system":
            continue

        relevance = _jaccard(_tokens(texts[index]), current)
        if relevance < relevance_threshold:
            stale.append(
                StaleMessage(
                    index=index,
                    role=role,
                    tokens=token_counts[index],
                    relevance=round(relevance, 4),
                )
            )

    reclaimable = sum(s.tokens for s in stale)

    if not stale:
        return ContextVerdict(
            reason="ALL_RELEVANT",
            total_tokens=total,
            message_count=len(messages),
        )

    return ContextVerdict(
        warn=True,
        reason="STALE_HISTORY",
        total_tokens=total,
        message_count=len(messages),
        stale=stale,
        reclaimable_tokens=reclaimable,
    )


def _current_question(messages: list[Any], texts: list[str]) -> set[str]:
    """Content words of the latest user message — what relevance is measured against."""
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, dict) and message.get("role") == "user":
            return _tokens(texts[index])
    return set()


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    union = len(left | right)
    return len(left & right) / union if union else 0.0


@dataclass(frozen=True)
class CompressionSuggestion:
    """A candidate rewrite the user may accept — UC-27.

    Never applied. ``messages`` is what the request *would* look like; whether
    it becomes the request is the user's call.
    """

    messages: list[dict[str, Any]]
    tokens_before: int
    tokens_after: int
    removed_indices: list[int]
    strategy: str

    @property
    def tokens_saved(self) -> int:
        return max(0, self.tokens_before - self.tokens_after)

    @property
    def fraction_saved(self) -> float:
        if self.tokens_before <= 0:
            return 0.0
        return self.tokens_saved / self.tokens_before


def suggest_compression(
    body: dict[str, Any],
    verdict: ContextVerdict | None = None,
    **kwargs: Any,
) -> CompressionSuggestion | None:
    """Build a trimmed candidate, or ``None`` if there is nothing worth doing.

    The strategy is deliberately the boring one: **drop the messages the
    analysis judged stale, keep everything else byte for byte.** No
    summarisation, no paraphrasing, no model call.

    That is a product decision, not a shortcut. Summarising history with an LLM
    costs money to save money, and it silently changes what the model is told —
    which for a user debugging their own application is the last thing they
    want from a cost tool. A dropped message is something the user can see and
    reason about; a summarised one is not.
    """
    verdict = verdict or analyse_context(body, **kwargs)
    if not verdict.warn or not verdict.stale:
        return None

    messages = body.get("messages")
    if not isinstance(messages, list):
        return None

    drop = {s.index for s in verdict.stale}
    kept = [m for index, m in enumerate(messages) if index not in drop]

    after = sum(estimate_tokens(_message_text(m)) for m in kept if _message_text(m))

    return CompressionSuggestion(
        messages=kept,
        tokens_before=verdict.total_tokens,
        tokens_after=after,
        removed_indices=sorted(drop),
        strategy="drop_stale_messages",
    )
