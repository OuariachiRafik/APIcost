"""Budgets, alert history, and rolling baseline checkpoints

UC-29 (budgets), UC-30 (enforcement action), UC-31/32 (anomaly alerts),
UC-34 (alert history with resolution status).

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-08
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_USER_SQL = "NULLIF(current_setting('app.user_id', true), '')"


def upgrade() -> None:
    # -- budgets (UC-29, UC-30) --------------------------------------------
    op.execute(
        """
        CREATE TABLE budgets (
            id           text        NOT NULL,
            user_id      text        NOT NULL,
            project_id   text        NOT NULL,
            period       text        NOT NULL,
            limit_usd    numeric(12, 6) NOT NULL,
            action       text        NOT NULL DEFAULT 'alert_only',
            is_active    boolean     NOT NULL DEFAULT true,
            created_at   timestamptz NOT NULL DEFAULT now(),
            updated_at   timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_budgets PRIMARY KEY (id),
            CONSTRAINT ck_budgets_period CHECK (period IN ('daily', 'weekly', 'monthly')),
            CONSTRAINT ck_budgets_action CHECK (
                action IN ('alert_only', 'soft_throttle', 'hard_stop')
            ),
            CONSTRAINT ck_budgets_limit CHECK (limit_usd > 0),
            -- One budget per period per project. Two daily budgets would make
            -- "the" limit ambiguous on the hot path, where there is no time to
            -- reconcile them.
            CONSTRAINT uq_budgets_project_period UNIQUE (project_id, period),
            CONSTRAINT fk_budgets_project FOREIGN KEY (project_id)
                REFERENCES projects (id) ON DELETE CASCADE
        )
        """
    )
    op.execute("CREATE INDEX ix_budgets_project_active ON budgets (project_id, is_active)")

    op.execute("ALTER TABLE budgets ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE budgets FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY budgets_user_isolation ON budgets
        USING (user_id = {CURRENT_USER_SQL})
        WITH CHECK (user_id = {CURRENT_USER_SQL})
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON budgets TO apicost_app")

    # -- alert_events (UC-31, UC-32, UC-34) ---------------------------------
    op.execute(
        """
        CREATE TABLE alert_events (
            id            text        NOT NULL,
            user_id       text        NOT NULL,
            project_id    text        NOT NULL,
            alert_type    text        NOT NULL,
            severity      text        NOT NULL DEFAULT 'warning',
            title         text        NOT NULL,
            detail        jsonb       NOT NULL DEFAULT '{}'::jsonb,
            status        text        NOT NULL DEFAULT 'open',
            notified_at   timestamptz,
            resolved_at   timestamptz,
            resolution    text,
            created_at    timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_alert_events PRIMARY KEY (id),
            CONSTRAINT ck_alert_events_type CHECK (
                alert_type IN (
                    'spend_spike', 'usage_pattern', 'budget_threshold',
                    'budget_exceeded', 'kill_switch'
                )
            ),
            CONSTRAINT ck_alert_events_status CHECK (
                status IN ('open', 'acknowledged', 'resolved')
            ),
            CONSTRAINT ck_alert_events_severity CHECK (
                severity IN ('info', 'warning', 'critical')
            ),
            CONSTRAINT fk_alert_events_project FOREIGN KEY (project_id)
                REFERENCES projects (id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_alert_events_project_created ON alert_events "
        "(project_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX ix_alert_events_open ON alert_events (project_id, alert_type) "
        "WHERE status = 'open'"
    )

    op.execute("ALTER TABLE alert_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE alert_events FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY alert_events_user_isolation ON alert_events
        USING (user_id = {CURRENT_USER_SQL})
        WITH CHECK (user_id = {CURRENT_USER_SQL})
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON alert_events TO apicost_app")

    # -- rolling_stats (BUILD_SPEC §6.5) ------------------------------------
    #
    # The durable copy of the Welford baseline. Redis holds the working copy;
    # this is what survives a Redis flush, which is otherwise a routine
    # operation that would silently reset every project's anomaly detection to
    # cold start and stop alerting for the next 30 minutes.
    op.execute(
        """
        CREATE TABLE rolling_stats (
            project_id        text        NOT NULL,
            metric            text        NOT NULL,
            observation_count integer     NOT NULL DEFAULT 0,
            mean              double precision NOT NULL DEFAULT 0,
            m2                double precision NOT NULL DEFAULT 0,
            window_started_at double precision NOT NULL DEFAULT 0,
            window_cost       double precision NOT NULL DEFAULT 0,
            window_requests   integer     NOT NULL DEFAULT 0,
            updated_at        timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_rolling_stats PRIMARY KEY (project_id, metric),
            CONSTRAINT fk_rolling_stats_project FOREIGN KEY (project_id)
                REFERENCES projects (id) ON DELETE CASCADE
        )
        """
    )
    # No RLS policy here, and that is deliberate: rolling_stats holds no user
    # data — counts and moments of a spend rate — and it is written by the
    # worker, which drains a stream spanning every project and therefore has no
    # single app.user_id to set. It is not granted to apicost_app at all, so
    # the application role cannot read it either. The proxy never touches it.
    op.execute("ALTER TABLE rolling_stats ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE rolling_stats FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS rolling_stats")
    op.execute("DROP POLICY IF EXISTS alert_events_user_isolation ON alert_events")
    op.execute("DROP TABLE IF EXISTS alert_events")
    op.execute("DROP POLICY IF EXISTS budgets_user_isolation ON budgets")
    op.execute("DROP TABLE IF EXISTS budgets")
