"""Settings behavior — the single environment boundary (hard rule 8)."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from apicost.config import Settings, get_settings


def test_defaults_are_local_safe() -> None:
    settings = Settings(_env_file=None)
    assert settings.environment == "local"
    assert settings.database_url.startswith("postgresql+asyncpg://")
    assert settings.redis_url.startswith("redis://")


def test_optimization_budget_matches_spec() -> None:
    """BUILD_SPEC §0.1: 150 ms total, with a 40 ms embedding sub-budget (§4 P4)."""
    settings = Settings(_env_file=None)
    assert settings.optimization_budget_ms == 150
    assert settings.embedding_budget_ms == 40


def test_env_prefix_is_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APICOST_ENVIRONMENT", "staging")
    monkeypatch.setenv("APICOST_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("APICOST_OPTIMIZATION_BUDGET_MS", "99")

    settings = Settings(_env_file=None)

    assert settings.environment == "staging"
    assert settings.log_level == "DEBUG"
    assert settings.optimization_budget_ms == 99


def test_unprefixed_env_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stray DATABASE_URL in the shell must not silently repoint the app."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://evil@elsewhere/db")

    settings = Settings(_env_file=None)

    assert "elsewhere" not in settings.database_url


def test_secrets_are_wrapped() -> None:
    """SecretStr keeps keys out of reprs, logs, and tracebacks by default."""
    settings = Settings(_env_file=None, kms_master_key="sk-super-secret-value")

    assert isinstance(settings.kms_master_key, SecretStr)
    assert "sk-super-secret-value" not in repr(settings)
    assert "sk-super-secret-value" not in str(settings)
    assert settings.kms_master_key.get_secret_value() == "sk-super-secret-value"


def test_settings_are_frozen() -> None:
    settings = Settings(_env_file=None)
    with pytest.raises(ValidationError):
        settings.environment = "production"  # type: ignore[misc]


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()
