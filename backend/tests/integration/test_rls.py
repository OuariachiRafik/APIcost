"""Row-level security — BUILD_SPEC §5 "Data isolation", CLAUDE.md hard rule 5.

These tests go around the application entirely and issue SQL directly, because
that is the whole point: the application filter is the first control and RLS is
the second, and only a test that bypasses the first can tell you the second
works. If someone deletes a ``WHERE user_id = ...`` clause tomorrow, these are
the tests that still fail.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from apicost.db.session import get_engine, session_scope
from tests.integration.conftest import register

pytestmark = pytest.mark.integration


async def _seed_two_users(api_client: AsyncClient) -> tuple[str, str]:
    alice = await register(api_client, "rls-alice@example.com")
    bob = await register(api_client, "rls-bob@example.com")

    for user in (alice, bob):
        await api_client.post("/projects", headers=user.auth, json={"name": "p"})
        await api_client.post(
            "/keys",
            headers=user.auth,
            json={"provider": "openai", "api_key": f"sk-proj-{user.email}-000000000"},
        )

    async with get_engine().connect() as conn:
        rows = await conn.execute(text("SELECT id, email FROM users ORDER BY email"))
        ids = {email: user_id for user_id, email in rows}

    return ids["rls-alice@example.com"], ids["rls-bob@example.com"]


@pytest.mark.usefixtures("clean_db")
async def test_scoped_session_sees_only_its_own_rows(api_client: AsyncClient) -> None:
    alice_id, bob_id = await _seed_two_users(api_client)

    async with session_scope(user_id=alice_id) as session:
        projects = (await session.execute(text("SELECT user_id FROM projects"))).scalars().all()
        keys = (await session.execute(text("SELECT user_id FROM provider_keys"))).scalars().all()

    assert projects == [alice_id]
    assert keys == [alice_id]
    assert bob_id not in projects


@pytest.mark.usefixtures("clean_db")
async def test_querying_as_alice_for_bobs_id_returns_nothing(
    api_client: AsyncClient,
) -> None:
    """The §5 test: "query as user A with user B's IDs, expect zero rows"."""
    alice_id, bob_id = await _seed_two_users(api_client)

    async with session_scope(user_id=alice_id) as session:
        result = await session.execute(
            text("SELECT count(*) FROM projects WHERE user_id = :other"), {"other": bob_id}
        )
        assert result.scalar() == 0

        result = await session.execute(
            text("SELECT count(*) FROM provider_keys WHERE user_id = :other"),
            {"other": bob_id},
        )
        assert result.scalar() == 0


@pytest.mark.usefixtures("clean_db")
async def test_unscoped_session_sees_no_user_scoped_rows(
    api_client: AsyncClient,
) -> None:
    """Without app.user_id, the strict policies deny everything.

    Failing closed matters: a code path that forgets to scope returns empty
    results rather than every tenant's data.
    """
    await _seed_two_users(api_client)

    async with session_scope() as session:
        for table in ("projects", "provider_keys", "proxy_keys"):
            count = (await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar()
            assert count == 0, f"{table} was readable without a scoped session"


@pytest.mark.usefixtures("clean_db")
async def test_rls_blocks_writing_a_row_for_another_user(
    api_client: AsyncClient,
) -> None:
    """WITH CHECK must stop a forged user_id, not just filter reads."""
    alice_id, bob_id = await _seed_two_users(api_client)

    with pytest.raises(Exception) as excinfo:
        async with session_scope(user_id=alice_id) as session:
            await session.execute(
                text(
                    "INSERT INTO projects (id, user_id, name, created_at, cache_enabled, "
                    "similarity_threshold, cache_ttl_seconds, routing_enabled, "
                    "escalation_enabled, store_raw_content) "
                    "VALUES ('forged', :bob, 'stolen', now(), true, 0.95, 86400, "
                    "false, true, false)"
                ),
                {"bob": bob_id},
            )

    assert "row-level security" in str(excinfo.value).lower()


@pytest.mark.usefixtures("clean_db")
async def test_rls_blocks_updating_another_users_row(api_client: AsyncClient) -> None:
    alice_id, bob_id = await _seed_two_users(api_client)

    async with session_scope(user_id=alice_id) as session:
        result = await session.execute(
            text("UPDATE projects SET name = 'hijacked' WHERE user_id = :bob"),
            {"bob": bob_id},
        )
        # RLS filters the rows the UPDATE can even see, so it matches nothing.
        assert result.rowcount == 0  # type: ignore[attr-defined]

    async with session_scope(user_id=bob_id) as session:
        name = (await session.execute(text("SELECT name FROM projects"))).scalar()
        assert name == "p"


@pytest.mark.usefixtures("clean_db")
async def test_policies_survive_connection_reuse(api_client: AsyncClient) -> None:
    """Regression: a committed ``set_config`` leaves the GUC as '', not unset.

    A pooled connection that has already served a scoped transaction reports
    ``current_setting('app.user_id', true) = ''``. Policies written with a bare
    ``IS NULL`` check then hide every row, so login breaks on the *second*
    request a connection serves and works fine on the first — which is why the
    fix is ``NULLIF(..., '')`` and why this test reuses the pool deliberately.
    """
    alice_id, _ = await _seed_two_users(api_client)

    # Dirty the pooled connection with a scoped transaction, then commit.
    async with session_scope(user_id=alice_id) as session:
        await session.execute(text("SELECT 1"))

    # An unscoped session on that same pooled connection must still be able to
    # read `users` — this is the pre-authentication path that login depends on.
    async with session_scope() as session:
        count = (await session.execute(text("SELECT count(*) FROM users"))).scalar()
        assert count == 2, "users became invisible after connection reuse"

    # And the API itself must keep working across sequential requests.
    for _ in range(3):
        response = await api_client.post(
            "/auth/login",
            json={"email": "rls-alice@example.com", "password": "a-very-long-password"},
        )
        assert response.status_code == 200


@pytest.mark.usefixtures("clean_db")
async def test_force_row_level_security_is_enabled_everywhere() -> None:
    """ENABLE alone is not enough — the app connects as the table owner.

    Without FORCE, every policy in the migration would exist and do nothing.
    """
    async with get_engine().connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relname IN "
                "('users','refresh_tokens','provider_keys','projects','proxy_keys')"
            )
        )
        table_flags = {name: (enabled, forced) for name, enabled, forced in rows}

    assert len(table_flags) == 5
    for table, (enabled, forced) in table_flags.items():
        assert enabled, f"RLS is not enabled on {table}"
        assert forced, f"RLS is not FORCEd on {table} — the owner bypasses it"
