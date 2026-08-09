"""Demo user, project, and synthetic ledger history — ``make seed``.

Exists for two reasons: to give the dashboard something to render during
development, and to make P3's acceptance criterion checkable — "usage endpoints
respond in <500 ms p95 against 1M seeded ledger rows". A criterion phrased in
terms of a million rows needs a way to produce a million rows.

The data is deliberately *shaped* rather than uniform. Real traffic has a
weekday rhythm, a long tail of model usage, occasional errors, and a handful of
expensive outliers — and a dashboard that looks right against uniform noise can
still be unreadable against real data.

Usage:
    uv run python scripts/seed.py            # 50k rows, fast
    uv run python scripts/seed.py --rows 1000000
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text

from apicost.core.ids import new_id
from apicost.core.security import generate_proxy_key, hash_password
from apicost.db.session import dispose_engine, get_admin_engine
from apicost.ledger.cost import compute_cost
from apicost.ledger.pricing import PriceNotFoundError
from apicost.ledger.rollup import rebuild_rollups

DEMO_EMAIL = "demo@apicost.dev"
DEMO_PASSWORD = "demo-password-long-enough"

# Weighted so one model dominates, as real usage does.
MODEL_MIX = [
    ("gpt-4o", "openai", 0.34),
    ("gpt-4o-mini", "openai", 0.38),
    ("claude-3-5-sonnet-20241022", "anthropic", 0.14),
    ("claude-3-5-haiku-20241022", "anthropic", 0.08),
    ("gemini-1.5-flash", "gemini", 0.06),
]

ENDPOINTS = [
    ("chat/completions", 0.82),
    ("embeddings", 0.18),
]

BATCH_SIZE = 5_000


def _weighted(options: list[tuple[str, str, float]]) -> tuple[str, str]:
    roll = random.random()
    cumulative = 0.0
    for name, provider, weight in options:
        cumulative += weight
        if roll <= cumulative:
            return name, provider
    return options[-1][0], options[-1][1]


def _weighted_endpoint() -> str:
    roll = random.random()
    cumulative = 0.0
    for name, weight in ENDPOINTS:
        cumulative += weight
        if roll <= cumulative:
            return name
    return ENDPOINTS[-1][0]


def _token_counts() -> tuple[int, int]:
    """Log-normal-ish sizes: mostly small, with a real tail."""
    if random.random() < 0.03:
        tokens_in = random.randint(8_000, 60_000)  # the long-context tail
    else:
        tokens_in = max(20, int(random.lognormvariate(6.0, 1.0)))
    tokens_out = max(1, int(tokens_in * random.uniform(0.05, 0.6)))
    return tokens_in, tokens_out


def _build_row(user_id: str, project_id: str, when: datetime) -> dict[str, object]:
    model, provider = _weighted(MODEL_MIX)
    endpoint = _weighted_endpoint()
    tokens_in, tokens_out = _token_counts()

    cache_hit = random.random() < 0.22
    routed = (not cache_hit) and random.random() < 0.18
    # 1.5%, not the 7% this used to be. Above ~2% the advisor correctly
    # refuses to recommend a downgrade (advisor/downgrade.py), so the old rate
    # described a project where the cheap tier was frequently failing — a real
    # scenario, but a strange one to ship as the demo of a router working.
    escalated = routed and random.random() < 0.015
    status = 200 if random.random() > 0.015 else random.choice([429, 500, 400])

    model_requested = model
    model_used = model
    if routed:
        model_used = "gpt-4o-mini" if provider == "openai" else "claude-3-5-haiku-20241022"

    try:
        would_have_been = compute_cost(model_requested, tokens_in, tokens_out, at=when).total_usd
    except (PriceNotFoundError, ValueError):
        would_have_been = Decimal("0")

    if cache_hit:
        cost = Decimal("0")  # the provider was never called
        tokens_out_recorded = tokens_out
    else:
        try:
            cost = compute_cost(model_used, tokens_in, tokens_out, at=when).total_usd
        except (PriceNotFoundError, ValueError):
            cost = Decimal("0")
        tokens_out_recorded = tokens_out

    streamed = random.random() < 0.55
    latency = random.uniform(180, 4_200) if not cache_hit else random.uniform(4, 26)

    return {
        "id": new_id(),
        "timestamp": when,
        "user_id": user_id,
        "project_id": project_id,
        "request_id": new_id(),
        "endpoint": endpoint,
        "provider": provider,
        "model_requested": model_requested,
        "model_used": model_used,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out_recorded,
        "tokens_estimated": random.random() < 0.08,
        "cost_usd": cost,
        "cost_would_have_been_usd": would_have_been,
        "latency_ms": latency,
        "ttft_ms": random.uniform(90, 800) if streamed else None,
        "itl_ms": random.uniform(8, 60) if streamed else None,
        "tps": random.uniform(15, 90) if streamed else None,
        "cache_hit": cache_hit,
        "cache_similarity": random.uniform(0.95, 0.999) if cache_hit else None,
        "routed": routed,
        "routing_reason_code": "CLASSIFIER_CHEAP_TIER" if routed else "PASSTHROUGH",
        "routing_model_version": "seed-v1" if routed else None,
        "escalation_triggered": escalated,
        "status": status,
        "error_code": None if status == 200 else "seeded_error",
        "streamed": streamed,
    }


_INSERT = text(
    """
    INSERT INTO requests_log (
        id, timestamp, user_id, project_id, request_id, endpoint, provider,
        model_requested, model_used, tokens_in, tokens_out, tokens_estimated,
        cost_usd, cost_would_have_been_usd, latency_ms, ttft_ms, itl_ms, tps,
        cache_hit, cache_similarity, routed, routing_reason_code,
        routing_model_version, escalation_triggered, status, error_code, streamed
    ) VALUES (
        :id, :timestamp, :user_id, :project_id, :request_id, :endpoint, :provider,
        :model_requested, :model_used, :tokens_in, :tokens_out, :tokens_estimated,
        :cost_usd, :cost_would_have_been_usd, :latency_ms, :ttft_ms, :itl_ms, :tps,
        :cache_hit, :cache_similarity, :routed, :routing_reason_code,
        :routing_model_version, :escalation_triggered, :status, :error_code, :streamed
    )
    ON CONFLICT DO NOTHING
    """
)


async def ensure_demo_account() -> tuple[str, str, str]:
    """Create (or reuse) the demo user, project, and proxy key."""
    engine = get_admin_engine()

    async with engine.begin() as conn:
        existing = await conn.execute(
            text("SELECT id FROM users WHERE email = :email"), {"email": DEMO_EMAIL}
        )
        user_id = existing.scalar_one_or_none()

        if user_id is None:
            user_id = new_id()
            await conn.execute(
                text(
                    "INSERT INTO users (id, email, password_hash) "
                    "VALUES (:id, :email, :password_hash)"
                ),
                {
                    "id": user_id,
                    "email": DEMO_EMAIL,
                    "password_hash": hash_password(DEMO_PASSWORD),
                },
            )

        project = await conn.execute(
            text("SELECT id FROM projects WHERE user_id = :user_id LIMIT 1"),
            {"user_id": user_id},
        )
        project_id = project.scalar_one_or_none()

        if project_id is None:
            project_id = new_id()
            await conn.execute(
                text("INSERT INTO projects (id, user_id, name) VALUES (:id, :user_id, :name)"),
                {"id": project_id, "user_id": user_id, "name": "production"},
            )

        raw_key, key_hash, last4 = generate_proxy_key()
        await conn.execute(
            text(
                "INSERT INTO proxy_keys (id, user_id, project_id, proxy_key_hash, "
                "key_last4, name) VALUES (:id, :user_id, :project_id, :hash, :last4, :name)"
            ),
            {
                "id": new_id(),
                "user_id": user_id,
                "project_id": project_id,
                "hash": key_hash,
                "last4": last4,
                "name": "seeded",
            },
        )

    return str(user_id), str(project_id), raw_key


async def seed_ledger(user_id: str, project_id: str, rows: int, days: int) -> None:
    """Write synthetic ledger history spread over the last ``days``."""
    engine = get_admin_engine()
    now = datetime.now(UTC)
    written = 0

    while written < rows:
        batch_size = min(BATCH_SIZE, rows - written)
        batch: list[dict[str, object]] = []

        for _ in range(batch_size):
            # Weekday-weighted: fewer requests at the weekend, which is what
            # makes the spend chart look like something rather than noise.
            age_days = random.uniform(0, days)
            when = now - timedelta(days=age_days)
            if when.weekday() >= 5 and random.random() < 0.55:
                continue
            batch.append(_build_row(user_id, project_id, when))

        if batch:
            async with engine.begin() as conn:
                await conn.execute(_INSERT, batch)

        written += batch_size
        print(f"  seeded {written:,} / {rows:,}", end="\r", flush=True)

    print(f"  seeded {rows:,} rows over {days} days" + " " * 20)


async def _seed_cache_entries(user_id: str, project_id: str, count: int = 40) -> int:
    """Populate the semantic cache through the real store path.

    Goes through `cache.semantic.store` rather than inserting rows, because the
    response payload is envelope-encrypted — fabricating the ciphertext, the
    wrapped key and the nonce by hand would seed rows that decrypt to nothing
    and would not exercise the code that has to read them.

    Embeddings are random unit vectors rather than real ones. Loading fastembed
    to seed a demo costs seconds per run and buys nothing: no seeded entry is
    ever looked up by similarity, only counted and listed.

    Without this, the cache screen shows a 22% hit rate against zero stored
    entries, which is not a state the product can actually be in.
    """
    import math

    from apicost.cache.semantic import store as cache_store
    from apicost.config import get_settings
    from apicost.db.redis import close_redis, get_redis
    from apicost.db.session import session_scope
    from apicost.vault.kms import KMSError, get_kms_client

    settings = get_settings()
    try:
        kms = get_kms_client(settings)
    except KMSError:
        # No master key in the environment. That is the normal state on the
        # host: the key is a compose-level default and there is no repo-root
        # .env, so `make seed` outside the container cannot encrypt anything.
        # Skipping is right — refusing to seed the other five tables because
        # one of them needs a key would be worse than an empty cache screen.
        print(
            "  cache_entries    skipped - APICOST_KMS_MASTER_KEY is not set.\n"
            "                   Run inside the container, or export the key from"
            " .env.example."
        )
        return 0

    redis = get_redis(settings)

    prompts = [
        "summarise this support ticket",
        "classify the sentiment of this review",
        "extract the invoice total",
        "write a commit message for this diff",
        "translate this paragraph to french",
    ]

    stored = 0
    try:
        async with session_scope(user_id) as session:
            for index in range(count):
                raw = [random.gauss(0, 1) for _ in range(384)]
                norm = math.sqrt(sum(v * v for v in raw)) or 1.0
                embedding = [v / norm for v in raw]

                entry_id = await cache_store(
                    session,
                    redis,
                    kms,
                    user_id=user_id,
                    project_id=project_id,
                    normalized_prompt=f"{prompts[index % len(prompts)]} #{index}",
                    embedding=embedding,
                    body={
                        "id": f"chatcmpl-seed-{index}",
                        "object": "chat.completion",
                        "model": "gpt-4o-mini",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": "Seeded response."},
                                "finish_reason": "stop",
                            }
                        ],
                    },
                    model_used="gpt-4o-mini",
                    tokens_in=random.randint(40, 400),
                    tokens_out=random.randint(20, 200),
                    ttl_seconds=86_400,
                )
                if entry_id is not None:
                    stored += 1
    finally:
        await close_redis()

    return stored


async def seed_feature_data(user_id: str, project_id: str) -> dict[str, int]:
    """Populate the tables the P4-P8 screens read.

    Without this the dashboard renders five empty screens and there is no way
    to tell a broken query from a project that has simply never had a budget.
    Everything here is idempotent: `make seed` is run repeatedly, and a seeder
    that duplicates its own rows makes the counts on those screens nonsense.
    """
    engine = get_admin_engine()
    now = datetime.now(UTC)
    counts: dict[str, int] = {}

    async with engine.begin() as conn:
        # -- Budgets (UC-29/30) — one per period, the unique constraint's shape.
        await conn.execute(text("DELETE FROM budgets WHERE project_id = :p"), {"p": project_id})
        budgets = [
            ("daily", "25.000000", "alert_only"),
            ("monthly", "400.000000", "soft_throttle"),
        ]
        for period, limit, action in budgets:
            await conn.execute(
                text(
                    "INSERT INTO budgets (id, user_id, project_id, period, limit_usd, action) "
                    "VALUES (:id, :u, :p, :period, :limit, :action)"
                ),
                {
                    "id": new_id(),
                    "u": user_id,
                    "p": project_id,
                    "period": period,
                    "limit": limit,
                    "action": action,
                },
            )
        counts["budgets"] = len(budgets)

        # -- Routing rules (UC-15/19)
        await conn.execute(
            text("DELETE FROM routing_rules WHERE project_id = :p"), {"p": project_id}
        )
        rules = [
            ("exclude", '{"endpoint": "/v1/embeddings"}', None, 100),
            ("override", '{"endpoint": "/v1/chat/completions"}', "gpt-4o-mini", 10),
        ]
        for rule_type, match, target, priority in rules:
            await conn.execute(
                text(
                    "INSERT INTO routing_rules (id, user_id, project_id, rule_type, "
                    "match_condition, target_model, priority) VALUES (:id, :u, :p, :t, "
                    "CAST(:m AS jsonb), :target, :priority)"
                ),
                {
                    "id": new_id(),
                    "u": user_id,
                    "p": project_id,
                    "t": rule_type,
                    "m": match,
                    "target": target,
                    "priority": priority,
                },
            )
        counts["routing_rules"] = len(rules)

        # -- Alert history (UC-31/32/34), including a resolved one so the
        #    dashboard has both states to render.
        await conn.execute(
            text("DELETE FROM alert_events WHERE project_id = :p"), {"p": project_id}
        )
        alerts = [
            (
                "spend_spike",
                "critical",
                "Spend spike on production",
                '{"window_spend_usd": "$4.1200", "normal_spend_usd": "$0.1200", '
                '"times_normal": "34.3x", "requests_in_window": 512}',
                "open",
                None,
                2,
            ),
            (
                "usage_pattern",
                "critical",
                "Unusual usage pattern on production",
                '{"what_changed": "model_entropy, unique_prompt_ratio", '
                '"requests_per_minute": "11.4"}',
                "resolved",
                "Checked the logs - it was our own load test.",
                9,
            ),
            (
                "budget_threshold",
                "warning",
                "production is at 80% of its monthly budget",
                '{"period": "monthly", "limit_usd": 400.0, "spent_usd": 321.4}',
                "acknowledged",
                None,
                4,
            ),
        ]
        for kind, severity, title, detail, status, resolution, days_ago in alerts:
            await conn.execute(
                text(
                    "INSERT INTO alert_events (id, user_id, project_id, alert_type, severity, "
                    "title, detail, status, resolution, resolved_at, created_at) "
                    "VALUES (:id, :u, :p, :kind, :sev, :title, CAST(:detail AS jsonb), "
                    ":status, :resolution, :resolved_at, :created_at)"
                ),
                {
                    "id": new_id(),
                    "u": user_id,
                    "p": project_id,
                    "kind": kind,
                    "sev": severity,
                    "title": title,
                    "detail": detail,
                    "status": status,
                    "resolution": resolution,
                    "resolved_at": now if status == "resolved" else None,
                    "created_at": now - timedelta(days=days_ago),
                },
            )
        counts["alert_events"] = len(alerts)

    counts["cache_entries"] = await _seed_cache_entries(user_id, project_id)

    # -- Recommendations (UC-35/36/37) come from the real nightly job rather
    #    than being invented here. Seeded advice that no code produced is how a
    #    screen ends up rendering a shape the generator never emits.
    from apicost.advisor.nightly import generate_recommendations

    counts["recommendations"] = await generate_recommendations()

    return counts


async def main() -> int:
    parser = argparse.ArgumentParser(description="Seed a demo account and ledger history")
    parser.add_argument("--rows", type=int, default=50_000)
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--seed", type=int, default=None, help="RNG seed, for reproducibility")
    parser.add_argument(
        "--rollup",
        action="store_true",
        help="Rebuild usage rollups afterwards. The dashboard reads those, not raw rows.",
    )
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    try:
        user_id, project_id, proxy_key = await ensure_demo_account()
        print(f"demo user   : {DEMO_EMAIL} / {DEMO_PASSWORD}")
        print(f"project     : {project_id}")
        print(f"proxy key   : {proxy_key}")
        print("ledger:")
        await seed_ledger(user_id, project_id, args.rows, args.days)

        if args.rollup:
            print("rollups:")
            rows = await rebuild_rollups(full=True)
            print(f"  built {rows:,} rollup rows")

        print("features:")
        for table, count in (await seed_feature_data(user_id, project_id)).items():
            print(f"  {table:<16} {count:,}")
    finally:
        await dispose_engine()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
