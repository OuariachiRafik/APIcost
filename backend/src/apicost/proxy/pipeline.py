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

from apicost.budgets.enforcement import (
    BudgetDecision,
    BudgetSpec,
    BudgetVerdict,
    check_budgets,
    record_spend,
)
from apicost.cache.embeddings import embed
from apicost.cache.policy import is_cacheable, normalize_prompt
from apicost.cache.semantic import CacheHit, lookup_exact, lookup_similar, record_hit
from apicost.cache.semantic import store as semantic_store
from apicost.config import Settings
from apicost.core.deadline import Deadline, failopen
from apicost.core.errors import BudgetExceededError, UpstreamError
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
from apicost.routing.engine import (
    REASON_FAILOPEN_TIMEOUT,
    RoutingDecision,
    cheaper_model_for,
)
from apicost.routing.engine import decide as routing_decide
from apicost.routing.escalation import looks_low_confidence
from apicost.routing.rules import RoutingRule
from apicost.vault.kms import KMSClient
from apicost.vault.provider_keys import EncryptedProviderKey, decrypt_provider_key

__all__ = ["PipelineResult", "ProxyRequest", "run_pipeline"]

_logger = get_logger(__name__)

REASON_PASSTHROUGH = "PASSTHROUGH"
REASON_CACHE_HIT = "CACHE_HIT"
REASON_BUDGET_THROTTLED = "BUDGET_THROTTLED"

ROUTING_BUDGET_MS = 20.0
"""BUILD_SPEC §4 P5: a classifier stall past this is a passthrough, not a
slower request."""


def _budget_message(verdict: BudgetVerdict) -> str:
    """The 402 body a user reads at 3am. Actionable, and free of internals."""
    if verdict.degraded:
        return (
            "Budget state could not be read, and this project has a hard-stop "
            "budget. Requests are refused until it can be verified, because a "
            "hard stop must not fail open. Retry shortly, or change the budget "
            "action to alert_only to allow traffic through while this clears."
        )
    return (
        f"This project has exceeded its {verdict.period} budget of "
        f"${verdict.limit_usd:,.2f} (spent ${verdict.spent_usd:,.2f}). "
        "Requests are stopped because the budget action is hard_stop. "
        "Raise the limit, change the action, or wait for the period to reset."
    )


def _budgets_for(resolved: ResolvedKey) -> list[BudgetSpec]:
    """Rehydrate the budget specs carried in the cached auth resolution."""
    specs: list[BudgetSpec] = []
    for raw in resolved.budgets:
        spec = BudgetSpec.from_raw(raw)
        if spec is not None:
            specs.append(spec)
        else:
            _logger.warning("budget_malformed", subsystem="budgets")
    return specs


