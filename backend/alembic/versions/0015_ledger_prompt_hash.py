"""Prompt hash on the ledger

Fixes UC-32's slow path. `anomaly/scan.py` has always selected
`requests_log.prompt_hash`, and the column has never existed — so
`scan_usage_patterns` raised on every run, was swallowed by its own exception
handler, and logged a warning nobody read. It had no test coverage, which is
why that survived.

A hash, never the prompt. Hard rule 9 is about raw prompt text; a SHA-256
digest of a normalized prompt is not text and cannot be reversed into one. It
is the same digest the cache already stores, so this adds no new class of data
to the system.

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE requests_log ADD COLUMN prompt_hash text")
    # No index. It is read by a per-project scan that already filters on
    # (project_id, timestamp), and it is never looked up on its own.


def downgrade() -> None:
    op.execute("ALTER TABLE requests_log DROP COLUMN IF EXISTS prompt_hash")
