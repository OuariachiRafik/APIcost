"""Advisor recommendations

UC-35 (downgrades), UC-36 (break-even), UC-37 (projected dollar impact on
every recommendation).

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_USER_SQL = "NULLIF(current_setting('app.user_id', true), '')"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE advisor_recommendations (
            id                     text        NOT NULL,
            user_id                text        NOT NULL,
            project_id             text        NOT NULL,
            kind                   text        NOT NULL,
            title                  text        NOT NULL,
            detail                 jsonb       NOT NULL DEFAULT '{}'::jsonb,
            projected_savings_usd  numeric(14, 6) NOT NULL DEFAULT 0,
            confidence             text        NOT NULL DEFAULT 'low',
            sample_size            integer     NOT NULL DEFAULT 0,
            status                 text        NOT NULL DEFAULT 'open',
            generated_at           timestamptz NOT NULL DEFAULT now(),
            dismissed_at           timestamptz,
            CONSTRAINT pk_advisor_recommendations PRIMARY KEY (id),
            CONSTRAINT ck_advisor_kind CHECK (kind IN ('downgrade', 'breakeven', 'context')),
            CONSTRAINT ck_advisor_confidence CHECK (confidence IN ('low', 'medium', 'high')),
            CONSTRAINT ck_advisor_status CHECK (status IN ('open', 'adopted', 'dismissed')),
            CONSTRAINT fk_advisor_project FOREIGN KEY (project_id)
                REFERENCES projects (id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_advisor_project_open ON advisor_recommendations "
        "(project_id, projected_savings_usd DESC) WHERE status = 'open'"
    )
    # The nightly job replaces a project's open recommendations wholesale, so
    # it needs to find them by (project, kind) cheaply.
    op.execute(
        "CREATE INDEX ix_advisor_project_kind ON advisor_recommendations (project_id, kind)"
    )

    op.execute("ALTER TABLE advisor_recommendations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE advisor_recommendations FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY advisor_user_isolation ON advisor_recommendations
        USING (user_id = {CURRENT_USER_SQL})
        WITH CHECK (user_id = {CURRENT_USER_SQL})
        """
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON advisor_recommendations TO apicost_app"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS advisor_user_isolation ON advisor_recommendations")
    op.execute("DROP TABLE IF EXISTS advisor_recommendations")
