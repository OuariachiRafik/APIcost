"""Weekly savings digest — UC-38, BUILD_SPEC §4 P9.

Spend, savings split by mechanism, notable events, and the top recommendation,
sent once a week at a sensible local hour.

Three things this deliberately does not do:

**It does not send when there is nothing to say.** A digest reporting zero
requests and zero savings is a weekly reminder that the user is not using the
product, and the honest response to that is silence, not a graph of zeroes.

**It does not flatter.** Savings are reported the way `GET /usage` reports
them, net of escalation cost, and a negative week is shown as negative. A
digest that only ever brings good news is one people learn to skim.

**It does not omit the unsubscribe link.** Not a preference buried in settings
— a working one-click link in every send. That is a legal requirement in most
of the places this would ship and a decency requirement everywhere else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text

from apicost.config import Settings, get_settings
from apicost.core.logging import get_logger
from apicost.db.session import get_admin_engine
from apicost.notify.email import EmailSender, Message, get_sender

__all__ = [
    "DIGEST_HOUR_LOCAL",
    "DigestContent",
    "build_digest",
    "send_weekly_digests",
    "user_is_due",
]

_logger = get_logger(__name__)

DIGEST_HOUR_LOCAL = 8
"""08:00 in the user's own timezone. Monday morning, when someone might act on
it, rather than 03:00 UTC when it lands under everything else."""

DIGEST_WEEKDAY = 0
"""Monday."""

MIN_REQUESTS_TO_SEND = 1
"""Below this there is nothing to report and we stay quiet."""


@dataclass(frozen=True)
class DigestContent:
    email: str
    period_start: datetime
    period_end: datetime

    requests: int
    spend_usd: float
    cache_savings_usd: float
    routing_savings_usd: float
    total_savings_usd: float

    cache_hit_rate: float
    notable_events: list[str] = field(default_factory=list)
    top_recommendation: str | None = None
    unsubscribe_url: str = ""

    @property
    def worth_sending(self) -> bool:
        return self.requests >= MIN_REQUESTS_TO_SEND


def user_is_due(
    timezone_name: str,
    last_sent_at: datetime | None,
    now: datetime | None = None,
) -> bool:
    """Is it the digest hour, on the digest day, in this user's timezone?

    The job runs hourly and asks this per user, which is how "scheduled per
    user timezone" works without a scheduler per timezone.

    ``last_sent_at`` guards against the double-send that a retried job, a clock
    adjustment, or a timezone whose UTC offset shifts would otherwise cause. A
    user receiving the same digest twice is a small thing that reads as
    carelessness.
    """
    at = now or datetime.now(UTC)

    try:
        local = at.astimezone(ZoneInfo(timezone_name))
    except (ZoneInfoNotFoundError, ValueError):
        # An unknown timezone string should not cost the user their digest.
        local = at.astimezone(UTC)

    if local.weekday() != DIGEST_WEEKDAY or local.hour != DIGEST_HOUR_LOCAL:
        return False

    if last_sent_at is None:
        return True

    # Six days rather than seven: an hourly job whose previous run drifted by a
    # few minutes must not skip a whole week because "seven days" had not quite
    # elapsed.
    return (at - last_sent_at) >= timedelta(days=6)


async def build_digest(
    user_id: str,
    email: str,
    *,
    unsubscribe_token: str,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> DigestContent:
    """Assemble one user's week from the ledger and rollups."""
    cfg = settings or get_settings()
    end = now or datetime.now(UTC)
    start = end - timedelta(days=7)

    async with get_admin_engine().begin() as conn:
        totals = (
            await conn.execute(
                text(
                    """
                    SELECT
                      count(*)                                          AS requests,
                      COALESCE(sum(cost_usd), 0)                        AS spend,
                      count(*) FILTER (WHERE cache_hit)                 AS hits,
                      COALESCE(sum(cost_would_have_been_usd - cost_usd)
                               FILTER (WHERE cache_hit), 0)             AS cache_saved,
                      COALESCE(sum(cost_would_have_been_usd - cost_usd)
                               FILTER (WHERE routed AND NOT cache_hit
                                       AND NOT escalation_triggered), 0) AS routing_saved,
                      COALESCE(sum(cost_usd - cost_would_have_been_usd)
                               FILTER (WHERE escalation_triggered), 0)   AS escalation_cost
                    FROM requests_log
                    WHERE user_id = :user_id
                      AND timestamp >= :start AND timestamp < :end
                    """
                ),
                {"user_id": user_id, "start": start, "end": end},
            )
        ).one()

        events = (
            await conn.execute(
                text(
                    "SELECT title FROM alert_events WHERE user_id = :user_id "
                    "AND created_at >= :start ORDER BY "
                    "CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, "
                    "created_at DESC LIMIT 3"
                ),
                {"user_id": user_id, "start": start},
            )
        ).scalars()
        notable = list(events)

        top = (
            await conn.execute(
                text(
                    "SELECT title, projected_savings_usd FROM advisor_recommendations "
                    "WHERE user_id = :user_id AND status = 'open' "
                    "ORDER BY projected_savings_usd DESC LIMIT 1"
                ),
                {"user_id": user_id},
            )
        ).first()

    requests = int(totals.requests)
    cache_saved = float(totals.cache_saved)
    # Net of escalation, exactly as GET /routing/stats reports it. A digest
    # that used the gross figure would disagree with the dashboard, and the
    # dashboard is the one the user checks when the number looks too good.
    routing_saved = float(totals.routing_saved) - float(totals.escalation_cost)

    recommendation = None
    if top is not None:
        recommendation = f"{top.title} (about ${float(top.projected_savings_usd):,.2f}/month)"

    return DigestContent(
        email=email,
        period_start=start,
        period_end=end,
        requests=requests,
        spend_usd=round(float(totals.spend), 4),
        cache_savings_usd=round(cache_saved, 4),
        routing_savings_usd=round(routing_saved, 4),
        total_savings_usd=round(cache_saved + routing_saved, 4),
        cache_hit_rate=round(int(totals.hits) / requests, 4) if requests else 0.0,
        notable_events=notable,
        top_recommendation=recommendation,
        unsubscribe_url=f"{cfg.public_base_url}/unsubscribe/{unsubscribe_token}",
    )


