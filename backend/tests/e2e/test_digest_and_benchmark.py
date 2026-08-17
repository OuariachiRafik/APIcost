"""P9 acceptance — UC-38, UC-39.

The benchmark tests matter more than they look. UC-39 is the only feature in
the product that reads across account boundaries, so it is the only place where
a query bug is a data-protection incident rather than a wrong number.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from apicost.advisor.benchmark import MIN_COHORT_SIZE
from apicost.core.ids import new_id
from apicost.db.session import get_admin_engine
from apicost.notify.digest import build_digest, send_weekly_digests
from apicost.notify.email import Message
from tests.e2e.conftest import provision_account

pytestmark = pytest.mark.integration


class CapturingSender:
    """An EmailSender that records instead of sending."""

    def __init__(self, *, succeed: bool = True) -> None:
        self.sent: list[Message] = []
        self._succeed = succeed

    async def send(self, message: Message) -> bool:
        self.sent.append(message)
        return self._succeed


async def login(api: AsyncClient, email: str) -> tuple[dict[str, str], str]:
    response = await api.post(
        "/auth/login", json={"email": email, "password": "a-very-long-password"}
    )
    auth = {"Authorization": f"Bearer {response.json()['access_token']}"}
    project_id = (await api.get("/projects", headers=auth)).json()[0]["id"]
    return auth, project_id


async def _user_row(email: str) -> Any:
    async with get_admin_engine().connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT id, digest_unsubscribe_token, digest_enabled, timezone "
                    "FROM users WHERE email = :e"
                ),
                {"e": email},
            )
        ).one()


async def seed_requests(
    user_id: str,
    project_id: str,
    *,
    count: int,
    cost_each: float,
    cache_hits: int = 0,
    prefix: str = "row",
    when: datetime | None = None,
) -> None:
    """`when` must sit inside the window the assertion asks about.

    Defaulting to "yesterday" and then asserting against a hardcoded `now` is a
    test that passes on the day it is written and fails a week later with no
    code change — which is exactly what happened here.
    """
    at = when or datetime.now(UTC) - timedelta(days=1)
    rows = [
        {
            "id": f"{prefix}-{user_id}-{index}",
            "timestamp": at,
            "user_id": user_id,
            "project_id": project_id,
            "request_id": f"{prefix}-{user_id}-{index}",
            "endpoint": "/v1/chat/completions",
            "provider": "openai",
            "model_requested": "gpt-4o",
            "model_used": "gpt-4o",
            "tokens_in": 500,
            "tokens_out": 200,
            "cost_usd": 0.0 if index < cache_hits else cost_each,
            "cost_would_have_been_usd": cost_each,
            "cache_hit": index < cache_hits,
            "routed": False,
            "escalation_triggered": False,
            "status": 200,
        }
        for index in range(count)
    ]
    async with get_admin_engine().begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO requests_log (id, timestamp, user_id, project_id, request_id, "
                "endpoint, provider, model_requested, model_used, tokens_in, tokens_out, "
                "cost_usd, cost_would_have_been_usd, cache_hit, routed, escalation_triggered, "
                "status) VALUES (:id, :timestamp, :user_id, :project_id, :request_id, "
                ":endpoint, :provider, :model_requested, :model_used, :tokens_in, :tokens_out, "
                ":cost_usd, :cost_would_have_been_usd, :cache_hit, :routed, "
                ":escalation_triggered, :status)"
            ),
            rows,
        )


async def make_cohort(n: int, *, cost_each: float = 0.002) -> None:
    """Insert `n` synthetic accounts with traffic, straight to the tables.

    Going through signup would be a few hundred HTTP round trips and Argon2
    hashes to build a cohort, which is minutes per test for no extra coverage —
    the benchmark reads `users` and `requests_log`, not the signup path.
    """
    async with get_admin_engine().begin() as conn:
        for index in range(n):
            user_id = new_id()
            project_id = new_id()
            await conn.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, digest_unsubscribe_token) "
                    "VALUES (:id, :email, 'x', :tok)"
                ),
                {
                    "id": user_id,
                    "email": f"cohort-{index}-{user_id}@example.com",
                    "tok": new_id() + new_id(),
                },
            )
            await conn.execute(
                text("INSERT INTO projects (id, user_id, name) VALUES (:id, :u, 'p')"),
                {"id": project_id, "u": user_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO requests_log (id, timestamp, user_id, project_id, request_id, "
                    "endpoint, provider, model_requested, model_used, tokens_in, tokens_out, "
                    "cost_usd, cache_hit, routed, escalation_triggered, status) "
                    "VALUES (:id, now(), :u, :p, :id, '/v1/chat/completions', 'openai', "
                    "'gpt-4o', 'gpt-4o', 100, 50, :cost, false, false, false, 200)"
                ),
                {"id": new_id(), "u": user_id, "p": project_id, "cost": cost_each},
            )


# -- UC-39: the disclosure floor, against a real database -------------------


@pytest.mark.usefixtures("clean_all")
async def test_no_cohort_statistic_is_published_below_fifty_accounts(
    api_base: AsyncClient,
) -> None:
    """The P9 acceptance criterion, and the one that must never regress."""
    await provision_account(api_base, "solo@example.com")
    auth, project_id = await login(api_base, "solo@example.com")
    user = await _user_row("solo@example.com")
    await seed_requests(str(user.id), project_id, count=100, cost_each=0.002)

    await make_cohort(MIN_COHORT_SIZE - 2)

    body = (await api_base.get("/benchmark/peer", headers=auth)).json()

    assert body["available"] is False
    assert body["reason"] == "COHORT_TOO_SMALL"
    assert body["cohort_size"] == 0
    assert body["cohort_p25"] == 0.0
    assert body["cohort_p50"] == 0.0
    assert body["cohort_p75"] == 0.0
    assert body["percentile_band"] == ""


@pytest.mark.usefixtures("clean_all")
async def test_a_cohort_at_the_threshold_publishes(api_base: AsyncClient) -> None:
    await provision_account(api_base, "grouped@example.com")
    auth, project_id = await login(api_base, "grouped@example.com")
    user = await _user_row("grouped@example.com")
    await seed_requests(str(user.id), project_id, count=100, cost_each=0.002)

    # The caller is excluded from their own cohort, so we need exactly the
    # minimum *other* accounts.
    await make_cohort(MIN_COHORT_SIZE)

    body = (await api_base.get("/benchmark/peer", headers=auth)).json()

    assert body["available"] is True
    assert body["cohort_size"] >= MIN_COHORT_SIZE
    assert body["cohort_p50"] > 0
    assert body["percentile_band"]
    assert body["verdict"]


@pytest.mark.usefixtures("clean_all")
async def test_the_response_carries_nothing_traceable_to_another_account(
    api_base: AsyncClient,
) -> None:
    """UC-39's hard constraint, checked against the serialised payload.

    Cohort members are given distinctive emails and ids; none of it may appear
    anywhere in the response, in any form.
    """
    await provision_account(api_base, "privacy@example.com")
    auth, project_id = await login(api_base, "privacy@example.com")
    user = await _user_row("privacy@example.com")
    await seed_requests(str(user.id), project_id, count=100, cost_each=0.002)

    await make_cohort(MIN_COHORT_SIZE + 5)

    response = await api_base.get("/benchmark/peer", headers=auth)
    raw = response.text.lower()

    assert "cohort-" not in raw, "a cohort member's email leaked"
    assert "@example.com" not in raw, "an address leaked"

    async with get_admin_engine().connect() as conn:
        others = (
            await conn.execute(text("SELECT id FROM users WHERE email LIKE 'cohort-%' LIMIT 10"))
        ).scalars()
        for other_id in others:
            assert other_id.lower() not in raw, "a user id leaked"

    # And no field that could carry a single account's figure. Checked by name
    # rather than by substring: `minimum_cohort_size` is the published constant
    # 50, not a statistic about anybody.
    body = response.json()
    assert not {"cohort_min", "cohort_max", "cohort_min_cost", "cohort_max_cost"} & set(body)
    assert body["minimum_cohort_size"] == MIN_COHORT_SIZE


@pytest.mark.usefixtures("clean_all")
async def test_the_caller_is_excluded_from_their_own_cohort(
    api_base: AsyncClient,
) -> None:
    """Otherwise a user in a small cohort could infer it by moving the median."""
    await provision_account(api_base, "excluded@example.com")
    auth, project_id = await login(api_base, "excluded@example.com")
    user = await _user_row("excluded@example.com")

    # An extreme outlier. If the caller were counted, the percentiles would
    # move visibly with their own traffic.
    await seed_requests(str(user.id), project_id, count=100, cost_each=5.0)
    await make_cohort(MIN_COHORT_SIZE + 10, cost_each=0.002)

    body = (await api_base.get("/benchmark/peer", headers=auth)).json()

    assert body["available"] is True
    assert body["cohort_p75"] < 1.0, "the caller's own cost moved the cohort"
    assert body["percentile_band"] == "top_quartile"


@pytest.mark.usefixtures("clean_all")
async def test_a_user_with_no_traffic_gets_no_comparison(api_base: AsyncClient) -> None:
    await provision_account(api_base, "notraffic@example.com")
    auth, _ = await login(api_base, "notraffic@example.com")
    await make_cohort(MIN_COHORT_SIZE + 5)

    body = (await api_base.get("/benchmark/peer", headers=auth)).json()
    assert body["available"] is False
    assert body["reason"] == "NO_TRAFFIC"


@pytest.mark.usefixtures("clean_all")
async def test_the_benchmark_requires_authentication(api_base: AsyncClient) -> None:
    assert (await api_base.get("/benchmark/peer")).status_code == 401


# -- UC-38: the digest ------------------------------------------------------


@pytest.mark.usefixtures("clean_all")
async def test_the_digest_reports_the_week_by_mechanism(api_base: AsyncClient) -> None:
    await provision_account(api_base, "digest@example.com")
    _, project_id = await login(api_base, "digest@example.com")
    user = await _user_row("digest@example.com")

    await seed_requests(str(user.id), project_id, count=200, cost_each=0.01, cache_hits=50)

    content = await build_digest(
        str(user.id), "digest@example.com", unsubscribe_token=str(user.digest_unsubscribe_token)
    )

    assert content.requests == 200
    assert content.spend_usd > 0
    assert content.cache_savings_usd > 0
    assert content.cache_hit_rate == pytest.approx(0.25)
    assert content.worth_sending
    assert str(user.digest_unsubscribe_token) in content.unsubscribe_url


@pytest.mark.usefixtures("clean_all")
async def test_a_quiet_account_is_not_emailed(api_base: AsyncClient) -> None:
    """Silence, not a weekly graph of zeroes."""
    await provision_account(api_base, "quietweek@example.com")
    user = await _user_row("quietweek@example.com")

    sender = CapturingSender()
    monday = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)
    await _set_timezone(str(user.id), "UTC")

    await send_weekly_digests(sender=sender, now=monday)
    assert sender.sent == []


@pytest.mark.usefixtures("clean_all")
async def test_a_due_user_is_emailed_once_not_twice(api_base: AsyncClient) -> None:
    await provision_account(api_base, "weekly@example.com")
    _, project_id = await login(api_base, "weekly@example.com")
    user = await _user_row("weekly@example.com")
    await _set_timezone(str(user.id), "UTC")
    monday = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)
    await seed_requests(
        str(user.id), project_id, count=50, cost_each=0.01, when=monday - timedelta(days=1)
    )

    sender = CapturingSender()

    assert await send_weekly_digests(sender=sender, now=monday) == 1
    assert await send_weekly_digests(sender=sender, now=monday) == 0, "double-sent"
    assert len(sender.sent) == 1

    message = sender.sent[0]
    assert message.to == "weekly@example.com"
    assert "unsubscribe" in message.text.lower()


@pytest.mark.usefixtures("clean_all")
async def test_the_digest_is_not_sent_at_the_wrong_local_hour(
    api_base: AsyncClient,
) -> None:
    await provision_account(api_base, "tokyo@example.com")
    _, project_id = await login(api_base, "tokyo@example.com")
    user = await _user_row("tokyo@example.com")
    await _set_timezone(str(user.id), "Asia/Tokyo")
    tokyo_due = datetime(2026, 8, 9, 23, tzinfo=UTC)
    await seed_requests(
        str(user.id), project_id, count=50, cost_each=0.01, when=tokyo_due - timedelta(days=1)
    )

    sender = CapturingSender()

    # 08:00 UTC is 17:00 in Tokyo — not their digest hour.
    assert await send_weekly_digests(sender=sender, now=datetime(2026, 8, 10, 8, tzinfo=UTC)) == 0
    # 23:00 UTC Sunday is 08:00 Monday in Tokyo.
    assert await send_weekly_digests(sender=sender, now=tokyo_due) == 1


@pytest.mark.usefixtures("clean_all")
async def test_unsubscribing_works_in_one_click_and_stops_the_digest(
    api_base: AsyncClient,
) -> None:
    """No session, no JavaScript — it is opened from a mail client."""
    await provision_account(api_base, "unsub@example.com")
    _, project_id = await login(api_base, "unsub@example.com")
    user = await _user_row("unsub@example.com")
    await _set_timezone(str(user.id), "UTC")
    await seed_requests(str(user.id), project_id, count=50, cost_each=0.01)

    response = await api_base.get(f"/unsubscribe/{user.digest_unsubscribe_token}")
    assert response.status_code == 200
    assert "unsubscribed" in response.text.lower()

    after = await _user_row("unsub@example.com")
    assert after.digest_enabled is False

    sender = CapturingSender()
    sent = await send_weekly_digests(sender=sender, now=datetime(2026, 8, 10, 8, tzinfo=UTC))
    assert sent == 0
    assert sender.sent == []


@pytest.mark.usefixtures("clean_all")
async def test_an_unknown_unsubscribe_token_is_not_an_oracle(
    api_base: AsyncClient,
) -> None:
    response = await api_base.get("/unsubscribe/not-a-real-token")
    assert response.status_code == 404
    assert "@" not in response.text, "the page must not echo any address"


@pytest.mark.usefixtures("clean_all")
async def test_a_failed_send_is_retried_next_run(api_base: AsyncClient) -> None:
    """A user must not lose their digest to one SMTP failure."""
    await provision_account(api_base, "retry@example.com")
    _, project_id = await login(api_base, "retry@example.com")
    user = await _user_row("retry@example.com")
    await _set_timezone(str(user.id), "UTC")
    monday = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)
    await seed_requests(
        str(user.id), project_id, count=50, cost_each=0.01, when=monday - timedelta(days=1)
    )

    failing = CapturingSender(succeed=False)
    assert await send_weekly_digests(sender=failing, now=monday) == 0

    working = CapturingSender()
    assert await send_weekly_digests(sender=working, now=monday) == 1


async def _set_timezone(user_id: str, timezone_name: str) -> None:
    async with get_admin_engine().begin() as conn:
        await conn.execute(
            text("UPDATE users SET timezone = :tz WHERE id = :id"),
            {"id": user_id, "tz": timezone_name},
        )
