"""Long-context advisory flags on the ledger

UC-26. Recorded per request so the dashboard can rank offenders without
re-reading prompts it is not allowed to store (hard rule 9).

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Counts and a boolean, never text. The advisory is computed on the request
    # path where the prompt is in memory anyway; what survives is the verdict,
    # so a project that has not opted into storing raw content still gets the
    # UC-26 warning and the UC-28 report.
    op.execute("ALTER TABLE requests_log ADD COLUMN context_warning boolean NOT NULL DEFAULT false")
    op.execute("ALTER TABLE requests_log ADD COLUMN context_reclaimable_tokens integer")
    op.execute("ALTER TABLE requests_log ADD COLUMN context_message_count integer")

    # Partial: the warning is the rare case, and the index only ever serves
    # queries looking for it. On a table this size that is the difference
    # between an index the size of the table and one the size of the problem.
    op.execute(
        "CREATE INDEX ix_requests_log_context_warning ON requests_log "
        "(project_id, timestamp DESC) WHERE context_warning"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_requests_log_context_warning")
    op.execute("ALTER TABLE requests_log DROP COLUMN IF EXISTS context_message_count")
    op.execute("ALTER TABLE requests_log DROP COLUMN IF EXISTS context_reclaimable_tokens")
    op.execute("ALTER TABLE requests_log DROP COLUMN IF EXISTS context_warning")
