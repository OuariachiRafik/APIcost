"""The nightly recommendation job — UC-35, UC-37, BUILD_SPEC §4 P8.

Runs once a day over each project's usage history and *replaces* that
project's open recommendations. Replace rather than append, because a
recommendation is a statement about current usage: yesterday's advice about
traffic that has since changed is worse than no advice, and a list that only
grows is one the user stops reading.

Dismissed recommendations are never resurrected. If a user has said no to
moving an endpoint to the cheap tier, saying it again every morning is not
persistence, it is nagging.

This module does the I/O; the judgements live in `downgrade.py` and
`breakeven.py`, which are pure.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from apicost.advisor.breakeven import GpuOption, break_even_analysis
from apicost.advisor.downgrade import DowngradeCandidate, recommend_downgrades
from apicost.core.ids import new_id
from apicost.core.logging import get_logger
from apicost.db.session import get_admin_engine
from apicost.routing.engine import cheaper_model_for

__all__ = ["DEFAULT_GPU", "GPU_OPTIONS", "generate_recommendations"]

_logger = get_logger(__name__)

LOOKBACK_DAYS = 30

GPU_OPTIONS: list[GpuOption] = [
    # A maintained price table (BUILD_SPEC §6.7). On-demand list prices, which
    # are the ones a solo developer would actually pay — reserved and spot
    # pricing needs a commitment or a tolerance for eviction, and recommending
    # either without saying so would understate the real cost.
    GpuOption(name="A10G (24GB)", cost_per_hour_usd=1.006, max_tokens_per_second=450.0),
    GpuOption(name="L40S (48GB)", cost_per_hour_usd=1.96, max_tokens_per_second=1100.0),
    GpuOption(name="A100 (80GB)", cost_per_hour_usd=3.93, max_tokens_per_second=2400.0),
]

DEFAULT_GPU = GPU_OPTIONS[0]


async def generate_recommendations(now: datetime | None = None) -> int:
    """Regenerate recommendations for every active project. Returns rows written."""
    at = now or datetime.now(UTC)
    since = at - timedelta(days=LOOKBACK_DAYS)

    try:
        projects = await _active_projects(since)
    except Exception as exc:
        _logger.warning(
            "advisor_project_query_failed",
            subsystem="advisor",
            error_type=type(exc).__name__,
        )
        return 0

    written = 0
    for project_id, user_id in projects:
        try:
            written += await _for_project(project_id, user_id, since)
        except Exception as exc:
            _logger.warning(
                "advisor_project_failed",
                subsystem="advisor",
                project_id=project_id,
                error_type=type(exc).__name__,
            )

    if written:
        _logger.info("advisor_recommendations_generated", subsystem="advisor", count=written)
    return written


async def _active_projects(since: datetime) -> list[tuple[str, str]]:
    async with get_admin_engine().begin() as conn:
        rows = await conn.execute(
            text(
                "SELECT DISTINCT r.project_id, r.user_id FROM requests_log r "
                "JOIN projects p ON p.id = r.project_id "
                "WHERE r.timestamp >= :since AND p.archived_at IS NULL"
            ),
            {"since": since},
        )
        return [(str(r.project_id), str(r.user_id)) for r in rows]


async def _for_project(project_id: str, user_id: str, since: datetime) -> int:
    candidates = await _downgrade_candidates(project_id, since)
    recommendations = recommend_downgrades(candidates)

    rows: list[dict[str, Any]] = [
        {
            "id": new_id(),
            "user_id": user_id,
            "project_id": project_id,
            "kind": "downgrade",
            "title": f"Move {rec.endpoint} from {rec.from_model} to {rec.to_model}",
            "detail": json.dumps(
                {
                    "endpoint": rec.endpoint,
                    "from_model": rec.from_model,
                    "to_model": rec.to_model,
                    "escalation_rate": rec.escalation_rate,
                    "rationale": rec.rationale,
                }
            ),
            "projected_savings_usd": rec.projected_savings_usd,
            "confidence": rec.confidence,
            "sample_size": rec.sample_size,
        }
        for rec in recommendations
    ]

    breakeven = await _breakeven_row(project_id, user_id, since)
    if breakeven is not None:
        rows.append(breakeven)

    async with get_admin_engine().begin() as conn:
        # Clear only *open* rows. Adopted and dismissed ones are the user's
        # decisions and are not ours to overwrite — and dropping dismissed rows
        # would let the job re-suggest something already rejected.
        await conn.execute(
            text(
                "DELETE FROM advisor_recommendations "
                "WHERE project_id = :project_id AND status = 'open'"
            ),
            {"project_id": project_id},
        )

        if not rows:
            return 0

        dismissed = (
            await conn.execute(
                text(
                    "SELECT title FROM advisor_recommendations "
                    "WHERE project_id = :project_id AND status = 'dismissed'"
                ),
                {"project_id": project_id},
            )
        ).scalars()
        rejected = set(dismissed)

        fresh = [row for row in rows if row["title"] not in rejected]
        if not fresh:
            return 0

        await conn.execute(
            text(
                "INSERT INTO advisor_recommendations "
                "(id, user_id, project_id, kind, title, detail, projected_savings_usd, "
                " confidence, sample_size) "
                "VALUES (:id, :user_id, :project_id, :kind, :title, CAST(:detail AS jsonb), "
                ":projected_savings_usd, :confidence, :sample_size)"
            ),
            fresh,
        )
        return len(fresh)


async def _downgrade_candidates(project_id: str, since: datetime) -> list[DowngradeCandidate]:
    """Aggregate what routing has already demonstrated on this project.

    Cache hits are excluded. A cached response says nothing about whether the
    cheap model would have answered well — it says the answer was already
    known.
    """
    async with get_admin_engine().begin() as conn:
        rows = (
            await conn.execute(
                text(
                    """
                    SELECT endpoint,
                           model_requested,
                           count(*)                                        AS total,
                           count(*) FILTER (WHERE model_used <> model_requested)
                                                                           AS cheap,
                           count(*) FILTER (WHERE escalation_triggered)    AS escalated,
                           COALESCE(avg(cost_would_have_been_usd)
                                    FILTER (WHERE model_used <> model_requested), 0)
                                                                           AS avg_full,
                           COALESCE(avg(cost_usd)
                                    FILTER (WHERE model_used <> model_requested), 0)
                                                                           AS avg_cheap
                    FROM requests_log
                    WHERE project_id = :project_id AND timestamp >= :since
                      AND NOT cache_hit AND status = 200
                    GROUP BY endpoint, model_requested
                    """
                ),
                {"project_id": project_id, "since": since},
            )
        ).mappings()

        candidates: list[DowngradeCandidate] = []
        for row in rows:
            cheap_model = cheaper_model_for(str(row["model_requested"]))
            if cheap_model is None:
                continue
            candidates.append(
                DowngradeCandidate(
                    endpoint=str(row["endpoint"]),
                    model_requested=str(row["model_requested"]),
                    cheap_model=cheap_model,
                    total_requests=int(row["total"]),
                    cheap_requests=int(row["cheap"]),
                    escalated_requests=int(row["escalated"]),
                    avg_cost_requested_usd=float(row["avg_full"] or 0),
                    avg_cost_cheap_usd=float(row["avg_cheap"] or 0),
                )
            )
        return candidates


async def _breakeven_row(project_id: str, user_id: str, since: datetime) -> dict[str, Any] | None:
    """A self-hosting recommendation, only when it is actually favourable."""
    async with get_admin_engine().begin() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT COALESCE(sum(tokens_in + tokens_out), 0) AS tokens, "
                    "COALESCE(sum(cost_usd), 0) AS cost FROM requests_log "
                    "WHERE project_id = :project_id AND timestamp >= :since AND NOT cache_hit"
                ),
                {"project_id": project_id, "since": since},
            )
        ).one()

    tokens = int(row.tokens)
    cost = float(row.cost)
    if tokens <= 0 or cost <= 0:
        return None

    result = break_even_analysis(tokens, cost / tokens, DEFAULT_GPU)
    if result.recommendation != "gpu":
        # Only surfaced when self-hosting wins. "Keep using the API" is the
        # status quo and does not belong on a list of things to consider doing.
        return None

    return {
        "id": new_id(),
        "user_id": user_id,
        "project_id": project_id,
        "kind": "breakeven",
        "title": f"A dedicated {result.gpu_option} may be cheaper than the API at your volume",
        "detail": json.dumps(
            {
                "monthly_tokens": result.monthly_tokens,
                "api_monthly_cost_usd": result.api_monthly_cost_usd,
                "gpu_monthly_cost_usd": result.gpu_monthly_cost_usd,
                "n_gpus": result.n_gpus,
                "break_even_tokens": result.break_even_tokens,
                "capacity_tokens_per_gpu": result.capacity_tokens_per_gpu,
                "caveats": result.caveats,
            }
        ),
        "projected_savings_usd": max(0.0, result.monthly_saving_usd),
        "confidence": "low",
        # Always low. This compares infrastructure cost against a list price
        # and cannot see the user's tolerance for operating a GPU, so it is a
        # prompt to investigate rather than a conclusion.
        "sample_size": result.monthly_tokens,
    }