def _rules_for(resolved: ResolvedKey) -> list[RoutingRule]:
    """Rehydrate the rules carried in the cached auth resolution."""
    rules: list[RoutingRule] = []
    for raw in resolved.routing_rules:
        try:
            rules.append(
                RoutingRule(
                    id=str(raw["id"]),
                    rule_type=raw["rule_type"],
                    match_condition=raw.get("match_condition") or {},
                    target_model=raw.get("target_model"),
                    priority=int(raw.get("priority", 0)),
                )
            )
        except (KeyError, TypeError, ValueError):
            # A malformed rule is skipped, not fatal. The user's other rules
            # should still apply.
            _logger.warning("routing_rule_malformed", subsystem="routing")
    return rules


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
class _RoutingContext:
    """What routing decided, carried through to the ledger and the response."""

    decision: RoutingDecision | None
    reason_code: str
    model_requested: str

    @property
    def routed(self) -> bool:
        return self.decision is not None and self.decision.routed

    @property
    def model_version(self) -> str | None:
        return self.decision.model_version if self.decision else None


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

    reason_code = REASON_PASSTHROUGH

    model_requested = request.model_requested
    model_used = model_requested

    # -- [2] Budgets -------------------------------------------- UC-29, UC-30
    #
    # Deliberately OUTSIDE the Deadline and outside failopen. Every other step
    # below degrades to "forward the request unchanged" on failure; this one
    # cannot, because forwarding unchanged is exactly what a hard stop exists
    # to prevent (CLAUDE.md hard rule 1). Redis only — never Postgres
    # (hard rule 7).
    #
    # It runs before the cache lookup on purpose. A cache hit costs the user
    # nothing, so serving one over a hard stop would be defensible — but it
    # would also mean a project the user believes is stopped keeps answering
    # traffic, and "stopped" has to mean stopped.
    budget = await check_budgets(
        get_redis(), request.resolved.project_id, _budgets_for(request.resolved)
    )
    timer.mark("budget", time.perf_counter())

    if budget.blocked:
        _logger.warning(
            "budget_hard_stop",
            request_id=request.request_id,
            project_id=request.resolved.project_id,
            period=budget.period,
            reason=budget.reason,
            degraded=budget.degraded,
        )
        raise BudgetExceededError(
            _budget_message(budget),
            period=budget.period,
            limit_usd=round(budget.limit_usd, 6),
            spent_usd=round(budget.spent_usd, 6),
        )

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

    # -- [4] Routing --------------------------------------------------------
    #
    # Inside failopen with its own sub-budget: BUILD_SPEC §4 P5 requires that a
    # classifier stall past 20 ms results in passthrough logged
    # FAILOPEN_TIMEOUT, not an error and not a delayed request.
    routing: RoutingDecision | None = None

    async with failopen("routing", deadline, budget_ms=ROUTING_BUDGET_MS) as guard:
        routing = routing_decide(
            request.body,
            endpoint=request.endpoint,
            routing_enabled=request.resolved.routing_enabled,
            rules=_rules_for(request.resolved),
        )

    if guard.failed:
        # A router that broke or overran must cost the user nothing but the
        # saving it failed to find.
        routing = None
        reason_code = REASON_FAILOPEN_TIMEOUT
    elif routing is not None:
        reason_code = routing.reason_code
        model_used = routing.model

    # A soft-throttled project is over its limit but chose to degrade rather
    # than stop (UC-30). Force the cheapest equivalent model, overriding both
    # the caller's choice and the router's — this is the one case where the
    # user has explicitly asked us to prefer cost over their stated model.
    # Applied after routing so it wins, and never across providers.
    if budget.decision is BudgetDecision.THROTTLE:
        cheapest = cheaper_model_for(model_used)
        if cheapest is not None and cheapest != model_used:
            model_used = cheapest
            reason_code = REASON_BUDGET_THROTTLED
            routing = None

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

    routing_context = _RoutingContext(
        decision=routing,
        reason_code=reason_code,
        model_requested=model_requested,
    )

    if request.stream:
        return await _forward_streaming(
            request, api_key, model_used, started, deadline, cache_context, routing_context
        )
    return await _forward_unary(
        request, api_key, model_used, started, deadline, cache_context, routing_context
    )


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
    routing_context: _RoutingContext | None = None,
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
            # A routed request that got a 429 is still a routed request. Losing
            # this here would report it as a passthrough and quietly understate
            # what routing was doing when things went wrong.
            routing_context=routing_context,
        )
        return PipelineResult(
            status_code=response.status_code,
            body=raw_body,
            model_used=model_used,
            routed=routing_context.routed if routing_context else False,
            reason_code=routing_context.reason_code if routing_context else REASON_PASSTHROUGH,
            headers=_metadata_headers(
                request,
                model_used,
                cache_hit=False,
                reason_code=routing_context.reason_code if routing_context else REASON_PASSTHROUGH,
            ),
        )

    body = provider.denormalize_response(raw_body)
    usage = provider.parse_usage(raw_body) or _estimate_usage(request, body)

    # -- [7] Escalation ------------------------------------------------ UC-17
    #
    # Only after a request we routed *down*. Escalating a passthrough would
    # mean second-guessing the model the user chose, which is not our job.
    escalated = False
    if (
        routing_context is not None
        and routing_context.routed
        and request.resolved.escalation_enabled
        and model_used != routing_context.model_requested
    ):
        verdict = looks_low_confidence(body, request_body=request.body)
        if verdict.escalate:
            escalated = True
            body, usage, model_used = await _escalate(
                request,
                api_key,
                original_body=body,
                original_usage=usage,
                target_model=routing_context.model_requested,
                reason=verdict.reason,
            )

    await _record(
        request,
        model_used=model_used,
        usage=usage,
        latency_ms=latency_ms,
        status=response.status_code,
        routing_context=routing_context,
        escalated=escalated,
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
        routed=routing_context.routed if routing_context else False,
        reason_code=routing_context.reason_code if routing_context else REASON_PASSTHROUGH,
        headers=_metadata_headers(
            request,
            model_used,
            cache_hit=False,
            reason_code=routing_context.reason_code if routing_context else REASON_PASSTHROUGH,
        ),
    )


