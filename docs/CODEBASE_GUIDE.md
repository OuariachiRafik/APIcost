# APICost — Codebase Guide

> Orientation document. Read this before touching code. It explains what the system does, how a
> request actually flows through it, where every concept lives on disk, and the handful of invariants
> that will silently break the product if you violate them.
>
> Keep this file current. If you change architecture, update this in the same commit.

---

## 1. The one-paragraph version

A developer points their existing LLM client at APICost instead of at OpenAI/Anthropic/Gemini by
changing their `base_url`. Every request they make now passes through our **proxy**, which tries to
answer it from a **semantic cache** (free), and failing that **routes** it to the cheapest model
capable of handling it, forwards it to the real provider using the user's own encrypted API key, and
records exactly what happened in a **ledger**. A **dashboard** reads that ledger and shows the user
their spend, what we saved them, and what else they could do. If any of our clever parts break, the
request goes straight to the provider unchanged — the user's app must never go down because our cache
had a bad day.

---

## 2. Mental model: two planes

Everything in this codebase belongs to one of two planes. Knowing which one you're in tells you what
the constraints are.

**Data plane — `main_proxy.py` and everything under `proxy/`, `cache/`, `routing/`.**
On the critical path of somebody's production app. Constraints: <100 ms of added latency, 99.9%
uptime, stateless, fail-open, no synchronous Postgres writes. If you're tempted to add a database
query here, don't — put it in Redis or move it off the path.

**Control plane — `main_api.py` and everything under `api/`, `advisor/`, `worker/`, and `web/`.**
Not on anyone's critical path. Can be slower, can be down briefly, can do heavy queries and batch ML.

They share `db/`, `core/`, `ledger/`, `vault/`, and `metrics/`, and they run as **separate
processes** from the **same codebase**. A slow dashboard query must never be able to exhaust the
proxy's connection pool.

Both are built by `create_app()` in `app.py`, which supplies the request-id middleware, the RFC 7807
error handlers, and the `/healthz` + `/readyz` pair. That factory holds only what is genuinely common
to both planes — plane-specific behavior belongs in `proxy/` or `api/`, never behind a flag in the
factory.

---

## 3. The request lifecycle (read `proxy/pipeline.py` alongside this)

```
user's app
   │  POST /v1/chat/completions  (base_url swapped; Authorization: Bearer apc_live_...)
   ▼
[1] proxy/auth.py ────────── proxy key → user + project + settings   (Redis, 60s TTL)
   ▼
[2] budgets/enforcement.py ─ Redis spend counter; hard_stop → 402 and we're done
   ▼
[3] cache/semantic.py ────── embed prompt → pgvector cosine search
   │                          HIT → replay cached response ──────────────► RETURN (no provider call)
   ▼ miss
[4] routing/engine.py ────── user rules first, then classifier → model + reason code
   ▼
[5] vault/provider_keys.py ─ decrypt user's real provider key, in memory only
   ▼
[6] proxy/providers/*.py ─── forward to the real provider, stream the response back
   ▼
[7] routing/escalation.py ── response looks low-confidence? retry once on a stronger model
   ▼
    RETURN to the user's app (schema untouched; our metadata rides in headers)
   ▼
[8] async, off the critical path:
      ledger/writer.py  → Redis stream → worker → requests_log
      cache/semantic.py → store the new prompt/response pair if cacheable
      stats/rolling.py  → Welford update for this user/project
   ▼
[9] periodic workers:
      anomaly/  → spike + abuse alerts        advisor/ → nightly recommendations
      notify/   → alert emails, weekly digest ledger/pricing.py → refresh price tables
```

Steps 3, 4, 7, and 8 are all wrapped in `core/deadline.py`'s `failopen()`. Any of them can fail and
the request still succeeds.

---

## 4. Where things live

