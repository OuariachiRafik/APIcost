"""Connection health check — UC-06.

Sends a minimal completion through the **full proxy path** and reports a
structured result. Going through the real path is the point: a check that
merely validated configuration would pass in exactly the situations users need
it to catch, like a provider key that is well-formed but revoked.

The failure reasons are deliberately specific. "Connection failed" tells a user
nothing they can act on; "your OpenAI key was rejected" tells them where to go.
"""

from __future__ import annotations

import time
from typing import Literal

import httpx
from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from apicost.api.deps import CurrentUser, DbSession, Kms, SettingsDep, require_project
from apicost.core.logging import get_logger
from apicost.db.models import ProviderKey, ProxyKey
from apicost.proxy.providers.base import get_http_client
from apicost.vault.provider_keys import EncryptedProviderKey, decrypt_provider_key

router = APIRouter(prefix="/projects", tags=["projects"])

_logger = get_logger(__name__)

FailureReason = Literal[
    "no_provider_key",
    "no_proxy_key",
    "provider_key_rejected",
    "provider_unreachable",
    "provider_error",
    "key_decrypt_failed",
    "model_unavailable",
]


class TestConnectionRequest(BaseModel):
    provider: Literal["openai", "anthropic", "gemini"] = "openai"
    model: str = "gpt-4o-mini"


class TestConnectionResponse(BaseModel):
    ok: bool
    reason: FailureReason | None = None
    message: str
    provider: str
    model: str
    latency_ms: float | None = None
    tokens_used: int | None = None


@router.post("/{project_id}/test-connection", response_model=TestConnectionResponse)
async def test_connection(
    project_id: str,
    payload: TestConnectionRequest,
    user: CurrentUser,
    session: DbSession,
    kms: Kms,
    settings: SettingsDep,
) -> TestConnectionResponse:
    """Send a real, minimal completion and report what happened."""
    project = await require_project(project_id, user, session)

    live_proxy_key = await session.execute(
        select(ProxyKey.id).where(
            ProxyKey.project_id == project.id,
            ProxyKey.user_id == user.id,
            ProxyKey.revoked_at.is_(None),
        )
    )
    if live_proxy_key.scalars().first() is None:
        return TestConnectionResponse(
            ok=False,
            reason="no_proxy_key",
            message=("This project has no active proxy key. Issue one before sending traffic."),
            provider=payload.provider,
            model=payload.model,
        )

    stored = await session.execute(
        select(ProviderKey).where(
            ProviderKey.user_id == user.id,
            ProviderKey.provider == payload.provider,
            ProviderKey.is_active.is_(True),
        )
    )
    provider_key = stored.scalars().first()
    if provider_key is None:
        return TestConnectionResponse(
            ok=False,
            reason="no_provider_key",
            message=(f"No active {payload.provider} key on file. Add one before testing."),
            provider=payload.provider,
            model=payload.model,
        )

    try:
        api_key = await decrypt_provider_key(
            kms,
            EncryptedProviderKey(
                encrypted_key=provider_key.encrypted_key,
                wrapped_data_key=provider_key.wrapped_data_key,
                nonce=provider_key.nonce,
            ),
        )
    except Exception:
        _logger.warning("test_connection_decrypt_failed", user_id=user.id)
        return TestConnectionResponse(
            ok=False,
            reason="key_decrypt_failed",
            message=("Your stored key could not be decrypted. Remove and re-add it."),
            provider=payload.provider,
            model=payload.model,
        )

    from apicost.proxy.ingress import build_provider

    provider = build_provider(payload.provider, settings)
    body = provider.normalize_request(
        {
            "model": payload.model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        },
        payload.model,
    )

    client = get_http_client(settings)
    begin = time.perf_counter()

    try:
        response = await client.post(
            provider.endpoint_url("chat/completions"),
            json=body,
            headers={
                **provider.auth_headers(api_key),
                "Content-Type": "application/json",
            },
            timeout=20.0,
        )
    except httpx.HTTPError as exc:
        _logger.warning(
            "test_connection_unreachable",
            provider=payload.provider,
            error_type=type(exc).__name__,
        )
        return TestConnectionResponse(
            ok=False,
            reason="provider_unreachable",
            message=(
                f"Could not reach {payload.provider}. "
                "Check your network, or the provider's status page."
            ),
            provider=payload.provider,
            model=payload.model,
            latency_ms=(time.perf_counter() - begin) * 1000.0,
        )

    latency_ms = (time.perf_counter() - begin) * 1000.0

    if response.status_code in (401, 403):
        return TestConnectionResponse(
            ok=False,
            reason="provider_key_rejected",
            message=(
                f"{payload.provider} rejected your API key. "
                "It may have been revoked or rotated — re-add it in Settings."
            ),
            provider=payload.provider,
            model=payload.model,
            latency_ms=latency_ms,
        )

    if response.status_code == 404:
        return TestConnectionResponse(
            ok=False,
            reason="model_unavailable",
            message=(
                f"{payload.provider} does not recognise the model {payload.model!r}, "
                "or your account cannot access it."
            ),
            provider=payload.provider,
            model=payload.model,
            latency_ms=latency_ms,
        )

    if response.status_code >= 400:
        return TestConnectionResponse(
            ok=False,
            reason="provider_error",
            message=(
                f"{payload.provider} returned HTTP {response.status_code}. "
                "The key and connection look fine; the request itself was rejected."
            ),
            provider=payload.provider,
            model=payload.model,
            latency_ms=latency_ms,
        )

    tokens_used: int | None = None
    try:
        usage = provider.parse_usage(response.json())
        tokens_used = usage.total if usage else None
    except ValueError:
        tokens_used = None

    _logger.info(
        "test_connection_succeeded",
        user_id=user.id,
        project_id=project.id,
        provider=payload.provider,
    )

    return TestConnectionResponse(
        ok=True,
        message=(
            f"Connected. {payload.provider} answered in {latency_ms:.0f} ms — "
            "you can point your application at the proxy."
        ),
        provider=payload.provider,
        model=payload.model,
        latency_ms=latency_ms,
        tokens_used=tokens_used,
    )
