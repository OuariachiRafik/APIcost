"""P6 acceptance criteria — UC-29 through UC-34.

"A simulated runaway loop (500 requests in 60 s against a baseline of 5/min)
 fires a spike alert within 2 minutes. A hard_stop budget stops
 billing-relevant traffic within one request of the threshold being crossed.
 Budget enforcement is the one place where fail-open does not apply."
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from apicost.anomaly.pipeline import process_ledger_batch
from apicost.budgets.enforcement import (
    BudgetAction,
    BudgetDecision,
    BudgetSpec,
    budget_counter_key,
    check_budgets,
    record_spend,
)
from apicost.db.redis import get_redis
from apicost.db.session import get_admin_engine
from tests.e2e.conftest import LiveServer, provision_account

pytestmark = pytest.mark.integration


async def login(api: AsyncClient, email: str) -> tuple[dict[str, str], str]:
    response = await api.post(
        "/auth/login", json={"email": email, "password": "a-very-long-password"}
    )
    auth = {"Authorization": f"Bearer {response.json()['access_token']}"}
    project_id = (await api.get("/projects", headers=auth)).json()[0]["id"]
    return auth, project_id


async def set_budget(
    api: AsyncClient, auth: dict[str, str], project_id: str, limit: float, action: str
) -> dict[str, Any]:
    response = await api.post(
        "/budgets",
        headers=auth,
        json={
            "project_id": project_id,
            "period": "daily",
            "limit_usd": limit,
            "action": action,
        },
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


async def disable_cache(api: AsyncClient, auth: dict[str, str], project_id: str) -> None:
    """Turn caching off for budget tests.

    Not incidental setup — without it these tests measure the wrong thing. A
    cache hit costs nothing, so it correctly consumes no budget and never
    reaches the throttle branch, which sits after the cache returns. Repeated
    or merely *similar* probe prompts are therefore served from cache and the
    budget never moves. That is right in production and useless in a test of
    enforcement.
    """
    response = await api.put(
        f"/projects/{project_id}/settings", headers=auth, json={"cache_enabled": False}
    )
    assert response.status_code == 200, response.text


async def call(proxy: LiveServer, key: str, prompt: str = "hello") -> Any:
    async with AsyncClient(timeout=30.0) as client:
        return await client.post(
            f"{proxy.url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": prompt}]},
        )


# -- UC-29, UC-30: budgets and their actions --------------------------------


@pytest.mark.usefixtures("clean_all")
async def test_a_hard_stop_blocks_within_one_request_of_the_threshold(
    live_proxy: LiveServer, api_base: AsyncClient
) -> None:
    """The core P6 acceptance criterion.

    Deliberately not "blocks eventually". The counter is incremented by the
    proxy itself rather than by the worker precisely so that the request after
    the one that crosses the line is refused — a worker on a 5 s cron would let
    everything sent in those 5 seconds through.
    """
    key = await provision_account(api_base, "hardstop@example.com")
    auth, project_id = await login(api_base, "hardstop@example.com")

    await disable_cache(api_base, auth, project_id)
    # A limit small enough that a couple of calls cross it.
    await set_budget(api_base, auth, project_id, 0.0005, "hard_stop")

    statuses: list[int] = []
    for index in range(6):
        response = await call(live_proxy, key, prompt=f"budget probe {index} {'x' * index}")
        statuses.append(response.status_code)
        if response.status_code == 402:
            break

    assert 402 in statuses, f"never blocked: {statuses}"

    allowed = statuses.index(402)
    spent = await _counter(project_id)
    per_request = spent / allowed

    # The criterion is "within one request of the threshold", which means two
    # things, and both are worth pinning:
    #   1. we did not block early — the limit really was crossed;
    #   2. we did not overshoot by more than the single request that crossed it.
    assert spent >= 0.0005, f"blocked at ${spent:.6f}, before the ${0.0005} limit"
    assert spent - 0.0005 < per_request, (
        f"overshot by ${spent - 0.0005:.6f}, more than one request "
        f"(${per_request:.6f}); statuses {statuses}"
    )


@pytest.mark.usefixtures("clean_all")
async def test_the_block_explains_itself_and_leaks_nothing(
    live_proxy: LiveServer, api_base: AsyncClient
) -> None:
    key = await provision_account(api_base, "blockbody@example.com")
    auth, project_id = await login(api_base, "blockbody@example.com")
    await disable_cache(api_base, auth, project_id)
    await set_budget(api_base, auth, project_id, 0.0001, "hard_stop")

    response = None
    for index in range(6):
        response = await call(live_proxy, key, prompt=f"explain probe {index} {'y' * index}")
        if response.status_code == 402:
            break

    assert response is not None and response.status_code == 402
    body = response.json()
    text_body = response.text.lower()

    assert "budget" in text_body
    # Actionable: says what to do, not just that something went wrong.
    assert any(word in text_body for word in ("raise the limit", "change the action"))
    # And carries no credential material of any kind.
    assert key.lower() not in text_body
    assert "sk-" not in text_body
    assert "password" not in text_body
    assert body


@pytest.mark.usefixtures("clean_all")
async def test_a_limit_below_the_stored_precision_is_rejected_clearly(
    api_base: AsyncClient,
) -> None:
    """A 422 rather than a CHECK violation surfacing as a 500."""
    await provision_account(api_base, "tinylimit@example.com")
    auth, project_id = await login(api_base, "tinylimit@example.com")

    response = await api_base.post(
        "/budgets",
        headers=auth,
        json={
            "project_id": project_id,
            "period": "daily",
            "limit_usd": 0.0000001,
            "action": "hard_stop",
        },
    )
    assert response.status_code == 422, response.text


@pytest.mark.usefixtures("clean_all")
async def test_alert_only_never_blocks(live_proxy: LiveServer, api_base: AsyncClient) -> None:
    """A budget the user asked to be *told* about must not stop their product."""
    key = await provision_account(api_base, "alertonly@example.com")
    auth, project_id = await login(api_base, "alertonly@example.com")
    await disable_cache(api_base, auth, project_id)
    await set_budget(api_base, auth, project_id, 0.000001, "alert_only")

    for index in range(4):
        response = await call(live_proxy, key, prompt=f"alert probe {index} {'z' * index}")
        assert response.status_code == 200, response.text


@pytest.mark.usefixtures("clean_all")
async def test_soft_throttle_degrades_the_model_instead_of_refusing(
    live_proxy: LiveServer, api_base: AsyncClient
) -> None:
    """UC-30's middle option: keep serving, but on the cheap tier."""
    key = await provision_account(api_base, "throttle@example.com")
    auth, project_id = await login(api_base, "throttle@example.com")
    await disable_cache(api_base, auth, project_id)
    await set_budget(api_base, auth, project_id, 0.000001, "soft_throttle")

    models: list[str] = []
    for index in range(4):
        response = await call(live_proxy, key, prompt=f"throttle probe {index} {'q' * index}")
        assert response.status_code == 200, response.text
        models.append(response.headers["x-apicost-model-used"])

    assert "gpt-4o-mini" in models, f"never degraded: {models}"
    assert all(m != "" for m in models)


