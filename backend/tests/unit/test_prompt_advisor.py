"""Unit tests for the prompt/context analysis — P7, UC-26 and UC-27."""

from __future__ import annotations

from typing import Any

import pytest

from apicost.advisor.prompts import (
    DEFAULT_CONTEXT_TOKEN_THRESHOLD,
    analyse_context,
    estimate_tokens,
    suggest_compression,
)


def _msg(role: str, text: str) -> dict[str, Any]:
    return {"role": role, "content": text}


def _long(topic: str, tokens: int = 600) -> str:
    """A message about `topic`, of approximately `tokens` tokens.

    estimate_tokens is len//4, so the character count is what has to be right.
    """
    words = f"{topic} "
    return (words * (tokens * 4 // len(words) + 1))[: tokens * 4]


def _stale_conversation() -> dict[str, Any]:
    """A chat that drifted: early turns about shipping, now about pandas."""
    return {
        "model": "gpt-4o",
        "messages": [
            _msg("system", "You are a helpful assistant."),
            _msg("user", _long("warehouse shipping logistics pallets freight")),
            _msg("assistant", _long("freight pallets warehouse containers customs")),
            _msg("user", _long("customs paperwork tariff import duty broker")),
            _msg("assistant", _long("tariff broker duty import declaration forms")),
            _msg("user", "How do I merge two dataframes in pandas on multiple keys?"),
        ],
    }


def _coherent_conversation() -> dict[str, Any]:
    """Equally long, but every turn is about the same thing."""
    return {
        "model": "gpt-4o",
        "messages": [
            _msg("system", "You are a helpful assistant."),
            _msg("user", _long("pandas dataframe merge join keys index")),
            _msg("assistant", _long("pandas merge dataframe keys join index columns")),
            _msg("user", _long("dataframe merge keys pandas join suffixes index")),
            _msg("assistant", _long("merge pandas dataframe join keys index suffixes")),
            _msg("user", "How do I merge two pandas dataframe objects on multiple keys?"),
        ],
    }


# -- Thresholds -------------------------------------------------------------


def test_a_short_conversation_is_never_warned_about() -> None:
    verdict = analyse_context(
        {"messages": [_msg("user", "hi"), _msg("assistant", "hello"), _msg("user", "thanks")]}
    )
    assert not verdict.warn
    assert verdict.reason == "BELOW_THRESHOLD"


def test_a_long_single_message_is_not_a_conversation_problem() -> None:
    """One big document has no earlier history to trim.

    Warning here would be telling the user to shorten their document, which is
    not advice we are in a position to give.
    """
    verdict = analyse_context({"messages": [_msg("user", _long("contract clause", 3000))]})

    assert verdict.total_tokens >= DEFAULT_CONTEXT_TOKEN_THRESHOLD
    assert not verdict.warn
    assert verdict.reason == "FEW_MESSAGES"


def test_a_non_chat_body_is_handled_quietly() -> None:
    assert analyse_context({"prompt": "not a chat"}).reason == "NOT_A_CONVERSATION"
    assert analyse_context({}).reason == "NOT_A_CONVERSATION"
    assert analyse_context({"messages": []}).reason == "NOT_A_CONVERSATION"


def test_malformed_messages_never_raise() -> None:
    body = {"messages": [None, 42, {"role": "user"}, {"content": None}, "string"]}
    verdict = analyse_context(body)  # type: ignore[arg-type]
    assert not verdict.warn


# -- The actual signal ------------------------------------------------------


def test_a_drifted_conversation_is_flagged() -> None:
    verdict = analyse_context(_stale_conversation())

    assert verdict.warn
    assert verdict.reason == "STALE_HISTORY"
    assert verdict.reclaimable_tokens > 0
    assert verdict.stale


def test_a_long_but_coherent_conversation_is_not_flagged() -> None:
    """The property that stops this from being a length alarm.

    Both conversations here are the same size. Only one has history that
    stopped being relevant, and warning about the other would train the user to
    ignore the warning.
    """
    coherent = analyse_context(_coherent_conversation())
    drifted = analyse_context(_stale_conversation())

    assert drifted.total_tokens == pytest.approx(coherent.total_tokens, rel=0.2)
    assert drifted.warn
    assert not coherent.warn, f"flagged a coherent conversation: {coherent.stale}"


def test_the_system_prompt_is_never_called_stale() -> None:
    """It sets behaviour rather than supplying facts.

    Lexical overlap says nothing useful about it, and advising someone to drop
    it would break their application.
    """
    body = _stale_conversation()
    body["messages"][0] = _msg("system", _long("respond tersely formal register", 500))

    verdict = analyse_context(body)
    assert verdict.warn
    assert all(s.role != "system" for s in verdict.stale)
    assert 0 not in [s.index for s in verdict.stale]


def test_the_final_exchange_is_never_stale() -> None:
    verdict = analyse_context(_stale_conversation())
    messages = _stale_conversation()["messages"]
    last_two = {len(messages) - 1, len(messages) - 2}

    assert not last_two & {s.index for s in verdict.stale}


def test_content_parts_are_understood() -> None:
    """The multimodal message form must not read as an empty conversation."""
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _long("shipping freight customs")},
                    {"type": "image_url", "image_url": {"url": "data:..."}},
                ],
            }
        ]
    }
    verdict = analyse_context(body)
    assert verdict.total_tokens > 100


# -- UC-27: the suggestion --------------------------------------------------


def test_a_suggestion_drops_the_stale_messages_and_reports_the_saving() -> None:
    body = _stale_conversation()
    verdict = analyse_context(body)
    suggestion = suggest_compression(body, verdict)

    assert suggestion is not None
    assert suggestion.tokens_after < suggestion.tokens_before
    assert suggestion.tokens_saved > 0
    assert 0 < suggestion.fraction_saved < 1
    assert suggestion.strategy == "drop_stale_messages"


def test_a_suggestion_never_drops_the_current_question() -> None:
    body = _stale_conversation()
    suggestion = suggest_compression(body)

    assert suggestion is not None
    assert body["messages"][-1] in suggestion.messages


def test_a_suggestion_keeps_the_surviving_messages_byte_for_byte() -> None:
    """Advisory means advisory. Nothing is paraphrased or summarised."""
    body = _stale_conversation()
    suggestion = suggest_compression(body)

    assert suggestion is not None
    for message in suggestion.messages:
        assert message in body["messages"]


def test_no_suggestion_when_there_is_nothing_to_trim() -> None:
    assert suggest_compression(_coherent_conversation()) is None
    assert suggest_compression({"messages": [_msg("user", "hi")]}) is None


def test_the_original_body_is_not_mutated() -> None:
    """The single most important property of an advisory feature."""
    body = _stale_conversation()
    before = len(body["messages"])
    snapshot = [dict(m) for m in body["messages"]]

    suggest_compression(body)

    assert len(body["messages"]) == before
    assert body["messages"] == snapshot


# -- Token estimation -------------------------------------------------------


def test_token_estimate_is_monotonic_and_never_zero() -> None:
    assert estimate_tokens("") == 1
    assert estimate_tokens("a" * 400) > estimate_tokens("a" * 40)
