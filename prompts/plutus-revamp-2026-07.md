# Plutus revamp + awareness push — kickoff prompt (2026-07)

> Handoff prompt for an autonomous coding agent (Claude Code, Codex, Hermes, etc.).
> Self-contained. Verify every claim against the actual repo before acting — this
> doc may drift. Full send, fix root causes, blocked = pivot, no permission-asking.

## Deliverable conventions
- Git author for commits: `perseus <51974392+tcconnally@users.noreply.github.com>`.
- No em-dashes, no AI-speak. Deliver artifacts as committed repo files + links, not
  chat attachments. The human (tcconnally) handles all outward posting (Devpost,
  YouTube, X, Show HN); you produce the text and repo artifacts.
- Verify with real tool output; never fabricate results. Any external side-effect
  (PR, marketplace listing, outreach) must return a verifiable URL/ID you confirm.

## Skills / playbooks to apply (tool-agnostic — map to whatever your toolchain calls them)
Load or emulate the methodology for each; skill names differ across agents:
- **Deep-dive code review** — multi-phase: architecture, performance, security,
  test coverage, correctness. Produce a written verdict + prioritized punch list.
- **Integration / partnership discovery** — systematically find platforms to embed
  into or list on; produce a ranked target table with contact/PR paths.
- **OSS project promotion** — positioning, launch assets, awareness cadence.
- **GitHub PR + code-review workflow** — branch, PR, review, CI-green before done.
If your agent has a skill registry, scan it for a `plutus` skill first. If none
exists, create one at the end capturing this revamp workflow.

## What Plutus is (context — verify, don't trust blindly)
Plutus = self-hosted, Stripe-integrated usage metering + prepaid-credit billing for
LLM/agent spend, PLUS a runway-based model router. Tagline: "the billing layer for
AI agents." Named for the Greek god of wealth.
- Repo: https://github.com/Perseus-Computing-LLC/plutus (PRIVATE org repo; PyPI
  package `plutus-agent`).
- ~89 files. Key modules: `plutus.py` (metering/monitor + spend ledger read),
  `plutus_route.py` (runway router), `plutus_agent/` package (Meter SDK, bridge,
  cli, db, metering, alerts, client, config, demo), route/API layer, Stripe
  webhooks, `dashboard.html`, `docs/`, `tests/` (`test_plutus.py` + `tests/*`),
  `openapi.yaml`, `Dockerfile`, `docker-compose.yaml`.
- **Version discrepancy to reconcile:** `README.md`/`ROADMAP-1.0.md` claim v1.0.0
  (code-frozen; tag/publish pending), but `plutus.py --version` reports v0.1.1.
  Resolve this as part of the review.