@pytest.mark.usefixtures("clean_all")
async def test_a_new_hard_stop_applies_without_waiting_for_the_auth_cache(
    live_proxy: LiveServer, api_base: AsyncClient
) -> None:
    """Creating a budget purges the auth cache.

    Without that, the 60 s cached ResolvedKey means a user who has just
    discovered a runaway loop watches it keep running for a minute.
    """
    key = await provision_account(api_base, "instant@example.com")
    auth, project_id = await login(api_base, "instant@example.com")

    await disable_cache(api_base, auth, project_id)
    assert (await call(live_proxy, key)).status_code == 200

    # Force the counter above any plausible limit, then set the budget.
    await record_spend(get_redis(), project_id, 99.0, ["daily"])
    await set_budget(api_base, auth, project_id, 1.0, "hard_stop")

    response = await call(live_proxy, key)
    assert response.status_code == 402, "the budget took effect late"


# -- Fail-closed semantics --------------------------------------------------


async def test_unreadable_state_fails_closed_only_for_hard_stop() -> None:
    """CLAUDE.md hard rule 1's single exception, both halves of it."""

    class BrokenRedis:
        async def mget(self, keys: list[str]) -> list[str]:
            raise ConnectionError("redis is down")

    broken: Any = BrokenRedis()

    hard = [BudgetSpec("daily", 5.0, BudgetAction.HARD_STOP)]
    verdict = await check_budgets(broken, "proj_1", hard)
    assert verdict.decision is BudgetDecision.BLOCK
    assert verdict.degraded
    assert verdict.reason == "BUDGET_UNREADABLE_FAIL_CLOSED"

    soft = [BudgetSpec("daily", 5.0, BudgetAction.SOFT_THROTTLE)]
    passed = await check_budgets(broken, "proj_1", soft)
    assert passed.decision is BudgetDecision.ALLOW, "only hard_stop may fail closed"
    assert passed.reason == "BUDGET_UNREADABLE_PASSTHROUGH"


