"""Enable the pgvector extension

The semantic cache stores a ``vector(384)`` embedding per entry (BUILD_SPEC §7)
and searches it with an HNSW index under cosine distance (§6.3). The extension
has to exist before any of that, so it gets its own first migration rather than
riding along with the initial schema in P1.

Revision ID: 0001
Revises:
Create Date: 2026-08-04
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def downgrade() -> None:
    # Reversible per BUILD_SPEC §11.3. Safe because nothing depends on the
    # extension yet at this revision; from P1 on, the cache_entries migration
    # is dropped before this one runs.
    op.execute("DROP EXTENSION IF EXISTS vector")
