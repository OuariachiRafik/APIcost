# APICost — Build Specification for Claude Code

> **What this document is.** A single, self-contained, executable build plan for the APICost
> individual/Persona-A edition. It merges the System Design, Use Case Catalog, and Feature Backlog
> into one artifact, locks down every open technology choice, and sequences the work into phases with
> explicit acceptance criteria. Every requirement traces back to a use-case ID (`UC-##`).
>
> **How to use it.** Commit this file to the repo at `docs/BUILD_SPEC.md`. Work **one phase at a time**.
> Do not attempt to implement multiple phases in a single session. At the end of each phase, all
> acceptance criteria for that phase must pass before moving on.

---

## 0. Non-negotiable rules

These override anything else in this document if they conflict.

1. **Fail open, always.** If the cache, router, stats writer, or logger raises or exceeds its time
   budget (150 ms total for all optimization work), the proxy forwards the original, unmodified
   request to the originally requested model. A broken optimization feature must never break the
   user's application. Every optimization call site is wrapped in a timeout + `except Exception`
   that logs and continues.
2. **Never log a secret.** Provider API keys and proxy keys never appear in logs, error messages,
   stack traces, exception `__str__`, or HTTP responses. Add a log filter that redacts anything
   matching known key prefixes (`sk-`, `sk-ant-`, `apc_`) as a second line of defense.
3. **Tenant isolation at the database layer.** Every table with a `user_id` gets Postgres row-level
   security. Application-layer scoping is necessary but not sufficient.
4. **Privacy-conscious by default.** Raw prompt/response text is **not** stored unless the user
   explicitly opts in per project (`projects.store_raw_content`, default `false`). Default storage is
   hashes + embeddings only. The semantic cache is the one exception: it must store the response
   payload to be able to replay it, so cached response bodies are encrypted at rest with the
   per-user data key.
5. **Schema fidelity.** Responses returned to the caller must be byte-compatible with the provider's
   schema so existing OpenAI/Anthropic SDKs parse them without modification. Add APICost metadata in
   response *headers* (`X-APICost-Request-Id`, `X-APICost-Cache`, `X-APICost-Model-Used`,
   `X-APICost-Reason`), never in the JSON body.
6. **Streaming is not optional.** Most LLM clients stream by default. SSE passthrough must work from
   Phase 2 onward, including for cache hits (a cached response is re-chunked and replayed as SSE when
   the client asked for `stream: true`).

---

## 1. Product summary

APICost sits between a solo developer's application and their LLM provider. The user swaps one config
value — their API base URL — and APICost transparently applies semantic caching, model routing, spend
logging, budgets, and anomaly alerting on every request, then shows them what it saved.

Two halves, built together:

- **Proxy (data plane)** — OpenAI-compatible HTTP service on the critical path of the user's app.
- **Dashboard (control plane)** — React web app + REST API where the user configures and observes.

Target outcome: 30–60% spend reduction with no material quality degradation.

---

## 2. Locked technology decisions

The source design doc left several choices open ("or"). They are now decided. Do not re-litigate.

| Layer | Decision | Notes |
|---|---|---|
| Language (backend) | Python 3.12 | |
| Web framework | FastAPI + Pydantic v2 | Proxy and Dashboard API are **two ASGI apps in one codebase**, deployed as separate processes |
| ASGI server | uvicorn (workers via gunicorn in prod) | |
| ORM / migrations | SQLAlchemy 2.0 (async) + Alembic | |
| Primary datastore | PostgreSQL 16 + `pgvector` | |
| Vector search | `pgvector` HNSW index, cosine distance | Do **not** introduce FAISS/hnswlib in v1 |
| Hot cache / counters | Redis 7 | proxy-key auth lookups, rolling-stats checkpoints, rate limits |
| Background jobs | ARQ (async, Redis-backed) | Chosen over Celery for async-native fit with FastAPI |
| HTTP client | `httpx.AsyncClient` with connection pooling | one shared client per process, `http2=True` |
| Password hashing | Argon2id (`argon2-cffi`) | |
| Session auth | JWT access (15 min) + rotating refresh token (30 d, stored hashed) | |
| Envelope encryption | `cryptography` AES-256-GCM data keys, wrapped by KMS | KMS behind an interface — see §6.9 |
| Embeddings | `BAAI/bge-small-en-v1.5` via `fastembed`, run in-process | 384-dim, CPU, ~5 ms. No network hop on the hot path |
| Routing classifier | scikit-learn logistic regression, in-process, joblib artifact | |
| Anomaly (slow path) | scikit-learn `IsolationForest` | |
| Frontend | React 18 + TypeScript + Vite | |
| Styling | Tailwind CSS | |
| Data fetching | TanStack Query | |
| Charts | Recharts | |
| Email | Resend (interface-abstracted; Postmark/SendGrid swappable) | |
| Billing | Stripe Billing | |
| Local dev | Docker Compose (postgres, redis, mailpit) | |
| Tests | pytest + pytest-asyncio + httpx ASGITransport; Vitest + Testing Library for web | |
| Lint/format | ruff (lint + format), mypy strict on `apicost.core`/`apicost.metrics`; eslint + prettier | |

