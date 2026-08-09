"""Weekly digest preferences and unsubscribe

UC-38. The unsubscribe token is per user and stable, so a link in an email
sent three months ago still works — an unsubscribe that expires is not an
unsubscribe.

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN digest_enabled boolean NOT NULL DEFAULT true")
    # A *server* default, not just an ORM one. Every user must have a working
    # unsubscribe token, and that guarantee should not depend on every insert
    # path remembering to supply one — the seeder, the test fixtures and any
    # future raw INSERT all bypass the ORM. Postgres can always produce it.
    op.execute(
        "ALTER TABLE users ADD COLUMN digest_unsubscribe_token text "
        "DEFAULT (replace(gen_random_uuid()::text, '-', '') || "
        "replace(gen_random_uuid()::text, '-', ''))"
    )
    op.execute("ALTER TABLE users ADD COLUMN last_digest_sent_at timestamptz")

    # Backfill existing rows, then make it required. A user with no token could
    # be sent an email with no working unsubscribe link, which is both rude and
    # illegal in most of the places this would ship.
    # Two gen_random_uuid()s rather than gen_random_bytes(): the latter needs
    # the pgcrypto extension, and installing an extension for a one-time
    # backfill is a permanent cost for a momentary need. gen_random_uuid is
    # built in from PG13 and draws on a strong random source, so two of them
    # concatenated give 244 bits of entropy in 64 hex characters.
    op.execute(
        "UPDATE users SET digest_unsubscribe_token = "
        "replace(gen_random_uuid()::text, '-', '') || "
        "replace(gen_random_uuid()::text, '-', '') "
        "WHERE digest_unsubscribe_token IS NULL"
    )
    op.execute("ALTER TABLE users ALTER COLUMN digest_unsubscribe_token SET NOT NULL")
    op.execute(
        "CREATE UNIQUE INDEX ix_users_unsubscribe_token ON users (digest_unsubscribe_token)"
    )

    # The digest runs hourly and asks "who is due, in their own timezone".
    # Without this it is a sequential scan of every user, every hour, forever.
    op.execute(
        "CREATE INDEX ix_users_digest_due ON users (last_digest_sent_at) WHERE digest_enabled"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_users_digest_due")
    op.execute("DROP INDEX IF EXISTS ix_users_unsubscribe_token")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS last_digest_sent_at")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS digest_unsubscribe_token")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS digest_enabled")
