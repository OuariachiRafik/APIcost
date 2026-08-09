# P7 — Prompt & context optimization

**Use cases:** UC-26, UC-27, UC-28

## Acceptance criteria

| # | Criterion | Result |
|---|---|---|
| 1 | Long-context warning: over a token threshold **and** low relevance overlap with the latest message | ✅ both conditions required; a long coherent conversation of the same size is not flagged |
| 2 | Compression suggestion with before/after token counts, **advisory only** | ✅ asserted against what the *provider* received, not our response |
| 3 | Token-heavy endpoint report (UC-28) | ✅ ranked by average, not volume |

445 tests pass. Lint and `mypy --strict` clean.

## What shipped

- `advisor/prompts.py` — pure. Length **and** relevance, Jaccard overlap on content words.
- Migration 0011 — `context_warning`, `context_reclaimable_tokens`, `context_message_count` on the
  ledger, with a partial index on the warning.
- `X-APICost-Context-Warning` and `-Reclaimable-Tokens` response headers (hard rule 6).
- `POST /advisor/compress`, `GET /advisor/context`, `GET /advisor/token-heavy`.

## Decisions worth knowing

**Length alone is not the signal.** A long history that is all relevant — a document being edited, a
debugging session — is long for a reason, and warning about it teaches the user to ignore warnings.
The test that matters here builds two conversations of the *same size* and asserts only the drifted
one is flagged.

**The suggestion drops messages; it never summarises them.** Summarising history with an LLM costs
money to save money and silently changes what the model is told. A dropped message is something the
user can see and reason about. `strategy` is reported so this stays visible.

**The system prompt and the final exchange are never called stale.** The system prompt sets behaviour
rather than supplying facts, so lexical overlap says nothing useful about it and advising someone to
drop it would break their application.

**`/advisor/compress` takes a body, not a request id.** Raw prompts are not stored unless the project
opted in (hard rule 9), so an id-based endpoint would work for some users and fail for exactly the
privacy-conscious ones. This works for everybody and stores nothing.

**UC-28 ranks by average, not total.** Total ranks by traffic and tells the user what they already
know. The average finds the endpoint whose *shape* is expensive.

## Defect found: embeddings traffic was under-counted in the ledger

`_estimate_usage` built its prompt text from `messages` only. An `/v1/embeddings` request carries
`input`, and the legacy completions API carries `prompt` — so whenever a provider omitted usage,
both were ledgered at near-zero tokens.

The consequence was not confined to this phase. Token counts feed cost, and cost feeds the spend
dashboard, the budget counters, the savings math, and the anomaly baselines. Embeddings-heavy traffic
would have looked almost free everywhere in the product.

Found because the UC-28 report ranked a 2,000-token embeddings call *below* four two-word chat calls.
`_prompt_text` now handles all three shapes, including multimodal content parts.

The e2e stub also hardcoded `prompt_tokens: 8` for embeddings regardless of input, which would have
kept any token-volume test from ever noticing. It now scales with the input, as a real provider does.

## A limitation, stated rather than hidden

**Relevance is lexical, not semantic.** Jaccard overlap on content words does not know that "revenue"
and "sales" are related, so a conversation that restates its subject in different vocabulary can be
scored as drifted. The threshold is deliberately permissive (0.08) to bias toward silence, and the
warning is advisory, so the cost of a miss is an unnecessary suggestion rather than a broken request.

The embedding model is already loaded in the proxy and would do this better. It was not used because
it costs ~10 ms per message against a 5 ms advisory budget, and UC-26 is not worth spending a request's
latency on. Revisit if the advisory moves off the hot path.

## Not built

No UI. `web/src/routes/` has no context report, compression preview, or token-heavy screen.
