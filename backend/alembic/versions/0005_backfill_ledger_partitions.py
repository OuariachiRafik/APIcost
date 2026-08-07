"""Create ledger partitions for past months, not just future ones

Migration 0003 provisioned partitions from the current month forward. Nothing
created partitions for the *past*, so every row older than the deploy date fell
into the DEFAULT partition — a backfill, a data import, or simply a seeded
development database.

That is not a tidiness problem. The DEFAULT partition cannot be pruned by a
range predicate, so a query for "last 30 days" scans every historical row ever
written rather than the one or two months it needs. Measured against 841k
seeded rows, `/usage?range=30d` took **4.0 s** against a 500 ms budget, with
810k of those rows sitting in DEFAULT.

This migration:

1. detaches the DEFAULT partition;
2. creates monthly partitions covering a window either side of today;
3. re-inserts the detached rows through the parent, so they route correctly;
4. re-creates an empty DEFAULT as the safety net it was meant to be.

Step 3 rewrites every row that was in DEFAULT. On a large production table that
is a maintenance-window operation — but it only ever has to happen once, and
only for rows that should never have been there.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-05
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONTHS_BACK = 18
MONTHS_AHEAD = 3


def _shift(year: int, month: int, delta: int) -> tuple[int, int]:
    index = (year * 12 + (month - 1)) + delta
    return index // 12, index % 12 + 1


def _partition_sql(year: int, month: int) -> str:
    end_year, end_month = _shift(year, month, 1)
    return (
        f"CREATE TABLE IF NOT EXISTS requests_log_{year:04d}_{month:02d} "
        f"PARTITION OF requests_log FOR VALUES FROM "
        f"('{year:04d}-{month:02d}-01') TO ('{end_year:04d}-{end_month:02d}-01')"
    )


def upgrade() -> None:
    today = date.today()

    # 1. Detach DEFAULT so the new partitions can be created without tripping
    #    its implicit constraint.
    op.execute("ALTER TABLE requests_log DETACH PARTITION requests_log_default")

    # 2. Every month in the window.
    start_year, start_month = _shift(today.year, today.month, -MONTHS_BACK)
    for offset in range(MONTHS_BACK + MONTHS_AHEAD + 1):
        year, month = _shift(start_year, start_month, offset)
        op.execute(_partition_sql(year, month))

    # 3. Route the orphaned rows to where they belong.
    op.execute("INSERT INTO requests_log SELECT * FROM requests_log_default ON CONFLICT DO NOTHING")
    op.execute("DROP TABLE requests_log_default")

    # 4. A fresh, empty safety net.
    op.execute("CREATE TABLE requests_log_default PARTITION OF requests_log DEFAULT")

    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON requests_log TO apicost_app")


def downgrade() -> None:
    """Collapse back to a single DEFAULT partition.

    Lossless: rows are moved, not dropped.
    """
    today = date.today()

    op.execute("ALTER TABLE requests_log DETACH PARTITION requests_log_default")
    op.execute("CREATE TABLE requests_log_all AS SELECT * FROM requests_log")

    start_year, start_month = _shift(today.year, today.month, -MONTHS_BACK)
    for offset in range(MONTHS_BACK + MONTHS_AHEAD + 1):
        year, month = _shift(start_year, start_month, offset)
        op.execute(f"DROP TABLE IF EXISTS requests_log_{year:04d}_{month:02d}")

    op.execute("ALTER TABLE requests_log ATTACH PARTITION requests_log_default DEFAULT")
    op.execute("INSERT INTO requests_log SELECT * FROM requests_log_all ON CONFLICT DO NOTHING")
    op.execute("DROP TABLE requests_log_all")