| I want to... | Go to |
|---|---|
| Understand the whole request flow | `proxy/pipeline.py` — start here, always |
| Add a new LLM provider | `proxy/providers/base.py`, then a new adapter next to it |
| Change how streaming works | `proxy/streaming.py` |
| Change what counts as a cache hit | `cache/semantic.py` (search) and `cache/policy.py` (eligibility) |
| Change which model gets picked | `routing/engine.py`, `routing/rules.py`, `routing/classifier.py` |
| Change how costs are computed | `ledger/cost.py` and `ledger/pricing.py` |
| Add a dashboard endpoint | `api/routers/` + a migration if it needs new data |
| Change a spend chart | `web/src/routes/` + `web/src/lib/api.ts` |
| Add a scheduled job | `worker/tasks.py` + `worker/schedules.py` |
| Touch encryption | `vault/kms.py`, `vault/provider_keys.py` — read §7 first |
| Change the DB schema | `db/models.py` + `alembic revision --autogenerate` |
| Add config | `config.py` only — never read `os.environ` elsewhere |
| Change health checks, request-id binding, or error handlers | `app.py` — the factory both entrypoints call ([ADR 0003](adr/0003-shared-asgi-app-factory.md)) |
| Touch Postgres connectivity or RLS scoping | `db/session.py` |
| Touch Redis connectivity | `db/redis.py` ([ADR 0002](adr/0002-shared-redis-client-module.md)) |
| Add or upgrade a Python dependency | `backend/pyproject.toml`, then `uv lock` ([ADR 0001](adr/0001-uv-as-python-toolchain.md)) |
| Change hashing, JWTs, or proxy-key generation | `core/security.py` |
| Change how provider keys are encrypted | `vault/kms.py`, `vault/provider_keys.py` |
| Add a control-plane endpoint | `api/routers/`, wired up in `main_api.py` |
| Change database roles or grants | `docker/postgres/init/01-app-role.sql` — read §7.3 first |
| Change where the browser keeps tokens | `web/src/lib/auth.tsx` ([ADR 0004](adr/0004-spa-token-storage.md)) |

---

## 5. Domain glossary

| Term | Meaning |
|---|---|
| **Provider key** | The user's *own* OpenAI/Anthropic/Gemini key. We store it encrypted and use it on their behalf. Compromise here is catastrophic. |
| **Proxy key** (`apc_live_...`) | The credential the user's app sends to *us*. Stored as a hash only, shown exactly once at creation, revocable instantly. |
| **Project** | An isolation boundary: `prod` vs `staging` vs `side-project`. Owns its own toggles, thresholds, budgets, rules, cache namespace. |
| **Passthrough** | We forwarded the request unchanged to the model the user asked for. |
| **Routed** | We sent it to a different (cheaper) model than requested. |
| **Escalation** | A cheap-tier answer looked bad, so we retried on a stronger model and returned that instead. Costs *both* calls — savings math must account for this. |
| **Fail-open** | On internal failure, forward the original request unmodified rather than erroring. The single most important behavior in the system. |
| **Reason code** | Machine-readable explanation of a routing/caching decision, shown to the user (`RULE_OVERRIDE`, `CLASSIFIER_CHEAP_TIER`, `ESCALATED_LOW_CONFIDENCE`, `FAILOPEN_TIMEOUT`, ...). |
| **`cost_would_have_been_usd`** | What the request *would* have cost at the requested model's price. Every savings number in the product is derived from this column. |
| **Ledger** | `requests_log`. Append-only system of record. Dashboard, advisor, and alerting all read from it and nowhere else. |

---

## 6. The savings math (get this wrong and the product is a lie)

There are three savings mechanisms and they must never double-count:

- **Caching savings** = Σ `cost_would_have_been_usd` over rows where `cache_hit = true`.
  The provider call did not happen, so actual cost is 0.
- **Routing savings** = Σ (`cost_would_have_been_usd` − `cost_usd`) over rows where `routed = true`
  and `cache_hit = false`, **minus** the full cost of every escalation retry.
- **Prompt optimization savings** are advisory-only in v1 and are reported as *projected*, never
  mixed into realized savings.

