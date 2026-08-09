"""Stripe subscriptions and webhook idempotency

BUILD_SPEC §4 P10. `billing_events` exists so a webhook Stripe retries — which
it will, on any non-2xx and on its own schedule — cannot apply the same change
twice.

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN stripe_customer_id text")
    op.execute("ALTER TABLE users ADD COLUMN stripe_subscription_id text")
    op.execute("ALTER TABLE users ADD COLUMN plan_status text NOT NULL DEFAULT 'active'")
    op.execute("ALTER TABLE users ADD COLUMN plan_renews_at timestamptz")

    # Unique: two users sharing a Stripe customer would make every webhook
    # ambiguous about whose plan to change.
    op.execute(
        "CREATE UNIQUE INDEX ix_users_stripe_customer ON users (stripe_customer_id) "
        "WHERE stripe_customer_id IS NOT NULL"
    )

    op.execute(
        """
        CREATE TABLE billing_events (
            id           text        NOT NULL,
            event_type   text        NOT NULL,
            user_id      text,
            payload_hash text        NOT NULL,
            processed_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_billing_events PRIMARY KEY (id)
        )
        """
    )
    # `id` is Stripe's own event id, and the primary key is the idempotency
    # mechanism: a duplicate delivery fails the insert, which is how we know to
    # skip it. Doing this with a SELECT-then-INSERT would leave a window where
    # two concurrent deliveries both see nothing and both apply.

    op.execute(
        "CREATE INDEX ix_billing_events_processed ON billing_events (processed_at DESC)"
    )

    # No RLS and no grant to apicost_app. These are our billing records, not
    # user-scoped data, and nothing on the request path reads them.
    op.execute("ALTER TABLE billing_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE billing_events FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS billing_events")
    op.execute("DROP INDEX IF EXISTS ix_users_stripe_customer")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS plan_renews_at")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS plan_status")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS stripe_subscription_id")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS stripe_customer_id")
