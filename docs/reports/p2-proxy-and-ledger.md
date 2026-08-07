# P2 — Proxy passthrough & ledger

**Use cases:** UC-06, and the ledger foundation UC-12 reads from.
**Commit:** `7188085`

## Acceptance criteria

| # | Criterion | Result |
|---|---|---|
| 1 | Unmodified `openai` SDK works streaming and non-streaming | ✅ 10 e2e tests import the real package and change only `base_url` |
| 2 | Redis killed mid-request → logging degrades, completion returns | ✅ verified on both unary and streaming paths |
| 3 | Rows in `requests_log` with correct tokens and cost within 5 s | ✅ exact Decimal cost; drain cron runs every 5 s |

228 backend tests, 12 web tests. Lint and types clean; migrations reversible to base and back.

**Latency harness built now, not at the end** (§5): proxy overhead measured against the stub
provider at **14.5 ms p95**, against a 100 ms NFR.

## What shipped

- `proxy/pipeline.py` — the orchestrator, with §6.1's ordering fixed now so cache (P4), routing (P5)
  and escalation slot in rather than rearrange.
- `proxy/streaming.py` — SSE parse, non-buffering tee, and replay. Every function yields downstream
  *before* it records anything.
- `proxy/providers/` — OpenAI as the canonical shape; Anthropic and Gemini translate into and out of it.
- `core/deadline.py` — one shared 150 ms budget and the `failopen` guard.
- Ledger: Redis stream → ARQ consumer group → `requests_log`, partitioned monthly.
- `ledger/pricing.py` versioned by `effective_from`; `ledger/cost.py` Decimal throughout.
- `metrics/` — every §6.6 fix applied and tested.
- UC-06 `POST /projects/{id}/test-connection`, naming the specific failure rather than "failed".

## Defects found

**1. Every proxied request returned 401.** `proxy_keys` had a strict RLS policy, but proxy
authentication must read that table *before* it knows who the user is — the key hash is how it finds
out. Migration 0004 makes it readable unscoped and writable only when scoped. `projects` stays
strict: the resolver reads the key, scopes the session, then reads the project.

**2. `failopen` could not skip its body.** A step with zero budget left still ran. It now enters with
a zero timeout, cutting the step off at its first await — and every real optimization step awaits
something, so nothing meaningful executes.

**3. structlog cached its output stream on first use**, leaking a closed `capsys` stream into every
later test. Caching is now off; the cost is a dict lookup per call.

## Tradeoffs worth knowing

- **Under a Redis outage we lose usage records rather than fail the request.** Losing observability
  is recoverable; taking down somebody's production application is not. The stream is capped so a
  backed-up worker drops old events rather than exhausting Redis.
- **The data plane returns OpenAI's error envelope, not RFC 7807.** The caller is an SDK expecting
  the provider's format.
- **Anthropic and Gemini adapters are written but lightly exercised.** Unit-covered translation, but
  no live provider has answered them. Worth a smoke test against real keys before relying on them.
