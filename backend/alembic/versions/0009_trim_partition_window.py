"""Trim the ledger partition window

Migration 0005 provisioned 18 months of partitions behind the deploy date. That
was an over-correction for the DEFAULT-partition bug it was fixing: every older
row was landing in DEFAULT and could not be pruned, so I widened the window far
past what any real backfill needed.

The cost turned out to be real. 22 partitions with five indexes each is 139
relations, and operations that touch the whole table pay for all of them —
a `TRUNCATE` of the test fixture's tables measured **6.7 seconds** on an idle
database, which took the test suite from ~90 seconds to over eight minutes. It
also bloats `pg_class`, which every query's planner has to read.

This drops the **empty** partitions outside a tighter window. Partitions holding
rows are never touched, whatever their age — losing a user's usage history to a
tidy-up would be indefensible. `ensure_partitions` now maintains the narrower
window going forward, and a backfill older than it still works: rows land in
DEFAULT exactly as before, and the worker's next pass provisions what is needed.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-08
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KEEP_MONTHS_BACK = 2
KEEP_MONTHS_AHEAD = 3


def upgrade() -> None:
    op.execute(
        f"""
        DO $$
        DECLARE
            part   record;
            n_rows bigint;
            lower_bound date := date_trunc('month', now())::date
                                - interval '{KEEP_MONTHS_BACK} months';
            upper_bound date := date_trunc('month', now())::date
                                + interval '{KEEP_MONTHS_AHEAD} months';
            part_month  date;
        BEGIN
            FOR part IN
                SELECT c.relname
                FROM pg_class c
                JOIN pg_inherits i ON i.inhrelid = c.oid
                JOIN pg_class parent ON parent.oid = i.inhparent
                WHERE parent.relname = 'requests_log'
                  AND c.relname ~ '^requests_log_[0-9]{{4}}_[0-9]{{2}}$'
            LOOP
                part_month := to_date(substring(part.relname from 14), 'YYYY_MM');

                IF part_month >= lower_bound AND part_month < upper_bound THEN
                    CONTINUE;
                END IF;

                -- Only ever drop an empty partition. A partition with rows is
                -- somebody's usage history.
                EXECUTE format('SELECT count(*) FROM %I', part.relname) INTO n_rows;
                IF n_rows = 0 THEN
                    EXECUTE format('DROP TABLE %I', part.relname);
                END IF;
            END LOOP;
        END
        $$
        """
    )


def downgrade() -> None:
    """Recreating empty partitions is what ``ensure_partitions`` does anyway.

    Nothing to undo: no data was moved or removed, so the worker's next pass
    restores whatever window is configured.
    """
