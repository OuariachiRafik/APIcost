"""Unit tests for P9's pure parts — UC-38 scheduling, UC-39 disclosure rules."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from apicost.advisor.benchmark import (
    MIN_COHORT_SIZE,
    CohortStats,
    compare_to_peers,
)
from apicost.notify.digest import (
    DIGEST_HOUR_LOCAL,
    DigestContent,
    render_digest,
    user_is_due,
)

# -- UC-38: "scheduled per user timezone" -----------------------------------


def _monday_utc(hour: int) -> datetime:
    """2026-08-10 is a Monday."""
    return datetime(2026, 8, 10, hour, 0, tzinfo=UTC)


def test_a_utc_user_is_due_at_the_digest_hour() -> None:
    assert user_is_due("UTC", None, _monday_utc(DIGEST_HOUR_LOCAL))


def test_a_utc_user_is_not_due_at_other_hours() -> None:
    assert not user_is_due("UTC", None, _monday_utc(DIGEST_HOUR_LOCAL + 1))
    assert not user_is_due("UTC", None, _monday_utc(DIGEST_HOUR_LOCAL - 1))


def test_a_tokyo_user_is_due_at_their_own_local_hour() -> None:
    """The whole point of per-timezone scheduling.

    Tokyo is UTC+9, so their Monday 08:00 is Sunday 23:00 UTC — a different
    day as well as a different hour.
    """
    tokyo_monday_8am = datetime(2026, 8, 9, 23, 0, tzinfo=UTC)

    assert user_is_due("Asia/Tokyo", None, tokyo_monday_8am)
    assert not user_is_due("UTC", None, tokyo_monday_8am)


def test_a_los_angeles_user_is_due_at_their_own_local_hour() -> None:
    """UTC-7 in August, so 08:00 local is 15:00 UTC the same day."""
    la_monday_8am = datetime(2026, 8, 10, 15, 0, tzinfo=UTC)

    assert user_is_due("America/Los_Angeles", None, la_monday_8am)
    assert not user_is_due("Asia/Tokyo", None, la_monday_8am)


def test_nobody_is_due_on_a_tuesday() -> None:
    tuesday = datetime(2026, 8, 11, DIGEST_HOUR_LOCAL, 0, tzinfo=UTC)
    assert not user_is_due("UTC", None, tuesday)


def test_a_user_sent_to_this_morning_is_not_sent_to_again() -> None:
    """A retried job must not double-send."""
    at = _monday_utc(DIGEST_HOUR_LOCAL)
    assert not user_is_due("UTC", at - timedelta(minutes=5), at)


def test_a_user_sent_to_last_week_is_due_again() -> None:
    at = _monday_utc(DIGEST_HOUR_LOCAL)
    assert user_is_due("UTC", at - timedelta(days=7), at)


def test_a_slightly_early_run_still_sends() -> None:
    """Six days, not seven: hourly cron drift must not skip a whole week."""
    at = _monday_utc(DIGEST_HOUR_LOCAL)
    assert user_is_due("UTC", at - timedelta(days=6, hours=23), at)


def test_an_unknown_timezone_does_not_cost_the_user_their_digest() -> None:
    assert user_is_due("Mars/Olympus_Mons", None, _monday_utc(DIGEST_HOUR_LOCAL))
    assert user_is_due("", None, _monday_utc(DIGEST_HOUR_LOCAL))


# -- UC-38: content ---------------------------------------------------------


def _content(**overrides: object) -> DigestContent:
    base: dict[str, object] = {
        "email": "someone@example.com",
        "period_start": datetime(2026, 8, 3, tzinfo=UTC),
        "period_end": datetime(2026, 8, 10, tzinfo=UTC),
        "requests": 4200,
        "spend_usd": 31.55,
        "cache_savings_usd": 12.40,
        "routing_savings_usd": 6.10,
        "total_savings_usd": 18.50,
        "cache_hit_rate": 0.31,
        "notable_events": ["Spend spike on production"],
        "top_recommendation": "Move /v1/chat to gpt-4o-mini (about $6.30/month)",
        "unsubscribe_url": "https://api.example.com/unsubscribe/tok",
    }
    base.update(overrides)
    return DigestContent(**base)  # type: ignore[arg-type]


def test_a_quiet_week_is_not_worth_sending() -> None:
    """Silence beats a weekly graph of zeroes."""
    assert not _content(requests=0).worth_sending
    assert _content(requests=1).worth_sending


def test_every_digest_carries_a_working_unsubscribe_link() -> None:
    message = render_digest(_content())
    assert "https://api.example.com/unsubscribe/tok" in message.text


def test_the_digest_reports_savings_by_mechanism() -> None:
    text = render_digest(_content()).text
    assert "caching" in text
    assert "routing" in text
    assert "$12.40" in text
    assert "$6.10" in text


def test_notable_events_and_the_top_recommendation_appear() -> None:
    text = render_digest(_content()).text
    assert "Spend spike on production" in text
    assert "gpt-4o-mini" in text


def test_a_negative_routing_week_is_shown_not_hidden() -> None:
    """A digest that only ever brings good news is one people skim."""
    text = render_digest(_content(routing_savings_usd=-3.20, total_savings_usd=9.20)).text

    assert "-$3.20" in text or "$-3.20" in text
    assert "cost more than it saved" in text


# -- UC-39: the disclosure rules --------------------------------------------


def _cohort(size: int) -> CohortStats:
    return CohortStats(
        size=size,
        p25_cost_per_request=0.001,
        p50_cost_per_request=0.002,
        p75_cost_per_request=0.004,
    )


def test_nothing_is_published_below_the_minimum_cohort() -> None:
    """BUILD_SPEC §4 P9. Not rounded, not banded — nothing."""
    result = compare_to_peers(0.002, 500, _cohort(MIN_COHORT_SIZE - 1))

    assert not result.available
    assert result.reason == "COHORT_TOO_SMALL"
    assert result.cohort_size == 0
    assert result.cohort_p50 == 0.0
    assert result.percentile_band == ""


def test_no_cohort_at_all_publishes_nothing() -> None:
    result = compare_to_peers(0.002, 500, None)
    assert not result.available
    assert result.cohort_p50 == 0.0


def test_the_minimum_cohort_publishes() -> None:
    result = compare_to_peers(0.002, 500, _cohort(MIN_COHORT_SIZE))
    assert result.available
    assert result.cohort_size == MIN_COHORT_SIZE


def test_your_own_numbers_survive_a_refusal() -> None:
    """Your own spend is yours; only the comparison is withheld."""
    result = compare_to_peers(0.002, 500, _cohort(3))

    assert not result.available
    assert result.your_cost_per_request == 0.002
    assert result.your_requests == 500


def test_a_user_with_no_traffic_gets_no_comparison() -> None:
    result = compare_to_peers(0.0, 0, _cohort(200))
    assert not result.available
    assert result.reason == "NO_TRAFFIC"


def test_only_interior_percentiles_are_exposed() -> None:
    """Never min or max.

    In a cohort at exactly the minimum size, an extreme is one account's own
    number, so publishing it would disclose that account to everyone else.
    """
    result = compare_to_peers(0.002, 500, _cohort(MIN_COHORT_SIZE))
    fields = vars(result)

    assert not any("min" in name or "max" in name for name in fields)
    assert {"cohort_p25", "cohort_p50", "cohort_p75"} <= set(fields)


@pytest.mark.parametrize(
    ("cost", "band"),
    [
        (0.0005, "bottom_quartile"),
        (0.0015, "below_median"),
        (0.003, "above_median"),
        (0.010, "top_quartile"),
    ],
)
def test_the_band_places_the_caller_without_an_exact_rank(cost: float, band: str) -> None:
    """A band, not a percentile.

    An exact rank moves as other accounts join and leave, and watched over time
    that movement leaks their magnitude.
    """
    result = compare_to_peers(cost, 500, _cohort(200))
    assert result.percentile_band == band
    assert result.verdict


def test_cheaper_is_reported_as_better() -> None:
    cheap = compare_to_peers(0.0005, 500, _cohort(200))
    dear = compare_to_peers(0.010, 500, _cohort(200))

    assert "lower than most" in cheap.verdict
    assert "higher than most" in dear.verdict
