"""THE orchestrator. Read this before anything else in ``proxy/``.

Ordering is explicit and follows BUILD_SPEC §6.1:

```
authenticate(proxy_key)          -> user, project, config     # Redis-cached
check_plan_and_budget(...)                                    # P6; may hard-stop
deadline = Deadline(150ms)
  cache lookup      (failopen)   -> hit? replay and return, no provider call   # P4
  routing decision  (failopen)   -> model, or fall back to the requested one   # P5
decrypt provider key in memory
forward to provider (streamed)
  escalation check  (failopen)                                                 # P5
emit ledger event   (fire-and-forget)
emit cache write    (fire-and-forget, if cacheable)                            # P4
return
```

P2 implements authentication, forwarding, streaming, metrics, and the ledger.
The cache, routing, escalation, and budget steps have their slots marked and
arrive with their phases — the ordering is fixed now so later phases slot in
rather than rearrange.

The two rules that govern every line here:

1. **Fail open.** Every optimization step goes through
   :func:`~apicost.core.deadline.failopen`. If it raises or overruns, the
   request proceeds to the provider unchanged.
2. **Never modify the response body.** What the provider returned is what the
   caller gets, byte for byte. Our metadata goes in headers.

Prefer explicit over clever here (CLAUDE.md §Style). This file is read far more
often than it is edited.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from apicost.cache.embeddings import embed
from apicost.cache.policy import is_cacheable, normalize_prompt
from apicost.cache.semantic import CacheHit, lookup_exact, lookup_similar, record_hit
from apicost.cache.semantic import store as semantic_store
from apicost.config import Settings
from apicost.core.deadline import Deadline, failopen
from apicost.core.errors import UpstreamError
from apicost.core.ids import new_request_id
from apicost.core.logging import get_logger
from apicost.db.redis import get_redis
from apicost.db.session import session_scope
from apicost.ledger.cost import compute_cost, cost_would_have_been
from apicost.ledger.pricing import PriceNotFoundError
from apicost.ledger.writer import LedgerEvent, emit_ledger_event
from apicost.metrics.inference import compute_inference_metrics
from apicost.metrics.latency import StageTimer
from apicost.proxy.auth import ResolvedKey
from apicost.proxy.providers.base import (
    Provider,
    Usage,
    estimate_tokens,
    get_http_client,
)
from apicost.proxy.streaming import StreamCapture, replay_as_sse, tee_stream
from apicost.vault.kms import KMSClient
from apicost.vault.provider_keys import EncryptedProviderKey, decrypt_provider_key

__all__ = ["PipelineResult", "ProxyRequest", "run_pipeline"]

_logger = get_logger(__name__)

REASON_PASSTHROUGH = "PASSTHROUGH"
REASON_CACHE_HIT = "CACHE_HIT"
"""P5 introduces the rest of the reason-code vocabulary (UC-16)."""


@dataclass
class ProxyRequest:
    """Everything one proxied call needs, assembled by ``ingress.py``."""

    request_id: str
    endpoint: str
    body: dict[str, Any]
    resolved: ResolvedKey
    provider: Provider
    load_encrypted_key: Callable[[], Awaitable[EncryptedProviderKey]]
    """Deferred. A cache hit never forwards, so it should never fetch key
    material — both a latency win and one less place a key is handled."""

    kms: KMSClient
    settings: Settings
    stream: bool = False
    headers: dict[str, str] = field(default_factory=dict)
    """Inbound request headers, for the X-APICost-No-Cache marker (UC-24)."""

    @property
    def model_requested(self) -> str:
        model = self.body.get("model")
        return model if isinstance(model, str) else "unknown"


@dataclass
class _CacheContext:
    """What a cache write needs, carried from lookup to after the response.

    Only populated when the request was cacheable *and* the embedding
    succeeded — there is no point forwarding a prompt we cannot index.
    """

    normalized_prompt: str
    embedding: list[float]


@dataclass
class PipelineResult:
    """What ingress needs to build the HTTP response."""

    status_code: int
    body: dict[str, Any] | None = None
    stream: AsyncIterator[bytes] | None = None
    headers: dict[str, str] = field(default_factory=dict)
    model_used: str = ""
    cache_hit: bool = False
    routed: bool = False
    reason_code: str = REASON_PASSTHROUGH


async def run_pipeline(request: ProxyRequest) -> PipelineResult:
    """Run one request through the proxy.

    Never raises for an optimization failure. Provider errors *are* propagated,
    because the caller's error handling should behave exactly as it did before
    they put us in the path (CODEBASE_GUIDE §12).
    """
    started = time.perf_counter()
    deadline = Deadline(budget_ms=float(request.settings.optimization_budget_ms))

    timer = StageTimer()
    timer.start(started)

    model_requested = request.model_requested
    model_used = model_requested

    # -- [3] Semantic cache -------------------------------------------------
    #
    # Everything here is inside failopen: a cache that raises, hangs, or blows
    # the budget results in a normal provider call, never an error.
    decision = is_cacheable(
        request.body,
        headers=request.headers,
        cache_enabled=request.resolved.cache_enabled,
        endpoint=request.endpoint,
    )

    normalized = ""
    embedding: list[float] | None = None
    hit: CacheHit | None = None

    if decision.cacheable:
        normalized = normalize_prompt(request.body)

        timer.mark("policy", time.perf_counter())

        # Exact hash first: no embedding, and no database session. The
        # repeat-prompt case is the common one, and neither cost is needed to
        # answer it — that is the whole point of §6.3's two-tier design.
        # Opening a session alone costs a BEGIN, a set_config and a COMMIT,
        # which was most of the gap to the 30 ms hit budget.
        async with failopen("cache_lookup", deadline) as guard:
            hit = await lookup_exact(
                get_redis(request.settings),
                request.kms,
                user_id=request.resolved.user_id,
                project_id=request.resolved.project_id,
                normalized_prompt=normalized,
            )
            timer.mark("cache_lookup", time.perf_counter())
            if hit is not None:
                await record_hit(get_redis(request.settings), hit.entry_id)
                timer.mark("record_hit", time.perf_counter())
        if guard.failed:
            hit = None

        # Only on an exact miss is an embedding worth paying for: either to
        # find a semantically similar entry, or to index this prompt for later.
        if hit is None:
            async with failopen(
                "embed", deadline, budget_ms=float(request.settings.embedding_budget_ms)
            ) as guard:
                embedding = await embed(normalized)
            if guard.failed:
                embedding = None

            if embedding is not None:
                async with (
                    failopen("cache_lookup", deadline) as guard,
                    session_scope(user_id=request.resolved.user_id) as session,
                ):
                    hit = await lookup_similar(
                        session,
                        request.kms,
                        user_id=request.resolved.user_id,
                        project_id=request.resolved.project_id,
                        embedding=embedding,
                        threshold=request.resolved.similarity_threshold,
                    )
                    if hit is not None:
                        await record_hit(get_redis(request.settings), hit.entry_id)
                if guard.failed:
                    hit = None

            if hit is not None:
                await record_hit(get_redis(request.settings), hit.entry_id)
        if guard.failed:
            hit = None

    if hit is not None:
        # The provider is never called. This is the whole point.
        return await _serve_from_cache(request, hit, started, timer)

    # -- [4] Routing --------------------------------------------------- P5 --
    # with failopen("routing", deadline) as guard:
    #     decision = await routing_engine.decide(...)
    # model_used = decision.model if guard.ok and decision else model_requested

    # -- [5] Decrypt the provider key, in memory, immediately before use ---
    #
    # Not earlier. The plaintext key's lifetime should be as short as we can
    # make it, so it is fetched after every step that might have returned
    # without needing it at all (CODEBASE_GUIDE §7.1).
    api_key = await decrypt_provider_key(request.kms, await request.load_encrypted_key())

    # -- [6] Forward ------------------------------------------------------
    cache_context = (
        _CacheContext(normalized_prompt=normalized, embedding=embedding)
        if decision.cacheable and embedding is not None
        else None
    )

    if request.stream:
        return await _forward_streaming(
            request, api_key, model_used, started, deadline, cache_context
        )
    return await _forward_unary(request, api_key, model_used, started, deadline, cache_context)


# ---------------------------------------------------------------------------
# Forwarding
# ---------------------------------------------------------------------------


async def _forward_unary(
    request: ProxyRequest,
    api_key: str,
    model_used: str,
    started: float,
    deadline: Deadline,
    cache_context: _CacheContext | None = None,
) -> PipelineResult:
    """Non-streamed forward."""
    provider = request.provider
    payload = provider.normalize_request(request.body, model_used)

    client = get_http_client(request.settings)
    try:
        response = await client.post(
            provider.endpoint_url(request.endpoint),
            json=payload,
            headers={**provider.auth_headers(api_key), "Content-Type": "application/json"},
        )
    except httpx.HTTPError as exc:
        # A transport failure is ours to report; there is no provider response
        # to pass through. The message names no key material.
        _logger.warning(
            "provider_unreachable",
            provider=provider.name,
            error_type=type(exc).__name__,
            request_id=request.request_id,
        )
        raise UpstreamError(f"Could not reach {provider.name}") from exc

    latency_ms = (time.perf_counter() - started) * 1000.0

    try:
        raw_body: dict[str, Any] = response.json()
    except ValueError:
        raise UpstreamError(f"{provider.name} returned a non-JSON response") from None

    if response.status_code >= 400:
        # Verbatim, by design. Their error handling should keep working.
        await _record(
            request,
            model_used=model_used,
            usage=Usage(0, 0, estimated=True),
            latency_ms=latency_ms,
            status=response.status_code,
            error_code=_error_code(raw_body),
        )
        return PipelineResult(
            status_code=response.status_code,
            body=raw_body,
            model_used=model_used,
            headers=_metadata_headers(request, model_used, cache_hit=False),
        )

    body = provider.denormalize_response(raw_body)
    usage = provider.parse_usage(raw_body) or _estimate_usage(request, body)

    await _record(
        request,
        model_used=model_used,
        usage=usage,
        latency_ms=latency_ms,
        status=response.status_code,
    )

    if cache_context is not None:
        # After the ledger write and before returning: the response is already
        # complete, so this costs the caller nothing but a coroutine.
        await _write_cache_entry(request, cache_context, body, model_used, usage)

    _logger.info(
        "request_forwarded",
        request_id=request.request_id,
        provider=provider.name,
        model_used=model_used,
        tokens_in=usage.tokens_in,
        tokens_out=usage.tokens_out,
        latency_ms=round(latency_ms, 2),
        optimization_ms=round(deadline.elapsed_ms, 2),
        streamed=False,
    )

    return PipelineResult(
        status_code=response.status_code,
        body=body,
        model_used=model_used,
        headers=_metadata_headers(request, model_used, cache_hit=False),
    )


async def _forward_streaming(
    request: ProxyRequest,
    api_key: str,
    model_used: str,
    started: float,
    deadline: Deadline,
    cache_context: _CacheContext | None = None,
) -> PipelineResult:
    """Streamed forward with a non-buffering tee.

    The generator below owns the upstream connection for the life of the
    stream, and records the ledger event once the client has seen the last
    byte. Bookkeeping never precedes delivery.
    """
    provider = request.provider
    payload = provider.normalize_request(request.body, model_used)
    capture = StreamCapture()

    async def body_stream() -> AsyncIterator[bytes]:
        client = get_http_client(request.settings)
        status = 200
        error_code: str | None = None

        try:
            async with client.stream(
                "POST",
                provider.endpoint_url(request.endpoint),
                json=payload,
                headers={
                    **provider.auth_headers(api_key),
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                },
            ) as upstream:
                status = upstream.status_code

                if status >= 400:
                    # Forward the provider's error body untouched.
                    error_body = await upstream.aread()
                    error_code = "provider_error"
                    yield error_body
                    return

                async for chunk in tee_stream(upstream.aiter_bytes(), capture):
                    yield chunk

        except httpx.HTTPError as exc:
            status = 502
            error_code = type(exc).__name__
            _logger.warning(
                "provider_stream_failed",
                provider=provider.name,
                error_type=error_code,
                request_id=request.request_id,
            )
            raise
        finally:
            # Runs on success, on error, and on client disconnect. A partial
            # stream still consumed provider tokens, so it still gets a row.
            latency_ms = (time.perf_counter() - started) * 1000.0

            # Ledger first, always. It is the system of record; a cache entry
            # is disposable. Writing the cache first meant an exception or a
            # cancellation there could take the ledger row with it — which it
            # did, silently, on every streamed request.
            await _record_stream(
                request,
                model_used=model_used,
                capture=capture,
                latency_ms=latency_ms,
                started=started,
                status=status,
                error_code=error_code,
                optimization_ms=deadline.elapsed_ms,
            )

            if cache_context is not None and capture.completed and status < 400:
                # Only a stream that finished is worth caching — replaying a
                # truncated answer would hand the same failure to everyone.
                try:
                    await _write_cache_entry(
                        request,
                        cache_context,
                        _assemble_streamed_body(capture, model_used),
                        capture.model or model_used,
                        capture.usage or Usage(0, 0, estimated=True),
                    )
                except Exception:
                    _logger.warning("cache_write_failed", subsystem="cache")

    return PipelineResult(
        status_code=200,
        stream=body_stream(),
        model_used=model_used,
        headers=_metadata_headers(request, model_used, cache_hit=False),
    )


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def _estimate_usage(request: ProxyRequest, body: dict[str, Any]) -> Usage:
    """Fall back to estimation when the provider omits usage (§6.2)."""
    prompt_text = " ".join(
        str(message.get("content", ""))
        for message in request.body.get("messages", [])
        if isinstance(message, dict)
    )
    completion_text = ""
    for choice in body.get("choices", []):
        if isinstance(choice, dict):
            message = choice.get("message", {})
            if isinstance(message, dict):
                completion_text += str(message.get("content") or "")

    return Usage(
        tokens_in=estimate_tokens(prompt_text),
        tokens_out=estimate_tokens(completion_text) if completion_text else 0,
        estimated=True,
    )


async def _record(
    request: ProxyRequest,
    *,
    model_used: str,
    usage: Usage,
    latency_ms: float,
    status: int,
    error_code: str | None = None,
    ttft_ms: float | None = None,
    itl_ms: float | None = None,
    tps: float | None = None,
    streamed: bool = False,
    cache_hit: bool = False,
    cache_similarity: float | None = None,
    cost_override: str | None = None,
    timer: StageTimer | None = None,
) -> None:
    """Build and enqueue the ledger event. Never raises."""
    now = datetime.now(UTC)
    model_requested = request.model_requested

    try:
        cost = compute_cost(
            model_used,
            usage.tokens_in,
            usage.tokens_out,
            at=now,
            estimated=usage.estimated,
        )
        cost_usd = str(cost.total_usd)
    except (PriceNotFoundError, ValueError):
        # An unpriced model must not lose us the row.
        cost_usd = "0"

    if cost_override is not None:
        # A cache hit costs nothing: the provider was never called. The saving
        # is the whole of cost_would_have_been_usd.
        cost_usd = cost_override

    would_have_been = cost_would_have_been(
        model_requested, usage.tokens_in, usage.tokens_out, at=now
    )

    event = LedgerEvent(
        request_id=request.request_id,
        user_id=request.resolved.user_id,
        project_id=request.resolved.project_id,
        timestamp=now.isoformat(),
        endpoint=request.endpoint,
        provider=request.provider.name,
        model_requested=model_requested,
        model_used=model_used,
        tokens_in=usage.tokens_in,
        tokens_out=usage.tokens_out,
        tokens_estimated=usage.estimated,
        cost_usd=cost_usd,
        cost_would_have_been_usd=str(would_have_been) if would_have_been is not None else None,
        latency_ms=latency_ms,
        ttft_ms=ttft_ms,
        itl_ms=itl_ms,
        tps=tps,
        status=status,
        error_code=error_code,
        streamed=streamed,
        cache_hit=cache_hit,
        cache_similarity=cache_similarity,
        routing_reason_code=REASON_CACHE_HIT if cache_hit else REASON_PASSTHROUGH,
    )

    if timer is not None:
        timer.mark("ledger_build", time.perf_counter())

    from apicost.db.redis import get_redis

    redis = get_redis()
    if timer is not None:
        timer.mark("ledger_client", time.perf_counter())

    await emit_ledger_event(redis, event, request.settings)

    if timer is not None:
        timer.mark("ledger_emit", time.perf_counter())


async def _record_stream(
    request: ProxyRequest,
    *,
    model_used: str,
    capture: StreamCapture,
    latency_ms: float,
    started: float,
    status: int,
    error_code: str | None,
    optimization_ms: float,
) -> None:
    """Ledger a streamed request, including its inference metrics."""
    ttft_ms: float | None = None
    itl_ms: float | None = None
    tps: float | None = None

    if len(capture.chunk_timestamps) >= 2:
        try:
            metrics = compute_inference_metrics(capture.chunk_timestamps, request_start=started)
            ttft_ms, itl_ms, tps = metrics.ttft_ms, metrics.itl_ms, metrics.tps
        except ValueError:
            # Out-of-order or too-short stream. Record the row without the
            # metrics rather than losing the row.
            _logger.warning("inference_metrics_unavailable", request_id=request.request_id)
    elif capture.chunk_timestamps:
        ttft_ms = (capture.chunk_timestamps[0] - started) * 1000.0

    usage = capture.usage
    if usage is None:
        prompt_text = " ".join(
            str(message.get("content", ""))
            for message in request.body.get("messages", [])
            if isinstance(message, dict)
        )
        usage = Usage(
            tokens_in=estimate_tokens(prompt_text),
            tokens_out=max(1, capture.text_length // 4) if capture.text_length else 0,
            estimated=True,
        )

    await _record(
        request,
        model_used=capture.model or model_used,
        usage=usage,
        latency_ms=latency_ms,
        status=status,
        error_code=error_code if capture.completed or error_code else "stream_incomplete",
        ttft_ms=ttft_ms,
        itl_ms=itl_ms,
        tps=tps,
        streamed=True,
    )

    _logger.info(
        "request_streamed",
        request_id=request.request_id,
        provider=request.provider.name,
        model_used=model_used,
        chunks=capture.content_chunks,
        tokens_in=usage.tokens_in,
        tokens_out=usage.tokens_out,
        ttft_ms=round(ttft_ms, 2) if ttft_ms is not None else None,
        latency_ms=round(latency_ms, 2),
        optimization_ms=round(optimization_ms, 2),
        completed=capture.completed,
    )


def _metadata_headers(request: ProxyRequest, model_used: str, *, cache_hit: bool) -> dict[str, str]:
    """APICost metadata, in headers only (BUILD_SPEC §0.5).

    Never in the body — the caller's SDK parses that, and a stray field is a
    breaking change to somebody's application.
    """
    return {
        "X-APICost-Request-Id": request.request_id,
        "X-APICost-Cache": "hit" if cache_hit else "miss",
        "X-APICost-Model-Used": model_used,
        "X-APICost-Reason": REASON_PASSTHROUGH,
    }


def _error_code(body: dict[str, Any]) -> str | None:
    error = body.get("error")
    if isinstance(error, dict):
        code = error.get("code") or error.get("type")
        return str(code) if code else None
    return None


def new_pipeline_request_id() -> str:
    return new_request_id()


async def _serve_from_cache(
    request: ProxyRequest,
    hit: CacheHit,
    started: float,
    timer: StageTimer | None = None,
) -> PipelineResult:
    """Return a cached response without calling the provider.

    The saving recorded here is the *whole* cost the request would have
    incurred, because the provider call did not happen (CODEBASE_GUIDE §6).
    """
    latency_ms = (time.perf_counter() - started) * 1000.0

    # Ledgering is bookkeeping, but it still has to happen before we return —
    # a fire-and-forget task here would race the response and could be dropped
    # on shutdown. It costs one Redis round trip.
    if timer is not None:
        timer.mark("respond", time.perf_counter())

    await _record(
        request,
        model_used=hit.model_used,
        usage=Usage(hit.tokens_in, hit.tokens_out, estimated=False),
        latency_ms=latency_ms,
        status=200,
        cache_hit=True,
        cache_similarity=hit.similarity,
        cost_override="0",
        timer=timer,
    )

    if timer is not None:
        timer.mark("ledger", time.perf_counter())

    _logger.info(
        "cache_hit",
        request_id=request.request_id,
        similarity=round(hit.similarity, 4),
        exact=hit.exact,
        latency_ms=round(latency_ms, 2),
        **(timer.as_log_fields() if timer is not None else {}),
    )

    headers = _metadata_headers(request, hit.model_used, cache_hit=True)

    if request.stream:
        # Re-chunked as SSE so a streaming client cannot tell the difference
        # (BUILD_SPEC §4 P4).
        return PipelineResult(
            status_code=200,
            stream=replay_as_sse(hit.body),
            headers=headers,
            model_used=hit.model_used,
            cache_hit=True,
            reason_code=REASON_CACHE_HIT,
        )

    return PipelineResult(
        status_code=200,
        body=hit.body,
        headers=headers,
        model_used=hit.model_used,
        cache_hit=True,
        reason_code=REASON_CACHE_HIT,
    )


async def _write_cache_entry(
    request: ProxyRequest,
    context: _CacheContext,
    body: dict[str, Any],
    model_used: str,
    usage: Usage,
) -> None:
    """Store a response for next time. Off the critical path; never raises."""
    try:
        async with session_scope(user_id=request.resolved.user_id) as session:
            await semantic_store(
                session,
                get_redis(request.settings),
                request.kms,
                user_id=request.resolved.user_id,
                project_id=request.resolved.project_id,
                normalized_prompt=context.normalized_prompt,
                embedding=context.embedding,
                body=body,
                model_used=model_used,
                tokens_in=usage.tokens_in,
                tokens_out=usage.tokens_out,
                ttl_seconds=request.resolved.cache_ttl_seconds,
            )
    except Exception:
        _logger.warning("cache_write_failed", subsystem="cache")


def _assemble_streamed_body(capture: StreamCapture, model_used: str) -> dict[str, Any]:
    """Rebuild a complete response body from what the tee observed.

    A streamed response is never held in one piece, so caching one means
    reassembling it. The shape matches a non-streamed completion, which is what
    `replay_as_sse` expects to re-chunk on the way back out.
    """
    return {
        "id": f"chatcmpl-{new_request_id()}",
        "object": "chat.completion",
        "created": 0,
        "model": capture.model or model_used,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": capture.text},
                "finish_reason": capture.finish_reason or "stop",
            }
        ],
        "usage": {
            "prompt_tokens": capture.usage.tokens_in if capture.usage else 0,
            "completion_tokens": capture.usage.tokens_out if capture.usage else 0,
            "total_tokens": capture.usage.total if capture.usage else 0,
        },
    }
