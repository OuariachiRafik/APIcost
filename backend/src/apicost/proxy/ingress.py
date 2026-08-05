"""Data-plane routes: the OpenAI-compatible surface.

Thin on purpose. Ingress authenticates, assembles a
:class:`~apicost.proxy.pipeline.ProxyRequest`, and turns the result into an
HTTP response. Every decision lives in ``pipeline.py``.

``/v1/embeddings`` is logged passthrough only in v1 — not routed, not cached
(BUILD_SPEC §4 P2, CODEBASE_GUIDE §13).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select

from apicost.config import Settings, get_settings
from apicost.core.errors import APICostError, InvalidRequestError, NotFoundError
from apicost.core.logging import get_logger
from apicost.db.models import ProviderKey
from apicost.db.redis import get_redis
from apicost.db.session import session_scope
from apicost.proxy.auth import (
    ResolvedKey,
    extract_bearer_token,
    resolve_proxy_key,
    touch_last_used,
)
from apicost.proxy.pipeline import PipelineResult, ProxyRequest, run_pipeline
from apicost.proxy.providers.anthropic import AnthropicProvider
from apicost.proxy.providers.base import Provider
from apicost.proxy.providers.gemini import GeminiProvider
from apicost.proxy.providers.openai import OpenAIProvider
from apicost.vault.kms import get_kms_client
from apicost.vault.provider_keys import EncryptedProviderKey

__all__ = ["build_proxy_request", "router"]

router = APIRouter(prefix="/v1", tags=["proxy"])

_logger = get_logger(__name__)

_PROVIDER_CLASSES: dict[str, Callable[[str | None], Provider]] = {
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
    "gemini": GeminiProvider,
}

# Which provider serves a model, by prefix. The requested model decides, so a
# user with several keys reaches the right one without extra configuration.
_MODEL_PREFIXES = (
    ("gpt-", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("text-embedding-", "openai"),
    ("claude-", "anthropic"),
    ("gemini-", "gemini"),
)


def provider_for_model(model: str) -> str:
    """Which provider owns a model name. Defaults to OpenAI.

    Defaulting rather than rejecting is deliberate: an unrecognised model is
    usually one released more recently than this table, and the provider is a
    better judge of whether it exists than we are.
    """
    for prefix, provider in _MODEL_PREFIXES:
        if model.startswith(prefix):
            return provider
    return "openai"


def build_provider(name: str, settings: Settings) -> Provider:
    provider_class = _PROVIDER_CLASSES.get(name, OpenAIProvider)
    override = settings.provider_base_url_override or None
    return provider_class(override)


async def _load_provider_key(user_id: str, provider_name: str) -> EncryptedProviderKey:
    """Fetch the caller's stored key for a provider, still encrypted."""
    async with session_scope(user_id=user_id) as session:
        result = await session.execute(
            select(ProviderKey).where(
                ProviderKey.user_id == user_id,
                ProviderKey.provider == provider_name,
                ProviderKey.is_active.is_(True),
            )
        )
        stored = result.scalars().first()

    if stored is None:
        raise NotFoundError(
            f"No active {provider_name} key on file. Add one in the dashboard first."
        )

    return EncryptedProviderKey(
        encrypted_key=stored.encrypted_key,
        wrapped_data_key=stored.wrapped_data_key,
        nonce=stored.nonce,
    )


async def build_proxy_request(
    *,
    endpoint: str,
    body: dict[str, Any],
    authorization: str | None,
    request_id: str,
    settings: Settings,
) -> ProxyRequest:
    """Authenticate and assemble everything the pipeline needs."""
    raw_key = extract_bearer_token(authorization)
    redis = get_redis(settings)

    resolved: ResolvedKey = await resolve_proxy_key(redis, raw_key)
    await touch_last_used(redis, resolved.proxy_key_id)

    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise InvalidRequestError("Request body must include a 'model' string")

    provider_name = provider_for_model(model)

    return ProxyRequest(
        request_id=request_id,
        endpoint=endpoint,
        body=body,
        resolved=resolved,
        provider=build_provider(provider_name, settings),
        encrypted_key=await _load_provider_key(resolved.user_id, provider_name),
        kms=get_kms_client(settings),
        settings=settings,
        stream=bool(body.get("stream", False)),
    )


def _to_response(result: PipelineResult) -> JSONResponse | StreamingResponse:
    if result.stream is not None:
        return StreamingResponse(
            result.stream,
            media_type="text/event-stream",
            headers={
                **result.headers,
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # stop nginx from defeating the stream
            },
        )
    return JSONResponse(
        status_code=result.status_code,
        content=result.body,
        headers=result.headers,
    )


async def _handle(
    request: Request, endpoint: str, authorization: str | None
) -> JSONResponse | StreamingResponse:
    settings = get_settings()

    try:
        body = await request.json()
    except ValueError:
        raise InvalidRequestError("Request body must be valid JSON") from None

    if not isinstance(body, dict):
        raise InvalidRequestError("Request body must be a JSON object")

    proxy_request = await build_proxy_request(
        endpoint=endpoint,
        body=body,
        authorization=authorization,
        request_id=getattr(request.state, "request_id", ""),
        settings=settings,
    )

    result = await run_pipeline(proxy_request)
    return _to_response(result)


@router.post("/chat/completions", response_model=None)
async def chat_completions(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> JSONResponse | StreamingResponse:
    """OpenAI-compatible chat completions, streaming or not."""
    return await _handle(request, "chat/completions", authorization)


@router.post("/embeddings", response_model=None)
async def embeddings(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> JSONResponse | StreamingResponse:
    """Logged passthrough. Not routed and not cached in v1."""
    return await _handle(request, "embeddings", authorization)


@router.get("/models", response_model=None)
async def list_models(
    authorization: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    """Minimal ``/v1/models``.

    Present because several SDKs call it during client setup and treat a 404 as
    a broken endpoint. It reports the models we hold prices for.
    """
    extract_bearer_token(authorization)
    from apicost.ledger.pricing import known_models

    return JSONResponse(
        {
            "object": "list",
            "data": [
                {"id": model, "object": "model", "owned_by": provider_for_model(model)}
                for model in sorted(known_models())
            ],
        }
    )


def error_to_openai_shape(error: APICostError) -> dict[str, Any]:
    """Render an error the way an OpenAI client expects to receive one.

    The data plane is the one place we do *not* use RFC 7807: the caller's SDK
    is parsing this, and it expects OpenAI's ``{"error": {...}}`` envelope.
    Handing it problem+json would break error handling in exactly the
    applications we promised not to disturb.
    """
    return {
        "error": {
            "message": error.detail,
            "type": error.title.lower().replace(" ", "_"),
            "code": error.status_code,
        }
    }
