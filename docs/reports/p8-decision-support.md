# P8 — Decision support & advisory

**Use cases:** UC-35, UC-36, UC-37

## Acceptance criteria

| # | Criterion | Result |
|---|---|---|
| 1 | Nightly ARQ job over each user's usage history | ✅ `advisor_recommendations_job`, 03:20 |
| 2 | Downgrade recommendations with confidence and observed sample size (UC-35) | ✅ min sample 30, escalation ceiling 2%, confidence from sample size |
| 3 | Break-even advisor at `GET /advisor/breakeven` (UC-36), with §6.7's four fixes | ✅ all four, each pinned by tests |
| 4 | Every recommendation carries a projected dollar impact (UC-37) | ✅ `projected_savings_usd`, which also decides the ordering |

482 tests pass. Lint and `mypy --strict` clean across 18 files. Migrations reverse and reapply.

## What shipped

- `advisor/breakeven.py` — pure. UC-36 with the four §6.7 corrections.
- `advisor/downgrade.py` — pure. UC-35/UC-37 from observed routing history.
- `advisor/nightly.py` — the job. Regenerates open recommendations per project.
- Migration 0012 (`advisor_recommendations`) with RLS.
- `GET /advisor/recommendations`, `POST /advisor/recommendations/{id}/status`,
  `GET /advisor/breakeven`.

## The four break-even fixes

BUILD_SPEC §6.7 describes supplied code with four defects. **The code itself is not in the
repository** — §6.7 describes it but no code block exists — so this is an implementation from the
description, with each fix marked at the point it is made.

**Fix 2 — no advice below a meaningful volume.** The original computed `n_gpus = 0` at zero traffic,
hence a GPU cost of $0, hence "self-host" to someone with no usage. Below 1M tokens/month the answer
is `insufficient_data`, and it still explains itself.

**Fix 3 — utilization.** Assuming peak throughput for all 730 hours assumes traffic arrives perfectly
evenly. Default 0.5, and the assumption is stated in the payload rather than buried.

**Fix 4 — caveats in the response.** Ops burden, idle cost, model quality, no SLA, cold starts, and
the utilization assumption. They travel with the number: a caveat the frontend can forget to render
is a caveat that will be forgotten.

**Fix 1 — the step function — turned out to be more interesting than the brief suggests.**

The stated problem is that the naive formula divides one GPU's monthly cost by `cost_per_token`,
ignoring `n_gpus`. The obvious fix is to search each step for a crossing. I wrote that, then a test
premise failed and the reason was worth more than the test.

Within step `n` the GPU cost is flat at `n × step_cost` while the API cost rises linearly, so they
cross at `n × step_cost / cpt`. That crossing is real only if it falls inside the step:

```
n × step_cost / cpt  ≤  n × capacity      ⟺      step_cost / cpt  ≤  capacity
```

**The `n` cancels.** Cost and capacity both scale linearly with GPU count, so whether a break-even
exists does not depend on volume at all. It reduces to comparing two per-token prices — the GPU's
cost per token of capacity against the API's cost per token. Either self-hosting wins from the first
GPU, or it never wins.

Which sharpens what the original defect actually was. The naive answer exceeds one GPU's capacity
*exactly* when `step_cost / cpt > capacity` — the condition for there being **no break-even at all**.
Its single wrong case is the case where the honest answer is "never", and there it confidently
reports a volume the user could aim for and would never reach.

So the implementation is a closed form with the derivation in its docstring, not a loop. The loop
was correct and obscured the fact that the search was unnecessary. Three tests pin it: `None` where
the naive formula invents a number, a reported break-even that is genuinely break-even when
evaluated, and the n-cancels property asserted directly so a regression to per-step search would show.

## Decisions worth knowing

**Downgrade advice comes only from routing's own record.** The claim is narrow: on this endpoint,
requests that ran on the cheap tier came back good enough that escalation never fired. A project that
has never enabled routing gets no downgrade advice, because anything else would be a guess dressed as
evidence.

**Escalation is disqualifying, not just discounted.** Above a 2% escalation rate the recommendation
is suppressed entirely. Escalation already cost that user two calls; recommending more of it compounds
the error.

**Recommendations are replaced nightly, never accumulated.** A recommendation is a statement about
current usage, and a list that only grows is one users stop reading. Adopted and dismissed rows are
the user's decisions and are not overwritten.

**A dismissed recommendation is never resurrected.** Re-suggesting something the user rejected every
morning is nagging, not persistence. The job filters against dismissed titles.

**Break-even is only surfaced as a recommendation when self-hosting wins.** "Keep using the API" is
the status quo and does not belong on a list of things to consider doing. The endpoint always answers;
the nightly job only files a row when the answer is `gpu`.

**Break-even confidence is always `low`.** It compares infrastructure cost against list prices and
cannot see the user's tolerance for operating a GPU. It is a prompt to investigate, not a conclusion.

## A limitation, stated rather than hidden

**The GPU price table is hardcoded and will go stale.** `GPU_OPTIONS` in `advisor/nightly.py` holds
on-demand list prices and throughput figures entered by hand. Nothing refreshes them, and nothing
warns when they are old. Throughput in particular is model- and context-length-dependent, and a
single `max_tokens_per_second` per instance type is a rough figure standing in for a distribution.
The caveats say the comparison is infrastructure-only; they do not say the prices are a snapshot.
Before this is shown to real users it needs either a maintained source or a visible "as of" date.

## Not built

No UI for any of it. Recommendations, break-even, and the dismiss action exist as endpoints only.