---

## 3. Repository layout

Create exactly this structure. Deviating makes the codebase guide wrong.

```
apicost/
├── CLAUDE.md                       # persistent instructions for Claude Code
├── README.md
├── Makefile                        # make dev / test / migrate / seed / lint
├── docker-compose.yml
├── .env.example
├── docs/
│   ├── BUILD_SPEC.md               # this file
│   ├── CODEBASE_GUIDE.md           # orientation doc — keep updated as you build
│   ├── use-cases.md                # UC-01..UC-39 catalog, verbatim
│   └── adr/                        # one short ADR per non-obvious decision
├── backend/
│   ├── pyproject.toml
│   ├── alembic.ini
│   ├── alembic/versions/
│   ├── src/apicost/
│   │   ├── config.py               # pydantic-settings, single Settings object
│   │   ├── main_proxy.py           # ASGI app: data plane
│   │   ├── main_api.py             # ASGI app: control plane
│   │   ├── core/
│   │   │   ├── security.py         # hashing, JWT, proxy-key generation/verification
│   │   │   ├── errors.py           # typed exceptions + handlers
│   │   │   ├── ids.py              # request_id / ULID generation
│   │   │   ├── logging.py          # structlog config + secret redaction filter
│   │   │   └── deadline.py         # time-budget helper used by the whole pipeline
│   │   ├── db/
│   │   │   ├── base.py
│   │   │   ├── session.py          # async engine, session factory, RLS session var
│   │   │   └── models.py           # all SQLAlchemy models (§7)
│   │   ├── proxy/
│   │   │   ├── ingress.py          # routes: /v1/chat/completions, /v1/embeddings
│   │   │   ├── pipeline.py         # THE orchestrator — read this first (§6.1)
│   │   │   ├── streaming.py        # SSE parse/replay/tee
│   │   │   ├── auth.py             # proxy-key -> account resolution (Redis-cached)
│   │   │   └── providers/
│   │   │       ├── base.py         # Provider protocol
│   │   │       ├── openai.py
│   │   │       ├── anthropic.py
│   │   │       └── gemini.py
│   │   ├── routing/
│   │   │   ├── engine.py           # rules layer + classifier + escalation decision
│   │   │   ├── rules.py            # user override/exclude rule evaluation
│   │   │   ├── features.py         # prompt -> feature vector
│   │   │   ├── classifier.py       # load/serve joblib artifact
│   │   │   ├── escalation.py       # low-confidence response detection
│   │   │   └── train.py            # offline training script + seed dataset
│   │   ├── cache/
│   │   │   ├── semantic.py         # lookup / store / invalidate
│   │   │   ├── embeddings.py       # fastembed wrapper, warm at startup
│   │   │   └── policy.py           # cacheability rules, TTL, no-cache markers
│   │   ├── ledger/
│   │   │   ├── writer.py           # off-critical-path write (Redis stream -> worker)
│   │   │   ├── pricing.py          # provider price tables + refresh job
│   │   │   └── cost.py             # token counts -> USD
│   │   ├── stats/
│   │   │   ├── welford.py          # online mean/variance (§6.5)
│   │   │   └── rolling.py          # windowed state, Redis checkpointing
│   │   ├── anomaly/
│   │   │   ├── zscore.py           # fast path
│   │   │   └── forest.py           # slow path
│   │   ├── budgets/
│   │   │   └── enforcement.py      # alert_only / soft_throttle / hard_stop
│   │   ├── advisor/
│   │   │   ├── breakeven.py        # self-hosting vs API (§6.7)
│   │   │   ├── downgrade.py        # cheaper-tier recommendations
│   │   │   └── prompts.py          # long-context warning, compression suggestion
│   │   ├── metrics/
│   │   │   ├── inference.py        # TTFT / ITL / TPS (§6.6)
│   │   │   ├── latency.py          # stage decomposition (§6.6)
│   │   │   └── throughput.py       # step/cumulative TPS (§6.6)
│   │   ├── vault/
│   │   │   ├── kms.py              # KMSClient protocol + Local/AWS/GCP impls
│   │   │   └── provider_keys.py    # add / decrypt-in-memory / rotate / revoke
│   │   ├── notify/
│   │   │   ├── email.py            # EmailSender protocol + Resend impl
│   │   │   ├── templates/
│   │   │   └── digest.py           # weekly savings digest composition
│   │   ├── billing/stripe.py
│   │   ├── api/
│   │   │   ├── deps.py             # auth deps, current_user, project scoping
│   │   │   └── routers/            # auth, keys, projects, usage, cache, routing,
│   │   │                           #   budgets, alerts, advisor, billing
│   │   └── worker/
│   │       ├── tasks.py            # ledger drain, anomaly, advisor, digest, pricing
│   │       └── schedules.py        # ARQ cron definitions
│   └── tests/
│       ├── conftest.py
│       ├── unit/
│       ├── integration/
│       └── e2e/
└── web/
    ├── package.json
    ├── vite.config.ts
    └── src/
        ├── main.tsx
        ├── lib/api.ts              # typed client generated from OpenAPI
        ├── routes/                 # onboarding, dashboard, requests, cache,
        │                           #   routing, budgets, alerts, advisor, settings
        └── components/
```

