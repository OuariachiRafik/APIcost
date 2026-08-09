"""SQLAlchemy models — BUILD_SPEC §7.

Conventions that apply to every table here:

* **Primary keys are ULIDs stored as text.** Sortable by creation time and safe
  to expose, because they leak nothing but a timestamp.
* **Every user-scoped table carries ``user_id``** and has row-level security
  enabled in the migration. The application filter is the first control, RLS is
  the second, and both are required (CLAUDE.md hard rule 5).
* **No plaintext credential is ever a column.** Provider keys are stored as
  ciphertext plus a wrapped data key; proxy keys as a SHA-256 hash; passwords
  as an Argon2id digest.

P1 defines the account and credential tables. The ledger, cache, routing,
budget, stats, alert, advisor, and billing tables arrive with their phases.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from apicost.core.ids import new_id
from apicost.db.base import Base

if TYPE_CHECKING:
    pass


def _ulid() -> str:
    return new_id()


def _token() -> str:
    """A URL-safe unsubscribe token. CSPRNG, never derived from the user id."""
    return secrets.token_hex(32)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_ulid)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    """Stored lowercased. Uniqueness is enforced case-insensitively by a
    functional unique index in the migration, which is portable in a way the
    citext extension is not."""

    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    auth_provider_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    plan_id: Mapped[str] = mapped_column(Text, nullable=False, default="free")
    timezone: Mapped[str] = mapped_column(Text, nullable=False, default="UTC")

    digest_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    digest_unsubscribe_token: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default=_token,
        server_default=text(
            "replace(gen_random_uuid()::text, '-', '') || replace(gen_random_uuid()::text, '-', '')"
        ),
    )
    """Stable and per user. A link in an email sent months ago must still work;
    an unsubscribe that expires is not an unsubscribe.

    Defaulted on the server as well as here, so a raw INSERT cannot create a
    user who can never unsubscribe."""

    last_digest_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    stripe_customer_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    plan_status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    plan_renews_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    projects: Mapped[list[Project]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    provider_keys: Mapped[list[ProviderKey]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class RefreshToken(Base):
    """One row per issued refresh token.

    Rotation works by *family*: every token descends from one login. Presenting
    a token that has already been consumed means it leaked, so the entire
    family is revoked rather than just that token (BUILD_SPEC §4 P1).
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_ulid)
    user_id: Mapped[str] = mapped_column(
        Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    family_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    """SHA-256 of the raw token. The raw value is returned once and never stored."""

    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_refresh_tokens_user_family", "user_id", "family_id"),)


class ProviderKey(Base):
    """A user's own OpenAI/Anthropic/Gemini key, encrypted at rest.

    ``encrypted_key`` is AES-256-GCM ciphertext under a per-user data key;
    ``wrapped_data_key`` is that data key wrapped by the KMS master key
    (BUILD_SPEC §6.9). Neither column is useful without the KMS.
    """

    __tablename__ = "provider_keys"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_ulid)
    user_id: Mapped[str] = mapped_column(
        Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    wrapped_data_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_last4: Mapped[str] = mapped_column(String(4), nullable=False)
    """The only fragment of the key ever shown back to the user (UC-02)."""

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="provider_keys")

    __table_args__ = (Index("ix_provider_keys_user_provider", "user_id", "provider"),)


