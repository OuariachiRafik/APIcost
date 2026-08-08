"""User routing rules — UC-15, UC-19.

Rules are evaluated **before** the classifier and are absolute. A user who says
"never route this endpoint" is telling us something about their product that no
model of ours can know, and a probability is not a reason to overrule them.

Pure: this module decides, it does not fetch. Rules are loaded by the engine and
passed in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

__all__ = [
    "RoutingRule",
    "RuleMatch",
    "evaluate_rules",
    "matches",
]

RuleType = Literal["override", "exclude"]


@dataclass(frozen=True)
class RoutingRule:
    """One user-defined rule.

    ``match_condition`` is a small JSON document rather than an expression
    language: users write these through a UI, and an expression language on the
    request path is an evaluation-injection surface nobody needs.
    """

    id: str
    rule_type: RuleType
    match_condition: dict[str, Any]
    target_model: str | None
    priority: int
    is_active: bool = True


@dataclass(frozen=True)
class RuleMatch:
    rule_id: str
    rule_type: RuleType
    target_model: str | None


def matches(condition: dict[str, Any], body: dict[str, Any], endpoint: str) -> bool:
    """Whether a rule's condition applies to this request.

    Supported keys, all ANDed:

    * ``model`` — the requested model, exactly.
    * ``endpoint`` — the endpoint path, exactly.
    * ``model_prefix`` — requested model starts with this.
    * ``contains`` — case-insensitive substring of the prompt text.
    * ``matches`` — regular expression against the prompt text.

    An empty condition matches everything, which is how a user says "this
    project, always".
    """
    if not condition:
        return True

    model = body.get("model")
    model = model if isinstance(model, str) else ""

    if "model" in condition and condition["model"] != model:
        return False
    if "model_prefix" in condition and not model.startswith(str(condition["model_prefix"])):
        return False
    if "endpoint" in condition and condition["endpoint"] != endpoint:
        return False

    if "contains" in condition or "matches" in condition:
        text = " ".join(
            str(message.get("content", ""))
            for message in body.get("messages", [])
            if isinstance(message, dict)
        )
        if "contains" in condition and str(condition["contains"]).lower() not in text.lower():
            return False
        if "matches" in condition:
            try:
                if not re.search(str(condition["matches"]), text, re.IGNORECASE):
                    return False
            except re.error:
                # A malformed pattern must not match everything by accident.
                # Treat it as not matching and let the rule be a no-op.
                return False

    return True


def evaluate_rules(
    rules: list[RoutingRule], body: dict[str, Any], endpoint: str
) -> RuleMatch | None:
    """First applicable rule, by priority then by id.

    ``exclude`` beats ``override`` at equal priority: the safer instruction
    wins when a user has configured both, because "do not touch this" is a
    stronger statement than "use model X".
    """
    applicable = [
        rule for rule in rules if rule.is_active and matches(rule.match_condition, body, endpoint)
    ]
    if not applicable:
        return None

    applicable.sort(key=lambda rule: (-rule.priority, rule.rule_type != "exclude", rule.id))
    winner = applicable[0]

    return RuleMatch(
        rule_id=winner.id,
        rule_type=winner.rule_type,
        target_model=winner.target_model,
    )