async def test_a_project_with_no_budget_is_never_blocked_by_a_broken_redis() -> None:
    class BrokenRedis:
        async def mget(self, keys: list[str]) -> list[str]:
            raise ConnectionError("redis is down")

    verdict = await check_budgets(BrokenRedis(), "proj_1", [])  # type: ignore[arg-type]
    assert verdict.decision is BudgetDecision.ALLOW


@pytest.mark.usefixtures("clean_all")
async def test_the_most_restrictive_budget_wins() -> None:
    redis = get_redis()
    project = "proj_multi"
    await record_spend(redis, project, 10.0, ["daily", "monthly"])

    verdict = await check_budgets(
        redis,
        project,
        [
            BudgetSpec("daily", 100.0, BudgetAction.ALERT_ONLY),
            BudgetSpec("monthly", 5.0, BudgetAction.HARD_STOP),
        ],
    )
    assert verdict.decision is BudgetDecision.BLOCK
    assert verdict.period == "monthly"


# -- UC-31: the runaway loop ------------------------------------------------


@pytest.mark.usefixtures("clean_all")
async def test_a_runaway_loop_fires_a_spike_alert(
    live_proxy: LiveServer, api_base: AsyncClient
) -> None:
    """BUILD_SPEC §4 P6: 500 requests in 60 s against a 5/min baseline.

    Driving 500 real requests through the proxy would take minutes and measure
    the stub provider rather than the detector, so the ledger rows are
    synthesised at the exact rates the criterion names and pushed through the
    same `process_ledger_batch` the worker calls. The detector, the baseline,
    the cooldown, and the alert row are all the production path.
    """
    await provision_account(api_base, "runaway@example.com")
    _, project_id = await login(api_base, "runaway@example.com")
    user_id = await _user_id(project_id)

    base = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)

    # 40 minutes of baseline: 5 requests/min at $0.002 each = $0.01/min.
    baseline_rows: list[dict[str, Any]] = []
    for minute in range(40):
        for index in range(5):
            baseline_rows.append(
                _row(
                    user_id,
                    project_id,
                    base + timedelta(minutes=minute, seconds=index * 10),
                    0.002,
                )
            )
    await process_ledger_batch(get_redis(), baseline_rows)

    opened = await _open_alerts(project_id)
    assert opened == 0, "the baseline itself must not fire"

    # The runaway: 500 requests in the next 60 seconds.
    spike_rows = [
        _row(
            user_id,
            project_id,
            base + timedelta(minutes=40, milliseconds=index * 100),
            0.002,
        )
        for index in range(500)
    ]
    # One more request in the following window, to close the spike window.
    spike_rows.append(_row(user_id, project_id, base + timedelta(minutes=41, seconds=5), 0.002))

    await process_ledger_batch(get_redis(), spike_rows)

    alerts = await _alerts(project_id)
    assert alerts, "the runaway loop did not fire an alert"

    alert = alerts[0]
    assert alert["alert_type"] == "spend_spike"
    assert alert["status"] == "open"
    # The email has to carry numbers a human can act on, not just a flag.
    assert "times_normal" in alert["detail"]
    assert "normal_spend_usd" in alert["detail"]


@pytest.mark.usefixtures("clean_all")
async def test_a_sustained_incident_sends_one_alert_not_hundreds(
    api_base: AsyncClient,
) -> None:
    """BUILD_SPEC §6.8: a 30-minute cooldown per alert type per project."""
    await provision_account(api_base, "cooldown@example.com")
    _, project_id = await login(api_base, "cooldown@example.com")
    user_id = await _user_id(project_id)

    base = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
    rows = [
        _row(user_id, project_id, base + timedelta(minutes=m, seconds=s * 10), 0.002)
        for m in range(40)
        for s in range(5)
    ]
    await process_ledger_batch(get_redis(), rows)

    # Five consecutive spiking windows.
    for window in range(5):
        spike = [
            _row(
                user_id,
                project_id,
                base + timedelta(minutes=40 + window, milliseconds=i * 50),
                0.002,
            )
            for i in range(400)
        ]
        spike.append(
            _row(user_id, project_id, base + timedelta(minutes=41 + window, seconds=5), 0.002)
        )
        await process_ledger_batch(get_redis(), spike)

    alerts = await _alerts(project_id)
    assert len(alerts) == 1, f"cooldown failed: {len(alerts)} alerts for one incident"


