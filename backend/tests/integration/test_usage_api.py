"""Usage and reporting endpoints — UC-08 through UC-13, UC-28."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from apicost.core.ids import new_id
from apicost.db.session import get_admin_engine
from apicost.ledger.rollup import rebuild_rollups
from tests.integration.conftest import register

pytestmark = pytest.mark.integration


async def insert_rows(user_id: str, project_id: str, rows: list[dict[str, object]]) -> None:
    """Write ledger rows directly. The proxy path is covered by the e2e suite."""
    defaults: dict[str, object] = {
        "endpoint": "chat/completions",
        "provider": "openai",
        "model_requested": "gpt-4o",
        "model_used": "gpt-4o",
        "tokens_in": 100,
        "tokens_out": 50,
        "tokens_estimated": False,
        "cost_usd": Decimal("0.001"),
        "cost_would_have_been_usd": Decimal("0.001"),
        "latency_ms": 250.0,
        "ttft_ms": None,
        "itl_ms": None,
        "tps": None,
        "cache_hit": False,
        "cache_similarity": None,
        "routed": False,
        "routing_reason_code": "PASSTHROUGH",
        "routing_model_version": None,
        "escalation_triggered": False,
        "status": 200,
        "error_code": None,
        "streamed": False,
    }

    payload = []
    for row in rows:
        merged = {
            **defaults,
            "id": new_id(),
            "request_id": new_id(),
            "user_id": user_id,
            "project_id": project_id,
            "timestamp": datetime.now(UTC),
            **row,
        }
        payload.append(merged)

    async with get_admin_engine().begin() as conn:
        await conn.execute(
            text(
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
                """
            ),
            payload,
        )

    # The aggregation endpoints read the daily rollups (ADR 0006), so a test
    # that writes ledger rows has to rebuild them — exactly as the worker does.
    await rebuild_rollups(full=True)


async def setup_user(api_client: AsyncClient, email: str) -> tuple[dict[str, str], str, str]:
    user = await register(api_client, email)
    me = await api_client.get("/auth/me", headers=user.auth)
    project = await api_client.post("/projects", headers=user.auth, json={"name": "prod"})
    return user.auth, me.json()["id"], project.json()["id"]


# ---------------------------------------------------------------------------
# UC-08 — spend over time
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("clean_db")
async def test_usage_totals_and_series(api_client: AsyncClient) -> None:
    auth, user_id, project_id = await setup_user(api_client, "usage@example.com")
    await insert_rows(
        user_id,
        project_id,
        [
            {"cost_usd": Decimal("1.00"), "cost_would_have_been_usd": Decimal("1.00")},
            {"cost_usd": Decimal("2.00"), "cost_would_have_been_usd": Decimal("2.00")},
        ],
    )

    response = await api_client.get("/usage?range=30d", headers=auth)

    assert response.status_code == 200
    body = response.json()
    assert Decimal(body["summary"]["total_cost_usd"]) == Decimal("3.00")
    assert body["summary"]["total_requests"] == 2
    assert len(body["series"]) >= 1


@pytest.mark.usefixtures("clean_db")
async def test_savings_are_split_by_mechanism_and_never_double_counted(
    api_client: AsyncClient,
) -> None:
    """CODEBASE_GUIDE §6. A cache hit is never also a routing win."""
    auth, user_id, project_id = await setup_user(api_client, "savings@example.com")
    await insert_rows(
        user_id,
        project_id,
        [
            # Cache hit: the provider was never called, so the whole avoided
            # cost is a caching saving.
            {
                "cache_hit": True,
                "cost_usd": Decimal("0"),
                "cost_would_have_been_usd": Decimal("5.00"),
            },
            # Routed: saved the difference only.
            {
                "routed": True,
                "model_used": "gpt-4o-mini",
                "cost_usd": Decimal("1.00"),
                "cost_would_have_been_usd": Decimal("4.00"),
            },
            # A row that is both flags set — caching must win, so this must not
            # be counted twice.
            {
                "cache_hit": True,
                "routed": True,
                "cost_usd": Decimal("0"),
                "cost_would_have_been_usd": Decimal("2.00"),
            },
        ],
    )

    summary = (await api_client.get("/usage?range=30d", headers=auth)).json()["summary"]

    assert Decimal(summary["cache_savings_usd"]) == Decimal("7.00")  # 5 + 2
    assert Decimal(summary["routing_savings_usd"]) == Decimal("3.00")  # 4 - 1 only
    combined = Decimal(summary["cache_savings_usd"]) + Decimal(summary["routing_savings_usd"])
    assert combined == Decimal("10.00")
    assert combined <= Decimal(summary["total_would_have_been_usd"])


@pytest.mark.usefixtures("clean_db")
async def test_usage_is_scoped_to_the_caller(api_client: AsyncClient) -> None:
    _auth_a, user_a, project_a = await setup_user(api_client, "tenant-a@example.com")
    auth_b, user_b, project_b = await setup_user(api_client, "tenant-b@example.com")

    await insert_rows(user_a, project_a, [{"cost_usd": Decimal("9.99")}])
    await insert_rows(user_b, project_b, [{"cost_usd": Decimal("0.01")}])

    summary_b = (await api_client.get("/usage?range=30d", headers=auth_b)).json()["summary"]

    assert Decimal(summary_b["total_cost_usd"]) == Decimal("0.01")
    assert summary_b["total_requests"] == 1


@pytest.mark.usefixtures("clean_db")
async def test_custom_range_is_validated(api_client: AsyncClient) -> None:
    auth, _, _ = await setup_user(api_client, "range@example.com")

    missing = await api_client.get("/usage?range=custom", headers=auth)
    assert missing.status_code == 400

    # Passed as params, not interpolated: a raw "+00:00" in a query string is
    # decoded as a space and never reaches the handler.
    now = datetime.now(UTC)
    backwards = await api_client.get(
        "/usage",
        headers=auth,
        params={
            "range": "custom",
            "start": now.isoformat(),
            "end": (now - timedelta(days=1)).isoformat(),
        },
    )
    assert backwards.status_code == 400


