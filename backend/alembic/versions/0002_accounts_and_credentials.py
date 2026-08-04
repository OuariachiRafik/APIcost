"""Accounts, credentials, and projects, with row-level security

Creates the P1 tables from BUILD_SPEC §7 and enables RLS on every user-scoped
one.

Three details that are easy to get wrong and silently lose tenant isolation:

* ``FORCE ROW LEVEL SECURITY`` is required, not just ``ENABLE``. The
  application connects as the table owner, and an owner bypasses plain RLS —
  the policies would exist, pass review, and do nothing.

* Policies compare against ``NULLIF(current_setting('app.user_id', true), '')``.
  Both halves matter. The ``true`` makes a missing setting return NULL rather
  than raising. The ``NULLIF`` handles what happens *after* a transaction-local
  ``set_config`` commits: the GUC does not go back to unset, it goes back to
  the **empty string**. On a pooled connection that has already served one
  scoped transaction, a bare ``IS NULL`` test is therefore false, and the
  pre-authentication policies below would hide every row — breaking login on
  the second request a connection serves, and only then.

* ``refresh_tokens`` needs a **readable-when-unscoped** policy, and that is
  deliberate rather than an oversight. Token rotation looks a row up by its
  hash *before* anyone is authenticated — that lookup is how we learn who the
  caller is. Writes stay strict: ``WITH CHECK`` always demands a matching
  ``app.user_id``, so the auth handlers must scope the session before
  inserting or revoking, which they do. The read exposure is bounded to
  hashes, ids, and timestamps; no credential is recoverable from this table.

  The other three tables are strict in both directions, since nothing touches
  them before authentication.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STRICT_TABLES = ("provider_keys", "projects", "proxy_keys")

CURRENT_USER_SQL = "NULLIF(current_setting('app.user_id', true), '')"
"""The caller's user id, or NULL when the session is unscoped.

Never use a bare ``current_setting`` here — see the note in the module
docstring about the empty string a committed ``set_config`` leaves behind.
"""


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("auth_provider_id", sa.Text(), nullable=True),
        sa.Column("plan_id", sa.Text(), nullable=False, server_default="free"),
        sa.Column("timezone", sa.Text(), nullable=False, server_default="UTC"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    # Case-insensitive uniqueness without depending on the citext extension.
    op.create_index("ix_users_email_lower", "users", [sa.text("lower(email)")], unique=True)

    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("family_id", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column(
            "issued_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_refresh_tokens_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_refresh_tokens"),
        sa.UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.create_index("ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"])
    op.create_index("ix_refresh_tokens_user_family", "refresh_tokens", ["user_id", "family_id"])

    op.create_table(
        "provider_keys",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("encrypted_key", sa.LargeBinary(), nullable=False),
        sa.Column("wrapped_data_key", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("key_last4", sa.String(length=4), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_provider_keys_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_provider_keys"),
    )
    op.create_index("ix_provider_keys_user_id", "provider_keys", ["user_id"])
    op.create_index("ix_provider_keys_user_provider", "provider_keys", ["user_id", "provider"])

    op.create_table(
        "projects",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cache_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("similarity_threshold", sa.Float(), nullable=False, server_default="0.95"),
        sa.Column("cache_ttl_seconds", sa.Integer(), nullable=False, server_default="86400"),
        sa.Column("routing_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "escalation_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "store_raw_content", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_projects_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_projects"),
    )
    op.create_index("ix_projects_user_id", "projects", ["user_id"])
    op.create_index("ix_projects_user_created", "projects", ["user_id", "created_at"])

    op.create_table(
        "proxy_keys",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("proxy_key_hash", sa.Text(), nullable=False),
        sa.Column("key_last4", sa.String(length=4), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_proxy_keys_user_id_users", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_proxy_keys_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_proxy_keys"),
        sa.UniqueConstraint("proxy_key_hash", name="uq_proxy_keys_proxy_key_hash"),
    )
    op.create_index("ix_proxy_keys_user_id", "proxy_keys", ["user_id"])
    op.create_index("ix_proxy_keys_project_id", "proxy_keys", ["project_id"])
    op.create_index("ix_proxy_keys_project_revoked", "proxy_keys", ["project_id", "revoked_at"])

    # -- Row-level security ------------------------------------------------
    for table in STRICT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {table}_user_isolation ON {table}
            USING (user_id = {CURRENT_USER_SQL})
            WITH CHECK (user_id = {CURRENT_USER_SQL})
            """
        )

    # Readable pre-authentication (rotation looks up by hash to learn who the
    # caller is); every write still demands a scoped session.
    op.execute("ALTER TABLE refresh_tokens ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE refresh_tokens FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY refresh_tokens_user_isolation ON refresh_tokens
        USING (
            {CURRENT_USER_SQL} IS NULL
            OR user_id = {CURRENT_USER_SQL}
        )
        WITH CHECK (user_id = {CURRENT_USER_SQL})
        """
    )

    # `users` is scoped by its own id. Signup and login run unscoped by
    # necessity — there is no user yet, or we are still working out which one.
    op.execute("ALTER TABLE users ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE users FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY users_self_isolation ON users
        USING (
            {CURRENT_USER_SQL} IS NULL
            OR id = {CURRENT_USER_SQL}
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS users_self_isolation ON users")
    op.execute("DROP POLICY IF EXISTS refresh_tokens_user_isolation ON refresh_tokens")
    for table in STRICT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_user_isolation ON {table}")

    op.drop_table("proxy_keys")
    op.drop_table("projects")
    op.drop_table("provider_keys")
    op.drop_table("refresh_tokens")
    op.drop_index("ix_users_email_lower", table_name="users")
    op.drop_table("users")
