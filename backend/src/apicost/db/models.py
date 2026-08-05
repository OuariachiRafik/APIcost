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
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from apicost.core.ids import new_id
from apicost.db.base import Base

if TYPE_CHECKING:
    pass


def _ulid() -> str:
    return new_id()


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

    status: Mapped[int] = mapped_column(Integer, nullable=False, default=200)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    streamed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
