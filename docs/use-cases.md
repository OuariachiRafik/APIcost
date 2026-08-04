# APICost — Use Case Catalog

*Individual / Persona A Edition — organized by category.*

Every use case is written in the standard user-story form: "As a user, I need to be able to …". Each
has a unique ID (`UC-##`) so it can be traced directly to the phases, features, and endpoints in
`BUILD_SPEC.md` (see the traceability matrix in §10 of that document).

| Category | Count |
|---|---|
| A. Onboarding & Setup | 7 |
| B. Visibility & Reporting | 6 |
| C. Cost Reduction — Intelligent Routing | 6 |
| D. Cost Reduction — Semantic Caching | 6 |
| E. Cost Reduction — Prompt & Context Optimization | 3 |
| F. Budgeting & Safety Controls | 6 |
| G. Decision Support & Advisory | 3 |
| H. Engagement & Retention | 2 |
| **Total** | **39** |

---

## A. Onboarding & Setup

*Getting a new user from signup to their first proxied request.*

**UC-01** — As a user, I need to be able to create an account and log in securely.

**UC-02** — As a user, I need to be able to add my own provider API key (OpenAI/Anthropic/Gemini) and
have it stored securely, without ever seeing it displayed again in plaintext after I add it.

**UC-03** — As a user, I need to be able to remove or rotate a connected provider key at any time.

**UC-04** — As a user, I need to be able to create separate projects (e.g. 'production', 'staging',
'side-project-X') so my usage, rules, and budgets don't mix across apps.

**UC-05** — As a user, I need to be able to generate a proxy key for a project and get clear
instructions for swapping my application's API base URL to point at the proxy.

**UC-06** — As a user, I need to be able to send a test request through the proxy and get immediate
confirmation that the connection is working end-to-end before I rely on it in production.

**UC-07** — As a user, I need to be able to revoke a proxy key immediately if I suspect it has
leaked, without affecting my other projects.

---

## B. Visibility & Reporting

*Understanding where money is currently going, even before any optimization is turned on.*

**UC-08** — As a user, I need to be able to see my total spend over a selected time range (today,
this week, this month, custom range).

**UC-09** — As a user, I need to be able to see my spend broken down by model, so I know which models
are driving my costs.

**UC-10** — As a user, I need to be able to see my spend broken down by project, so I can compare
costs across different apps.

**UC-11** — As a user, I need to be able to see a distribution of my request sizes (token counts), so
I can tell whether most of my calls are short/simple or long/complex.

**UC-12** — As a user, I need to be able to see, per request, whether it was served from cache,
routed to a different model than requested, or passed through unchanged, so I can understand exactly
what the system did on my behalf.

**UC-13** — As a user, I need to be able to export my usage data (e.g. as CSV) for my own records or
to justify the tool's value to a co-founder or investor.

---

## C. Cost Reduction — Intelligent Routing

*Automatically sending each request to the cheapest model capable of handling it well.*

**UC-14** — As a user, I need to be able to turn automatic model routing on or off globally, or per
project.

**UC-15** — As a user, I need to be able to define a manual routing rule that overrides automatic
routing for specific cases (e.g. 'always use model X for this project').

**UC-16** — As a user, I need to be able to see, for any given request, why the router chose the
model it chose (reason/confidence shown in the request log).

**UC-17** — As a user, I need to be able to enable automatic escalation to a stronger model when a
cheap-tier response looks low-confidence, so I don't sacrifice quality on cases that actually needed
the stronger model.

**UC-18** — As a user, I need to be able to see a summary of how much I've saved specifically from
routing, separate from savings due to caching.

**UC-19** — As a user, I need to be able to exclude specific endpoints or projects from automatic
routing entirely (e.g. a quality-critical customer-facing feature I don't want touched).

---

## D. Cost Reduction — Semantic Caching

*Avoiding paying for a provider call at all when a sufficiently similar request was already answered.*

**UC-20** — As a user, I need to be able to turn semantic caching on or off globally, or per project.

**UC-21** — As a user, I need to be able to adjust the similarity threshold that determines what
counts as a cache hit, so I can trade off cache hit-rate against response freshness.

**UC-22** — As a user, I need to be able to set a time-to-live (TTL) on cached responses so stale
answers automatically expire.

**UC-23** — As a user, I need to be able to manually clear the cache for a project if I've changed
something upstream (e.g. a prompt template) and don't want stale cached responses served.

**UC-24** — As a user, I need to be able to mark specific requests or endpoints as non-cacheable
(e.g. anything time-sensitive or containing randomness).

**UC-25** — As a user, I need to be able to see my cache hit rate and the dollar amount saved from
caching specifically.

---

## E. Cost Reduction — Prompt & Context Optimization

*Reducing the token footprint of requests themselves, independent of which model serves them.*

**UC-26** — As a user, I need to be able to see a warning when a request is resending a large
conversation history that could likely be trimmed or summarized.

**UC-27** — As a user, I need to be able to see a suggested, compressed version of a long prompt
alongside the token-count difference, before deciding whether to adopt it.

**UC-28** — As a user, I need to be able to see which of my endpoints have the highest average token
count, so I know where prompt optimization would have the biggest impact.

---

## F. Budgeting & Safety Controls

*Protecting against overspend, runaway loops, and key leaks.*

**UC-29** — As a user, I need to be able to set a spend budget (daily, weekly, or monthly) per
project.

**UC-30** — As a user, I need to be able to choose what happens when a budget is exceeded: alert me
only, throttle further requests, or hard-stop the proxy for that project.

**UC-31** — As a user, I need to be able to receive a real-time alert when my spend rate suddenly
spikes compared to my normal pattern.

**UC-32** — As a user, I need to be able to receive a real-time alert if usage patterns suggest my
proxy key may have leaked or is being abused.

**UC-33** — As a user, I need to be able to immediately kill/revoke a project's proxy access in one
action if I confirm a leak or a runaway loop.

**UC-34** — As a user, I need to be able to view a history of past alerts and whether/how they were
resolved.

---

## G. Decision Support & Advisory

*Turning the user's own usage history into concrete, personalized recommendations.*

**UC-35** — As a user, I need to be able to see personalized recommendations for which requests could
safely move to a cheaper model tier, based on my own historical usage.

**UC-36** — As a user, I need to be able to see whether, at my current volume, self-hosting or a
dedicated deployment would be cheaper than my current pay-per-token usage.

**UC-37** — As a user, I need to be able to see the projected dollar savings of each recommendation
before deciding whether to adopt it.

---

## H. Engagement & Retention

*Keeping the value of the product visible over time, beyond the initial setup.*

**UC-38** — As a user, I need to be able to receive a weekly digest email summarizing my spend,
savings, and any notable events from the past week.

**UC-39** — As a user, I need to be able to see how my cost-per-request compares to similar
anonymized usage patterns, so I have a sense of whether I'm already well-optimized.
