# ADR 0005 — react-router for dashboard navigation

**Status:** accepted · **Date:** 2026-08-05 · **Phase:** P3

## Context

BUILD_SPEC §2 locks the frontend stack: React 18, TypeScript, Vite, Tailwind, TanStack Query,
Recharts. It names no router. §3 nonetheless lists a `web/src/routes/` directory holding "onboarding,
dashboard, requests, cache, routing, budgets, alerts, advisor, settings" — nine destinations.

P1 needed no router: two states, signed out and signed in, switched on a boolean. P3 introduces real
navigation, and several requirements are navigation requirements rather than rendering ones:

- the request log (UC-12) needs shareable per-request URLs so a user can send a colleague a link to
  the row they are asking about;
- filters and date ranges belong in the query string, or a refresh loses the user's place;
- the detail drawer needs to be addressable, and to restore correctly on reload.

Hand-rolling this is not a small amount of code — history integration, param parsing and
serialisation, nested layouts, scroll restoration — and every line of it is a thing the team would
own and debug instead of using the library that already solved it.

## Decision

Add `react-router-dom` and use its data router.

This is an **addition** to §2's list, not a substitution: nothing locked is being replaced or
"improved". The locked choices all stand — React 18, Vite, Tailwind, TanStack Query, Recharts are
exactly as specified. The gap being filled is one §2 did not address while §3 clearly assumed.

## Consequences

- One more frontend dependency, widely used and stable, with types included.
- URLs become part of the product surface: `/requests?model=gpt-4o&cursor=...` is now a thing users
  can bookmark and share, which is the point.
- TanStack Query keeps owning data fetching and caching. The router owns navigation only. Loaders
  are deliberately *not* used for data — that would split fetching across two systems.
- If this is unwanted, the containment is small: routing lives in `web/src/main.tsx` and
  `web/src/routes/`, and the components themselves take props rather than reading router state
  directly wherever that was practical.
EOF
