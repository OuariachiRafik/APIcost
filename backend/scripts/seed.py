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
    escalated = routed and random.random() < 0.07
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
    finally:
        await dispose_engine()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