@pytest.mark.usefixtures("clean_all")
async def test_a_quiet_project_never_alerts(api_base: AsyncClient) -> None:
    """Cold start must be silent — under 30 windows there is no baseline."""
    await provision_account(api_base, "quiet@example.com")
    _, project_id = await login(api_base, "quiet@example.com")
    user_id = await _user_id(project_id)

    base = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
    rows = [_row(user_id, project_id, base + timedelta(minutes=m), 0.002) for m in range(10)]
    rows.append(_row(user_id, project_id, base + timedelta(minutes=11), 50.0))

    await process_ledger_batch(get_redis(), rows)
    assert await _open_alerts(project_id) == 0


# -- UC-33: the kill switch -------------------------------------------------


@pytest.mark.usefixtures("clean_all")
async def test_the_kill_switch_stops_traffic_in_under_a_second(
    live_proxy: LiveServer, api_base: AsyncClient
) -> None:
    key = await provision_account(api_base, "killswitch@example.com")
    auth, project_id = await login(api_base, "killswitch@example.com")

    assert (await call(live_proxy, key)).status_code == 200

    started = time.perf_counter()
    response = await api_base.post(f"/projects/{project_id}/kill", headers=auth)
    assert response.status_code == 200, response.text

    blocked = await call(live_proxy, key)
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    assert blocked.status_code == 401, blocked.text
    assert elapsed_ms < 1000.0, f"took {elapsed_ms:.0f} ms to take effect"

    body = response.json()
    assert body["keys_revoked"] >= 1
    assert body["took_ms"] < 1000.0


@pytest.mark.usefixtures("clean_all")
async def test_the_kill_switch_leaves_provider_keys_alone(
    api_base: AsyncClient,
) -> None:
    """Containing a leak of our credential must not destroy theirs."""
    await provision_account(api_base, "killprovider@example.com")
    auth, project_id = await login(api_base, "killprovider@example.com")

    before = (await api_base.get("/keys", headers=auth)).json()
    assert before, "fixture should have provisioned a provider key"

    await api_base.post(f"/projects/{project_id}/kill", headers=auth)

    after = (await api_base.get("/keys", headers=auth)).json()
    assert len(after) == len(before)


@pytest.mark.usefixtures("clean_all")
async def test_the_kill_switch_records_itself_in_the_alert_history(
    api_base: AsyncClient,
) -> None:
    await provision_account(api_base, "killaudit@example.com")
    auth, project_id = await login(api_base, "killaudit@example.com")

    await api_base.post(f"/projects/{project_id}/kill", headers=auth)

    alerts = (await api_base.get("/alert-events", headers=auth)).json()["alerts"]
    assert any(a["alert_type"] == "kill_switch" for a in alerts)


# -- UC-34: alert history ---------------------------------------------------


@pytest.mark.usefixtures("clean_all")
async def test_alerts_can_be_resolved_with_a_note(api_base: AsyncClient) -> None:
    await provision_account(api_base, "resolve@example.com")
    auth, project_id = await login(api_base, "resolve@example.com")
    await api_base.post(f"/projects/{project_id}/kill", headers=auth)

    alert_id = (await api_base.get("/alert-events", headers=auth)).json()["alerts"][0]["id"]

    response = await api_base.post(
        f"/alert-events/{alert_id}/resolve",
        headers=auth,
        json={"status": "resolved", "resolution": "Confirmed leak, rotated the key."},
    )
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["status"] == "resolved"
    assert body["resolution"] == "Confirmed leak, rotated the key."
    assert body["resolved_at"] is not None


