# APICost — Frontend Specification (v1)
### Individual / Persona A Dashboard — For Implementation by Claude Code

This document resolves every open decision from the blocking-questions list using the brand/visual
guidance provided, and gives concrete, implementable specs: design tokens, component behavior,
screen-by-screen requirements, copy, and build order. Where a decision was made on the team's
behalf (rather than being explicit in the source material), it is marked **[Decision]** with a
one-line rationale so it can be revisited.

---

## 1. Purpose & Scope

Build the web dashboard (control plane) for APICost's individual/proxy edition: seven authenticated
routes — **cache, routing, budgets, alerts, advisor, settings, billing** — plus the onboarding flow
that gets a new user from signup to a verified proxy connection. This spec covers the dashboard app
only; a brief landing-page section is included at the end since visual assets for it were provided,
but the dashboard is the priority build.

Reference source documents: `apicost_system_design.pdf` (architecture/API), `apicost_use_cases.pdf`
(UC-01 to UC-39), `apicost_feature_backlog.pdf` (epics/features). This spec maps directly onto that
feature backlog — do not re-derive scope, only presentation and interaction.

---

## 2. Brand & Visual System

### 2.1 Logo
Wordmark only for v1 — no abstract mark. Render "APICost" in the UI font, bold weight, off-white on
dark backgrounds. **[Decision]** Do not commission an icon/mark yet; the source guidance explicitly
suggested this is acceptable since the name is self-explanatory.

Brand usage rules (from source guidance — apply to any exported/marketing use of the wordmark):
- Use on dark backgrounds or light surfaces only.
- Maintain clear space around the mark equal to the height of the mark itself.
- Never change the color or proportions of the mark.
- Use the off-white variant for text on dark backgrounds, for readability.

### 2.2 Color Tokens

Dark mode is the **default and primary target**. Build with CSS variables / a token system so light
mode can be added later without a redesign, but light mode is **out of scope for v1** — do not build
a theme switcher. **[Decision]** The source guidance was explicit that the target audience (developers
in their 20s) skews dark-mode-native; shipping one polished theme beats two half-finished ones.

```css
:root[data-theme="dark"] {
  --bg-page:        #0B0F14;  /* near-black navy — page background in dark mode */
  --bg-surface:     #111820;  /* dark slate — cards, panels, table rows */
  --text-primary:   #F1F5F9;  /* off-white — primary text on dark backgrounds */
  --text-secondary: #94A3B8;  /* muted slate gray — labels, secondary text (derived, not in source palette — tune in review) */
  --border-subtle:  #1E2733;  /* derived — card/table borders, one step up from bg-surface */

  --accent-positive: #22C55E; /* green — savings, positive deltas, "Cached" badge */
  --accent-warning:  #F59E0B; /* amber — warnings, "High Cost" badge, threshold alerts */
  --accent-critical: #EF4444; /* red — critical alerts, hard_stop / kill-switch confirmations */
  --accent-info:     #3B82F6; /* blue — informational badges, links, info-level alerts */
  --accent-neutral:  #F1F5F9; /* off-white — "Optimized" badge, neutral status */
}
```

**Explicit constraint from source guidance: no purple in brand/UI chrome.** This does not apply to
syntax highlighting inside code blocks, which should use a standard developer-familiar theme (see
2.5) even if that theme includes purple for keywords — that's a separate, expected convention.

The three accent colors beyond the literal source palette (positive/warning/critical/info) are
**[Decision]**: the source only specified navy/slate/off-white plus badge examples; a dashboard
showing money, savings, and risk needs semantic status color beyond a monochrome palette. Keep
saturation low/muted to stay consistent with the "minimal, data-forward" visual style — these should
read as quiet status signals, not loud UI decoration.

### 2.3 Typography

- **UI font:** A clean geometric/grotesque sans (e.g. Inter). **[Decision]** — not specified in
  source guidance; Inter is the de facto standard for this exact visual category (Linear, Vercel,
  Helicone-style dashboards) and pairs well with the monospace requirement below.
- **Monospace font:** Required by source guidance for "numbers/pricing" and all code. Use a
  developer-familiar monospace (e.g. JetBrains Mono or IBM Plex Mono). **[Decision on exact family]**
  — apply monospace to: every dollar figure, every percentage, every token count, table numeric
  columns, and all code snippets.
