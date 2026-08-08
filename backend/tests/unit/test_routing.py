"""Routing decisions — UC-14 through UC-19.

Two themes run through these tests:

* **A user rule is absolute.** No probability, however high, overrules someone
  who said "never route this endpoint".
* **Uncertainty means passthrough.** Routing wrongly costs the user a bad
  answer in their product; not routing costs them a few cents. The tests encode
  that asymmetry rather than chasing accuracy.
"""

from __future__ import annotations

from typing import Any

import pytest

from apicost.routing.classifier import (
    TierPrediction,
    classifier_is_ready,
    load_classifier,
    unload_classifier,
)
from apicost.routing.engine import (
    REASON_CLASSIFIER_CHEAP_TIER,
    REASON_CLASSIFIER_LOW_CONFIDENCE,
    REASON_EXCLUDED_ENDPOINT,
    REASON_NO_CHEAPER_MODEL,
    REASON_PASSTHROUGH,
    REASON_RULE_OVERRIDE,
    cheaper_model_for,
    decide,
)
from apicost.routing.escalation import looks_low_confidence
from apicost.routing.features import FEATURE_NAMES, extract_features, to_vector
from apicost.routing.rules import RoutingRule, evaluate_rules, matches


def body(prompt: str = "hello", model: str = "gpt-4o", **extra: Any) -> dict[str, Any]:
    return {"model": model, "messages": [{"role": "user", "content": prompt}], **extra}


# ---------------------------------------------------------------------------
# Features
# ---------------------------------------------------------------------------


def test_vector_matches_the_declared_feature_order() -> None:
    """Training and serving share this order; drift would silently invalidate
    every trained artifact."""
    vector = to_vector(extract_features(body()))
    assert len(vector) == len(FEATURE_NAMES)


def test_features_are_bounded() -> None:
    """Unscaled features would let raw length dominate a linear model."""
    huge = extract_features(body("word " * 50_000))
    for name, value in huge.as_dict().items():
        assert 0.0 <= value <= 1.0, f"{name} escaped [0, 1]: {value}"


def test_code_and_reasoning_are_distinguished() -> None:
    code = extract_features(body("```python\ndef f(): return 1\n```\nrefactor this"))
    simple = extract_features(body("translate 'hello' to Spanish"))

    assert code.has_code_fence == 1.0
    assert simple.has_code_fence == 0.0
    assert simple.simple_keywords > 0


def test_conversation_depth_counts_assistant_turns() -> None:
    """A long back-and-forth is context a weak model tends to lose."""
    shallow = extract_features(body())
    deep = extract_features(
        {
            "model": "gpt-4o",
            "messages": [
                {"role": "user", "content": "a"},
                {"role": "assistant", "content": "b"},
                {"role": "user", "content": "c"},
                {"role": "assistant", "content": "d"},
            ],
        }
    )
    assert deep.conversation_depth > shallow.conversation_depth


def test_multipart_content_is_handled() -> None:
    features = extract_features(
        {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": [{"type": "text", "text": "explain why"}]}],
        }
    )
    assert features.reasoning_keywords > 0


# ---------------------------------------------------------------------------
# Rules — UC-15, UC-19
# ---------------------------------------------------------------------------


def test_an_empty_condition_matches_everything() -> None:
    assert matches({}, body(), "chat/completions")


def test_conditions_are_anded() -> None:
    condition = {"model": "gpt-4o", "endpoint": "chat/completions"}
    assert matches(condition, body(), "chat/completions")
    assert not matches(condition, body(model="gpt-4o-mini"), "chat/completions")
    assert not matches(condition, body(), "embeddings")


def test_contains_and_regex_conditions() -> None:
    assert matches({"contains": "INVOICE"}, body("generate an invoice"), "chat/completions")
    assert matches({"matches": r"invoice|receipt"}, body("a receipt"), "chat/completions")
    assert not matches({"contains": "invoice"}, body("hello"), "chat/completions")


def test_a_malformed_regex_matches_nothing() -> None:
    """A broken pattern must not accidentally match everything."""
    assert not matches({"matches": "([unclosed"}, body("anything"), "chat/completions")


