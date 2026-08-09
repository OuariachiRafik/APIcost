# P9 — Engagement & retention

**Use cases:** UC-38, UC-39

## Acceptance criteria

| # | Criterion | Result |
|---|---|---|
| 1 | Weekly digest: spend, savings by mechanism, notable events, top recommendation | ✅ all four, and a negative routing week is shown as negative |
| 2 | Scheduled per user timezone | ✅ hourly job asks who is due in their own local time; Tokyo and Los Angeles asserted separately |
| 3 | Unsubscribe link required | ✅ in every send, one click, no session, no JavaScript |
| 4 | Peer benchmark publishes **only** at a cohort of ≥50 | ✅ nothing below it — not rounded, not banded, nothing |
| 5 | Only aggregates, never anything traceable to another account | ✅ asserted against the serialised payload with distinctive cohort emails and ids |

521 tests pass. Lint and `mypy --strict` clean across 19 files. Migrations reverse and reapply.

## What shipped

- `notify/digest.py` — content, per-timezone scheduling, rendering.
- `advisor/benchmark.py` — pure. The disclosure rules, separate from the SQL.
- `api/routers/benchmark.py` — `GET /benchmark/peer`, `GET /unsubscribe/{token}`.
- Migration 0013 — digest preferences and a unique unsubscribe token on `users`.
- Hourly `weekly_digest_job` on the worker.

## Decisions worth knowing

**The benchmark reports interior percentiles only — p25, p50, p75, never min or max.** In a cohort at
exactly the minimum size, an extreme *is* one account's own number. Publishing a maximum tells the
top spender nothing they did not know and discloses them to the other forty-nine.

**And a band rather than an exact rank.** An exact percentile moves as other accounts join and leave,
and watched over time that movement leaks their magnitude. A quartile is what the user can act on
anyway.

**The caller is excluded from their own cohort.** Otherwise a user in a small cohort could infer its
composition by watching their own traffic move the median. Asserted with a caller whose cost is 2,500×
the cohort's.

**Below the threshold the numbers are never computed into Python.** `_cohort()` returns `None` rather
than the row, so the percentiles cannot be logged, cached, or surfaced by a later edit that forgets
why the check was there. The refusal is structural, not a formatting decision.

**The digest is silent in a quiet week.** A weekly email reporting zero requests and zero savings is
a reminder that the user is not using the product. The row is still marked as sent so a dormant
account is not re-evaluated every hour for the rest of the day.

**The digest does not flatter.** Routing savings are net of escalation, matching `GET /routing/stats`,
and a negative week prints as negative with an explanation. A digest that only ever brings good news
is one people learn to skim.

**Unsubscribe is unauthenticated by necessity.** The link is opened from a mail client months later.
One that requires logging in is one people report as spam instead. It acts on a 256-bit CSPRNG token
and nothing else, and an unknown token returns the same shape of page as a known one — nothing here
should be an oracle.

## Defects found and fixed

**1. A `str.replace` without a count put the digest columns on four models.** `is_active` appears on
`users`, `provider_keys`, `routing_rules` and `budgets`, so `digest_enabled`,
`digest_unsubscribe_token` and `last_digest_sent_at` were added to all four while the migration
altered only `users`. Caught immediately by the tests, as `column "digest_enabled" of relation
"provider_keys" does not exist`.

This is the third time in this project that an unanchored string edit has done silent damage — it is
also what caused the P4 cache bug. The lesson that keeps not sticking: replacing on a line that is
not unique to its target will hit every other occurrence, and the ORM will not complain until
something touches the table.

**2. The unsubscribe token had no server-side default,** so any raw `INSERT` into `users` violated
the NOT NULL constraint. Found by three unrelated cache tests that build users with raw SQL. The fix
is not to patch those inserts: "every user has a working unsubscribe link" is a guarantee that should
live in the database rather than depend on every insert path remembering. The column now defaults
server-side.

**3. `gen_random_bytes` needs pgcrypto**, which Postgres 16 does not enable by default. Installing an
extension for a one-time backfill is a permanent cost for a momentary need; two `gen_random_uuid()`s
give 244 bits from a built-in strong source.

## A limitation, stated rather than hidden

**The cohort is every account with traffic, not a comparable one.** UC-39 says "a cohort aggregate",
and this compares a solo developer's hobby project against every other account in the system
regardless of size, workload, or model mix. A user running batch summarisation at $0.0002/request and
one running long-context agents at $0.30/request are told they are in different quartiles, which is
true and not useful.

Segmenting by workload would make it useful and would also shrink every cohort, which is precisely
what the ≥50 floor exists to prevent — the more comparable the cohort, the more each disclosure says
about its members. That tension is real and is not resolved here. The current answer is the safe one.

## Not built

No UI. The digest is sent, the benchmark is served, and neither is visible in the dashboard.
