"""The usage ledger, partitioned monthly

``requests_log`` is append-only and grows with every proxied request, so it is
range-partitioned on ``timestamp`` (BUILD_SPEC §7). Two consequences worth
knowing:

* The primary key must contain the partition key, hence ``(id, timestamp)``
  rather than ``id`` alone. Postgres will not accept a unique constraint on a
  partitioned table that omits it.
* Indexes declared on the parent propagate to every partition, existing and
  future, so the dashboard's access patterns only need declaring once.

A ``DEFAULT`` partition catches anything outside the named months. Without it
an insert for an unprovisioned month fails outright — which on this path would
mean a dropped ledger row every time partition creation fell behind. The worker
provisions ahead of time (``worker/tasks.py``); the default partition is what
makes falling behind survivable rather than lossy.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-05
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONTHS_AHEAD = 3


def _month_bounds(year: int, month: int) -> tuple[str, str]:
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start.isoformat(), end.isoformat()


def partition_name(year: int, month: int) -> str:
    return f"requests_log_{year:04d}_{month:02d}"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE requests_log (
            id                        text        NOT NULL,
            timestamp                 timestamptz NOT NULL DEFAULT now(),
            user_id                   text        NOT NULL,
            project_id                text        NOT NULL,
            request_id                text        NOT NULL,
            endpoint                  text        NOT NULL,
            provider                  text        NOT NULL,
            model_requested           text        NOT NULL,
            model_used                text        NOT NULL,
            tokens_in                 integer     NOT NULL DEFAULT 0,
            tokens_out                integer     NOT NULL DEFAULT 0,
            tokens_estimated          boolean     NOT NULL DEFAULT false,
            cost_usd                  numeric(20, 10) NOT NULL DEFAULT 0,
            cost_would_have_been_usd  numeric(20, 10),
            latency_ms                double precision NOT NULL DEFAULT 0,
            ttft_ms                   double precision,
            itl_ms                    double precision,
            tps                       double precision,
            cache_hit                 boolean     NOT NULL DEFAULT false,
            cache_similarity          double precision,
            routed                    boolean     NOT NULL DEFAULT false,
            routing_reason_code       text,
            routing_model_version     text,
            escalation_triggered      boolean     NOT NULL DEFAULT false,
            status                    integer     NOT NULL DEFAULT 200,
            error_code                text,
            streamed                  boolean     NOT NULL DEFAULT false,
            CONSTRAINT pk_requests_log PRIMARY KEY (id, timestamp)
        ) PARTITION BY RANGE (timestamp)
        """
    )

    # The queries the dashboard actually runs (BUILD_SPEC §7).
    op.execute(
        "CREATE INDEX ix_requests_log_user_timestamp ON requests_log (user_id, timestamp DESC)"
    )
    op.execute(
        "CREATE INDEX ix_requests_log_project_timestamp "
        "ON requests_log (project_id, timestamp DESC)"
    )
    op.execute(
        "CREATE INDEX ix_requests_log_user_model_timestamp "
        "ON requests_log (user_id, model_used, timestamp)"
    )
    op.execute("CREATE INDEX ix_requests_log_request_id ON requests_log (request_id)")

    today = date.today()
    year, month = today.year, today.month
    for _ in range(MONTHS_AHEAD + 1):
        start, end = _month_bounds(year, month)
        op.execute(
            f"CREATE TABLE {partition_name(year, month)} PARTITION OF requests_log "
            f"FOR VALUES FROM ('{start}') TO ('{end}')"
        )
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)

    op.execute("CREATE TABLE requests_log_default PARTITION OF requests_log DEFAULT")

    op.execute("ALTER TABLE requests_log ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE requests_log FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY requests_log_user_isolation ON requests_log
        USING (user_id = NULLIF(current_setting('app.user_id', true), ''))
        WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), ''))
        """
    )

    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON requests_log TO apicost_app")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS requests_log_user_isolation ON requests_log")
    # Dropping the parent drops every partition with it.
    op.execute("DROP TABLE IF EXISTS requests_log")
