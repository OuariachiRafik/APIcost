"""Cost arithmetic — ledger/pricing.py and ledger/cost.py.

Every savings figure in the product comes out of these two modules, so the
tests care about exactness rather than approximation.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from apicost.ledger.cost import compute_cost, cost_would_have_been, quantize_usd
from apicost.ledger.pricing import (
    PRICE_TABLE,
    ModelPrice,
    PriceNotFoundError,
    known_models,
    resolve_price,
)

# ---------------------------------------------------------------------------
# pricing
# ---------------------------------------------------------------------------


def test_known_models_are_priced() -> None:
    assert "gpt-4o" in known_models()
    assert "claude-3-5-sonnet-20241022" in known_models()
    assert "gemini-1.5-flash" in known_models()


def test_prices_are_decimal_not_float() -> None:
    """Float would reintroduce exactly the drift this module exists to avoid."""
    price = resolve_price("gpt-4o")
    assert isinstance(price.input_usd_per_million, Decimal)
    assert isinstance(price.output_usd_per_million, Decimal)


def test_unknown_model_raises() -> None:
    with pytest.raises(PriceNotFoundError, match="no price on file"):
        resolve_price("no-such-model-9000")


def test_price_history_is_resolved_by_date() -> None:
    """A row written in the past keeps the price that was current then."""
    old = ModelPrice(
        model="test-model",
        provider="test",
        input_usd_per_million=Decimal("1.00"),
        output_usd_per_million=Decimal("2.00"),
        effective_from=date(2024, 1, 1),
    )
    new = ModelPrice(
        model="test-model",
        provider="test",
        input_usd_per_million=Decimal("0.50"),
        output_usd_per_million=Decimal("1.00"),
        effective_from=date(2025, 6, 1),
    )
    PRICE_TABLE["test-model"] = [old, new]
    try:
        assert resolve_price("test-model", date(2024, 6, 1)) is old
        assert resolve_price("test-model", date(2025, 5, 31)) is old
        assert resolve_price("test-model", date(2025, 6, 1)) is new
        assert resolve_price("test-model", date(2026, 1, 1)) is new
    finally:
        del PRICE_TABLE["test-model"]


def test_date_before_any_price_raises() -> None:
    with pytest.raises(PriceNotFoundError, match="effective on or before"):
        resolve_price("gpt-4o", date(2020, 1, 1))


def test_naive_and_aware_datetimes_both_work() -> None:
    assert resolve_price("gpt-4o", datetime(2025, 3, 1, tzinfo=UTC))
    assert resolve_price("gpt-4o", datetime(2025, 3, 1))


# ---------------------------------------------------------------------------
# cost
# ---------------------------------------------------------------------------


def test_cost_is_exact_for_a_known_model() -> None:
    # gpt-4o: $2.50/M in, $10.00/M out.
    cost = compute_cost("gpt-4o", tokens_in=1_000_000, tokens_out=1_000_000)
    assert cost.input_usd == Decimal("2.50000000")
    assert cost.output_usd == Decimal("10.00000000")
    assert cost.total_usd == Decimal("12.50000000")


def test_small_requests_are_not_rounded_to_zero() -> None:
    """A gpt-4o-mini call costs a fraction of a cent; cents would report free."""
    cost = compute_cost("gpt-4o-mini", tokens_in=1_000, tokens_out=500)
    assert cost.total_usd > 0
    assert cost.total_usd < Decimal("0.001")


def test_zero_tokens_costs_nothing() -> None:
    assert compute_cost("gpt-4o", 0, 0).total_usd == Decimal("0E-8")


def test_negative_tokens_are_rejected() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        compute_cost("gpt-4o", -1, 0)


def test_estimated_flag_is_carried_through() -> None:
    assert compute_cost("gpt-4o", 10, 10, estimated=True).estimated is True
    assert compute_cost("gpt-4o", 10, 10).estimated is False


def test_costs_sum_without_drift() -> None:
    """The property that matters: a million small rows must add up exactly."""
    single = compute_cost("gpt-4o-mini", 1_000, 1_000).total_usd
    total = sum((single for _ in range(10_000)), Decimal("0"))
    assert total == single * 10_000


def test_would_have_been_prices_the_requested_model() -> None:
    """A cheap model served it; the saving is measured against what was asked for."""
    requested = cost_would_have_been("gpt-4o", 1_000_000, 0)
    actual = compute_cost("gpt-4o-mini", 1_000_000, 0).total_usd

    assert requested == Decimal("2.50000000")
    assert actual == Decimal("0.15000000")
    assert requested is not None
    assert requested - actual == Decimal("2.35000000")


def test_would_have_been_returns_none_for_an_unpriced_model() -> None:
    """An unpriced model must not lose us the ledger row."""
    assert cost_would_have_been("model-we-have-never-heard-of", 100, 100) is None


def test_quantize_rounds_half_up() -> None:
    assert quantize_usd(Decimal("0.000000005")) == Decimal("0.00000001")
