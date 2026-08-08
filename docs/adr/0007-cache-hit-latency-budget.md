# ADR 0007 — Measure the cache-hit NFR inside the proxy, not at the client

**Status:** accepted · **Date:** 2026-08-08 · **Phase:** P5

## Context

BUILD_SPEC §5 sets a **30 ms p95** target for cache-hit responses. P4 measured 11.14 ms and passed.
After P5 the same assertion began failing, and the first instinct — mine — was that P5 had cost
something, or that a full suite run was noisy. Both were wrong, and worth recording as such.

The number was not merely high, it was **unstable**. Three consecutive runs of identical code:

| run | client wall-clock p95 | proxy in-process p95 |
|-----|----------------------|----------------------|
| 1 | 40.3 ms | 7.2 ms |
| 2 | 19.7 ms | 7.6 ms |
| 3 | 28.9 ms | 4.8 ms |

With the `make dev` containers also running, the same test reached **102 ms**. Those containers
hold a second proxy and API under `--reload`, and Docker runs a healthcheck on each every 15 s,
which spawns a `runc` exec that briefly takes ~150 % CPU. The e2e harness starts its *own* proxy, so
none of that serves test traffic — it is pure contention.

Two things follow. Wall-clock here includes httpx, a single-worker uvicorn, and a WSL2 loopback, and
is dominated by whatever else the machine is doing. And a p95 over 20 samples is decided by its
single worst sample, so one scheduling hiccup fails the run.

Raising the budget was considered and rejected. It was briefly set to 35 ms; the next run came back
at 38.6 ms, which is the whole argument against that approach — a threshold tuned against noise gets
tuned again, and each increment quietly retires the guarantee it was meant to hold.

## Decision

Assert the NFR against **`X-APICost-Latency-Ms`**, a new response header carrying the proxy's own
in-process time for the request, and keep the budget at the spec's **30 ms**.

Client wall-clock keeps a loose 150 ms ceiling as a gross-regression guard, with a failure message
that points at the environment before the code.

The header is not test scaffolding. It is the latency APICost adds, isolated from the network on
either side, and it is the number a user comparing us against calling the provider directly actually
wants — supplied rather than asserted on a marketing page. It goes in a header, never the body
(hard rule 6).

## Consequences

- The 30 ms NFR is **met and enforced**: 7.4 / 9.8 / 23.5 ms p95 across three runs.
- The measurement no longer moves when the dev stack is up, so the test stops producing failures
  nobody can act on — the failure mode that makes teams delete latency tests.
- **It now measures server time, so a regression in client-visible cost that is not in-process
  would not trip the strict assertion.** The 150 ms wall ceiling is the backstop; it is deliberately
  loose, and it is the weaker of the two checks.
- Benchmarks on this machine should stop the app containers first: `docker compose stop proxy api
  web worker`. Postgres and Redis must stay up — the tests use them.