---

## 4. Build phases

Each phase is a working, demoable increment. **Stop at the end of each phase and run its acceptance
criteria.** Phases 0–4 together constitute the MVP identified in the feature backlog: *connect, see
spend, see caching working.*

| Phase | Name | Use cases | MVP? |
|---|---|---|---|
| P0 | Scaffolding & infrastructure | — | ✅ |
| P1 | Auth, projects, keys, vault | UC-01→05, 07 | ✅ |
| P2 | Proxy passthrough + ledger | UC-06, UC-12 | ✅ |
| P3 | Dashboard: visibility & reporting | UC-08→13 | ✅ (partial) |
| P4 | Semantic caching | UC-20→25 | ✅ (partial) |
| P5 | Intelligent routing | UC-14→19 | |
| P6 | Stats, anomaly, budgets, alerts | UC-29→34 | |
| P7 | Prompt & context optimization | UC-26→28 | |
| P8 | Decision support & advisory | UC-35→37 | |
| P9 | Engagement & retention | UC-38, 39 | |
| P10 | Our own billing | — | |

### P0 — Scaffolding & infrastructure

- Monorepo per §3. `pyproject.toml` with dependency groups (`dev`, `ml`).
- `docker-compose.yml`: `postgres:16` with pgvector, `redis:7`, `mailpit` (local SMTP UI).
- `Settings` in `config.py` via `pydantic-settings`; **no `os.environ` reads anywhere else**.
- Alembic wired up; first migration creates the `vector` extension.
- `structlog` JSON logging with `request_id` bound in a context var, plus the secret redaction filter.
- Health endpoints: `GET /healthz` (liveness), `GET /readyz` (checks Postgres + Redis).
- `Makefile` targets: `dev`, `test`, `lint`, `migrate`, `revision`, `seed`.
- CI: ruff, mypy, pytest, vitest.

**Acceptance:** `make dev` brings up both apps and dependencies; `curl localhost:8000/readyz` and
`localhost:8001/readyz` both return 200; `make test` passes with zero tests failing.

### P1 — Auth, projects, keys, vault → UC-01..05, UC-07

- `POST /auth/signup|login|logout|refresh-token`. Argon2id. Refresh tokens stored hashed and rotated
  on use; reuse of a consumed refresh token revokes the whole family.
- `POST /keys` accepts a raw provider key over TLS, **encrypts before the request handler returns**,
  and never persists plaintext. `GET /keys` returns `{provider, last4, added_at, last_used_at,
  is_active}` and nothing else. `DELETE /keys/{id}`.
- Envelope encryption: per-user 256-bit data key, AES-256-GCM, wrapped by KMS master key. In local
  dev the `LocalKMS` impl reads a master key from env — the interface must be identical to the AWS
  impl so swapping is a one-line config change.
- `POST /projects`, `GET /projects`. Project holds all feature toggles (cache on/off, routing on/off,
  thresholds, TTL, `store_raw_content`).
- `POST /projects/{id}/proxy-keys` issues `apc_live_<32 bytes base62>`; **returns the raw key exactly
  once**, stores only a SHA-256 hash. `DELETE /proxy-keys/{id}` revokes instantly (sets `revoked_at`
  **and** purges the Redis auth cache entry).
- Web: signup/login, onboarding wizard (add provider key → create project → issue proxy key →
  copy-pasteable integration snippet showing the base-URL swap for OpenAI Python/Node SDKs and cURL).

**Acceptance:** a new user can go signup → key → project → proxy key → see integration instructions
without leaving the wizard. Revoking a proxy key rejects the next request within 1 second and does
not affect other projects (UC-07). No test can retrieve a stored provider key in plaintext through
any API path.

### P2 — Proxy passthrough + ledger → UC-06, UC-12

- `POST /v1/chat/completions` — authenticate the proxy key, resolve user + project (Redis-cached,
  60 s TTL, invalidated on revoke), decrypt provider key in memory, forward to the provider,
  stream back.
- Streaming: true SSE passthrough. Tee the stream so chunk timestamps and token counts are captured
  without adding buffering latency to the client.
- `POST /v1/embeddings` — logged passthrough only; not routed or cached in v1.
- Ledger write is **off the critical path**: push a compact event to a Redis Stream, an ARQ worker
  drains it into `requests_log` in batches. Dropping a ledger event must never fail a request.
- `pricing.py` holds per-model input/output prices as a versioned table with an `effective_from`
  date, so historical rows keep the price that was current when they were made.
- Connection health check (UC-06): `POST /projects/{id}/test-connection` sends a minimal completion
  through the full proxy path and returns a structured success/failure with the specific failure
  reason (bad provider key, provider unreachable, revoked proxy key, etc.).
- Record per-request metrics using `metrics/inference.py` on streamed responses: TTFT, inter-token
  latency, tokens/sec.

**Acceptance:** an unmodified `openai` Python SDK client pointed at the proxy works for both
streaming and non-streaming calls. Killing Redis mid-request degrades logging but the completion
still returns (fail-open). Every request appears in `requests_log` with correct token counts and
computed cost within 5 seconds.