# ---------------------------------------------------------------------------
# UC-09, UC-10, UC-28 — breakdowns
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("clean_db")
async def test_breakdown_by_model(api_client: AsyncClient) -> None:
    auth, user_id, project_id = await setup_user(api_client, "bymodel@example.com")
    await insert_rows(
        user_id,
        project_id,
        [
            {"model_used": "gpt-4o", "cost_usd": Decimal("3.00")},
            {"model_used": "gpt-4o", "cost_usd": Decimal("1.00")},
            {"model_used": "gpt-4o-mini", "cost_usd": Decimal("1.00")},
        ],
    )

    rows = (await api_client.get("/usage/breakdown?by=model", headers=auth)).json()["rows"]

    assert rows[0]["key"] == "gpt-4o"  # ordered by spend
    assert Decimal(rows[0]["cost_usd"]) == Decimal("4.00")
    assert rows[0]["requests"] == 2
    assert rows[0]["share"] == pytest.approx(0.8)


@pytest.mark.usefixtures("clean_db")
async def test_breakdown_by_endpoint_ranks_token_heavy_endpoints(
    api_client: AsyncClient,
) -> None:
    """UC-28 ranks by average tokens, so optimization effort lands where it pays."""
    auth, user_id, project_id = await setup_user(api_client, "byendpoint@example.com")
    await insert_rows(
        user_id,
        project_id,
        [
            {"endpoint": "chat/completions", "tokens_in": 8_000, "tokens_out": 2_000},
            {"endpoint": "embeddings", "tokens_in": 50, "tokens_out": 0},
        ],
    )

    rows = (await api_client.get("/usage/breakdown?by=endpoint", headers=auth)).json()["rows"]
    by_key = {row["key"]: row for row in rows}

    assert by_key["chat/completions"]["avg_tokens"] == pytest.approx(10_000)
    assert by_key["embeddings"]["avg_tokens"] == pytest.approx(50)


@pytest.mark.usefixtures("clean_db")
async def test_breakdown_rejects_an_unknown_dimension(api_client: AsyncClient) -> None:
    auth, _, _ = await setup_user(api_client, "baddim@example.com")
    response = await api_client.get("/usage/breakdown?by=DROP TABLE", headers=auth)
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# UC-11 — token distribution
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("clean_db")
async def test_token_distribution_buckets(api_client: AsyncClient) -> None:
    auth, user_id, project_id = await setup_user(api_client, "hist@example.com")
    await insert_rows(
        user_id,
        project_id,
        [
            {"tokens_in": 30, "tokens_out": 20},  # 50  -> 0-100
            {"tokens_in": 200, "tokens_out": 100},  # 300 -> 100-500
            {"tokens_in": 40_000, "tokens_out": 1_000},  # -> 32000+
        ],
    )

    body = (await api_client.get("/usage/token-distribution", headers=auth)).json()
    counts = {bucket["label"]: bucket["requests"] for bucket in body["buckets"]}

    assert counts["0-100"] == 1
    assert counts["100-500"] == 1
    assert counts["32,000+"] == 1
    # The bucket floor, not the exact median: percentiles come from the rollup
    # histogram, where the precise value no longer exists (ADR 0006).
    assert body["median_tokens_bucket"] == 100


# ---------------------------------------------------------------------------
# UC-13 — CSV export
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("clean_db")
async def test_csv_export(api_client: AsyncClient) -> None:
    auth, user_id, project_id = await setup_user(api_client, "csv@example.com")
    await insert_rows(
        user_id, project_id, [{"model_used": "gpt-4o"}, {"model_used": "gpt-4o-mini"}]
    )

    response = await api_client.get("/usage/export.csv", headers=auth)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]

    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == 2
    assert {row["model_used"] for row in rows} == {"gpt-4o", "gpt-4o-mini"}


@pytest.mark.usefixtures("clean_db")
async def test_csv_export_quotes_fields_containing_commas(api_client: AsyncClient) -> None:
    """A model name with a comma must not shift every later column."""
    auth, user_id, project_id = await setup_user(api_client, "csvquote@example.com")
    await insert_rows(user_id, project_id, [{"error_code": "rate_limit, retry later"}])

    response = await api_client.get("/usage/export.csv", headers=auth)
    rows = list(csv.DictReader(io.StringIO(response.text)))

    assert rows[0]["error_code"] == "rate_limit, retry later"
    assert rows[0]["status"] == "200"


@pytest.mark.usefixtures("clean_db")
async def test_csv_export_is_scoped_to_the_caller(api_client: AsyncClient) -> None:
    _auth_a, user_a, project_a = await setup_user(api_client, "csv-a@example.com")
    auth_b, user_b, project_b = await setup_user(api_client, "csv-b@example.com")

    await insert_rows(user_a, project_a, [{"model_used": "secret-model"}])
    await insert_rows(user_b, project_b, [{"model_used": "gpt-4o"}])

    response = await api_client.get("/usage/export.csv", headers=auth_b)

    assert "secret-model" not in response.text
    assert len(list(csv.DictReader(io.StringIO(response.text)))) == 1


@pytest.mark.usefixtures("clean_db")
async def test_usage_endpoints_require_authentication(api_client: AsyncClient) -> None:
    for path in (
        "/usage",
        "/usage/breakdown",
        "/usage/token-distribution",
        "/usage/export.csv",
    ):
        assert (await api_client.get(path)).status_code == 401, path