- Headline numbers (stat cards) should be large and bold in the monospace font — see the reference
  UI example (`$1,284.75`, `32.1%`, `gpt-4o-mini`) which uses monospace specifically for these hero
  figures, not just for code.

### 2.4 Tone of Voice

Direct, technical, concise — written for developers, by developers. Three pillars from source
guidance, apply to every label, empty state, tooltip, and error message in this spec:
- **Clear** — say what it is, no fluff. Avoid marketing language inside the app itself.
- **Technical** — speak the language of developers (use real field/model/status names, not
  euphemisms).
- **Data-driven** — back every claim with a real number. Never say "significant savings" — say
  "32.1% savings."

### 2.5 Visual Style Principles

Dark, minimal, data-forward. Real dashboards, real code, no unnecessary decoration or illustration
inside the app (illustration is acceptable on the marketing landing page only, per source guidance's
"real dashboards and real code snippets over illustration" note, which was framed as a preference
even for marketing — so inside the product, illustration should not appear at all).

- **Density: data-dense.** Small type, tight row heights, compact card padding — closer to Linear/
  Helicone than to a roomy Stripe-style layout. **[Decision]** — the source's own inspiration
  references (Helicone dashboard screenshot) and the UI-example card sizing in the brand reference
  both point this way; this choice affects every component so it's locked in now rather than
  discovered mid-build.
- Cards: `--bg-surface` fill, 1px `--border-subtle` border, small border-radius (6-8px, not
  pill-rounded), minimal shadow (dark UIs read better with border separation than drop shadow).
- Charts: line charts on transparent/`--bg-surface` background, thin lines, small dot markers only
  at data points (matches the reference "Total Spend" chart), muted gridlines, no chart borders.
- Code blocks: dark background one step darker than `--bg-surface`, line numbers in
  `--text-secondary`, standard syntax highlighting theme (a Dracula/One-Dark-style palette is
  acceptable — this is the one place purple is fine), copy-to-clipboard affordance in the top-right
  corner of every code block.

### 2.6 UI Primitives

**Buttons**
- Primary: filled, `--text-primary`-colored background with `--bg-page` text (i.e. inverted — light
  fill, dark label, matching the reference "Primary Button" swatch), used for the single most
  important action on a screen.
- Secondary: outlined, `--border-subtle` border, transparent fill, `--text-primary` label.
- Text link: no border/fill, `--accent-info` or `--text-primary` label with a trailing arrow (→) for
  navigational links, matching the reference "Text Link →" style.

**Badges** — small pill, dot + label, per the reference:
- `Optimized` → neutral dot (`--accent-neutral`)
- `Cached` → green dot (`--accent-positive`)
- `High Cost` → amber dot (`--accent-warning`) — **[Decision]** the reference mockup shows this as a
  gray dot, but semantically "High Cost" is a warning signal; recommend amber instead of gray so
  badge color actually carries information, not just decoration. Flag for design review if brand
  consistency with the original mockup matters more than semantic color-coding.
- Alert severities reuse this same badge system: `info` → blue, `warning` → amber, `critical` → red.

**Stat cards** — headline monospace number, label below in `--text-secondary`, small delta pill
beside the label (up arrow + green for positive, down arrow + color depending on whether "down" is
good or bad in context — e.g. down in spend is good/green, down in savings is bad/red). Matches the
reference `$1,284.75 / Total Spend / Spend ↑ 12.5%` pattern exactly.

**Tables** — dense rows, header row in `--text-secondary` uppercase small type, numeric columns
right-aligned in monospace, savings/positive values colored green inline (no badge needed in-table,
per the reference table showing `32.1%` in green plain text).

**Charts** — line chart as the default visualization for anything over time (spend, savings, token
volume). Do not introduce additional chart types beyond line/bar unless a screen spec below calls
for it explicitly.

**Code blocks** — used for: the integration snippet in onboarding, and the paste-your-JSON textarea
in the advisor's compression tool (styled as a code editor, not a plain `<textarea>`, for visual
consistency and to enable line numbers/monospace by default).

---

## 3. Information Architecture & Navigation

### 3.1 Global Navigation
Left sidebar (matches the density/reference dashboard pattern), persistent across all authenticated
routes:
- Logo/wordmark at top
- **Project switcher** immediately below the logo — a dropdown, not a per-page selector.
  **[Decision]** Nearly every endpoint takes `project_id`; a global switcher means the user sets
  context once and every screen respects it, which is both fewer clicks and consistent with how
  Linear/similar tools handle workspace-scoped data.
