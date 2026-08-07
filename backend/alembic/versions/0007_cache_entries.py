"""Semantic cache entries

The vector store behind UC-20..UC-25 (BUILD_SPEC §6.3, §7).

Two details worth stating:

* **HNSW with `vector_cosine_ops`.** The lookup ranks by cosine distance, so
  the index has to be built for the same operator or Postgres ignores it and
  falls back to a sequential scan — which at cache size would blow the 30 ms
  hit budget silently.

* **`response_payload` is encrypted.** The semantic cache is the one place
  raw response bodies are stored at all (BUILD_SPEC §0.4), because replaying a
  response requires having it. Each row carries its own KMS-wrapped data key,
  so the table is inert without the KMS — the same position `provider_keys`
  is in.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-07
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_USER_SQL = "NULLIF(current_setting('app.user_id', true), '')"
EMBEDDING_DIMENSIONS = 384


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE cache_entries (
            id                text        NOT NULL,
            user_id           text        NOT NULL,
            project_id        text        NOT NULL,
            embedding_vector  vector({EMBEDDING_DIMENSIONS}) NOT NULL,
            prompt_hash       text        NOT NULL,
            response_payload  bytea       NOT NULL,
            wrapped_data_key  bytea       NOT NULL,
            nonce             bytea       NOT NULL,
            model_used        text        NOT NULL,
            tokens_in         integer     NOT NULL DEFAULT 0,
            tokens_out        integer     NOT NULL DEFAULT 0,
            created_at        timestamptz NOT NULL DEFAULT now(),
            ttl_expires_at    timestamptz NOT NULL,
            hit_count         integer     NOT NULL DEFAULT 0,
            last_hit_at       timestamptz,
            CONSTRAINT pk_cache_entries PRIMARY KEY (id)
        )
        """
    )

    # Ranked by cosine distance, so the index must be cosine too.
    op.execute(
        "CREATE INDEX ix_cache_entries_embedding ON cache_entries "
        "USING hnsw (embedding_vector vector_cosine_ops)"
    )

    # The exact-match path: an identical prompt should never touch the vector
    # index at all (§6.3).
    op.execute(
        "CREATE INDEX ix_cache_entries_prompt_hash ON cache_entries "
        "(user_id, project_id, prompt_hash)"
    )

    # Expiry sweeps and per-project invalidation (UC-22, UC-23).
    op.execute(
        "CREATE INDEX ix_cache_entries_user_project_ttl ON cache_entries "
        "(user_id, project_id, ttl_expires_at)"
    )

    op.execute("ALTER TABLE cache_entries ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE cache_entries FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY cache_entries_user_isolation ON cache_entries
        USING (user_id = {CURRENT_USER_SQL})
        WITH CHECK (user_id = {CURRENT_USER_SQL})
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON cache_entries TO apicost_app")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS cache_entries_user_isolation ON cache_entries")
    op.execute("DROP TABLE IF EXISTS cache_entries")