A cache hit is never also a routing win. Escalations reduce reported routing savings, sometimes below
zero for a given endpoint — report that honestly; it is the signal that tells the user to exclude
that endpoint from routing.

---

## 7. Security invariants

Break any of these and you have a serious incident, not a bug.

1. Provider keys exist in plaintext only in process memory, only during a forward, and are zeroed
   after. Never in a log, a response, an exception message, a stack trace, or a DB column.
2. Proxy keys are stored as SHA-256 hashes. Revocation must invalidate the Redis auth cache in the
   same operation as the DB write, or a revoked key keeps working for up to 60 seconds.
3. Every user-scoped query is bounded by `user_id` **and** protected by Postgres row-level security.
   The application-layer filter is the first line, RLS is the second. Both are required.

   Three things make RLS actually work here, and each has silently defeated it once already:

   - **The app connects as `apicost_app`, never as the schema owner.** A Postgres superuser bypasses
     RLS unconditionally — `FORCE ROW LEVEL SECURITY` does not apply to them — so connecting as the
     owner leaves every policy in place and inert. Migrations use `database_admin_url`; the
     application uses `database_url`. See `docker/postgres/init/01-app-role.sql`.
   - **Policies use `NULLIF(current_setting('app.user_id', true), '')`.** After a transaction-local
     `set_config` commits, the setting reverts to the *empty string*, not to unset. A bare `IS NULL`
     check therefore fails on any pooled connection that has already served one scoped request.
   - **`FORCE ROW LEVEL SECURITY`, not just `ENABLE`.** The tables are owned by the role running the
     migrations, and a plain `ENABLE` exempts the owner.
4. Raw prompt/response text is not persisted unless the project has `store_raw_content = true`. The
   cache is the exception, and cached bodies are encrypted with the per-user data key.
5. TLS everywhere, no plaintext fallback, including on the proxy path.
6. Never return another user's data through any code path — there is a test for this; keep it green.

---

## 8. Reliability invariants

1. **Fail-open everywhere except `hard_stop` budgets.** Cache down, router down, embedder slow,
   ledger backed up — the user's completion still returns. The only deliberate fail-closed path is a
   `hard_stop` budget whose state can't be read; that fails closed and logs loudly.
2. **150 ms total optimization budget**, enforced by a shared `Deadline` object threaded through the
   pipeline, not by per-step timeouts that can sum to more than the budget.
3. **No local state in the proxy.** Everything shared lives in Redis or Postgres so any instance can
   serve any request and the tier scales horizontally.
4. **Never block on logging.** Ledger writes go to a Redis stream and are drained by a worker. If the
   stream is unavailable, drop the event and increment a counter — never fail the request.
5. **Every request has a `request_id`**, bound to the logging context, written to the ledger, and
   returned in the `X-APICost-Request-Id` header. Without it, nobody can debug anything.

---

## 9. The pure-function core

These modules have no I/O, no ORM imports, and no framework dependencies. They are the parts most
worth testing exhaustively and the parts most likely to be wrong in subtle ways:

- `metrics/inference.py` — TTFT, inter-token latency, tokens/sec from streamed chunk timestamps.
- `metrics/latency.py` — stage-by-stage latency decomposition and bottleneck identification. Powers
  the NFR harness that proves the <100 ms / <30 ms targets.
- `metrics/throughput.py` — step and cumulative throughput series.
- `stats/welford.py` — online mean/variance. Note it supports no deletion; windowed stats are done by
  keeping per-bucket states and merging, never by subtracting.
- `advisor/breakeven.py` — self-hosting vs pay-per-token. Remember GPU cost is a **step function**;
  the break-even volume is piecewise, and the whole calculation is sensitive to the assumed
  utilization factor.
- `ledger/cost.py` — tokens → USD against a time-versioned price table.

If you're new to the codebase, read these first. They're small, self-contained, and they encode most
of the product's actual arithmetic.

---

## 10. Running it locally

