# ADR 0004 — Access token in memory, refresh token in localStorage

**Status:** accepted, with a known follow-up · **Date:** 2026-08-05 · **Phase:** P1

## Context

BUILD_SPEC §2 fixes the session scheme — "JWT access (15 min) + rotating refresh token (30 d, stored
hashed)" — but says nothing about where the browser keeps them. The options carry real differences:

| Where | Survives reload | Readable by injected JS | Needs CSRF defense |
|---|---|---|---|
| Memory only | no | no | no |
| localStorage | yes | **yes** | no |
| httpOnly cookie | yes | no | **yes** |

The product stores users' provider API keys. An attacker with a session token cannot read those back —
the API has no path that returns key material (enforced by a test) — but they could issue proxy keys,
create projects, and run up spend against the victim's provider account. So session theft is
materially damaging even though key theft is not directly possible.

## Decision

Split by lifetime:

- **Access token in memory only.** Never written to storage. Lives 15 minutes. An injected script has
  to be running at the time to capture it, and loses it on reload.
- **Refresh token in `localStorage`.** Survives a page reload, so users are not asked to log in every
  time they refresh the tab.

Refresh-token rotation already limits the damage: a stolen refresh token can be used once, and the
moment either the attacker or the legitimate client rotates again, the reuse is detected and the
whole family is revoked. Theft becomes a detected, self-terminating event rather than durable access.

## Consequences

- **This is the weakest link in P1's security posture, and it is deliberate.** An XSS bug in the
  dashboard yields a refresh token. The compensating controls are rotation with family revocation
  (implemented), no key material on any response (implemented and tested), and CSP plus dependency
  hygiene (not yet).
- The fix that actually closes it is httpOnly, SameSite=Strict cookies, which requires the API to set
  cookies, a CSRF token scheme for state-changing requests, and CORS changes. That is a coherent
  chunk of work, not a tweak, and it does not block anything in P1–P4.
- **Revisit before the product is exposed to the public internet.** Local development and private
  beta are within the risk this decision accepts; a public launch is not.