def render_digest(content: DigestContent) -> Message:
    """Plain text. Every number the subject line implies is in the body."""
    week = content.period_start.strftime("%-d %B")
    lines = [
        f"Your APICost week — {week} to {content.period_end.strftime('%-d %B %Y')}",
        "",
        f"  Requests        {content.requests:,}",
        f"  Spend           ${content.spend_usd:,.2f}",
        f"  Saved           ${content.total_savings_usd:,.2f}",
        f"    from caching  ${content.cache_savings_usd:,.2f} "
        f"({content.cache_hit_rate:.0%} hit rate)",
        f"    from routing  ${content.routing_savings_usd:,.2f}",
    ]

    if content.routing_savings_usd < 0:
        lines.append(
            "    (routing cost more than it saved this week — escalations outweighed the savings)"
        )

    if content.notable_events:
        lines += ["", "Notable:"]
        lines += [f"  - {event}" for event in content.notable_events]

    if content.top_recommendation:
        lines += ["", "Worth a look:", f"  {content.top_recommendation}"]

    lines += [
        "",
        "-- APICost",
        f"Stop these emails: {content.unsubscribe_url}",
    ]

    return Message(
        to=content.email,
        subject=f"APICost: you saved ${content.total_savings_usd:,.2f} this week",
        text="\n".join(lines),
    )


async def send_weekly_digests(
    *,
    settings: Settings | None = None,
    sender: EmailSender | None = None,
    now: datetime | None = None,
) -> int:
    """Send to every user due in their own timezone. Returns emails sent."""
    cfg = settings or get_settings()
    at = now or datetime.now(UTC)

    try:
        async with get_admin_engine().begin() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT id, email, timezone, digest_unsubscribe_token, "
                        "last_digest_sent_at FROM users "
                        "WHERE digest_enabled AND is_active"
                    )
                )
            ).all()
    except Exception as exc:
        _logger.warning(
            "digest_user_query_failed", subsystem="notify", error_type=type(exc).__name__
        )
        return 0

    transport = sender or get_sender(cfg)
    sent = 0

    for row in rows:
        if not user_is_due(str(row.timezone), row.last_digest_sent_at, at):
            continue

        try:
            content = await build_digest(
                str(row.id),
                str(row.email),
                unsubscribe_token=str(row.digest_unsubscribe_token),
                settings=cfg,
                now=at,
            )
        except Exception as exc:
            _logger.warning(
                "digest_build_failed", subsystem="notify", error_type=type(exc).__name__
            )
            continue

        if not content.worth_sending:
            # Quiet week. Mark it anyway so a user with no traffic is not
            # re-evaluated every hour for the rest of the day.
            await _mark_sent(str(row.id), at)
            continue

        if await transport.send(render_digest(content)):
            await _mark_sent(str(row.id), at)
            sent += 1

    if sent:
        _logger.info("weekly_digests_sent", subsystem="notify", count=sent)
    return sent


async def _mark_sent(user_id: str, at: datetime) -> None:
    try:
        async with get_admin_engine().begin() as conn:
            await conn.execute(
                text("UPDATE users SET last_digest_sent_at = :at WHERE id = :id"),
                {"id": user_id, "at": at},
            )
    except Exception as exc:
        _logger.warning(
            "digest_mark_sent_failed", subsystem="notify", error_type=type(exc).__name__
        )


def unsubscribe_page(success: bool) -> str:
    """The page behind the link. Deliberately trivial and self-contained."""
    if success:
        return (
            "<!doctype html><meta charset=utf-8><title>Unsubscribed</title>"
            "<body style='font-family:system-ui;max-width:32rem;margin:4rem auto'>"
            "<h1>Unsubscribed</h1><p>You will not receive any more weekly digests. "
            "Alerts about your budgets and your keys are separate and still on — "
            "you can turn those off in your project settings.</p>"
        )
    return (
        "<!doctype html><meta charset=utf-8><title>Link not recognised</title>"
        "<body style='font-family:system-ui;max-width:32rem;margin:4rem auto'>"
        "<h1>Link not recognised</h1><p>This unsubscribe link is not valid. "
        "You can turn digests off from your account settings.</p>"
    )