- Nav items in this order: **Overview** (spend dashboard — the visibility epic from the feature
  backlog, serves as the landing screen after login), **Cache**, **Routing**, **Budgets**,
  **Alerts**, **Advisor**, **Settings**. Billing appears at the bottom of the sidebar, visually
  de-emphasized (smaller text or a subtle divider above it), since it's parked/read-only for now.
- User/account menu at the bottom of the sidebar (logout, account settings).

### 3.2 Time Range Control
A single, **shared, global control** in the top header bar (today / 7d / 30d / 90d / custom),
applying to all time-series data on the current screen. **[Decision]** — per-widget overrides add
complexity the MVP doesn't need; a global control matches how Helicone and similar tools handle this,
and keeps every chart on a screen mutually comparable.

### 3.3 Build Order

The source question list presents the seven routes in this order: cache, routing, budgets, alerts,
advisor, settings, billing. **Do not build in that literal order.** Settings contains the
provider-key and proxy-key flows that are prerequisites for every other screen having any data at
all — build it first, as part of onboarding, not last.

**Recommended build order [Decision, with rationale]:**

1. **Onboarding + Settings core** (account, provider keys, projects, proxy key issuance, connection
   test) — nothing else can be meaningfully tested without this.
2. **Overview / Visibility** (spend dashboard, per-request decision log) — this is the screen that
   makes the product's value legible before any optimization feature is even turned on, and it's the
   post-login landing screen.
3. **Cache** — smallest, most self-contained optimization feature; fastest path to a demonstrable
   "you saved money" moment.
4. **Routing** — the more complex, more heavily marketed feature (per the landing-page asset showing
   "Smart & Speedy LLM Routing" as a headline feature); build once cache has proven the pattern for
   showing before/after savings.
5. **Budgets** — straightforward CRUD plus one high-stakes confirmation flow (hard_stop).
6. **Alerts** — depends conceptually on budgets/anomaly detection already producing events; includes
   the kill switch.
7. **Advisor** — depends on accumulated usage history to be useful at all; naturally the last
   feature a new user would benefit from.
8. **Billing** — read-only plan/usage view against `GET /billing/plan` only; no checkout flow yet.

---

## 4. Cross-Cutting Behaviors

### 4.1 Project Switching
Global sidebar dropdown (see 3.1). Switching projects re-fetches all data on the current screen using
the newly selected `project_id`; do not require a page reload.

