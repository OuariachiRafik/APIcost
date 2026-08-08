# P5 — Intelligent routing

**Use cases:** UC-14, UC-15, UC-16, UC-17, UC-18, UC-19

## Acceptance criteria

| # | Criterion | Result |
|---|---|---|
| 1 | Routing and caching savings reported separately, never double-counted | ✅ asserted end to end and in the SQL |
| 2 | A routing exception or >20 ms classifier stall → passthrough, not an error | ✅ both injected and verified |

32 routing unit tests, 13 routing e2e tests. Lint and types clean across 110 files.

## What shipped

- `routing/features.py` — 16 cheap features, all scaled to `[0, 1]`. Pure, and shared verbatim
  between training and serving so the two cannot drift.
- `routing/rules.py` — override and exclude, evaluated **before** the classifier. Exclude beats
  override at equal priority: "do not touch this" is a stronger statement than "use model X".
- `routing/classifier.py` — calibrated logistic regression from a versioned joblib artifact.
  Returns `None` on any problem, and `None` means passthrough.
- `routing/seed_dataset.py` + `train.py` — 350 hand-labelled prompts, **90.3% 5-fold accuracy**.
- `routing/escalation.py` — low-confidence detection (UC-17).
- `routing/engine.py` — the decision, with the UC-16 reason-code vocabulary.
- Migration 0008 (`routing_rules`), `POST/GET/DELETE /routing-rules`, `GET /routing/stats`.

## Decisions worth knowing

**Routing is off by default.** Caching returns the same answer more cheaply; routing returns a
*different* answer. Something that changes which model replies to your users should be opt-in.

**Uncertainty means passthrough.** Below 0.70 confidence the classifier's opinion is discarded and
the request goes where the caller asked. Routing wrongly costs a user a bad answer inside their
product; not routing costs a few cents. The threshold encodes that asymmetry, and it is deliberately
conservative while the artifact is trained only on seed data (CODEBASE_GUIDE §13).

**Routing never crosses providers.** The user holds a key for one provider, and switching would bill
an account they did not choose.

**Escalation counts both calls.** A retried request records the *sum* of the cheap attempt and the
strong one, because that is what the user was charged. `GET /routing/stats` therefore reports
`savings = gross − escalation_cost`, which **can go negative**. That is not a bug to hide — it is the
signal telling the user to exclude that endpoint (CODEBASE_GUIDE §12).

**Rules apply even when routing is disabled.** An exclusion is a user instruction, not an
optimization, so turning routing off does not discard it.

## Two performance defects, both mine, both found by measuring

**1. The classifier was ~95 ms per prediction, against a 20 ms budget.** I built it as
`CalibratedClassifierCV(cv=5)`, which keeps five calibrated pipelines and runs *all of them* on every
prediction. The consequence would have been invisible in review and obvious in production: the
router would have blown its budget on every request and failed open permanently — working perfectly
in tests, never routing a single real request.

Switching to `ensemble=False` fits one calibrated model over cross-validated predictions instead of
five. Calibration is kept, prediction cost drops ~4×, and 5-fold accuracy went **up**, 0.903 → 0.920.

**2. The ledger had 22 monthly partitions and 139 relations.** Migration 0005 provisioned 18 months
backward — my over-correction for P3's DEFAULT-partition bug. The cost showed up as a test suite that
went from ~90 seconds to over eight minutes, and tracing it found a single `TRUNCATE` of the fixture
tables taking **6.7 seconds on an idle database**, run twice per test.

Migration 0009 trims the window to 2 months back and 3 ahead, dropping only **empty** partitions —
one holding rows is somebody's usage history, whatever its age. 139 relations → 37, TRUNCATE 6.7 s →
2.9 s. The fixtures then switched from `TRUNCATE` to `DELETE`, which is 75 ms, because on tables this
small TRUNCATE's per-partition DDL work is pure overhead.

That last change contained a trap worth recording: **`TRUNCATE` is exempt from row-level security and
`DELETE` is not.** Run through the application role, the new cleanup would have matched zero rows and
silently cleaned nothing, leaving every test to inherit the previous one's data. The fixtures use the
admin engine for exactly this reason.

## Defects found and fixed

**1. The provider-error path dropped the routing context.** A routed request that came back 429 was
ledgered as a passthrough — understating what routing was doing precisely when things went wrong.
Found by reading the forward functions rather than trusting that earlier string-replace edits had
landed, after that exact failure mode caused the P4 cache bug.

**2. Project settings and new rules took up to 60 s to apply.** Both are carried in the cached
`ResolvedKey`, so creating a rule appeared to do nothing and then start working on its own. Rule
creation and deletion now purge the auth cache, as revocation and settings changes already did.

## One operational note

**Proxy startup now takes seconds.** The embedding model and the classifier both load before the
process accepts traffic. That is the right tradeoff — the alternative is one unlucky user paying for
it mid-request — but a rolling deploy's readiness probe needs a matching grace period. The e2e
harness's startup wait had to go from 4 s to 60 s for the same reason.

## A limitation, stated rather than hidden

**Escalation applies to non-streamed requests only.** You cannot un-send a stream: by the time a
cheap answer can be judged low-confidence, its tokens are already with the client. The only
alternatives would be sending a second, contradictory answer, or buffering the entire response
before sending any of it — and the second destroys streaming, which most callers chose deliberately.
So a routed streaming request stays on the cheap model. The pipeline says so at the point where the
check would otherwise go.

This matters for the savings math: streamed traffic gets routing's upside without escalation's
safety net, so a user routing quality-critical *streaming* endpoints has less protection than the
same endpoint unstreamed. An exclusion rule is the remedy (UC-19).

## Not built

No routing UI yet. The endpoints exist and are tested; `web/src/routes/` has no rules editor or
routing-stats screen.
