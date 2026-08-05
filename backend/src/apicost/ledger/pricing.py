"""Provider price tables, versioned by ``effective_from``.

Prices change. A row written in March must keep March's price forever, or every
historical spend number in the product silently rewrites itself the next time a
provider adjusts its rates (CODEBASE_GUIDE §12). So this is a *time-versioned*
table: look a price up by model **and date**, never by model alone.

Prices are USD per one million tokens, matching how providers publish them.

This table is seeded from published rates and refreshed by a worker job
(``worker/tasks.py``, P2). It is not authoritative: `tokens_estimated` and
stale-price handling exist because we cannot guarantee it is current.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Final

__all__ = [
    "TOKENS_PER_PRICE_UNIT",
    "ModelPrice",
    "PriceNotFoundError",
    "known_models",
    "resolve_price",
]

TOKENS_PER_PRICE_UNIT: Final = Decimal(1_000_000)


class PriceNotFoundError(LookupError):
    """No price is on file for this model at this date."""


@dataclass(frozen=True)
class ModelPrice:
    """A price that took effect on a date and held until superseded."""

    model: str
    provider: str
    input_usd_per_million: Decimal
    output_usd_per_million: Decimal
    effective_from: date

    def input_usd_per_token(self) -> Decimal:
        return self.input_usd_per_million / TOKENS_PER_PRICE_UNIT

    def output_usd_per_token(self) -> Decimal:
        return self.output_usd_per_million / TOKENS_PER_PRICE_UNIT


def _price(
    model: str,
    provider: str,
    input_usd: str,
    output_usd: str,
    effective_from: date,
) -> ModelPrice:
    # Decimal from str, never from float: 0.15 is not 0.15 in binary floating
    # point, and these values are multiplied by token counts in the millions.
    return ModelPrice(
        model=model,
        provider=provider,
        input_usd_per_million=Decimal(input_usd),
        output_usd_per_million=Decimal(output_usd),
        effective_from=effective_from,
    )


_EPOCH: Final = date(2024, 1, 1)

# Seeded from published list prices. Each model maps to its price history,
# oldest first; `resolve_price` picks the entry in force on a given date.
PRICE_TABLE: Final[dict[str, list[ModelPrice]]] = {
    # -- OpenAI --------------------------------------------------------
    "gpt-4o": [_price("gpt-4o", "openai", "2.50", "10.00", _EPOCH)],
    "gpt-4o-mini": [_price("gpt-4o-mini", "openai", "0.15", "0.60", _EPOCH)],
    "gpt-4-turbo": [_price("gpt-4-turbo", "openai", "10.00", "30.00", _EPOCH)],
    "gpt-3.5-turbo": [_price("gpt-3.5-turbo", "openai", "0.50", "1.50", _EPOCH)],
    "text-embedding-3-small": [_price("text-embedding-3-small", "openai", "0.02", "0.00", _EPOCH)],
    "text-embedding-3-large": [_price("text-embedding-3-large", "openai", "0.13", "0.00", _EPOCH)],
    # -- Anthropic -----------------------------------------------------
    "claude-3-5-sonnet-20241022": [
        _price("claude-3-5-sonnet-20241022", "anthropic", "3.00", "15.00", _EPOCH)
    ],
    "claude-3-5-haiku-20241022": [
        _price("claude-3-5-haiku-20241022", "anthropic", "0.80", "4.00", _EPOCH)
    ],
    "claude-3-opus-20240229": [
        _price("claude-3-opus-20240229", "anthropic", "15.00", "75.00", _EPOCH)
    ],
    # -- Google --------------------------------------------------------
    "gemini-1.5-pro": [_price("gemini-1.5-pro", "gemini", "1.25", "5.00", _EPOCH)],
    "gemini-1.5-flash": [_price("gemini-1.5-flash", "gemini", "0.075", "0.30", _EPOCH)],
}


def known_models() -> frozenset[str]:
    return frozenset(PRICE_TABLE)


def resolve_price(model: str, at: datetime | date | None = None) -> ModelPrice:
    """The price in force for ``model`` on ``at``.

    Args:
        model: Model identifier as the provider names it.
        at: Point in time. Defaults to now — but pass the request's own
            timestamp when costing a historical row, which is the entire
            reason this table is versioned.

    Raises:
        PriceNotFoundError: Unknown model, or no price effective that early.
    """
    history = PRICE_TABLE.get(model)
    if not history:
        raise PriceNotFoundError(f"no price on file for model {model!r}")

    if at is None:
        as_of = datetime.now(UTC).date()
    elif isinstance(at, datetime):
        as_of = at.astimezone(UTC).date() if at.tzinfo else at.date()
    else:
        as_of = at

    # History is oldest-first, so the entry in force is the last one whose
    # effective_from is on or before the date in question.
    boundaries = [entry.effective_from for entry in history]
    index = bisect_right(boundaries, as_of) - 1

    if index < 0:
        raise PriceNotFoundError(
            f"no price for model {model!r} effective on or before {as_of.isoformat()}"
        )

    return history[index]