### 4.2 Empty States
Every screen must have a designed empty state — a solo developer signs up with zero data and will
hit all seven screens empty on day one. Copy below follows the tone-of-voice pillars (clear,
technical, data-driven where possible, honest where there's no data yet to be "data-driven" about).

| Screen | Empty state copy |
|---|---|
| Overview | "No requests yet. Point your app at your proxy key and this dashboard fills in automatically." + link to Settings/integration guide. |
| Cache | "No cache activity yet. Once your proxied requests start repeating, cache hits and savings will show up here." |
| Routing | "Routing is off. Turn it on to start sending requests to the cheapest model that can handle them." (if off) / "No routed requests yet." (if on, no data) |
| Budgets | "No budgets set. Add one to cap spend per project before it happens, not after." + primary button "Add budget." |
| Alerts | "No alerts triggered. We'll notify you here the moment something looks unusual." |
| Advisor | "Not enough usage history yet to generate recommendations. Check back once you've made some requests." |
| Billing | "You're on the [plan name] plan." (always has content — plan info, not usage-dependent) |

### 4.3 Error Display
The API returns RFC 7807 `problem+json`. Use two patterns depending on severity **[Decision]**:
- **Inline banner** (top of the content area, below the header) for page-level failures that prevent
  a screen from loading data at all (e.g. usage query fails) — persistent until resolved/retried,
  includes a "Retry" action.
- **Toast** (transient, bottom-right or top-right, auto-dismiss) for action-result failures that
  don't block the page — e.g. saving a routing rule fails, but the rest of the page still works.
  Toast text should surface the `problem+json` `title`/`detail` fields directly (technical, direct —
  don't paraphrase into vague language).

### 4.4 Dark Mode
Default and only mode for v1 (see 2.2). Build the token system to allow light mode later; do not
build a toggle now.

### 4.5 Mobile
**Desktop-only for v1.** **[Decision]** — this is a data-dense developer dashboard, not a consumer
app; the source guidance itself flagged desktop-only as a legitimate answer for this category of
product. Do not spend build time on responsive breakpoints below tablet width; a minimum-width
warning is sufficient if someone opens it on a phone.

---

## 5. Screen-by-Screen Specs

### 5.1 Onboarding & Settings
*(Build first — see 3.3. Covers UC-01 through UC-07 and the Settings portion of Epic A.)*

- **Sign up / log in** — standard auth flow, no special visual requirements beyond the token system
  above.
- **Provider keys** — a list of connected providers (OpenAI/Anthropic/Gemini) with an "Add key"
  action. Once added, the raw key is **never shown again** — the list shows provider name, added
  date, last-used date, and a masked reference only.
- **Projects** — simple list/create UI; each project is a scoping boundary for everything else in
  the app.
- **Proxy keys** — issuing a new proxy key surfaces the raw key **exactly once**, in a modal with:
  - the key in a monospace field with a copy-to-clipboard button,
  - a required **"I've saved my key" checkbox or confirm button** gating the modal's close action.
    **[Decision, adopting the source's own suggested default]** — standard pattern (GitHub/Stripe),
    prevents users losing a key they can never retrieve again.
- **Connection test** — a "Send test request" button that fires a real request through the proxy and
  shows pass/fail inline within a few seconds, before the user is expected to wire up their own app.
- **Privacy toggle** — `store_raw_content`, off by default, with inline explanatory text (e.g. "When
  off, we store only hashes and embeddings of your prompts — never the raw text.") directly beside
  the toggle, not hidden in a tooltip, since this is a meaningful privacy decision.

### 5.2 Overview (Visibility)
*(Post-login landing screen. Covers Epic B — UC-08 to UC-13.)*

- Stat cards row at top: Total Spend, Savings (combined cache + routing), Requests count — using the
  stat-card primitive from 2.6.
- Line chart: spend over the selected time range.
- Breakdown views: by model, by project (table or bar breakdown — table preferred for density/
  consistency with the reference table style).
- Token size distribution: a simple histogram or summary stat, not a priority visual investment for
  v1.
- Per-request decision log: dense table, one row per request, columns for timestamp, model
  requested, model used, cache hit (badge), cost, latency. This table is the most information-dense
  screen in the product — lean fully into the "data-dense" density decision here.
- Export button (CSV) — secondary button, top-right of the table.

### 5.3 Cache
*(Build third. Covers Epic D — UC-20 to UC-25.)*

- Header row: on/off toggle (prominent, since this is the first optimization feature a user
  encounters) + hit-rate and dollars-saved stat cards.
- **Similarity threshold** — a **slider with a numeric readout**, not a free-text input.
  **[Decision, adopting source's own default]** — prevents invalid values entirely. Show an inline
  **warning below 0.90** (e.g. amber text: "Below 0.90, cache hits may return answers that don't
  actually match the new request.") — this is the one setting on this screen that can cause silently
  wrong behavior, so it should be the most visually cautious element on the page.
- **TTL** — numeric input with a unit selector (minutes/hours/days).
- **Invalidate cache** — plain button + a lightweight confirm dialog (not type-to-confirm).
  **[Decision]** — clearing cache is low-stakes (cache simply rebuilds from new traffic), unlike the
  kill switch; a standard "Are you sure?" modal is proportionate.
- Non-cacheable request marking — a settings sub-section or per-endpoint rule list, secondary
  priority relative to the three main knobs above.

### 5.4 Routing
*(Build fourth. Covers Epic C — UC-14 to UC-19.)*

- Header row: on/off toggle, styled as **prominent** — primary-button visual weight, not a small
  switch, since routing defaults to off and is a headline marketed feature.
  **[Decision, adopting source's own lean]**.
- **Routing rules editor** — the constrained builder (endpoint dropdown, model dropdown), **not** a
  free-form JSON editor. **[Decision, adopting the source's own recommended default]** — safer for
  users, and the underlying `match_condition` JSONB can still be constructed from the structured
  builder on the backend.
- Rule list: table showing each rule's condition, target model, priority, active state, with
  edit/delete actions.
- **Savings display** — can go negative when escalations outweigh gains; this is deliberate and
  honest per the source spec. Show negative savings in **amber, not red**, with a small info icon /
  tooltip explaining "Negative means escalations to a stronger model cost more than routing saved —
  this is expected when your escalation threshold is conservative." **[Decision]** — red would read
  as an error state; amber communicates "notable, not broken."
- Escalation config: toggle + confidence threshold setting, grouped visually near the rules editor
  since they interact directly.
- Routing decision transparency: surfaced in the Overview's per-request log (UC-16 — model +
  reason/confidence shown there), not duplicated as a separate view on this screen.

### 5.5 Budgets
*(Build fifth. Covers Epic F budget portion — UC-29, UC-30.)*

- **Show all three periods always** (daily, weekly, monthly) as three consistent cards/rows, even if
  unset. **[Decision, adopting source's own lean]** — full visibility of what's configured and what
  isn't is more useful than an "add as needed" list for a screen about spend safety.
- Each period card: current spend vs. limit (progress bar), limit input, action selector
  (`alert_only` / `soft_throttle` / `hard_stop`).
- **`hard_stop` requires a heightened confirmation** — **[Decision]** given it returns HTTP 402 and
  stops the user's application entirely: on selecting `hard_stop`, show a modal explaining the exact
  consequence in plain language ("Requests will be blocked with a 402 error once this budget is
  exceeded, until the period resets or you raise the limit.") with an explicit confirm button; do not
  require typing the project name here (reserve that heavier pattern for the kill switch, which is
  more severe and less reversible — a budget action can be changed back instantly, a kill switch
  revokes keys that must be reissued).

### 5.6 Alerts
*(Build sixth. Covers Epic F alert portion — UC-31 to UC-34.)*

- Alert history list: severity badge (info/warning/critical per 2.6), timestamp, message,
  resolved/unresolved state, resolve-with-note action.
- **Kill switch** — **type-the-project-name-to-confirm**. **[Decision, adopting source's own
  recommended default]** — this revokes every proxy key for a project in under a second; the
  highest-friction confirmation pattern on the heaviest-consequence action in the product is the
  right tradeoff.
- Place the kill switch visually separate from the alert history list (e.g. a distinct danger-zone
  card at the bottom of the screen, red/critical accent border) so it isn't casually discoverable
  next to routine alert-browsing.

### 5.7 Advisor
*(Build seventh. Covers Epic G — UC-35 to UC-37.)*

- Recommendations list: each with adopt/dismiss actions. **Dismiss is permanent** (never
  re-suggested) — implement as an optimistic UI update with a **5-second "Undo" toast** after
  dismissal. **[Decision]** — protects against accidental clicks without contradicting the
  "permanent" rule, since the undo window is only for the immediate action, not a standing re-offer.
- **Break-even advisor** — the headline self-hosting-vs-API number must appear **with its six
  mandatory caveats visible inline, not in a tooltip or collapsed accordion**. **[Decision, following
  the source spec's explicit instruction that a bare number is misleading]** — render the number in
  the stat-card style, immediately followed by a compact caveat list (small type, `--text-secondary`,
  bulleted) directly beneath it, always visible by default. Do not require a click to reveal the
  caveats.
- **Prompt compression tool** — a paste-your-JSON textarea, styled as the code-block primitive (2.6)
  for visual consistency, since the backend doesn't store prompts and can't pre-fill this.
  **[Decision, accepting source's own suggested default]**. Show a token-count before/after
  comparison once compression runs.

### 5.8 Billing
*(Build last. Read-only for v1.)*

- Plan name, current usage against plan limits (progress bar, consistent with the budget-card
  pattern from 5.5 for visual consistency), renewal date.
- No checkout/upgrade flow in v1 — a "Contact us to upgrade" text link is sufficient.

---

## 6. Terminology Glossary

Use these terms consistently across every screen, tooltip, and error message.

| Use this term | Not this | Why |
|---|---|---|
| **Proxy key** | "API key" (for our-issued key) | Must be distinguishable from the user's *provider* API key at all times — using "API key" for both is a support/confusion risk. |
| **Provider key** | "OpenAI key" / generic "key" | Consistent umbrella term across OpenAI/Anthropic/Gemini. |
| **Savings** | "Saved" | Matches the reference UI mockup's own label exactly (`32.1% / Savings`). |
| **Project** | "Workspace" / "App" | Matches the data model (`project_id` is the actual scoping field throughout the API). |
| **Cache hit** | "Cache match" | Matches standard caching terminology developers already know. |
| **Routed** | "Redirected" | Matches the `routed` boolean field name in the data model exactly. |

---

## 7. Landing Page (Brief — Secondary Priority)

Visual assets were provided for this; include for completeness, but the dashboard above is the build
priority. Keep the landing page intentionally simple for v1, per source guidance ("we need to keep it
simple for starters").

**Section order [Decision, following the source's own "Overall Layout" note]:**
1. **Hero** — headline copy already drafted in source material: *"See exactly where your API money
   goes → automatically choose the right model → prevent unnecessary spending."* Primary CTA: sign
   up. Secondary: "Contact sales" style link is not needed for this individual-focused product —
   omit it (contrast with the Lyceum reference, which is B2B-infra-focused).
2. **With vs. without APICost** — a simple two-column or before/after comparison, using the
   dollar-figure framing suggested in source copy (*"$ with vs. without APICost"*).
3. **Case study link** — a text link out to a written case study (content TBD, not part of this
   spec).
4. **Dashboard introduction** — embed real dashboard screenshots (per source guidance: "real
   dashboards... over illustration"), ideally interactive if time allows, otherwise static images of
   the actual Overview and Cache screens once built. Do not commission illustrated graphics for this
   section.
5. **Interactive pricing** — a slider-based calculator (reference: the "Mortgage Calculator" pattern
   in the source inspiration images) — e.g. slider for monthly API spend → live-computed estimated
   savings. **[Decision]** Full call-for-pricing flow is explicitly deferred to "after we land bigger
   deals" per source guidance — v1 should be self-serve interactive pricing only, no contact form
   gating the number.
6. **Testimonials** — fake/placeholder testimonials are acceptable for v1 launch per source guidance,
   styled as a simple card grid (reference: EmbedSocial-style testimonial wall), to be swapped for
   real quotes post-launch.
7. **Community links** — placement TBD/optional per source guidance's own uncertainty ("(?)").

Visual treatment for the landing page follows the same token system as the dashboard (Section 2) —
dark, minimal, monospace accents on numbers, no purple, same button styles.

---

## 8. Explicit Assumptions Log

Everything tagged **[Decision]** above, consolidated for quick review:

1. No abstract logo mark for v1 — wordmark only.
2. Dark mode only for v1; light mode deferred, token system built to support it later.
3. Semantic accent colors (green/amber/red/blue) added beyond the three source-specified neutrals —
   needed for status/badge meaning, kept low-saturation to match minimal style.
4. UI font: Inter (or equivalent geometric sans) — not specified in source.
5. Monospace font: JetBrains Mono or equivalent — family not specified in source, only the
   requirement to use one.
6. Density: data-dense (Linear/Helicone-style), not roomy.
7. "High Cost" badge recommended as amber, not the gray shown in the reference mockup, for semantic
   consistency — flagged for design review.
8. Build order re-sequenced from the source's listed route order: Settings/onboarding first (it's a
   prerequisite), then Overview, Cache, Routing, Budgets, Alerts, Advisor, Billing last.
9. Global project switcher in the sidebar, not per-page.
10. Global, shared time-range control, not per-widget.
11. Empty-state copy drafted directly in this spec (Section 4.2).
12. Error display split by severity: inline banner for page-blocking failures, toast for
    action-level failures.
13. Desktop-only for v1 — no responsive/mobile work.
14. Cache similarity threshold: slider with numeric readout, warning below 0.90.
15. Cache invalidation: plain confirm dialog, not type-to-confirm.
16. Routing rules: constrained builder, not free-form JSON editor.
17. Negative routing savings shown in amber with explanatory tooltip, not red.
18. Routing enable toggle: prominent, primary-button visual weight.
19. Budgets: all three periods always shown, not add-as-needed.
20. Budget `hard_stop`: explanatory confirmation modal (lighter than kill-switch pattern).
21. Alerts kill switch: type-the-project-name-to-confirm.
22. Advisor dismiss: optimistic update + 5-second undo toast, then permanent.
23. Advisor break-even caveats: always visible inline, never behind a click.
24. Advisor compression tool: paste-JSON textarea styled as a code block.
25. Proxy key reveal: copy button + required "I've saved my key" confirmation gate.
26. Terminology: "Proxy key" vs. "Provider key" kept strictly distinct; "Savings" not "Saved."
27. Landing page section order and interactive-pricing-over-contact-form approach for v1.

Any of these can be overridden by direct instruction — they exist so Claude Code has an unambiguous
spec to build against rather than needing to stop and ask mid-implementation.
