"""User routing rules

UC-15 (override) and UC-19 (exclude). Evaluated before the classifier and
absolute — see routing/rules.py for why.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-07
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_USER_SQL = "NULLIF(current_setting('app.user_id', true), '')"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE routing_rules (
            id              text        NOT NULL,
            user_id         text        NOT NULL,
            project_id      text        NOT NULL,
            rule_type       text        NOT NULL,
            match_condition jsonb       NOT NULL DEFAULT '{}'::jsonb,
            target_model    text,
            priority        integer     NOT NULL DEFAULT 0,
            is_active       boolean     NOT NULL DEFAULT true,
            created_at      timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_routing_rules PRIMARY KEY (id),
            CONSTRAINT ck_routing_rules_type CHECK (rule_type IN ('override', 'exclude')),
            -- An override with no target is not a rule, it is a mistake. Catch
            -- it here rather than discovering it on the request path.
            CONSTRAINT ck_routing_rules_target CHECK (
                rule_type <> 'override' OR target_model IS NOT NULL
            ),
            CONSTRAINT fk_routing_rules_project FOREIGN KEY (project_id)
                REFERENCES projects (id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_routing_rules_project_active ON routing_rules "
        "(project_id, is_active, priority DESC)"
    )

    op.execute("ALTER TABLE routing_rules ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE routing_rules FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY routing_rules_user_isolation ON routing_rules
        USING (user_id = {CURRENT_USER_SQL})
        WITH CHECK (user_id = {CURRENT_USER_SQL})
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON routing_rules TO apicost_app")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS routing_rules_user_isolation ON routing_rules")
    op.execute("DROP TABLE IF EXISTS routing_rules")
