# P1 — Auth, projects, keys, vault

**Use cases:** UC-01, UC-02, UC-03, UC-04, UC-05, UC-07
**Commit:** `20d247e`

## Acceptance criteria

| # | Criterion | Result |
|---|---|---|
| 1 | signup → provider key → project → proxy key → instructions, without leaving the wizard | ✅ `Onboarding.test.tsx` plus a live cURL walkthrough |
| 2 | Revocation rejects within 1 s; other projects unaffected | ✅ measured **32 ms**; cache purged in the same operation as the DB write |
| 3 | No API path returns a provider key in plaintext | ✅ swept over every GET in the OpenAPI document, plus a raw-row ciphertext check |

129 backend tests, 12 web tests. Lint and types clean; migration reversible.

## What shipped

- `core/security.py` — Argon2id passwords, pinned-algorithm JWTs, proxy-key generation. SHA-256 for
  proxy keys rather than Argon2: 192 bits of CSPRNG output has no dictionary to attack, and the
  proxy verifies them on the hot path where an Argon2 verify would blow the latency budget.
- `vault/` — `KMSClient` protocol with `LocalKMS` and `AwsKmsClient` behind one interface; a fresh
  AES-256-GCM data key per provider key, wrapped by the master key.
- Migration 0002 — users, refresh_tokens, provider_keys, projects, proxy_keys, all with RLS.
- Routers for auth, keys, projects, proxy-keys.
- Web: signup/login and a four-step onboarding wizard ending in copy-pasteable base-URL swaps.

## Defects found

Three, all of which would have shipped looking correct.

**1. Row-level security was completely inert.** The app connected as `POSTGRES_USER`, which the
Postgres image creates as a **superuser** — and superusers bypass RLS unconditionally, `FORCE`
included. Every policy existed, read correctly, and did nothing. Fixed with two roles: `apicost`
owns the schema and runs migrations; `apicost_app` is `NOSUPERUSER NOBYPASSRLS` and is what the
application connects as.

**2. The policies broke on the second request a connection served.** After a transaction-local
`set_config` commits, the Postgres setting reverts to the **empty string, not unset**. Policies
testing `IS NULL` therefore failed on any pooled connection that had already served one scoped
request — login worked exactly once per connection, then 401'd. Fixed with `NULLIF(..., '')`. The
regression test dirties the pool deliberately, because a single-request test passes either way.

**3. Reuse detection revoked a token family and then threw the revocation away.** It wrote the
`UPDATE` and raised `401`, which rolled the transaction back. A leaked family stayed live while the
log claimed it had been revoked. The revocation now runs in its own transaction.

## Decisions recorded

- [ADR 0004](../adr/0004-spa-token-storage.md) — access token in memory, refresh token in
  `localStorage`. **This is the weakest link in the current security posture and it is deliberate.**
  An XSS bug in the dashboard yields a refresh token. Compensating controls: rotation with family
  revocation, and no key material on any response. The real fix is httpOnly cookies plus CSRF.
  Revisit before public exposure.
