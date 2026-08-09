# P10 — Our own billing

**Use cases:** none directly — this is how the product sustains itself.

## Acceptance criteria

| # | Criterion | Result |
|---|---|---|
| 1 | Free tier up to a request-volume cap, paid tiers above | ✅ free 10k, pro 250k, scale unlimited |
| 2 | `GET /billing/plan`, `POST /billing/checkout-session`, `POST /billing/webhook` | ✅ all three |
| 3 | Webhook signature-verified | ✅ six tests, including forged, wrong-secret, replayed, and tampered |
| 4 | Webhook idempotent | ✅ primary-key claim on Stripe's event id |
| 5 | Plan-limit signals consumed by the proxy | ✅ Redis counter, headers on the response |

540 tests pass. Lint and `mypy --strict` clean. Migrations reverse and reapply.

## What shipped

- `billing/plans.py` — pure. The plan table as data, and the limit arithmetic.
- `billing/usage.py` — the monthly request counter. Redis, never Postgres.
- `billing/stripe_gateway.py` — checkout, signature verification, webhook handlers.
- Migration 0014 — subscription columns on `users`, `billing_events` for idempotency.
- `api/routers/billing.py` and the proxy's plan headers.

## The webhook is the whole security story

`POST /billing/webhook` is public, unauthenticated, and changes what an account is allowed to do.
Without a verified signature, anyone who learns the URL upgrades themselves with one curl command.
So the tests are written from the attacker's side: unsigned, forged, signed with a different secret,
signed correctly but replayed a day later, and correctly signed but with the body altered afterwards.
All six are rejected.

**A deployment with no webhook secret rejects everything with a 503.** Unconfigured is not a reason to
start trusting the internet, and the failure is loud rather than silent.

**Idempotency is a primary-key insert on Stripe's own event id.** A duplicate delivery fails the
insert, which is how we know to skip it. A `SELECT` then `INSERT` would leave a window where two
concurrent deliveries both see nothing and both apply — and Stripe delivers concurrently. If applying
the event then fails, the claim is released so the retry can work; leaving it would let one transient
database error permanently swallow a subscription the user has already paid for.

## Decisions worth knowing

**Going over the free cap warns; it does not block.** This is a cost-optimization product, so the
traffic we would be refusing is traffic the user is *already paying a provider for*. Blocking costs
them money and saves us nothing, and cutting off a working application mid-month is how you lose a
developer permanently. They get told in the response headers and they keep working. `BLOCK` exists in
the vocabulary for a lapsed paid plan or an abusive account, and being popular does not reach it.

**A failed payment marks `past_due` and does not downgrade.** An expired card is often fixed within
the hour, and Stripe runs its own dunning schedule. Downgrading on the first failure would punish a
paying customer for a bank's fraud heuristic.

**A cancellation returns the account to free and deletes nothing.** Keys, projects and history are
untouched. A cancelled subscription is not a deleted account, and treating it as one is how a product
loses someone who meant to come back next quarter.

**An unknown plan id resolves to free, not to an error.** If our own data is wrong, the safe reading
is the most restrictive plan, not unlimited capacity.

**The verified bytes are parsed with `json.loads`, not used as a `StripeObject`.** The SDK's object
overrides attribute access and carries its own `object` field naming the resource type, which
collides with the `data.object` path every handler walks — and `dict()` only flattens the top level.
This is not a second parse of untrusted input: `construct_event` has already proven those exact bytes
came from Stripe.

## Defects found and fixed

**1. `billing_events` was missing from the test cleanup tables.** Three webhook tests passed in
isolation and failed in the full suite, because event ids from an earlier standalone run were still
marked processed. The failure was the idempotency mechanism working correctly across runs, which made
it briefly convincing as a code bug. Any table whose primary key is a natural id has to be in the
cleanup list or its tests become order-dependent.

**2. The test fixtures were not faithful to real Stripe events.** They omitted the top-level
`object: "event"` field, and the SDK reads it to distinguish v1 from v2 events — so `construct_event`
raised `AttributeError` for a reason unrelated to anything under test. Worth recording because the
temptation was to work around it in the gateway rather than fix the fixture.

## Limitations, stated rather than hidden

**This has never talked to Stripe.** Signature verification is exercised against real HMACs computed
in the tests, and the handlers are exercised against realistic payloads, but no request has been made
to Stripe's API and no real webhook has been received. `create_checkout_session` in particular is
written from the API documentation and has not been run. Before charging anyone: run the flow against
Stripe's test mode, and use the CLI's `stripe listen --forward-to` to replay genuine events.

**The `stripe_price_id` values are placeholders.** `price_pro_monthly` and `price_scale_monthly` are
not real price ids; they must be replaced with the ones created in the Stripe dashboard before
checkout can work at all.

**Plan counters are approximate.** They are incremented by the proxy alongside the ledger emit, so a
process that dies between forwarding and incrementing undercounts. Postgres remains the authority and
nothing enforces a hard limit off this number, so the cost of drift is an upgrade prompt shown
slightly late.

## Not built

No UI. No plan-change screen, no upgrade prompt, no billing history — the endpoints exist and are
tested. The proxy's plan headers have no consumer yet.