def test_exclude_beats_override_at_equal_priority() -> None:
    """'Do not touch this' is a stronger statement than 'use model X'."""
    rules = [
        RoutingRule("a", "override", {}, "gpt-4o-mini", 0),
        RoutingRule("b", "exclude", {}, None, 0),
    ]
    match = evaluate_rules(rules, body(), "chat/completions")
    assert match is not None
    assert match.rule_type == "exclude"


def test_higher_priority_wins() -> None:
    rules = [
        RoutingRule("a", "exclude", {}, None, 0),
        RoutingRule("b", "override", {}, "gpt-4o-mini", 10),
    ]
    match = evaluate_rules(rules, body(), "chat/completions")
    assert match is not None
    assert match.rule_type == "override"


def test_inactive_rules_are_ignored() -> None:
    rules = [RoutingRule("a", "exclude", {}, None, 0, is_active=False)]
    assert evaluate_rules(rules, body(), "chat/completions") is None


# ---------------------------------------------------------------------------
# The decision — rules before classifier
# ---------------------------------------------------------------------------


def test_an_exclude_rule_stops_routing_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even with a confident classifier saying otherwise."""
    monkeypatch.setattr(
        "apicost.routing.engine.predict",
        lambda _f: TierPrediction("cheap", 0.99, "test"),
    )

    decision = decide(
        body(),
        endpoint="chat/completions",
        routing_enabled=True,
        rules=[RoutingRule("r", "exclude", {}, None, 0)],
    )

    assert decision is not None
    assert decision.excluded
    assert decision.routed is False
    assert decision.model == "gpt-4o"
    assert decision.reason_code == REASON_EXCLUDED_ENDPOINT


def test_an_override_rule_wins_over_the_classifier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "apicost.routing.engine.predict",
        lambda _f: TierPrediction("strong", 0.99, "test"),
    )

    decision = decide(
        body(),
        endpoint="chat/completions",
        routing_enabled=True,
        rules=[RoutingRule("r", "override", {}, "gpt-4o-mini", 0)],
    )

    assert decision is not None
    assert decision.model == "gpt-4o-mini"
    assert decision.routed is True
    assert decision.reason_code == REASON_RULE_OVERRIDE


def test_rules_apply_even_when_routing_is_disabled() -> None:
    """UC-19: an exclusion is a user instruction, not an optimization."""
    decision = decide(
        body(),
        endpoint="chat/completions",
        routing_enabled=False,
        rules=[RoutingRule("r", "override", {}, "gpt-4o-mini", 0)],
    )
    assert decision is not None
    assert decision.reason_code == REASON_RULE_OVERRIDE


def test_routing_disabled_means_no_opinion() -> None:
    assert decide(body(), endpoint="chat/completions", routing_enabled=False) is None


def test_a_cheap_prediction_routes_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "apicost.routing.engine.predict",
        lambda _f: TierPrediction("cheap", 0.95, "v1"),
    )

    decision = decide(body(), endpoint="chat/completions", routing_enabled=True)

    assert decision is not None
    assert decision.model == "gpt-4o-mini"
    assert decision.routed is True
    assert decision.reason_code == REASON_CLASSIFIER_CHEAP_TIER
    assert decision.model_version == "v1"


def test_low_confidence_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """The asymmetry: an unsure router should do nothing."""
    monkeypatch.setattr(
        "apicost.routing.engine.predict",
        lambda _f: TierPrediction("cheap", 0.51, "v1"),
    )

    decision = decide(body(), endpoint="chat/completions", routing_enabled=True)

    assert decision is not None
    assert decision.routed is False
    assert decision.model == "gpt-4o"
    assert decision.reason_code == REASON_CLASSIFIER_LOW_CONFIDENCE


@pytest.mark.parametrize("tier", ["mid", "strong"])
def test_a_hard_request_stays_where_it_was_sent(monkeypatch: pytest.MonkeyPatch, tier: str) -> None:
    monkeypatch.setattr(
        "apicost.routing.engine.predict",
        lambda _f: TierPrediction(tier, 0.95, "v1"),  # type: ignore[arg-type]
    )
    decision = decide(body(), endpoint="chat/completions", routing_enabled=True)
    assert decision is not None
    assert decision.routed is False
    assert decision.reason_code == REASON_PASSTHROUGH


def test_an_already_cheap_model_is_not_routed() -> None:
    decision = decide(body(model="gpt-4o-mini"), endpoint="chat/completions", routing_enabled=True)
    assert decision is not None
    assert decision.routed is False
    assert decision.reason_code == REASON_NO_CHEAPER_MODEL


def test_routing_never_crosses_providers() -> None:
    """The user has a key for one provider; switching would bill the wrong account."""
    assert cheaper_model_for("claude-3-5-sonnet-20241022") == "claude-3-5-haiku-20241022"
    assert cheaper_model_for("gemini-1.5-pro") == "gemini-1.5-flash"
    assert cheaper_model_for("gpt-4o") == "gpt-4o-mini"


def test_a_classifier_exception_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """§6.4: never raises to the caller."""

    def explode(_features: object) -> None:
        raise RuntimeError("artifact is corrupt")

    monkeypatch.setattr("apicost.routing.engine.predict", explode)
    assert decide(body(), endpoint="chat/completions", routing_enabled=True) is None


def test_a_request_without_a_model_gets_no_opinion() -> None:
    assert decide({"messages": []}, endpoint="chat/completions", routing_enabled=True) is None


# ---------------------------------------------------------------------------
# Escalation — UC-17
# ---------------------------------------------------------------------------


def completion(text: str, finish: str = "stop") -> dict[str, Any]:
    return {
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": finish}
        ]
    }


def test_an_empty_answer_escalates() -> None:
    assert looks_low_confidence(completion("")).reason == "EMPTY_RESPONSE"


def test_refusals_and_uncertainty_escalate() -> None:
    assert looks_low_confidence(completion("I'm sorry, I cannot help with that")).escalate
    assert looks_low_confidence(completion("I do not have enough information to answer")).escalate
    assert looks_low_confidence(completion("Could you clarify what you mean?")).escalate


def test_a_truncated_answer_escalates() -> None:
    decision = looks_low_confidence(completion("The first step is", finish="length"))
    assert decision.reason == "TRUNCATED"


def test_malformed_json_escalates_only_when_json_was_asked_for() -> None:
    request = {"response_format": {"type": "json_object"}}
    assert looks_low_confidence(completion('{"a": 1'), request_body=request).escalate
    assert not looks_low_confidence(completion('{"a": 1')).escalate


def test_a_short_but_correct_answer_does_not_escalate() -> None:
    """ "Paris." is a complete answer. Escalating it burns two calls for nothing."""
    assert not looks_low_confidence(completion("Paris.")).escalate


def test_short_answers_escalate_only_on_quality_critical_endpoints() -> None:
    decision = looks_low_confidence(completion("Paris."), quality_critical=True)
    assert decision.reason == "SHORT_RESPONSE"


def test_ordinary_answers_are_left_alone() -> None:
    """ "I cannot divide by zero" is a good answer, not a refusal."""
    answer = "You cannot divide by zero, because the operation is undefined in arithmetic."
    assert not looks_low_confidence(completion(answer)).escalate


# ---------------------------------------------------------------------------
# The trained artifact
# ---------------------------------------------------------------------------


def test_the_shipped_artifact_loads_and_predicts() -> None:
    """A trained artifact is checked in; it should actually work."""
    unload_classifier()
    if not load_classifier():
        pytest.skip("no trained artifact — run: uv run python -m apicost.routing.train")

    assert classifier_is_ready()

    from apicost.routing.classifier import predict

    simple = predict(extract_features(body("Translate 'hello' into Spanish")))
    hard = predict(
        extract_features(
            body(
                "Design a distributed rate limiter across 200 stateless nodes "
                "with no shared clock, and analyse the failure modes"
            )
        )
    )

    assert simple is not None and hard is not None
    assert simple.tier == "cheap"
    assert hard.tier in {"mid", "strong"}
    assert simple.model_version.startswith("seed-")