class Project(Base):
    """An isolation boundary: prod vs staging vs side-project.

    Owns its own toggles, thresholds, budgets, rules, and cache namespace.
    """

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_ulid)
    user_id: Mapped[str] = mapped_column(
        Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # -- Feature configuration (UC-14, 20, 21, 22) ------------------------
    cache_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    similarity_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.95)
    cache_ttl_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=86_400)
    routing_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    escalation_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    store_raw_content: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    """Default false. Raw prompt/response text is not persisted unless the user
    opts in per project (CLAUDE.md hard rule 9)."""

    user: Mapped[User] = relationship(back_populates="projects")
    proxy_keys: Mapped[list[ProxyKey]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_projects_user_created", "user_id", "created_at"),)


class ProxyKey(Base):
    """The credential a user's application sends to *us*.

    Stored as a SHA-256 hash only. The raw ``apc_live_...`` value is shown
    exactly once at creation and is unrecoverable afterwards (UC-05).
    """

    __tablename__ = "proxy_keys"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_ulid)
    user_id: Mapped[str] = mapped_column(
        Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[str] = mapped_column(
        Text, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    proxy_key_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    key_last4: Mapped[str] = mapped_column(String(4), nullable=False)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    project: Mapped[Project] = relationship(back_populates="proxy_keys")

    __table_args__ = (Index("ix_proxy_keys_project_revoked", "project_id", "revoked_at"),)


class RequestLog(Base):
    """The ledger — append-only system of record (CODEBASE_GUIDE §5).

    The dashboard, the advisor, and every alert read from this table and
    nowhere else. Partitioned monthly by ``timestamp``; the primary key is
    ``(id, timestamp)`` because Postgres requires the partition key to be part
    of it.

    ``cost_would_have_been_usd`` is populated on every row, including
    passthroughs. Every savings number in the product derives from it.
    """

    __tablename__ = "requests_log"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_ulid)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, nullable=False, server_default=func.now()
    )

    user_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    request_id: Mapped[str] = mapped_column(Text, nullable=False)

    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model_requested: Mapped[str] = mapped_column(Text, nullable=False)
    model_used: Mapped[str] = mapped_column(Text, nullable=False)

    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_estimated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    cost_usd: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False, default=Decimal("0"))
    cost_would_have_been_usd: Mapped[Decimal | None] = mapped_column(Numeric(20, 10), nullable=True)

    latency_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    ttft_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    itl_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    tps: Mapped[float | None] = mapped_column(Float, nullable=True)

    cache_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cache_similarity: Mapped[float | None] = mapped_column(Float, nullable=True)
    routed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    routing_reason_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    routing_model_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    escalation_triggered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    context_warning: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    context_reclaimable_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_message_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    status: Mapped[int] = mapped_column(Integer, nullable=False, default=200)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    streamed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class RoutingRule(Base):
    """A user-defined routing rule — UC-15 (override), UC-19 (exclude).

    Evaluated before the classifier and absolute; see ``routing/rules.py``.
    """

    __tablename__ = "routing_rules"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_ulid)
    user_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(
        Text, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rule_type: Mapped[str] = mapped_column(Text, nullable=False)
    match_condition: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    target_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Budget(Base):
    """A spend limit for one project over one period — UC-29, UC-30.

    ``action`` decides what crossing it does: notify, degrade, or refuse. Only
    ``hard_stop`` refuses, and it is the one place in the system where a failure
    to read state blocks the request rather than passing it through
    (CLAUDE.md hard rule 1).
    """

    __tablename__ = "budgets"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_ulid)
    user_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(
        Text, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    period: Mapped[str] = mapped_column(Text, nullable=False)
    limit_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False, default="alert_only")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AlertEvent(Base):
    """One thing worth telling the user about — UC-31, UC-32, UC-34.

    Rows are kept after resolution. "Has this happened before, and what did we
    do about it" is most of the value of an alert history; a table that only
    holds open alerts answers neither question.
    """

    __tablename__ = "alert_events"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_ulid)
    user_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(
        Text, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    alert_type: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False, default="warning")
    title: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RollingStat(Base):
    """Durable copy of a project's Welford baseline (BUILD_SPEC §6.5).

    Redis holds the working copy. This exists so that flushing Redis — routine
    — does not silently reset every project to cold start and stop anomaly
    detection for the next 30 windows.
    """

    __tablename__ = "rolling_stats"

    project_id: Mapped[str] = mapped_column(
        Text, ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    metric: Mapped[str] = mapped_column(Text, primary_key=True)
    observation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mean: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    m2: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    window_started_at: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    window_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    window_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AdvisorRecommendation(Base):
    """One piece of advice, with its projected impact — UC-35, UC-36, UC-37.

    Regenerated nightly rather than accumulated: a recommendation is a
    statement about *current* usage, and yesterday's advice about traffic that
    has since changed is worse than no advice. Dismissed rows are kept so the
    job does not re-suggest something the user has already rejected.
    """

    __tablename__ = "advisor_recommendations"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_ulid)
    user_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(
        Text, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    projected_savings_usd: Mapped[Decimal] = mapped_column(
        Numeric(14, 6), nullable=False, default=Decimal("0")
    )
    confidence: Mapped[str] = mapped_column(Text, nullable=False, default="low")
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