@pytest.mark.usefixtures("clean_all")
async def test_alert_history_pages_with_a_cursor(api_base: AsyncClient) -> None:
    """§8 convention. `alert_events` is a history and grows without bound."""
    await provision_account(api_base, "paging@example.com")
    auth, project_id = await login(api_base, "paging@example.com")

    for _ in range(5):
        await api_base.post(f"/projects/{project_id}/kill", headers=auth)

    first = (await api_base.get("/alert-events?limit=2", headers=auth)).json()
    assert len(first["alerts"]) == 2
    assert first["next_cursor"]

    second = (
        await api_base.get(f"/alert-events?limit=2&cursor={first['next_cursor']}", headers=auth)
    ).json()
    assert len(second["alerts"]) == 2

    seen = {a["id"] for a in first["alerts"]} | {a["id"] for a in second["alerts"]}
    assert len(seen) == 4, "pages overlapped"

    last = (
        await api_base.get(f"/alert-events?limit=10&cursor={second['next_cursor']}", headers=auth)
    ).json()
    assert last["next_cursor"] is None


@pytest.mark.usefixtures("clean_all")
async def test_one_users_alerts_are_invisible_to_another(api_base: AsyncClient) -> None:
    """Hard rule 5, on the newest user-scoped table."""
    await provision_account(api_base, "alert-a@example.com")
    auth_a, project_a = await login(api_base, "alert-a@example.com")
    await api_base.post(f"/projects/{project_a}/kill", headers=auth_a)

    await provision_account(api_base, "alert-b@example.com")
    auth_b, _ = await login(api_base, "alert-b@example.com")

    assert (await api_base.get("/alert-events", headers=auth_a)).json()["alerts"]
    assert (await api_base.get("/alert-events", headers=auth_b)).json()["alerts"] == []

    stolen = await api_base.get(f"/alert-events?project_id={project_a}", headers=auth_b)
    assert stolen.status_code == 404


@pytest.mark.usefixtures("clean_all")
async def test_one_users_budgets_are_invisible_to_another(api_base: AsyncClient) -> None:
    await provision_account(api_base, "budget-a@example.com")
    auth_a, project_a = await login(api_base, "budget-a@example.com")
    await set_budget(api_base, auth_a, project_a, 5.0, "hard_stop")

    await provision_account(api_base, "budget-b@example.com")
    auth_b, _ = await login(api_base, "budget-b@example.com")

    assert (await api_base.get("/budgets", headers=auth_b)).json() == []


@pytest.mark.usefixtures("clean_all")
async def test_budget_reporting_shows_the_number_enforcement_uses(
    api_base: AsyncClient,
) -> None:
    """A dashboard that disagrees with the blocker is worse than no dashboard."""
    await provision_account(api_base, "budgetreport@example.com")
    auth, project_id = await login(api_base, "budgetreport@example.com")
    await set_budget(api_base, auth, project_id, 10.0, "alert_only")

    await record_spend(get_redis(), project_id, 2.5, ["daily"])

    listed = (await api_base.get("/budgets", headers=auth)).json()
    assert listed[0]["spent_usd"] == pytest.approx(2.5)
    assert listed[0]["fraction_used"] == pytest.approx(0.25)


# -- helpers ----------------------------------------------------------------


def _row(user_id: str, project_id: str, at: datetime, cost: float) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "project_id": project_id,
        "timestamp": at.isoformat(),
        "cost_usd": cost,
        "model_used": "gpt-4o",
        "endpoint": "/v1/chat/completions",
        "prompt_hash": None,
    }


async def _counter(project_id: str) -> float:
    raw = await get_redis().get(budget_counter_key(project_id, "daily"))
    return float(raw) if raw else 0.0


async def _user_id(project_id: str) -> str:
    async with get_admin_engine().connect() as conn:
        row = (
            await conn.execute(
                text("SELECT user_id FROM projects WHERE id = :id"), {"id": project_id}
            )
        ).one()
        return str(row.user_id)


async def _alerts(project_id: str) -> list[dict[str, Any]]:
    async with get_admin_engine().connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT alert_type, status, detail FROM alert_events "
                    "WHERE project_id = :id ORDER BY created_at"
                ),
                {"id": project_id},
            )
        ).mappings()
        return [dict(r) for r in rows]


async def _open_alerts(project_id: str) -> int:
    async with get_admin_engine().connect() as conn:
        row = (
            await conn.execute(
                text("SELECT count(*) AS n FROM alert_events WHERE project_id = :id"),
                {"id": project_id},
            )
        ).one()
        return int(row.n)
