# Plutus — Roadmap to 1.0

> Last updated: 2026-07-11 · Current: **1.0.1 (shipped: tagged, on PyPI + GHCR)**
> This is the **billing-engine** roadmap. The older `ROADMAP.md` is the long-term monitor/FinOps vision.

> ## Current state (2026-07-11) — 1.0 is shipped
>
> **v0.7.1 — Foundation hardening: COMPLETE.** Every carryover issue is closed
> *in code* (not just in the tracker): #28 (prepaid hard-stop default-on and
> enforced inside the debit transaction), #30 (atomic `balance_after` + quota
> race under `BEGIN IMMEDIATE`), #32 (CSRF fail-closed + per-session token), #33
> (DB-backed per-day org cap), #37 (500 error-leak / 404 XSS / OIDC test-bypass
> gated). 1.0.0 then shipped (#67), followed by the **1.0.1 security-hardening
> release** (#92–#96: OIDC login-CSRF, HTTP hardening, money-atomicity,
> fail-closed public bind). v1.0.1 is tagged and live on PyPI (`plutus-agent`) +
> GHCR. Independently verified in [`docs/HERMES-VERIFICATION-2026-07.md`] and
> reviewed in [`docs/REVIEW-2026-07.md`].
>
> **Remaining gates (human/outward), not code:**
> 1. **External security-review pass** on the public surface (scope includes the
>    hand-rolled OIDC RS256 verifier — see `docs/REVIEW-2026-07.md` P5). This is
>    the last gate before a public launch.
> 2. Close the review punch-list (P1–P7) before the Perseus / Perseus-Vault
>    convergence. As of 2026-07: P3/P4/P6 (version single-source) done; P2
>    (concurrency proof) in progress.
>
> The milestone framing below predates the issue-numbered queue and is kept for
> historical context; where it says "carried to v0.7.1 / pending", read the
> current-state block above as authoritative.

## What 1.0 means

Plutus 1.0 is a **production-grade billing layer for AI agents** that a stranger can self-host or sign up for and trust with real money. That bar means four things, in priority order:

1. **Money is correct** — no double-credit, ingest is atomic, prepaid credit is enforceable, amounts reconcile with Stripe.
2. **It's safe to expose publicly** — the self-serve funnel (open signup + ingest API + Stripe) is hardened against abuse, CSRF, DoS, and injection.
3. **The product loop is complete** — signup → API key → meter → see spend → hit a limit → pay, with the tiers that actually sell (incl. a Team tier).
4. **It's documented and stable** — API reference, self-host guide, SDK quickstarts, and a frozen public API + DB schema under semver.

We do **not** call it 1.0 until the money-correctness and public-exposure items below are closed, because open signup + live Stripe are already on.

---

## Milestones

### v0.6 — Money & concurrency correctness  *(shipped v0.6.0, 2026-06-24 — partial; see v0.7.1 carryover)*
The deep-review findings that can corrupt billing data. Root cause for most: read-modify-write with no atomic transaction under the threaded, connection-per-request server.
- **#27** make `/v1/usage` atomic per request (validate-all → one transaction → commit once)
- **#26** webhook idempotency: insert the dedup row *first*, apply side-effect only if newly inserted
- **#30** `PRAGMA busy_timeout`, atomic `balance_after`, fix the free-tier quota race
- **#29** credit from Stripe `amount_total`, never client metadata
- **#28** prepaid-credit hard-stop policy (stop debiting past zero; opt-in 402)
- **#38** store money as integer micro-dollars (schema migration) — do before freeze
- *Status (verified 2026-06-26):* ✅ #26, #27, #29, #38 landed. ⚠️ #30 partial (only `busy_timeout`/WAL landed; `balance_after` still non-atomic + quota race remains) and #28 partial (hard-stop is off-by-default and racy) — both carried to **v0.7.1**.
- *Exit:* a concurrency/load test on the ingest + webhook paths proves no double-count, no lost writes, correct balances.

### v0.7 — Security hardening  *(shipped v0.7.0, 2026-06-24 — partial; see v0.7.1 carryover)*
- **#31** request body-size cap on `/v1/usage` + `/webhook/stripe` (DoS)
- **#32** CSRF tokens on state-changing POSTs; make logout a POST
- **#33** signup rate limiting + per-day org cap (abuse)
- **#34** escape attacker-controlled names in HTML/PDF reports
- **#35** SMTP: TLS-only login, 465 support
- **#36** OIDC JWKS signature verification (defense-in-depth)
- **#37** polish punch-list (error-leak, 404 escaping, hook backup, etc.)
- *Status (verified 2026-06-26):* ✅ #31, #34, #35, #36 landed. ⚠️ #32 (CSRF fails open when `base_url` unset / auth off), #33 (no DB-backed per-day org cap; only an in-memory hourly limiter), #37 (500 error-leak + 404 reflected-XSS) partial — carried to **v0.7.1**.
- *Exit:* an external security-review pass on the public surface.

### v0.7.1 — Foundation hardening  *(COMPLETE — shipped in 1.0.0 / 1.0.1)*
A 2026-06-26 foundation review verified v0.6/v0.7 against `main`. The items below were then fully closed in code (confirmed by reading the guards, not just the tracker) and shipped in 1.0.0 + the 1.0.1 security-hardening release; see the current-state block at the top and `docs/REVIEW-2026-07.md` §2–§3. Retained for historical context.
- **#28** prepaid hard-stop: default-on for prepaid orgs + enforce *inside* the debit transaction (currently off-by-default and racy).
- **#30** atomic `balance_after` + free-tier quota race: wrap the ledger read-modify-write in `BEGIN IMMEDIATE` (or one conditional `INSERT…SELECT`).
- **#32** CSRF: fail closed in `_same_origin` when `base_url` is unset, and/or add a per-session token.
- **#33** DB-backed per-day org-creation cap alongside the hourly limiter.
- **#37** stop leaking `str(e)` to clients on 500s; escape the 404 `path`; gate the OIDC `"hdr"` test-bypass behind an explicit flag.
- **#47** `plutus --db` crash (`os` not imported in `cli.py`).
- **#48** add `windows-latest`/`macos-latest` CI; align the Python matrix with the classifiers (3.10/3.13); fix the `release.yml` double-publish trigger; drop stale `assets/` packaging.
- *Exit:* every reopened/new issue above closed; a concurrency test covers the #28/#30 transaction; CI green on Linux + Windows across the advertised Python versions.

### v0.8 — Product completeness  *(what makes it sell)*
- **Team tier (~$149/mo)** — multi-seat, more workspaces, the missing money tier (drives ramen MRR).
- Per-org credit-enforcement policy (the #28 hard-stop, surfaced in dashboard + API).
- **Usage export** — CSV + webhook out (cost-attribution / "$/task" for customers' own billing).
- **First-class integrations** — LangChain & CrewAI callback handlers, and a Plutus **MCP server** so agents meter themselves.
- Dashboard: date-range selector, per-workspace drill-down, API-key last-used/usage view.

### v0.9 — Hardening, observability & docs
- Structured request logging + a `/metrics` endpoint; per-request ids.
- Full docs site: **API reference**, self-host guide, SDK quickstarts (Python now; TS later), migration notes.
- Backup/restore + schema-migration tooling for self-hosters.
- Soak-test the hosted instance; define SLOs.

### v1.0 — Freeze & launch
- **Freeze the public API + DB schema**; commit to semver.
- Public **status page**; documented upgrade path.
- Cut 1.0, then run the launch (below).

---

## Get it out there  *(parallel GTM track — start now)*

Already done: README reframed, `pip install plutus-agent`, GHCR image, hosted dashboard, Hermes dogfooding live.

**Pre-launch (do alongside v0.6/v0.7):**
- Merge + release the **v0.5.1 Cloudflare-UA fix** (#PR 25) — external SDK ingest is broken through CF without it; hard launch blocker.
- Turn on `PLUTUS_ALLOW_SIGNUP` on the hosted instance once v0.7 abuse controls land.
- Launch assets: a 60-second "meter your agent in 3 lines" asciinema/GIF, dashboard screenshots, polish `/pricing`, a landing section on perseus.observer/plutus.
- A short **"Plutus vs Helicone / Langfuse / OpenMeter"** positioning post — own *billing* (charging end-users), not just observability.

**Launch (after v0.7):**
- Show HN, r/LocalLLaMA, MCP/agent-framework communities; list in LangChain/CrewAI integration directories.
- First 5 design-partner orgs from the warm network; 1–2 reference customers billing their own users through Plutus.

**Don't launch publicly before** the v0.7 items (CSRF, body limits, signup rate-limit) — open signup + live money are exposed today.

---

## Success metrics for 1.0
| Metric | Now | 1.0 target |
|---|---|---|
| Money-correctness bugs (open) | 0 (#28, #30 closed) | 0 ✅ |
| Public-surface security findings (open) | 0 (#32, #33, #37 closed) | 0 ✅ |
| Paying orgs | 1 (us) | 10+ |
| First-class integrations | adapters + hook | + LangChain, CrewAI, MCP |
| API stability | unfrozen | frozen, semver |
| Docs | README + 3 docs | full reference + guides |

## Sequencing note
v0.6/v0.7 shipped, the v0.7.1 foundation-hardening carryover is **closed**, and
1.0.0 → 1.0.1 shipped with a frozen `/v1` API + DB schema. The one remaining gate
before a public push is an **external security-review pass**; see the
current-state block at the top. v0.8 (Team tier + integrations) is the
revenue/distribution lever and proceeds in parallel.