- Roadmaps: `ROADMAP-1.0.md` (billing-engine; has open v0.7.1 carryover items
  #28/#30/#32/#33/#37), `ROADMAP.md` (long-term FinOps vision),
  `prompts/plutus-roadmap-2026-2027.md` (12-month plan).
- Integrates with Hermes Agent: reads Hermes `state.db` for the spend ledger and
  Hermes `config.yaml` for provider keys. Ships two Hermes cron jobs (credit
  refresh hourly, balance check-in) and sync scripts/examples
  (`examples/hermes_sync.py`, `examples/hermes_integration.py`,
  `tests/test_hermes_sync.py`).

## THE NEW UPSTREAM FEATURE THAT TRIGGERS THIS REVAMP
Nous Research merged our feature into `hermes-agent` main:
**`feat(agent): track per-model token usage for mid-session model switches`**
(upstream commit `cb7f6bbb2`, authored by tcconnally; fixes hermes-agent #51607),
plus follow-up hardening (PR #62610: persist first accounted fallback route;
harden per-route attribution).

What it does: adds a `session_model_usage` table in `hermes_state.py` keyed
`(session_id, model, billing_provider)` that accumulates per-API-call token/cost
deltas under the model active at each call. `SCHEMA_VERSION` bumped to 17 with an
idempotent `INSERT OR IGNORE` backfill seeding one row per existing token-bearing
session. Insights `_compute_model_breakdown` reads from it so mid-session model
switches split correctly across models, with a defensive fallback to the aggregate
`sessions` row so totals never regress.

**Why it matters for Plutus (the headline improvement):** Plutus reads Hermes
`state.db` for spend. The old `sessions` row attributes ALL of a session's tokens
to the INITIAL model, so any mid-session model switch corrupts per-provider spend,
which corrupts Plutus's runway projections AND its routing decisions. Consuming
`session_model_usage` fixes attribution at the source. This is both a real accuracy
win and the awareness narrative: we authored the upstream fix that makes our own
product more accurate (dogfooding story).

## Phase 1 — Deep-dive code review + present/future-state evaluation
Run a multi-phase deep review of the repo. Commit a written report (e.g.
`docs/REVIEW-2026-07.md`) covering:
- Architecture; money-correctness (double-credit, ingest atomicity, prepaid-credit
  enforcement, Stripe reconciliation); security posture (confirm status of the
  v0.7.1 carryover: CSRF fail-open when base_url unset, missing DB-backed per-day
  org cap, 500 error-leak / 404 reflected-XSS); test coverage; the version/tag
  discrepancy.
- Present state: what actually works, shipped-vs-claimed, PyPI/GHCR publish status,
  open issues/PRs.
- Future state: gap analysis vs the roadmaps, highest-leverage next moves, and
  exactly how the Hermes `session_model_usage` integration slots in.
- Verdict + prioritized punch list.

## Phase 2 — Wire in the Hermes per-model attribution feature
- Inspect how Plutus reads `state.db` today (`plutus.py` spend ledger;
  `examples/hermes_sync.py`, `examples/hermes_integration.py`,
  `tests/test_hermes_sync.py`).
- Update Plutus to consume `session_model_usage` for accurate per-model /
  per-provider spend, with graceful fallback to the aggregate `sessions` row for
  pre-v17 DBs (mirror the upstream COALESCE fallback so totals never regress).
- Verify runway projections and the router (`plutus_route.py`) improve under
  correct attribution. Add/extend tests. Open a PR. Run the full test suite
  (`pytest tests/ -q` plus `test_plutus.py`) and get CI green before declaring done.

## Phase 3 — Partner / deep-integration target hunt (the core ask)
Systematically find platforms where Plutus can be embedded as their billing/metering
layer OR listed as an integration on their marketplace/plugin directory. Two buckets:
1. **DEEP integration** (Plutus as the billing/metering primitive inside their
   platform): agent frameworks/orchestrators (LangChain/LangGraph, CrewAI, AutoGen,
   LlamaIndex, Google ADK, DSPy), agent-hosting/PaaS, LLM gateways/routers (LiteLLM,
   OpenRouter, Helicone, Portkey, Braintrust), self-host agent stacks.
2. **MARKETPLACE / PLUGIN listing** (lower-touch distribution): Stripe App
   Marketplace, Vercel/Cloudflare integrations, the MCP ecosystem (Plutus as an MCP
   server for spend/billing — high fit), Zapier/n8n, Dify/Flowise plugin listings,
   Hugging Face Spaces.
Deliver a ranked target CSV committed to the repo (e.g.
`docs/partner-targets-2026-07.csv`) with columns: `partner, category,
integration_type (deep|marketplace), fit_rationale, exact_surface_to_list_on,
contact_or_PR_path, effort_estimate, priority`. Start with a small validation
sample, write the CSV, then continue from the same file (do not restart).
For the top 3-5, draft the concrete first move (a marketplace listing spec, an
MCP-server wrapper spike, or an outreach issue/PR on their repo). For LLM
gateways/routers, note where Plutus's runway-router overlaps vs complements theirs
(positioning: "billing + credit runway they don't have").

## Phase 4 — Awareness push kickoff
- Positioning one-pager committed to the repo: what Plutus is, the "we authored the
  upstream Hermes attribution fix and it makes our own product more accurate"
  dogfooding narrative, and the unique combo (live balance monitoring + ledger spend
  + self-calibrating budgets + runway routing — no OSS tool does all four).
- Draft launch assets: README polish, a demo recording plan (`plutus demo` →
  http://localhost:8420 ; asciinema/GIF), a Show HN / X thread / dev.to post draft,
  and a submission-ready blurb. Produce text + repo artifacts only; the human posts.
- If the tag/publish is still pending, prep the v1.0.0 release checklist
  (tag → PyPI + GHCR) but leave the human/outward gates (pushing the tag, external
  security review) flagged.

## Operating rules
- Full court press: advance all four phases; don't stall on one.
- Anything recurring (partner-pipeline monitoring, awareness cadence) → propose a
  Hermes cron job, not host cron.
- End with: a crisp status, the exact outward actions left for the human, and (if
  your toolchain supports skills) a saved `plutus-revamp` skill capturing this
  workflow.
