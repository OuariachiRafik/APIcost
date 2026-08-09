# ADR 0009 — API-first sequencing, and reconciling the surface with §8

**Status:** accepted · **Date:** 2026-08-09 · **Phases:** P4–P8 (retrospective)

## Context

Phases P4 through P8 each shipped their backend complete and tested, and each ended with a "Not
built: no UI" note. Five phases, five footnotes, no decision recorded anywhere. That is how a
standing choice hides, and it is what this ADR should have been written to cover the first time.

The choice was defensible. Every acceptance criterion in BUILD_SPEC §4 is backend-testable, and the
traceability matrix in §10 maps every single use case to a backend artefact — an endpoint or a
module. Not one row names a web route. By the spec's own mapping, a phase is satisfied when its
endpoints exist and pass their tests.

It is also a little convenient, because the use cases are worded from the user's side: *"I need to be
able to **see** a cache performance report"*, *"**view** a history of past alerts"*. A solo developer
does not read JSON. UC-20 through UC-37 are satisfied **as §10 defines them** and not **as a user
would experience them**, and the gap between those two readings is six screens: `cache`, `routing`,
`budgets`, `alerts`, `advisor`, `settings`.

Checking §8 and §10 while writing this turned up three places where the shipped surface had drifted,
including one piece of duplication.

## Decision

**1. API-first is the sequencing, stated once.** Backend and endpoints per phase; the frontend after
P10, against a stable and generated client. The reason is that `web/src/lib/api.ts` is typed against
the API, and building screens against a surface still being renamed means writing the same code
twice — which is exactly what the reconciliation below would have caused.

**2. `GET /advisor/prompt-optimizations` replaces `/advisor/context` and `/advisor/token-heavy`.**
§8 names this endpoint for UC-26, 27 and 28 together. I had built two, neither with that name.

**3. `GET /advisor/token-heavy` was redundant and is gone.** §10 maps UC-28 to
`GET /usage/breakdown?by=endpoint`, which shipped in P3 and already returns `avg_tokens` per
endpoint. I built a second endpoint for a use case that was already satisfied, because I did not read
the matrix before building. The ranking now lives inside `prompt-optimizations`, where it sits next
to the context warnings that make it actionable; `/usage/breakdown` remains the general-purpose
aggregate. There is still overlap between the two, and that is a deliberate, bounded duplication
rather than an accidental one.

**4. `GET /alerts` becomes `GET /alert-events`,** matching §10. Same for the resolve sub-resource.

**5. `POST /advisor/compress` stays, despite §8 folding UC-27 into the GET.** Generating a compressed
candidate requires the prompt itself, and prompts are not stored unless the project opts in (hard
rule 9). A GET over history cannot serve UC-27 for the users who most want the privacy guarantee. So
the report is a GET and the suggestion is a POST, and this is a deliberate deviation from §8's
one-endpoint framing.

**6. Cursor pagination on `/alert-events` only.** §8 says "cursor pagination on all list endpoints".
`alert_events` is the only new table that grows without bound. Budgets are capped at one per period
per project by a unique constraint, and recommendations are replaced wholesale nightly. Both are
bounded by construction, and paginating a list that cannot exceed a handful of rows adds a cursor
the caller must thread for no benefit.

## Consequences

- The surface now matches §8 and §10 except where item 5 says otherwise, and the exception is
  recorded rather than silent.
- **The product still has no UI, and that is now a stated position rather than a repeated footnote.**
  Nothing a user can see exists for caching, routing, budgets, alerts, or the advisor.
- Renaming endpoints before generating the TypeScript client means the generated client is correct
  the first time.
- Item 3 is worth remembering as a process failure, not just a code one: the traceability matrix
  exists precisely to prevent building something twice, and it only works if it is read before the
  building rather than during the audit.
