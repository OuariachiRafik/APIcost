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
    database_url: str = "postgresql+asyncpg://apicost:apicost@localhost:5433/apicost"
    """Host port 5433, not 5432 — see the note in docker-compose.yml. A system
    Postgres on the default port must never be hit by our migrations."""

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

    # -- Secrets (consumed from P1 onward) --------------------------------
    kms_master_key: SecretStr = SecretStr("")
    """LocalKMS master key for dev. In prod the AWS KMS impl is used instead."""

    jwt_secret: SecretStr = SecretStr("")

    # -- Outbound ---------------------------------------------------------
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    email_from: str = "apicost@localhost"

    # -- Web / CORS -------------------------------------------------------
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings object.

    Cached, so the environment is read exactly once per process. Tests that
    manipulate the environment must call ``get_settings.cache_clear()``.
    """
    return Settings()