### P3 — Visibility & reporting → UC-08..13

- `GET /usage?range=&project_id=` time series for the spend chart (UC-08).
- `GET /usage/breakdown?by=model|project|endpoint` (UC-09, UC-10).
- `GET /usage/token-distribution` — histogram buckets of request token counts (UC-11).
- `GET /requests?cursor=&filter=` — per-request decision log showing, for each call:
  `cache_hit | routed | passthrough`, model requested vs used, tokens, cost, latency, reason code
  (UC-12). This is the single most important trust-building screen in the product; make it fast and
  filterable.
- `GET /usage/export.csv` — streaming CSV export (UC-13).
- Web: dashboard shell, spend overview with trend, breakdown charts, token histogram, request log
  table with detail drawer, export button.

**Acceptance:** all six use cases visible in the UI with real data produced by P2. Usage endpoints
respond in <500 ms p95 against 1M seeded ledger rows.

### P4 — Semantic caching → UC-20..25

- On each request: embed the normalized prompt, search `cache_entries` for the same user/project with
  cosine similarity above the project's threshold and a non-expired TTL. Hit → return the stored
  response (re-chunked as SSE if the client asked to stream), increment `hit_count`, log
  `cache_hit=true`, and **skip the provider call entirely**.
- Normalization before embedding: strip system-prompt boilerplate that the user marks as ignorable,
  collapse whitespace, drop non-deterministic fields (`user`, `metadata`, timestamps).
- Toggles: global + per project (UC-20). Threshold slider, default `0.95`, range 0.80–0.99 (UC-21).
  TTL config, default 24 h (UC-22). Manual per-project invalidation (UC-23). Non-cacheable marking by
  endpoint pattern, by header (`X-APICost-No-Cache: true`), or by detected non-determinism
  (`temperature > 0.7`, tool calls, or the prompt containing a live timestamp) (UC-24).