Prerequisites: [uv](https://docs.astral.sh/uv/), Docker with Compose **v2**, Node 20+, and `make`.
uv provisions the pinned Python 3.12 itself, so no system Python is required — see
[ADR 0001](adr/0001-uv-as-python-toolchain.md). Every Python command in this repo runs through
`uv run --project backend`; a bare `python3` picks up whatever is first on PATH and is always a bug.

```bash
cp .env.example .env          # set a local KMS master key; provider keys are your own
make install                  # uv sync (backend) + npm install (web)
make dev                      # postgres + redis + mailpit + proxy + api + worker + web
make migrate                  # apply Alembic migrations
make seed                     # demo user, project, and synthetic ledger history
make test                     # backend + frontend suites
make lint                     # ruff, mypy, eslint
```

`make test` runs without the stack up: tests needing live Postgres or Redis are marked
`integration` and skip themselves when those services are unreachable.

Ports: proxy `:8000`, dashboard API `:8001`, web `:5173`, mailpit UI `:8025`.

To exercise the proxy the way a real user would:

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8000/v1", api_key="apc_live_...")
client.chat.completions.create(model="gpt-4o", messages=[{"role":"user","content":"hi"}])
```

Then check the request log in the dashboard and confirm the decision, cost, and reason code are all
present.

---

## 11. Debugging playbook

| Symptom | Look at |
|---|---|
| Request 401s | `proxy/auth.py`; check `proxy_keys.revoked_at` and the Redis auth cache key |
| Request 402s | `budgets/enforcement.py`; check the Redis period counter and `budgets.action_on_exceed` |
| Everything is passthrough, nothing routes | Look for `FAILOPEN_TIMEOUT` reason codes — the classifier is probably blowing its budget |
| Cache never hits | Threshold too high, TTL expired, prompt normalization changed, or `cache/policy.py` marked it non-cacheable (check `temperature`) |
| Cache hits when it shouldn't | Threshold too low; check `cache_similarity` on the offending row in `requests_log` |
| Costs look wrong | `ledger/pricing.py` staleness, or `tokens_estimated = true` rows where the provider didn't return usage |
| Savings look impossible | §6 — you are almost certainly double-counting a cached row as a routing win |
| Latency regression | Run the harness and read `decompose_latency`'s `bottleneck` and `stage_pct` |
| Alerts spamming | Cooldown in `anomaly/`; also check the minimum-sample guard on the z-score |
| Streaming broken in a client SDK | `proxy/streaming.py` — you probably altered the response body schema instead of using headers |

---

## 12. Things that look like bugs but aren't

- **Escalation makes some endpoints show negative routing savings.** Correct and intentional. It is
  the signal that the endpoint should be excluded from routing.
- **`tokens_estimated = true` rows exist.** Some providers omit usage on streamed responses; we
  estimate and mark it rather than silently reporting a confident wrong number.
- **The proxy returns a provider error verbatim.** By design — the user's error handling should work
  exactly as it did before they installed us.
- **Historical rows use old prices.** Price tables are versioned by `effective_from`; recomputing
  history at today's prices would misstate what the user actually paid.
- **Peer benchmark is missing for a user.** Cohorts with fewer than 50 users are suppressed for
  privacy.

---

## 13. Known limitations of the v1 design

Worth knowing before you promise anything to a user:

- The routing classifier is trained on a small seed dataset. Its confidence calibration is weak until
  there's real escalation-outcome data to retrain on. Be conservative with default thresholds.
- Semantic caching is genuinely risky for anything time-sensitive or stateful. The default threshold
  of 0.95 is deliberately conservative; the non-cacheable heuristics in `cache/policy.py` are the
  main safety mechanism and deserve scrutiny.
- `/v1/embeddings` is logged passthrough only — not routed, not cached.
- The break-even advisor's utilization assumption dominates its output. Show the assumption in the UI.
- Peer benchmarking has an inherent privacy surface; keep the cohort floor and never expose anything
  below aggregate level.
- Prompt compression is advisory only. Silently rewriting a user's prompt would break the product's
  core promise that we don't change their application's behavior.
