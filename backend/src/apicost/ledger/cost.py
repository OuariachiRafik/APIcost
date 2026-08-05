"""Token counts to USD.

Pure (CODEBASE_GUIDE §9). Every savings number in the product is derived from
this arithmetic, so it is worth being fussy about:

* ``Decimal`` throughout. Float arithmetic on fractions of a cent, summed over
  a million ledger rows, drifts — and the drift shows up in exactly the number
  the user is being asked to trust.
* :func:`compute_cost` takes a timestamp and prices the request as of *then*,
  so historical rows keep the price that was current when they were made.
* :func:`cost_would_have_been` prices the **requested** model even when a
  cheaper one served the request. That column is what makes savings reporting
  possible, and it must be populated on every row, including passthroughs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from apicost.ledger.pricing import ModelPrice, PriceNotFoundError, resolve_price

__all__ = [
    "COST_PRECISION",
    "RequestCost",
    "compute_cost",
    "cost_would_have_been",
    "quantize_usd",
]

COST_PRECISION = Decimal("0.00000001")
"""Eight decimal places. A gpt-4o-mini call can cost a few hundred nanodollars;
rounding to cents would report most individual requests as free."""


def quantize_usd(value: Decimal) -> Decimal:
    """Round to storage precision, half-up."""
    return value.quantize(COST_PRECISION, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class RequestCost:
    """What one request cost, and at which prices."""

    total_usd: Decimal
    input_usd: Decimal
    output_usd: Decimal
    price: ModelPrice
    estimated: bool
    """True when token counts came from estimation rather than the provider's
    usage block. Surfaced as ``tokens_estimated`` so cost accuracy is never
    silently overstated (BUILD_SPEC §6.2)."""


def compute_cost(
    model: str,
    tokens_in: int,
    tokens_out: int,
    *,
    at: datetime | None = None,
    estimated: bool = False,
) -> RequestCost:
    """Price a request against the table in force at ``at``.

    Raises:
        ValueError: Negative token counts.
        PriceNotFoundError: No price on file for the model at that date.
    """
    if tokens_in < 0 or tokens_out < 0:
        raise ValueError(f"token counts cannot be negative (in={tokens_in}, out={tokens_out})")

    price = resolve_price(model, at)

    input_usd = quantize_usd(price.input_usd_per_token() * tokens_in)
    output_usd = quantize_usd(price.output_usd_per_token() * tokens_out)

    return RequestCost(
        total_usd=quantize_usd(input_usd + output_usd),
        input_usd=input_usd,
        output_usd=output_usd,
        price=price,
        estimated=estimated,
    )


def cost_would_have_been(
    model_requested: str,
    tokens_in: int,
    tokens_out: int,
    *,
    at: datetime | None = None,
) -> Decimal | None:
    """What the request would have cost at the model the caller asked for.

    Populated on every ledger row (BUILD_SPEC §7). For a passthrough it equals
    the actual cost; for a cache hit the actual cost is zero and this is the
    whole saving; for a routed request the difference is the saving.

    Returns ``None`` when the requested model has no price on file, rather than
    raising — an unpriced model must not fail the ledger write. The row is
    still recorded, just without a savings figure.
    """
    try:
        return compute_cost(model_requested, tokens_in, tokens_out, at=at).total_usd
    except (PriceNotFoundError, ValueError):
        return None
