"""Application configuration.

This module is the **only** place in the codebase permitted to read the
environment (CLAUDE.md hard rule 8). Everything else takes a ``Settings``
instance, via :func:`get_settings` or dependency injection.

Every variable is read with the ``APICOST_`` prefix, e.g. ``APICOST_DATABASE_URL``.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "test", "staging", "production"]


class Settings(BaseSettings):
    """The single settings object for both ASGI apps and the worker."""

    model_config = SettingsConfigDict(
        env_prefix="APICOST_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # -- Environment ------------------------------------------------------
    environment: Environment = "local"
    debug: bool = False

    # -- Logging ----------------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = True
    """JSON renderer in every environment; flip to False for human-readable local logs."""

    # -- Datastores -------------------------------------------------------
    database_url: str = "postgresql+asyncpg://apicost_app:apicost_app@localhost:5433/apicost"
    """Connection used by the application, as the **unprivileged** role.

    A Postgres superuser bypasses row-level security unconditionally, so
    connecting as the schema owner would disable every policy in the migration
    while leaving them visible in the code. See
    docker/postgres/init/01-app-role.sql.

    Host port 5433, not 5432 — see the note in docker-compose.yml. A system
    Postgres on the default port must never be hit by our migrations.
    """

    database_admin_url: str = "postgresql+asyncpg://apicost:apicost@localhost:5433/apicost"
    """Connection used by Alembic only. Owns the schema and may run DDL."""

    redis_url: str = "redis://localhost:6379/0"

    db_pool_size: int = 10
    db_max_overflow: int = 5
    db_echo: bool = False

    # -- Readiness --------------------------------------------------------
    readiness_timeout_seconds: float = 2.0
    """Per-dependency ceiling for /readyz probes, so the endpoint can never hang."""

    # -- Time budgets (BUILD_SPEC §0.1, §6.1) -----------------------------
    optimization_budget_ms: int = 150
    """Hard ceiling for ALL optimization work, enforced by one shared Deadline."""

    embedding_budget_ms: int = 40
    """Sub-budget for embedding on the cache path; overrun proceeds as a miss."""

    # -- Provider forwarding ----------------------------------------------
    provider_timeout_seconds: float = 120.0
    """Generous on purpose. This bounds the *provider's* own generation time,
    not our overhead — a long completion is normal, and cutting it off would
    break the caller's request for no benefit."""

    provider_base_url_override: str = ""
    """Points every provider at a stub. Tests and local development only."""

    # -- Ledger (BUILD_SPEC §4 P2) ----------------------------------------
    ledger_stream_key: str = "apicost:ledger"
    ledger_stream_maxlen: int = 500_000
    """Approximate cap on the Redis stream. If the worker falls this far
    behind, the oldest events are dropped rather than exhausting memory and
    taking the proxy down with it — losing observability beats losing the
    data plane."""

    ledger_batch_size: int = 500
    ledger_block_ms: int = 5_000

    # -- Secrets (consumed from P1 onward) --------------------------------
    kms_provider: Literal["local", "aws"] = "local"
    """Switching to "aws" is the one-line change BUILD_SPEC §4 P1 calls for."""

    kms_master_key: SecretStr = SecretStr("")
    """LocalKMS master key for dev. Unused when kms_provider is "aws"."""

    kms_key_id: str = ""
    kms_region: str = ""

    jwt_secret: SecretStr = SecretStr("")

    # -- Outbound ---------------------------------------------------------
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    email_from: str = "apicost@localhost"
    public_base_url: str = "http://localhost:8001"
    """Origin used to build links in outbound email — unsubscribe, mainly.

    Must be the API's public origin, not the SPA's: the unsubscribe link has to
    work with one click from a mail client, without a logged-in session and
    without JavaScript."""
    resend_api_key: SecretStr = SecretStr("")
    """Empty in development, where alerts go to mailpit over SMTP instead."""

    # -- Anomaly detection and alerting (BUILD_SPEC §6.8) ------------------
    anomaly_z_threshold: float = 3.0
    anomaly_min_observations: int = 30
    alert_cooldown_seconds: int = 1800
    """30 minutes per alert type per project. A sustained incident should send
    one email, not one per window."""

    # -- Web / CORS -------------------------------------------------------
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings object.

    Cached, so the environment is read exactly once per process. Tests that
    manipulate the environment must call ``get_settings.cache_clear()``.
    """
    return Settings()
