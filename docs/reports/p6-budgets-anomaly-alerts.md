# P6 — Stats, anomaly, budgets, alerts

**Use cases:** UC-29, UC-30, UC-31, UC-32, UC-33, UC-34

## Acceptance criteria

| # | Criterion | Result |
|---|---|---|
| 1 | Runaway loop (500 req/60 s vs 5/min baseline) fires a spike alert within 2 min | ✅ fires on the first closed window — 1 minute |
| 2 | `hard_stop` stops traffic within one request of the threshold | ✅ asserted in both directions: not early, and not overshooting by more than the one request that crossed |
| 3 | Fail-open does not apply to budget enforcement | ✅ fails closed for `hard_stop` only; `soft_throttle` and `alert_only` pass through, logged loudly |
| 4 | Kill switch takes effect in <1 s | ✅ measured end to end, revocation + cache purge |

420 tests pass. Lint clean, `mypy --strict` clean across 14 files, migrations reverse to base and back.

## What shipped

- `stats/welford.py` — online mean/variance, O(1) per observation, exactly invertible.
- `stats/rolling.py` — per-project windowed spend rate. One closed minute = one observation.
- `anomaly/zscore.py` — fast path (UC-31): z ≥ 3.0 over ≥ 30 windows.
- `anomaly/forest.py` — slow path (UC-32): IsolationForest over five shape features, every 5 min.
- `anomaly/alerts.py` — dedupe → persist → notify, with the §6.8 30-minute cooldown.
- `anomaly/store.py` — Redis working copy, `rolling_stats` durable copy (ADR 0008).
- `budgets/enforcement.py` — Redis-only hot-path check, three actions.
- `notify/email.py` — `EmailSender` protocol, Resend and SMTP implementations.
- Migration 0010 (`budgets`, `alert_events`, `rolling_stats`), the budgets/alerts/kill-switch API.

## Decisions worth knowing

**Budget counters are incremented by the proxy, not the worker.** The acceptance criterion is one
request, and the ledger drain runs on a 5-second cron — at production rates that is hundreds of
requests of overrun. It costs one extra Redis command on a round trip the ledger was making anyway.
The counter is consequently an optimistic view that can drift if a process dies mid-request;
Postgres remains the authority and reconciles it.

**The budget check runs before the cache lookup.** A cache hit costs nothing, so serving one to a
stopped project would be defensible on cost grounds. It would also mean a project the user believes
is stopped keeps answering traffic, and "stopped" has to mean stopped.

**A cache hit consumes no budget.** It was never billed, so it must not count. This is correct and
it has a testing consequence worth stating: a budget test that reuses prompts measures the cache,
not the budget. Both of this phase's enforcement tests initially passed traffic that never reached
the provider — including one where merely *similar* probe prompts ("throttle probe 0", "…1") were
close enough for the semantic cache. They now disable caching explicitly and say why.

**Budget boundaries are UTC.** A user-timezone boundary needs a per-user offset on the hot path, and
daylight saving would hand some users a 23- or 25-hour "day" twice a year. Spend limits should not
have leap hours.

**The kill switch does not touch provider keys.** The user is containing a leak of *our* credential.
Destroying their OpenAI key turns one incident into two, and it is not ours to destroy.

## The defect that mattered: the leak detector could not detect leaks

`anomaly/forest.py` is the module whose entire purpose is UC-32 — spotting a stolen key being used
at *ordinary* volume, where the z-score sees nothing because the spend is normal. Written the
obvious way, it scored that exact scenario as **normal (+0.014)** and would have shipped silently
broken.

An isolation tree can only split a feature between the minimum and maximum it saw while fitting. A
well-behaved project holds model entropy, endpoint entropy and unique-prompt ratio *constant* — that
is what being well-behaved means — so a forest fit on history alone has a degenerate range on
precisely the three features a leaked key changes. It never splits on them, and the stolen-key window
lands in the same leaf as every normal one.

Fitting on history **including** the point being scored makes the deviant dimension splittable:
-0.377, clearly anomalous. One point among 40 moves the model negligibly.

That fix then produced the opposite failure. An isolation forest measures *uniqueness*, not distance,
so once the scored window is the only point differing on a constant feature, it is isolated in one
split whether the move was 0.05 → 0.051 or 0.1 → 2.4. Benign traffic scored **-0.339**, effectively
indistinguishable from the leak's -0.377.

The forest's verdict is therefore now gated on a robust magnitude test — median absolute deviation,
with a relative fallback for the constant case — and a feature must move at least 3 robust units
before the forest's opinion counts. Same shape of argument as the z-score path's absolute-dollar
floor: relative anomaly is necessary but not sufficient. Both failures are pinned by tests, including
one that asserts the forest *did* call the benign window unique and the gate is what stopped it.

**None of this would have surfaced without the test that describes the actual scenario.** A test
asserting "detect() returns a verdict" passes against a detector that never fires.

## Defects found and fixed

**1. Alert scoring could have caused ledger redelivery.** The anomaly hook was placed inside the
ledger insert's `try`, so a detector failure would have fallen into the handler that skips the ack
and leaves rows for redelivery — rows Postgres had already accepted. Moved out, with its own guard.

**2. Routers cannot call `session.commit()`.** The request-scoped session commits after the handler
returns, so committing inside one raises `Can't operate on closed transaction`. That mattered beyond
the error: the kill switch and every budget write must purge the auth cache *after* their write is
durable. Purging first lets a concurrent proxy request re-resolve from rows that are still live and
re-cache a working key for another 60 seconds — the one failure a kill switch cannot have. Both now
write in their own committed transaction and purge after.

**3. A budget limit below the stored precision returned a 500.** `numeric(12, 6)` rounds anything
under a micro-dollar to zero, tripping the `limit_usd > 0` CHECK after the API had accepted the
request. Now a 422 that names the minimum.

## A limitation, stated rather than hidden

**The slow-path detector has never run against real traffic.** Its thresholds — `CONTAMINATION`,
`SCORE_THRESHOLD`, `DEVIATION_FLOOR` — are reasoned from the algorithm's behaviour and pinned by two
synthetic scenarios. That is enough to know it is not inert and not trigger-happy on the cases
described, and it is not enough to claim a false-positive rate. The first real deployment should
watch what it fires on before anyone trusts the email it sends.

## Not built

No budgets or alerts UI; `web/src/routes/` has no budget editor, alert list, or kill-switch control.
The endpoints exist and are tested. Budget counter reconciliation from Postgres is described in
`budgets/enforcement.py` but the repair job itself is not written — drift is currently bounded by the
counter's period TTL rather than actively corrected.
