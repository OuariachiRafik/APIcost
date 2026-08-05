"""Allow pre-authentication reads of proxy_keys

``proxy_keys`` was created in 0002 with a policy strict in both directions:
reads and writes both require a matching ``app.user_id``. That is correct for
every control-plane query, and wrong for the one query the *data plane* makes.

The proxy authenticates a request by looking a key hash up in this table — that
lookup is how it learns which user the caller is. Requiring the answer before
asking the question makes it unanswerable, and the symptom is every proxied
request returning 401.

So ``proxy_keys`` now matches ``refresh_tokens``: readable when the session is
unscoped, writable only when scoped. The exposure is bounded to hashes, ids,
and timestamps — no credential is recoverable from this table, and the raw key
is unrecoverable by construction.

``projects`` stays strict. The resolver reads the proxy key first, scopes the
session to the user id it finds, and only then reads the project.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-05
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_USER_SQL = "NULLIF(current_setting('app.user_id', true), '')"


def upgrade() -> None:
    op.execute("DROP POLICY IF EXISTS proxy_keys_user_isolation ON proxy_keys")
    op.execute(
        f"""
        CREATE POLICY proxy_keys_user_isolation ON proxy_keys
        USING (
            {CURRENT_USER_SQL} IS NULL
            OR user_id = {CURRENT_USER_SQL}
        )
        WITH CHECK (user_id = {CURRENT_USER_SQL})
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS proxy_keys_user_isolation ON proxy_keys")
    op.execute(
        f"""
        CREATE POLICY proxy_keys_user_isolation ON proxy_keys
        USING (user_id = {CURRENT_USER_SQL})
        WITH CHECK (user_id = {CURRENT_USER_SQL})
        """
    )
