"""P7 acceptance — UC-26, UC-27, UC-28.

The property that matters most here is the one that is easiest to break:
**advisory means the request is untouched.** A cost tool that quietly edits
prompts changes model output in ways the user cannot predict and did not ask
for, and it would do so on the exact requests they were already debugging.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from apicost.db.session import get_admin_engine
from apicost.worker.tasks import drain_ledger
from tests.e2e.conftest import LiveServer, provision_account

pytestmark = pytest.mark.integration


def _long(topic: str, tokens: int = 600) -> str:
    words = f"{topic} "
    return (words * (tokens * 4 // len(words) + 1))[: tokens * 4]


def _drifted_messages() -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": _long("warehouse shipping logistics pallets freight")},
        {"role": "assistant", "content": _long("freight pallets warehouse containers customs")},
        {"role": "user", "content": _long("customs paperwork tariff import duty broker")},
        {"role": "assistant", "content": _long("tariff broker duty import declaration forms")},
        {"role": "user", "content": "How do I merge two dataframes in pandas on multiple keys?"},
    ]


async def login(api: AsyncClient, email: str) -> tuple[dict[str, str], str]:
    response = await api.post(
        "/auth/login", json={"email": email, "password": "a-very-long-password"}
    )
    auth = {"Authorization": f"Bearer {response.json()['access_token']}"}
    project_id = (await api.get("/projects", headers=auth)).json()[0]["id"]
    return auth, project_id


# -- UC-26 on the request path ----------------------------------------------


@pytest.mark.usefixtures("clean_all")
async def test_a_bloated_request_is_warned_about_in_headers_only(
    live_proxy: LiveServer, api_base: AsyncClient
) -> None:
    key = await provision_account(api_base, "context@example.com")

    async with AsyncClient(timeout=30.0) as raw:
        response = await raw.post(
            f"{live_proxy.url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": "gpt-4o", "messages": _drifted_messages()},
        )

    assert response.status_code == 200, response.text
    assert response.headers.get("x-apicost-context-warning") == "stale-history"
    assert int(response.headers["x-apicost-context-reclaimable-tokens"]) > 0

    # Hard rule 6: the body is the provider's, untouched.
    body = response.json()
    assert "apicost" not in response.text.lower()
    assert set(body) >= {"choices", "model"}


@pytest.mark.usefixtures("clean_all")
async def test_the_forwarded_request_is_not_rewritten(
    live_proxy: LiveServer, api_base: AsyncClient, stub_provider: LiveServer
) -> None:
    """The whole point of "advisory only".

    Asserted against what the *provider* received, not against our response —
    the only place a silent rewrite would show up.
    """
    key = await provision_account(api_base, "noverwrite@example.com")
    messages = _drifted_messages()

    async with AsyncClient(timeout=30.0) as raw:
        await raw.post(
            f"{live_proxy.url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": "gpt-4o", "messages": messages},
        )
        seen = (await raw.get(f"{stub_provider.url}/_last_request")).json()

    assert len(seen["messages"]) == len(messages), "the proxy dropped messages"
    assert seen["messages"] == messages, "the proxy altered the prompt"


@pytest.mark.usefixtures("clean_all")
async def test_a_coherent_request_gets_no_warning(
    live_proxy: LiveServer, api_base: AsyncClient
) -> None:
    key = await provision_account(api_base, "coherent@example.com")
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": _long("pandas dataframe merge join keys index")},
        {"role": "assistant", "content": _long("pandas merge dataframe keys join index columns")},
        {"role": "user", "content": _long("dataframe merge keys pandas join suffixes index")},
        {"role": "assistant", "content": _long("merge pandas dataframe join keys index suffixes")},
        {"role": "user", "content": "How do I merge two pandas dataframe objects on keys?"},
    ]

    async with AsyncClient(timeout=30.0) as raw:
        response = await raw.post(
            f"{live_proxy.url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": "gpt-4o", "messages": messages},
        )

    assert response.status_code == 200
    assert "x-apicost-context-warning" not in response.headers


@pytest.mark.usefixtures("clean_all")
async def test_the_warning_reaches_the_ledger_without_the_prompt(
    live_proxy: LiveServer, api_base: AsyncClient
) -> None:
    """Hard rule 9: the verdict is stored, the text never is."""
    key = await provision_account(api_base, "ledgerctx@example.com")

    async with AsyncClient(timeout=30.0) as raw:
        await raw.post(
            f"{live_proxy.url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": "gpt-4o", "messages": _drifted_messages()},
        )

    await drain_ledger()

    async with get_admin_engine().connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT context_warning, context_reclaimable_tokens, context_message_count "
                    "FROM requests_log ORDER BY timestamp DESC LIMIT 1"
                )
            )
        ).one()

    assert row.context_warning is True
    assert row.context_reclaimable_tokens > 0
    assert row.context_message_count == 6

    async with get_admin_engine().connect() as conn:
        columns = (
            await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'requests_log'"
                )
            )
        ).scalars()
        names = set(columns)

    assert not {"prompt", "prompt_text", "messages", "response_text"} & names


# -- UC-27: the suggestion endpoint -----------------------------------------


@pytest.mark.usefixtures("clean_all")
async def test_the_compress_endpoint_returns_a_candidate_and_a_saving(
    api_base: AsyncClient,
) -> None:
    await provision_account(api_base, "compress@example.com")
    auth, _ = await login(api_base, "compress@example.com")

    response = await api_base.post(
        "/advisor/compress",
        headers=auth,
        json={"body": {"model": "gpt-4o", "messages": _drifted_messages()}},
    )
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["warn"] is True
    assert body["reason"] == "STALE_HISTORY"
    assert body["reclaimable_tokens"] > 0

    suggestion = body["suggestion"]
    assert suggestion is not None
    assert suggestion["tokens_after"] < suggestion["tokens_before"]
    assert suggestion["tokens_saved"] > 0
    assert suggestion["applied"] is False, "a suggestion must never be applied"
    assert len(suggestion["messages"]) < len(_drifted_messages())
    assert suggestion["messages"][-1] == _drifted_messages()[-1]


@pytest.mark.usefixtures("clean_all")
async def test_compress_says_so_when_there_is_nothing_to_do(api_base: AsyncClient) -> None:
    await provision_account(api_base, "nocompress@example.com")
    auth, _ = await login(api_base, "nocompress@example.com")

    response = await api_base.post(
        "/advisor/compress",
        headers=auth,
        json={"body": {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}},
    )
    body = response.json()

    assert body["warn"] is False
    assert body["suggestion"] is None


@pytest.mark.usefixtures("clean_all")
async def test_compress_requires_authentication(api_base: AsyncClient) -> None:
    response = await api_base.post("/advisor/compress", json={"body": {"messages": []}})
    assert response.status_code == 401


# -- UC-26 / UC-28 reports --------------------------------------------------


@pytest.mark.usefixtures("clean_all")
async def test_the_context_report_ranks_offending_endpoints(
    live_proxy: LiveServer, api_base: AsyncClient
) -> None:
    key = await provision_account(api_base, "ctxreport@example.com")
    auth, project_id = await login(api_base, "ctxreport@example.com")

    async with AsyncClient(timeout=30.0) as raw:
        for index in range(3):
            messages = _drifted_messages()
            messages[-1]["content"] = f"How do I merge dataframes in pandas, take {index}?"
            await raw.post(
                f"{live_proxy.url}/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={"model": "gpt-4o", "messages": messages},
            )

    await drain_ledger()

    response = await api_base.get(f"/advisor/context?project_id={project_id}", headers=auth)
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["warned_requests"] >= 1
    assert body["total_requests"] >= 1
    assert 0 < body["warned_fraction"] <= 1
    assert body["by_endpoint"]
    assert body["by_endpoint"][0]["avg_reclaimable_tokens"] > 0


@pytest.mark.usefixtures("clean_all")
async def test_token_heavy_ranks_by_average_not_volume(
    live_proxy: LiveServer, api_base: AsyncClient
) -> None:
    """UC-28. Ranking by total would just name the busiest endpoint."""
    key = await provision_account(api_base, "tokenheavy@example.com")
    auth, project_id = await login(api_base, "tokenheavy@example.com")

    async with AsyncClient(timeout=30.0) as raw:
        headers = {"Authorization": f"Bearer {key}"}
        # One big embeddings call, several small chat calls. Chat has more
        # traffic; embeddings has the heavier shape.
        await raw.post(
            f"{live_proxy.url}/v1/embeddings",
            headers=headers,
            json={"model": "text-embedding-3-small", "input": _long("payload", 2000)},
        )
        for index in range(4):
            await raw.post(
                f"{live_proxy.url}/v1/chat/completions",
                headers=headers,
                json={"model": "gpt-4o", "messages": [{"role": "user", "content": f"hi {index}"}]},
            )

    await drain_ledger()

    response = await api_base.get(f"/advisor/token-heavy?project_id={project_id}", headers=auth)
    assert response.status_code == 200, response.text

    rows = response.json()
    assert len(rows) >= 2

    averages = [r["avg_tokens_total"] for r in rows]
    assert averages == sorted(averages, reverse=True), "not ranked by average"
    assert "embeddings" in rows[0]["endpoint"], f"ranked by volume, not shape: {rows}"


@pytest.mark.usefixtures("clean_all")
async def test_one_users_advisor_reports_are_invisible_to_another(
    api_base: AsyncClient,
) -> None:
    await provision_account(api_base, "adv-a@example.com")
    _, project_a = await login(api_base, "adv-a@example.com")

    await provision_account(api_base, "adv-b@example.com")
    auth_b, _ = await login(api_base, "adv-b@example.com")

    for path in ("/advisor/context", "/advisor/token-heavy"):
        response = await api_base.get(f"{path}?project_id={project_a}", headers=auth_b)
        assert response.status_code == 404, f"{path} leaked across users"
