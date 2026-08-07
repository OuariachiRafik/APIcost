"""Cacheability rules — UC-24, and the main safety mechanism for caching.

The asymmetry these tests encode: a false negative costs a few cents, a false
positive returns wrong data to somebody's production application. So the
refusals are tested harder than the acceptances.
"""

from __future__ import annotations

import pytest

from apicost.cache.policy import (
    NO_CACHE_HEADER,
    TEMPERATURE_CEILING,
    is_cacheable,
    normalize_prompt,
)


def request(**overrides: object) -> dict[str, object]:
    return {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "What is the capital of France?"}],
        **overrides,
    }


# ---------------------------------------------------------------------------
# Accepted
# ---------------------------------------------------------------------------


def test_a_plain_question_is_cacheable() -> None:
    decision = is_cacheable(request())
    assert decision.cacheable
    assert decision.reason == "CACHEABLE"
    assert bool(decision) is True


def test_low_temperature_is_cacheable() -> None:
    assert is_cacheable(request(temperature=0.0))
    assert is_cacheable(request(temperature=TEMPERATURE_CEILING))


# ---------------------------------------------------------------------------
# Refused — each with the reason a user would be shown
# ---------------------------------------------------------------------------


def test_caching_disabled_for_the_project() -> None:
    assert is_cacheable(request(), cache_enabled=False).reason == "CACHE_DISABLED"


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", " true "])
def test_no_cache_header_always_wins(value: str) -> None:
    decision = is_cacheable(request(), headers={NO_CACHE_HEADER: value})
    assert not decision
    assert decision.reason == "NO_CACHE_HEADER"


def test_no_cache_header_is_case_insensitive_on_the_name() -> None:
    assert not is_cacheable(request(), headers={"x-apicost-no-cache": "true"})


def test_no_cache_header_ignores_other_values() -> None:
    assert is_cacheable(request(), headers={NO_CACHE_HEADER: "false"})


def test_high_temperature_is_refused() -> None:
    """Above the ceiling the caller asked for variety; replaying defeats it."""
    decision = is_cacheable(request(temperature=0.9))
    assert not decision
    assert decision.reason == "HIGH_TEMPERATURE"


def test_multiple_completions_are_refused() -> None:
    assert is_cacheable(request(n=3)).reason == "MULTIPLE_COMPLETIONS"


@pytest.mark.parametrize("field", ["tools", "functions", "tool_choice"])
def test_tool_calls_are_refused(field: str) -> None:
    """Tool calls are side-effecting: the model is being asked to *do* something."""
    decision = is_cacheable(request(**{field: [{"type": "function"}]}))
    assert not decision
    assert decision.reason == "TOOL_CALLS"


def test_structured_output_is_refused() -> None:
    decision = is_cacheable(
        request(response_format={"type": "json_schema", "json_schema": {"name": "x"}})
    )
    assert decision.reason == "STRUCTURED_OUTPUT"


@pytest.mark.parametrize(
    "prompt",
    [
        "What is today's date?",
        "Who is the current president?",
        "Give me the latest news",
        "What happened yesterday?",
        "Summarise this week's commits",
        "What time is it right now?",
    ],
)
def test_time_sensitive_prompts_are_refused(prompt: str) -> None:
    """The one way caching actively harms a user: a stale answer to 'when'."""
    decision = is_cacheable(request(messages=[{"role": "user", "content": prompt}]))
    assert not decision
    assert decision.reason == "TIME_SENSITIVE"


@pytest.mark.parametrize(
    "prompt",
    [
        "Context as of 2026-08-05T14:30 — summarise",
        "Log line at 14:22:07 please explain",
        "Event id 1754400000 occurred",
        "Report for 2026-08-05 09:15",
    ],
)
def test_prompts_carrying_a_timestamp_are_refused(prompt: str) -> None:
    """A templated timestamp means no two requests are really the same."""
    decision = is_cacheable(request(messages=[{"role": "user", "content": prompt}]))
    assert not decision
    assert decision.reason == "CONTAINS_TIMESTAMP"


def test_embeddings_are_not_cacheable() -> None:
    assert is_cacheable(request(), endpoint="embeddings").reason == "ENDPOINT_NOT_CACHEABLE"


def test_excluded_endpoints_are_refused() -> None:
    decision = is_cacheable(
        request(), endpoint="chat/completions", excluded_endpoints=frozenset({"chat/completions"})
    )
    assert decision.reason == "EXCLUDED_ENDPOINT"


def test_empty_and_missing_messages_are_refused() -> None:
    assert is_cacheable(request(messages=[])).reason == "NO_MESSAGES"
    assert is_cacheable({"model": "gpt-4o"}).reason == "NO_MESSAGES"
    assert is_cacheable(request(messages=[{"role": "user", "content": "   "}])).reason == (
        "EMPTY_PROMPT"
    )


def test_enormous_prompts_are_refused() -> None:
    huge = "word " * 120_000
    assert is_cacheable(request(messages=[{"role": "user", "content": huge}])).reason == (
        "PROMPT_TOO_LARGE"
    )


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def test_whitespace_differences_normalize_away() -> None:
    a = normalize_prompt(request(messages=[{"role": "user", "content": "hello   world"}]))
    b = normalize_prompt(request(messages=[{"role": "user", "content": "hello world"}]))
    assert a == b


def test_non_deterministic_fields_are_excluded() -> None:
    """Two identical questions from different end-users must look identical."""
    a = normalize_prompt(request(user="alice", stream=True, seed=1))
    b = normalize_prompt(request(user="bob", stream=False, seed=999))
    assert a == b


def test_the_model_is_part_of_the_identity() -> None:
    """The same question asked of a different model has a different answer."""
    a = normalize_prompt(request(model="gpt-4o"))
    b = normalize_prompt(request(model="gpt-4o-mini"))
    assert a != b


def test_temperature_is_part_of_the_identity() -> None:
    assert normalize_prompt(request(temperature=0.0)) != normalize_prompt(request(temperature=0.5))


def test_different_questions_do_not_collide() -> None:
    a = normalize_prompt(request(messages=[{"role": "user", "content": "capital of France"}]))
    b = normalize_prompt(request(messages=[{"role": "user", "content": "capital of Spain"}]))
    assert a != b


def test_message_order_matters() -> None:
    """Conversation order changes meaning, so it must change identity."""
    first = request(
        messages=[
            {"role": "user", "content": "A"},
            {"role": "assistant", "content": "B"},
        ]
    )
    second = request(
        messages=[
            {"role": "assistant", "content": "B"},
            {"role": "user", "content": "A"},
        ]
    )
    assert normalize_prompt(first) != normalize_prompt(second)


def test_ignorable_system_boilerplate_is_dropped() -> None:
    boilerplate = "You are a helpful assistant deployed at Acme."
    with_system = request(
        messages=[
            {"role": "system", "content": boilerplate},
            {"role": "user", "content": "capital of France"},
        ]
    )
    without = request(messages=[{"role": "user", "content": "capital of France"}])

    assert normalize_prompt(with_system) != normalize_prompt(without)
    assert normalize_prompt(
        with_system, ignore_system_prefixes=("You are a helpful assistant",)
    ) == normalize_prompt(without)


def test_unmarked_system_prompts_are_kept() -> None:
    """Only boilerplate the user marked ignorable is dropped."""
    body = request(
        messages=[
            {"role": "system", "content": "Answer only in French."},
            {"role": "user", "content": "capital of France"},
        ]
    )
    normalized = normalize_prompt(body, ignore_system_prefixes=("You are a helpful",))
    assert "Answer only in French." in normalized