- `GET /cache/stats` — hit rate, dollars saved (sum of what the skipped calls would have cost at the
  requested model's price), hits over time (UC-25).
- Embedding model warmed at startup; embedding must complete inside a 40 ms budget or the request
  proceeds as a miss.

**Acceptance:** two semantically-equivalent-but-textually-different prompts produce a hit at the
default threshold; raising the threshold to 0.99 makes it a miss. Cache hits return in <30 ms p95
(NFR). Dollars-saved on the cache report reconciles exactly with the sum of avoided costs in
`requests_log`. **MVP complete at the end of this phase.**

### P5 — Intelligent routing → UC-14..19

- `routing/features.py`: prompt length, message count, presence of code fences, structured content
  (JSON/XML), task-type keyword flags, requested model tier, embedding-based similarity to labeled
  exemplars.
- `routing/classifier.py`: logistic regression predicting required-capability tier
  (`cheap | mid | strong`) with a calibrated probability. Must return in single-digit ms. Artifact is
  versioned (`model_version` recorded on every routed request) and swappable without redeploy.
- `routing/rules.py`: deterministic user rules evaluated **before** the classifier — `override`
  (always use model X) and `exclude` (never route this endpoint/project) (UC-15, UC-19).
- Escalation (UC-17): after a cheap-tier response, detect low confidence — very short output,
  refusal/uncertainty phrasing, truncated JSON when JSON was requested, or an endpoint flagged
  quality-critical — and retry once on the stronger tier, returning that response. Both attempts are
  logged; the cost of both is counted honestly in savings math.
- Reason codes surfaced per request (UC-16): e.g. `RULE_OVERRIDE`, `CLASSIFIER_CHEAP_TIER`,
  `EXCLUDED_ENDPOINT`, `ESCALATED_LOW_CONFIDENCE`, `FAILOPEN_TIMEOUT`.
- `GET /routing/stats` — tier distribution and savings attributable to routing **only**, computed as
  (price of requested model − price of used model) × tokens, minus escalation retry cost (UC-18).
- Training: `routing/train.py` with a checked-in seed dataset of a few hundred labeled prompts;
  document how to retrain from the user's own escalation outcomes later.

**Acceptance:** routing savings and caching savings are reported separately and never double-count.
A routing-engine exception or a >20 ms classifier stall results in passthrough to the requested
model, logged with `FAILOPEN_TIMEOUT`, not an error.

### P6 — Stats, anomaly, budgets, alerts → UC-29..34

- `stats/welford.py` — Welford's online mean/variance (see §6.5). O(1) update per ledger record, no
  full-history recomputation. State checkpointed to Redis and persisted to `rolling_stats` so it
  survives restarts.
- `anomaly/zscore.py` — fast path: flag when the current window's spend rate exceeds the rolling mean
  by a configurable z (default 3.0) with a minimum-sample guard (≥30 observations) to avoid firing on
  cold-start noise (UC-31).
- `anomaly/forest.py` — slow path: IsolationForest over a feature window (request rate, cost rate,
  model mix, endpoint entropy, unique-prompt ratio) run every 5 minutes to catch patterns z-score
  misses, e.g. a leaked key showing an unfamiliar model mix at normal volume (UC-32).
- Budgets: daily/weekly/monthly per project (UC-29) with `alert_only | soft_throttle | hard_stop`
  (UC-30). Enforcement reads a Redis counter on the hot path — an in-memory/Redis check only, never a
  Postgres query. `hard_stop` returns HTTP 402 with a clear, actionable error body.
- Emergency kill switch (UC-33): one action revokes all proxy keys for a project and purges auth
  cache; must take effect in <1 s.
- `alert_events` history with resolution status (UC-34).
- Email alerts via the notification channel.

**Acceptance:** a simulated runaway loop (500 requests in 60 s against a baseline of 5/min) fires a
spike alert within 2 minutes. A `hard_stop` budget stops billing-relevant traffic within one request
of the threshold being crossed. **Budget enforcement is the one place where fail-open does not
apply** — if the budget state is unreadable, fail *closed* only for `hard_stop` projects and log it
loudly; otherwise pass through.

### P7 — Prompt & context optimization → UC-26..28

- Long-context warning (UC-26): flag requests whose conversation history exceeds a configurable token
  threshold and whose earliest messages have low relevance overlap with the latest user message.
- Compression suggestion (UC-27): generate a compressed prompt candidate with a before/after token
  count. **Advisory only** — never silently rewrite the user's prompt in v1.
- Token-heavy endpoint report (UC-28): rank endpoints by average token count.

### P8 — Decision support & advisory → UC-35..37

- Nightly ARQ job over each user's usage history.
- Downgrade recommendations (UC-35): endpoints where the cheap tier historically succeeded without
  escalation, with a confidence level and observed sample size.
- Break-even advisor (UC-36): see §6.7 — `GET /advisor/breakeven`.
- Every recommendation carries a projected dollar impact before adoption (UC-37), stored in
  `advisor_recommendations.projected_savings_usd`.

### P9 — Engagement & retention → UC-38, 39

- Weekly digest email (UC-38): spend, savings by mechanism, notable events, top recommendation.
  Scheduled per user timezone. Unsubscribe link required.
- Anonymized peer benchmark (UC-39): compare the user's cost-per-request against a cohort aggregate.
  **Only publish a cohort statistic when the cohort has ≥50 users**, and only ever expose aggregates
  — never anything traceable to another account.

### P10 — Our own billing

- Stripe Billing: free tier up to a request-volume cap, paid tiers above. `GET /billing/plan`,
  `POST /billing/checkout-session`, `POST /billing/webhook` (signature-verified, idempotent).
- Plan-limit signals consumed by the proxy for throttling/upgrade prompts.

---

## 5. Non-functional requirements (test these, don't assume them)

| Requirement | Target | How to verify |
|---|---|---|
| Proxy overhead, cache miss | <100 ms p95, excluding provider time | Load test with a stub provider; assert on `decompose_latency` output |
| Cache hit response | <30 ms p95 | Same harness, cache pre-warmed |
| Optimization time budget | 150 ms hard ceiling, then fail open | Fault-injection test per subsystem |
| Proxy availability | 99.9% | Stateless + horizontally scaled; no local state anywhere |
| Data isolation | No cross-user access under any code path | RLS test: query as user A with user B's IDs, expect zero rows |
| Key security | Never at rest or in logs in plaintext | Grep-based test over log output in the e2e suite |
| Observability | Every request traceable end-to-end | `request_id` propagated to logs, ledger, and response header |

Build the latency harness in P2, not at the end. It is what makes the rest of the NFR work honest.

---

## 6. Component specifications

### 6.1 Proxy pipeline (`proxy/pipeline.py`)

The single most important file. Explicit ordering, with a shared deadline:

```
authenticate(proxy_key) -> user, project, config     # Redis-cached
check_plan_and_budget(user, project)                 # Redis counters; may hard-stop
deadline = Deadline(150ms)

with failopen("cache", deadline):
    hit = semantic_cache.lookup(prompt, project)
    if hit: return replay(hit)                       # <- flow ends here, no provider call

with failopen("routing", deadline):
    decision = routing_engine.decide(prompt, project) # rules first, then classifier
model = decision.model if decision else request.model # fail open to requested model

provider_key = vault.decrypt_in_memory(user, provider)
response = provider.forward(request, model, provider_key)   # streamed

if escalation_enabled and looks_low_confidence(response):
    response = provider.forward(request, stronger_tier, provider_key)

emit_async(ledger_event)      # Redis stream, fire-and-forget
emit_async(cache_write)       # only if cacheable
return response
```

`failopen(name, deadline)` is a context manager that enforces the remaining budget, swallows
exceptions, logs them with `subsystem=name`, and yields `None` on failure. **Every optimization step
goes through it.**

### 6.2 Provider adapters (`proxy/providers/`)

A `Provider` protocol with `forward()`, `parse_usage()`, `normalize_request()`, `to_sse()`. The
OpenAI shape is the canonical internal representation; the Anthropic and Gemini adapters translate to
and from it. Token counts come from the provider's usage block when present; fall back to
`tiktoken`-style estimation and mark the ledger row `tokens_estimated=true` so cost accuracy is never
silently overstated.

### 6.3 Semantic cache (`cache/semantic.py`)

pgvector query, scoped by `user_id` and `project_id`, HNSW index on `embedding_vector`, cosine
distance, filtered by `ttl_expires_at > now()`. Redis fronts an exact-hash lookup for identical
prompts so the common repeat case never touches the vector index at all. Cached response payloads are
encrypted with the per-user data key.

### 6.4 Routing engine (`routing/engine.py`)

Rules → classifier → escalation, in that order. Returns a `RoutingDecision(model, confidence,
reason_code, model_version)`. Never raises to the caller; internal failures return `None` and the
pipeline passes through.

### 6.5 Rolling stats — Welford (`stats/welford.py`)

Not supplied; implement it. Required interface:

```python
@dataclass
class WelfordState:
    count: int = 0
    mean: float = 0.0
    m2: float = 0.0            # sum of squares of differences from the current mean

    def update(self, x: float) -> None:
        self.count += 1
        delta = x - self.mean
        self.mean += delta / self.count
        self.m2 += delta * (x - self.mean)

    @property
    def variance(self) -> float:      # sample variance
        return self.m2 / (self.count - 1) if self.count > 1 else 0.0

    @property
    def stddev(self) -> float: ...
    def zscore(self, x: float) -> float:  # 0.0 when stddev == 0 — never divide by zero
        ...
```

Serialize the state to Redis as JSON and checkpoint to the `rolling_stats` table every 60 s. For a
*windowed* rolling statistic, keep one Welford state per fixed time bucket (hourly) and combine
buckets using the parallel/Chan merge formula rather than trying to remove observations from a single
state — Welford does not support deletion, and attempting it accumulates numerical error.

### 6.6 Metrics library (`metrics/`) — supplied code

Three functions were provided by the user and should be adopted essentially as written, with the
fixes below. Keep them **pure** (no I/O, no ORM imports) so they stay trivially testable.

**`metrics/inference.py` — `compute_inference_metrics(timestamps)`**
Computes TTFT (first token latency), ITL (mean inter-token latency), and TPS from the timestamps
captured by the SSE tee in `proxy/streaming.py`. Wire it into P2.

Fixes required before use:
- Raise `ValueError` on `len(timestamps) < 2` instead of an `IndexError` on `timestamps[1]`.
- Guard `tps` against `timestamps[-1] == timestamps[0]` (zero elapsed → return `float('inf')` or
  `0.0`; pick one, document it, test it).
- Assert timestamps are monotonically non-decreasing — an out-of-order stream currently produces
  silently negative ITL.
- Use `time.perf_counter()` at the capture site, never `time.time()`.

**`metrics/latency.py` — `decompose_latency(stage_latencies, percentiles)`**
Powers the internal latency harness that verifies the <100 ms / <30 ms NFRs, and an admin-only
dashboard view. Stages to instrument: `auth`, `budget_check`, `embed`, `cache_lookup`,
`routing`, `key_decrypt`, `provider`, `serialize`.

Fixes required:
- Validate that all stage arrays are the same length before `np.sum(..., axis=0)`; mismatched lengths
  currently broadcast or raise deep inside numpy with an unhelpful message.
- Handle the empty-input case explicitly.
- `e2e_percentiles` keys are raw percentile values while `stage_stats[...]["percentiles"]` keys are
  `"p50"`-style strings. Make both use the `"p50"` string form — the inconsistency will bite the
  frontend.
- Note that summing stage means assumes stages are sequential and non-overlapping. Since the cache
  lookup and the routing decision may run concurrently in a later optimization, record a measured
  end-to-end value alongside the sum and alert if they diverge by more than 10%.

**`metrics/throughput.py` — `compute_throughput(intervals)`**
Use for streaming throughput per request and for aggregate tokens/sec on the usage dashboard. The
supplied implementation is sound — keep the validation, keep returning both step and cumulative
series. Only change: return `overall_tps` as `0.0` rather than raising when `total_seconds` is 0,
which can't currently happen given the guard but should be defensive.

### 6.7 Break-even advisor (`advisor/breakeven.py`) — supplied code

`break_even_analysis(...)` implements UC-36: at the user's actual monthly token volume, is a
dedicated/self-hosted deployment cheaper than pay-per-token? Inputs come from the Usage Ledger
(`monthly_tokens`, blended `cost_per_token`) and a maintained GPU price table
(`gpu_cost_per_hour`, `max_tokens_per_second` per instance type). Surface it at
`GET /advisor/breakeven`.

Fixes required before shipping:
- **`break_even_tokens` is wrong above one GPU's capacity.** The supplied formula divides a single
  GPU's monthly cost by `cost_per_token`, ignoring `n_gpus`. Because GPU cost is a *step function*
  (you buy whole GPUs), the true break-even is piecewise: solve within each step and return the
  smallest volume at which `gpu_cost(n) <= api_cost`. Return the step-aware value, and also return
  `capacity_tokens_per_gpu` so the dashboard can explain the steps.
- `n_gpus` is 0 when `monthly_tokens` is 0 → `gpu_cost` of 0 → recommends "gpu" for a user with no
  traffic. Return `recommendation="insufficient_data"` below a minimum volume threshold.
- Add a `utilization` factor (default 0.5). Assuming a GPU sustains `max_tokens_per_second` for all
  730 hours a month is wildly optimistic and will produce recommendations the user regrets.
- Include the honest caveats in the response payload: self-hosting adds ops burden, cold-start and
  idle cost, model-quality differences, and no provider SLA. The dashboard must show these next to
  the number — a bare "self-hosting is cheaper" is a misleading recommendation.

### 6.8 Anomaly detection (`anomaly/`)

Fast path z-score → immediate alert. Slow path IsolationForest every 5 min → alert on unusual
patterns. Both write to `alert_events` and enqueue notification delivery. Include a per-user
cooldown (default 30 min per alert type) so a sustained incident sends one alert, not two hundred.

### 6.9 Key vault (`vault/`)

```python
class KMSClient(Protocol):
    async def generate_data_key(self) -> tuple[bytes, bytes]:  # (plaintext, wrapped)
    async def unwrap(self, wrapped: bytes) -> bytes: ...
```

`LocalKMS` for dev, `AwsKmsClient` for prod. The plaintext data key lives in memory for the duration
of one request and is explicitly zeroed after use. Provider keys are decrypted only in `pipeline.py`,
immediately before forwarding.

---

## 7. Data model

Implement the tables from the system design as SQLAlchemy models, with these additions and
clarifications:

- **All primary keys are ULIDs** stored as `text` — sortable by creation time and safe to expose.
- **`users`**: `id, email (citext, unique), password_hash, auth_provider_id, created_at, plan_id,
  timezone, is_active`.
- **`provider_keys`**: `id, user_id, provider, encrypted_key (bytea), wrapped_data_key (bytea),
  key_last4, is_active, added_at, last_used_at`. Never a plaintext column.
- **`projects`**: `id, user_id, name, created_at` **plus config**: `cache_enabled,
  similarity_threshold, cache_ttl_seconds, routing_enabled, escalation_enabled, store_raw_content,
  archived_at`.
- **`proxy_keys`**: `id, user_id, project_id, proxy_key_hash (unique), key_last4, created_at,
  revoked_at, last_used_at`.
- **`requests_log`** (append-only, partitioned monthly by `timestamp`): `id, user_id, project_id,
  timestamp, request_id, endpoint, model_requested, model_used, provider, tokens_in, tokens_out,
  tokens_estimated, cost_usd, cost_would_have_been_usd, latency_ms, ttft_ms, itl_ms, tps,
  cache_hit, cache_similarity, routed, routing_reason_code, routing_model_version,
  escalation_triggered, status, error_code`.
  `cost_would_have_been_usd` is what makes savings reporting possible — populate it on every row.
- **`cache_entries`**: `id, user_id, project_id, embedding_vector vector(384), prompt_hash,
  response_payload (bytea, encrypted), model_used, created_at, ttl_expires_at, hit_count, last_hit_at`.
  HNSW index on `embedding_vector` with `vector_cosine_ops`; btree on `(user_id, project_id, prompt_hash)`.
- **`routing_rules`**: `id, user_id, project_id, rule_type (override|exclude), match_condition (jsonb),
  target_model, priority, is_active`.
- **`budgets`**: `id, user_id, project_id, period, limit_usd, action_on_exceed, current_period_start`.
- **`rolling_stats`**: `id, user_id, project_id, window, running_mean, running_variance, count,
  updated_at`.
- **`alerts_config`**, **`alert_events`**, **`advisor_recommendations`**, **`billing_subscriptions`**
  per the design doc.

Indexes to create up front (these are the queries the dashboard actually runs):
`requests_log (user_id, timestamp DESC)`, `requests_log (project_id, timestamp DESC)`,
`requests_log (user_id, model_used, timestamp)`, `cache_entries (user_id, project_id, ttl_expires_at)`.

Enable RLS on every user-scoped table; the app sets `SET LOCAL app.user_id` per transaction in
`db/session.py`.

---

## 8. API surface

Implement exactly the endpoints in the system design's §6, plus these additions:

- `POST /projects/{id}/test-connection` (UC-06)
- `GET /usage/token-distribution` (UC-11)
- `GET /requests` and `GET /requests/{request_id}` (UC-12)
- `GET /usage/export.csv` (UC-13)
- `POST /cache/invalidate` with `project_id` (UC-23)
- `POST /projects/{id}/kill` — emergency kill switch (UC-33)
- `PUT /projects/{id}/settings` — toggles and thresholds (UC-14, 20, 21, 22)
- `GET /advisor/prompt-optimizations` (UC-26, 27, 28)
- `GET /benchmark/peer` (UC-39)

Conventions: cursor pagination on all list endpoints; RFC 7807 problem+json error bodies; every
response carries `X-Request-Id`; the OpenAPI schema is the source of truth for the generated
TypeScript client.

---

## 9. Testing requirements

- **Unit**: pure functions in `metrics/`, `stats/welford.py`, `advisor/breakeven.py`,
  `ledger/cost.py`, `routing/features.py`, `cache/policy.py`. These should be fast and exhaustive,
  including the edge cases called out in §6.6 and §6.7.
- **Integration** (real Postgres + Redis from compose): cache hit/miss with a threshold sweep,
  routing rule precedence, budget enforcement, RLS isolation, ledger drain.
- **Fail-open suite**: one test per subsystem that injects a failure (raise, hang past deadline,
  dependency down) and asserts the completion still returns correctly. This suite is the product's
  reliability guarantee — do not let it rot.
- **E2E**: a stub provider server; drive the flow with the real `openai` SDK pointed at the proxy,
  streaming and non-streaming.
- **Load/latency**: the harness from §5, asserting p95 targets against the stub provider.
- **Security**: no-plaintext-key test, log-redaction test, revoked-key test, cross-tenant test.

Target: ≥85% coverage on `backend/src/apicost/`, with 100% on `metrics/`, `stats/`, and `advisor/`.

---

## 10. Traceability matrix

Every use case must map to at least one test. Maintain this table as you build.

| UC | Feature | Phase | Primary endpoint / surface |
|---|---|---|---|
| UC-01 | User Authentication | P1 | `POST /auth/signup`, `/auth/login` |
| UC-02 | Provider Key Management | P1 | `POST /keys` |
| UC-03 | Key rotation/removal | P1 | `DELETE /keys/{id}` |
| UC-04 | Project Workspace Management | P1 | `POST /projects` |
| UC-05 | Proxy Key Issuance & Guide | P1 | `POST /projects/{id}/proxy-keys` |
| UC-06 | Connection Health Check | P2 | `POST /projects/{id}/test-connection` |
| UC-07 | Proxy Key Revocation | P1 | `DELETE /proxy-keys/{id}` |
| UC-08 | Spend Overview Dashboard | P3 | `GET /usage` |
| UC-09 | Cost Breakdown by Model | P3 | `GET /usage/breakdown?by=model` |
| UC-10 | Cost Breakdown by Project | P3 | `GET /usage/breakdown?by=project` |
| UC-11 | Token Size Distribution | P3 | `GET /usage/token-distribution` |
| UC-12 | Per-Request Decision Log | P3 | `GET /requests` |
| UC-13 | Usage Data Export | P3 | `GET /usage/export.csv` |
| UC-14 | Routing Engine Toggle | P5 | `PUT /projects/{id}/settings` |
| UC-15 | Manual Routing Rules | P5 | `POST /routing-rules` |
| UC-16 | Routing Decision Transparency | P5 | `GET /requests/{id}` reason code |
| UC-17 | Confidence-Based Escalation | P5 | `routing/escalation.py` |
| UC-18 | Routing Savings Report | P5 | `GET /routing/stats` |
| UC-19 | Routing Exclusion Rules | P5 | `POST /routing-rules` (exclude) |
| UC-20 | Cache Toggle | P4 | `PUT /projects/{id}/settings` |
| UC-21 | Similarity Threshold Control | P4 | `PUT /projects/{id}/settings` |
| UC-22 | Cache TTL Configuration | P4 | `PUT /projects/{id}/settings` |
| UC-23 | Manual Cache Invalidation | P4 | `POST /cache/invalidate` |
| UC-24 | Non-Cacheable Marking | P4 | `cache/policy.py`, `X-APICost-No-Cache` |
| UC-25 | Cache Performance Report | P4 | `GET /cache/stats` |
| UC-26 | Long-Context Warning | P7 | `GET /advisor/prompt-optimizations` |
| UC-27 | Prompt Compression Suggestion | P7 | same |
| UC-28 | Token-Heavy Endpoint Report | P7 | `GET /usage/breakdown?by=endpoint` |
| UC-29 | Budget Configuration | P6 | `POST /budgets` |
| UC-30 | Budget Enforcement Actions | P6 | `budgets/enforcement.py` |
| UC-31 | Spend Spike Alerting | P6 | `anomaly/zscore.py` |
| UC-32 | Key Leak / Abuse Detection | P6 | `anomaly/forest.py` |
| UC-33 | Emergency Kill Switch | P6 | `POST /projects/{id}/kill` |
| UC-34 | Alert History Log | P6 | `GET /alert-events` |
| UC-35 | Model Downgrade Recommendations | P8 | `GET /advisor/recommendations` |
| UC-36 | Self-Hosting Break-Even Advisor | P8 | `GET /advisor/breakeven` |
| UC-37 | Recommendation Savings Projections | P8 | `GET /advisor/recommendations` |
| UC-38 | Weekly Savings Digest Email | P9 | `notify/digest.py` |
| UC-39 | Anonymized Peer Benchmark | P9 | `GET /benchmark/peer` |

---

## 11. Definition of done, per phase

1. All acceptance criteria for the phase pass.
2. Tests written and green; fail-open suite still green.
3. Alembic migration created and reversible (`downgrade` actually works).
4. `docs/CODEBASE_GUIDE.md` updated to reflect anything that changed.
5. An ADR written in `docs/adr/` for any decision that deviates from this spec, explaining why.
6. `ruff`, `mypy`, `eslint` clean.
