"""Daily usage rollups

Pre-aggregated spend, so the dashboard reads hundreds of rows instead of
hundreds of thousands. See docs/adr/0006-usage-rollups.md for the measurements
that forced this.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-05
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_USER_SQL = "NULLIF(current_setting('app.user_id', true), '')"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE usage_rollup_daily (
            user_id                  text        NOT NULL,
            project_id               text        NOT NULL,
            day                      date        NOT NULL,
            model_used               text        NOT NULL,
            endpoint                 text        NOT NULL,
            provider                 text        NOT NULL,
            requests                 bigint      NOT NULL DEFAULT 0,
            tokens_in                bigint      NOT NULL DEFAULT 0,
            tokens_out               bigint      NOT NULL DEFAULT 0,
            cost_usd                 numeric(24, 10) NOT NULL DEFAULT 0,
            would_have_been_usd      numeric(24, 10) NOT NULL DEFAULT 0,
            cache_hits               bigint      NOT NULL DEFAULT 0,
            cache_savings_usd        numeric(24, 10) NOT NULL DEFAULT 0,
            routing_savings_usd      numeric(24, 10) NOT NULL DEFAULT 0,
            errors                   bigint      NOT NULL DEFAULT 0,
            latency_ms_sum           double precision NOT NULL DEFAULT 0,
            updated_at               timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_usage_rollup_daily
                PRIMARY KEY (user_id, project_id, day, model_used, endpoint, provider)
        )
        """
    )
    op.execute("CREATE INDEX ix_usage_rollup_user_day ON usage_rollup_daily (user_id, day DESC)")

    op.execute(
        """
        CREATE TABLE token_bucket_rollup_daily (
            user_id       text   NOT NULL,
            project_id    text   NOT NULL,
            day           date   NOT NULL,
            bucket_index  int    NOT NULL,
            requests      bigint NOT NULL DEFAULT 0,
            cost_usd      numeric(24, 10) NOT NULL DEFAULT 0,
            tokens_total  bigint NOT NULL DEFAULT 0,
            CONSTRAINT pk_token_bucket_rollup_daily
                PRIMARY KEY (user_id, project_id, day, bucket_index)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_token_bucket_rollup_user_day "
        "ON token_bucket_rollup_daily (user_id, day DESC)"
    )

    # Derived from user-scoped data, so it inherits the same isolation.
    for table in ("usage_rollup_daily", "token_bucket_rollup_daily"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {table}_user_isolation ON {table}
            USING (user_id = {CURRENT_USER_SQL})
            WITH CHECK (user_id = {CURRENT_USER_SQL})
            """
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON {table} TO apicost_app")


def downgrade() -> None:
    for table in ("usage_rollup_daily", "token_bucket_rollup_daily"):
        op.execute(f"DROP POLICY IF EXISTS {table}_user_isolation ON {table}")
        op.execute(f"DROP TABLE IF EXISTS {table}")