async def _forward_streaming(
    request: ProxyRequest,
    api_key: str,
    model_used: str,
    started: float,
    deadline: Deadline,
    cache_context: _CacheContext | None = None,
    routing_context: _RoutingContext | None = None,
) -> PipelineResult:
    """Streamed forward with a non-buffering tee.

    The generator below owns the upstream connection for the life of the
    stream, and records the ledger event once the client has seen the last
    byte. Bookkeeping never precedes delivery.
    """
    provider = request.provider
    payload = provider.normalize_request(request.body, model_used)
    capture = StreamCapture()

    # Note what is deliberately absent here: escalation (UC-17). It applies to
    # non-streamed requests only, and that is a limitation of streaming rather
    # than an oversight — you cannot un-send a stream. By the time a cheap
    # answer can be judged low-confidence, its tokens are already in the
    # client's hands, so the only ways to escalate would be to send a second
    # contradictory answer or to buffer the whole response before sending any
    # of it. The second destroys streaming, which most callers chose on
    # purpose. So a routed streaming request stays with the cheap model.

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
                routing_context=routing_context,
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
        routed=routing_context.routed if routing_context else False,
        reason_code=routing_context.reason_code if routing_context else REASON_PASSTHROUGH,
        headers=_metadata_headers(
            request,
            model_used,
            cache_hit=False,
            reason_code=routing_context.reason_code if routing_context else REASON_PASSTHROUGH,
        ),
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
    routing_context: _RoutingContext | None = None,
    escalated: bool = False,
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
        routed=routing_context.routed if routing_context else False,
        routing_reason_code=(
            REASON_CACHE_HIT
            if cache_hit
            else (routing_context.reason_code if routing_context else REASON_PASSTHROUGH)
        ),
        routing_model_version=routing_context.model_version if routing_context else None,
        escalation_triggered=escalated,
    )

    if timer is not None:
        timer.mark("ledger_build", time.perf_counter())

    from apicost.db.redis import get_redis

    redis = get_redis()
    if timer is not None:
        timer.mark("ledger_client", time.perf_counter())

    await emit_ledger_event(redis, event, request.settings)

    # Increment the budget counters here rather than in the worker. The
    # acceptance criterion is that a hard stop engages within *one request* of
    # the threshold; a worker draining on a 5 s cron would let everything sent
    # in those 5 s through, which at production rates is the whole overrun.
    # This is the same Redis round trip the ledger just made, and it never
    # raises.
    await record_spend(
        redis,
        request.resolved.project_id,
        cost_usd,
        [str(b.get("period")) for b in request.resolved.budgets],
    )

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
    routing_context: _RoutingContext | None = None,
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
        routing_context=routing_context,
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


def _metadata_headers(
    request: ProxyRequest,
    model_used: str,
    *,
    cache_hit: bool,
    reason_code: str = REASON_PASSTHROUGH,
    elapsed_ms: float | None = None,
) -> dict[str, str]:
    """APICost metadata, in headers only (BUILD_SPEC §0.5).

    Never in the body — the caller's SDK parses that, and a stray field is a
    breaking change to somebody's application.
    """
    headers = {
        "X-APICost-Request-Id": request.request_id,
        "X-APICost-Cache": "hit" if cache_hit else "miss",
        "X-APICost-Model-Used": model_used,
        "X-APICost-Reason": reason_code,
    }
    if elapsed_ms is not None:
        # The time *we* added, measured inside the process. A user comparing us
        # against calling the provider directly deserves that number without
        # having to trust a marketing page, and it is the only latency figure
        # that isolates APICost from the network on either side of it.
        headers["X-APICost-Latency-Ms"] = f"{elapsed_ms:.2f}"
    return headers


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

    headers = _metadata_headers(request, hit.model_used, cache_hit=True, elapsed_ms=latency_ms)

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


async def _escalate(
    request: ProxyRequest,
    api_key: str,
    *,
    original_body: dict[str, Any],
    original_usage: Usage,
    target_model: str,
    reason: str,
) -> tuple[dict[str, Any], Usage, str]:
    """Retry once on the stronger model and return that answer instead — UC-17.

    Two things this deliberately does not do:

    * It does not retry more than once. A second escalation would mean three
      paid calls for one request, and by then routing has clearly cost the user
      money rather than saved it.
    * It does not discard the cheap call's tokens. The returned usage is the
      **sum of both attempts**, because that is what the user was charged. The
      savings report has to show routing losing money on endpoints where
      escalation fires often — that is the signal telling them to exclude it
      (CODEBASE_GUIDE §12), and hiding it would make the number a lie.
    """
    provider = request.provider
    payload = provider.normalize_request(request.body, target_model)

    try:
        response = await get_http_client(request.settings).post(
            provider.endpoint_url(request.endpoint),
            json=payload,
            headers={
                **provider.auth_headers(api_key),
                "Content-Type": "application/json",
            },
        )
        if response.status_code >= 400:
            # The stronger model failed where the cheap one merely disappointed.
            # Return the answer we already have rather than nothing.
            _logger.warning(
                "escalation_failed",
                request_id=request.request_id,
                status=response.status_code,
            )
            return original_body, original_usage, target_model

        raw = response.json()
    except (httpx.HTTPError, ValueError):
        _logger.warning("escalation_unreachable", request_id=request.request_id)
        return original_body, original_usage, target_model

    stronger_body = provider.denormalize_response(raw)
    stronger_usage = provider.parse_usage(raw) or _estimate_usage(request, stronger_body)

    combined = Usage(
        tokens_in=original_usage.tokens_in + stronger_usage.tokens_in,
        tokens_out=original_usage.tokens_out + stronger_usage.tokens_out,
        estimated=original_usage.estimated or stronger_usage.estimated,
    )

    _logger.info(
        "request_escalated",
        request_id=request.request_id,
        reason=reason,
        to_model=target_model,
        tokens_total=combined.total,
    )
    return stronger_body, combined, target_model
