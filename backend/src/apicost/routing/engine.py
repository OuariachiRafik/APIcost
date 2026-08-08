"""The routing decision — BUILD_SPEC §6.4.

Order is fixed: **rules, then classifier, then nothing**. A user rule is
absolute; the classifier only gets a say where the user has not expressed one.

:func:`decide` never raises. Internal failure returns ``None`` and the pipeline
passes through to the model the caller asked for — the same fail-open contract
every other optimization has (hard rule 1).

The reason code on every decision is the point of UC-16: a user looking at a
request log should be able to see *why* we did what we did, not just what.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from apicost.core.logging import get_logger
from apicost.routing.classifier import (
    DEFAULT_MIN_CONFIDENCE,
    Tier,
    predict,
)
from apicost.routing.features import extract_features
from apicost.routing.rules import RoutingRule, evaluate_rules

__all__ = [
    "REASON_CLASSIFIER_CHEAP_TIER",
    "REASON_CLASSIFIER_LOW_CONFIDENCE",
    "REASON_ESCALATED_LOW_CONFIDENCE",
    "REASON_EXCLUDED_ENDPOINT",
    "REASON_FAILOPEN_TIMEOUT",
    "REASON_NO_CHEAPER_MODEL",
    "REASON_PASSTHROUGH",
    "REASON_RULE_OVERRIDE",
    "RoutingDecision",
    "cheaper_model_for",
    "decide",
]

# The vocabulary UC-16 surfaces on every request row.
REASON_PASSTHROUGH: Final = "PASSTHROUGH"
REASON_RULE_OVERRIDE: Final = "RULE_OVERRIDE"
REASON_EXCLUDED_ENDPOINT: Final = "EXCLUDED_ENDPOINT"
REASON_CLASSIFIER_CHEAP_TIER: Final = "CLASSIFIER_CHEAP_TIER"
REASON_CLASSIFIER_LOW_CONFIDENCE: Final = "CLASSIFIER_LOW_CONFIDENCE"
REASON_NO_CHEAPER_MODEL: Final = "NO_CHEAPER_MODEL"
REASON_ESCALATED_LOW_CONFIDENCE: Final = "ESCALATED_LOW_CONFIDENCE"
REASON_FAILOPEN_TIMEOUT: Final = "FAILOPEN_TIMEOUT"

_logger = get_logger(__name__)

# The cheap substitute for each model, within the same provider. Staying with
# the provider matters: the user has a key for it, and cross-provider routing
# would change which account gets billed.
_CHEAPER_MODEL: Final[dict[str, str]] = {
    "gpt-4o": "gpt-4o-mini",
    "gpt-4-turbo": "gpt-4o-mini",
    "gpt-4": "gpt-4o-mini",
    "claude-3-5-sonnet-20241022": "claude-3-5-haiku-20241022",
    "claude-3-opus-20240229": "claude-3-5-haiku-20241022",
    "gemini-1.5-pro": "gemini-1.5-flash",
}

# Models that are already the cheap option — routing them saves nothing.
_ALREADY_CHEAP: Final[frozenset[str]] = frozenset(
    {
        "gpt-4o-mini",
        "gpt-3.5-turbo",
        "claude-3-5-haiku-20241022",
        "gemini-1.5-flash",
    }
)


@dataclass(frozen=True)
class RoutingDecision:
    """What to do, and why."""

    model: str
    routed: bool
    reason_code: str
    confidence: float | None = None
    model_version: str | None = None
    rule_id: str | None = None

    @property
    def excluded(self) -> bool:
        """The user asked us not to touch this request."""
        return self.reason_code == REASON_EXCLUDED_ENDPOINT


def cheaper_model_for(model: str) -> str | None:
    """The cheap-tier substitute, or ``None`` if there is not one."""
    if model in _ALREADY_CHEAP:
        return None
    return _CHEAPER_MODEL.get(model)


def decide(
    body: dict[str, Any],
    *,
    endpoint: str,
    routing_enabled: bool,
    rules: list[RoutingRule] | None = None,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> RoutingDecision | None:
    """Choose a model for this request.

    Returns ``None`` when there is nothing to say — no opinion, pass through.
    A returned decision with ``routed=False`` is different: it means we
    deliberately decided *not* to route, and the reason code says why.
    """
    requested = body.get("model")
    if not isinstance(requested, str) or not requested:
        return None

    try:
        # -- Rules first. A user instruction is not something a probability
        #    gets to overrule (UC-15, UC-19).
        match = evaluate_rules(rules or [], body, endpoint)
        if match is not None:
            if match.rule_type == "exclude":
                return RoutingDecision(
                    model=requested,
                    routed=False,
                    reason_code=REASON_EXCLUDED_ENDPOINT,
                    rule_id=match.rule_id,
                )
            if match.target_model:
                return RoutingDecision(
                    model=match.target_model,
                    routed=match.target_model != requested,
                    reason_code=REASON_RULE_OVERRIDE,
                    rule_id=match.rule_id,
                )

        # -- Only now does the classifier get a say.
        if not routing_enabled:
            return None

        cheaper = cheaper_model_for(requested)
        if cheaper is None:
            # Already the cheap option, or one we have no substitute for.
            return RoutingDecision(
                model=requested, routed=False, reason_code=REASON_NO_CHEAPER_MODEL
            )

        prediction = predict(extract_features(body))
        if prediction is None:
            return None

        if prediction.confidence < min_confidence:
            # Unsure. Passing through costs money; routing wrongly costs the
            # user a bad answer. The second is worse.
            return RoutingDecision(
                model=requested,
                routed=False,
                reason_code=REASON_CLASSIFIER_LOW_CONFIDENCE,
                confidence=prediction.confidence,
                model_version=prediction.model_version,
            )

        if prediction.tier == "cheap":
            return RoutingDecision(
                model=cheaper,
                routed=True,
                reason_code=REASON_CLASSIFIER_CHEAP_TIER,
                confidence=prediction.confidence,
                model_version=prediction.model_version,
            )

        # mid or strong: the request needs what the caller asked for.
        return RoutingDecision(
            model=requested,
            routed=False,
            reason_code=REASON_PASSTHROUGH,
            confidence=prediction.confidence,
            model_version=prediction.model_version,
        )
    except Exception:
        # §6.4: never raises to the caller.
        _logger.warning("routing_decision_failed", subsystem="routing", exc_info=True)
        return None


def stronger_model_for(model: str, requested: str) -> str:
    """The model to escalate to after a weak answer (UC-17).

    Escalation returns to what the caller originally asked for, rather than
    reaching for something stronger still: they chose that model, and the
    honest correction to "we sent this somewhere cheaper" is "we sent it where
    you asked".
    """
    del model
    return requested


def tier_of(model: str) -> Tier:
    """Rough tier of a model, for reporting."""
    if model in _ALREADY_CHEAP:
        return "cheap"
    if model in {"gpt-4-turbo", "claude-3-opus-20240229"}:
        return "strong"
    return "mid"
