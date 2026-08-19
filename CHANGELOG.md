# Changelog

All notable changes to Ledger are documented here.

## [Unreleased]

### Added
- **Deterministic OSCAL evidence projection** (#259). A hash-only input
  contract projects explicit Ledger evidence states into schema-valid OSCAL
  1.2.3 Assessment Results and POA&M JSON/YAML, with stable UUIDs,
  reproducible export hashes, coverage by expected/observed control, and
  fail-closed handling for missing, unknown, stale, superseded, and
  unreported evidence. See `docs/oscal-projection.md`.
- **CUI-safe context release decisions** (#260). A versioned, hash-bound
  publication-boundary contract separates internal visibility from external
  release, binds source/projection/redaction/policy/authority/destination and
  artifact digests, fails closed on unknown or stale evidence, and provides
  idempotent outbox receipts plus non-resurrecting expiry/revocation tombstones.
  See `docs/context-release.md`.
- **Runtime-contract enforcement** (#250). The Agent Trajectory Schema +
  Evidence Chain from arXiv:2608.11274: hash-chained trajectory events
  (`tool_call`, `tool_result`, `file_read`, `file_write`, `shell_exec`,
  `commit`, `screenshot`, `citation_lookup`, `human_approval`,
  `model_message`), a deterministic verifier registry separating hard from
  soft evidence, evidence-chain construction, a fail-closed evidence-gated
  submission contract, and a `ComposedGate` realizing the paper's
  compositional gating proposition (preventive monitors + evidential gates
  over disjoint requirement sets). See `docs/runtime-contract.md`.
- **CVA authorization property contract** (#252). Cryptographically
  verifiable authorization (arXiv:2607.21325) over the AAR prebind:
  statements bind agent principal + request hash + policy + context +
  nonce/epoch; `cva_relation_holds` enforces
  R_CVA = BindPrincipal ∧ BindRequest ∧ BindContext ∧ SatisfyPolicy;
  replay resistance via a trusted consumed-nonce gateway with an inclusive
  timestamp window; prebind v2 receipts now carry `request_hash`/`nonce`/
  `epoch` in the hash-covered payload (backward compatible). See
  `docs/cva-contract-spec.md`.
- **Tool-execution receipts + epistemic-source classification** (#251).
  Runtime-issued HMAC-SHA256 tool receipts (unforgeable by the model,
  arXiv:2603.10060) with a pramāṇa claim classifier
  (pratyakṣa/anumāna/upamāna/śabda/abhāva/ungrounded), six hallucination-type
  flags, five trust levels, and omitted-call completeness detection. Ships a
  deterministic 1,800-scenario NyayaVerifyBench adaptation
  (`benchmark/nyaya_verify_bench.py`) gated at ≥90% fabricated-reference
  detection and <20 ms/response verification overhead. See
  `docs/tool-receipts.md`.
- **Evidence levels for receipts** (#235). Receipts now state what they prove:
  a four-level ladder (`structural` → `attested` → `replay` → `inclusion`)
  under `verification.evidence`, with stable per-level reason codes. A signed
  receipt is never conflated with a durably committed one: Commit receipts
  (executed actions) require an inclusion anchor — a retained checkpoint
  covering the receipt's events — and sign-then-abort receipts verify at
  `attested` but NOT `inclusion`. Optional HMAC-SHA256 receipt signatures and
  trusted-key terminal-stage attestations; watermark reclamation downgrades
  Replay while Inclusion stays verifiable across restarts. See
  `docs/evidence-receipts.md` → "Evidence levels".
- **`ledger reconcile-webhooks`** (#177). Diffs the Stripe event log (source
  of truth) against the local `stripe_events` table to surface webhook
  events dropped by deploy windows or restarts. Dry-run by default;
  `--apply` replays gaps oldest-first through the idempotent app handler.
  Flags: `--days` (default 7), `--types`, `--json`.

### Added
- **Recurring-billing gate** (#175). New `billing.subscriptions_enabled`
  config key + `LEDGER_SUBSCRIPTIONS_ENABLED` env override (default OFF).
  While gated, `POST /billing/checkout/pro` and `/billing/checkout/team`
  return 403 with a "Subscriptions open at launch" page; credit top-ups and
  donations are unaffected. Flip on once the launch-readiness checks
  (#4/#164) pass.

### Fixed
- **Remote-mode `Meter.track` no longer drops the savings fields** (#143). The
  `/v1/usage` response has carried `savings_usd`/`leaked_usd` since #7/#134,
  but the SDK's response→result mapping predated them, so a remote `track()`
  with a baseline reported `savings_usd=0.0` even though the server recorded
  the saving in the hash-chained ledger. The reconstructed `MeterResult` now
  maps `savings_usd`, `baseline_usd` (newly returned per event and documented
  in `openapi.yaml`), `leaked_usd`, `over_balance`, and `unpriced`, with
  defaults so older servers keep working. Ledger and billing were unaffected;
  only the caller-visible result understated.

## [1.1.0] — 2026-07-16

### Changed
- **Copy aligned with shipped reality** (audit 2026-07-16). The Team tier no
  longer advertises "Attribution by user" or a team roster: usage_events has no
  user column and no seat plumbing exists yet (both tracked in #136); the tier
  now promises workspace + provider attribution, which the ledger actually
  does. "Verifiable receipts" on the Pro tier is now "chain-verifiable ledger"
  (checkpoint receipts in #121 are real; a packaged savings-receipt artifact is
  not, yet). The efficiency module's docstring no longer quotes dogfood dollar
  figures that have no committed artifact.

### Added
- **Savings baselines exposed end to end** (#134). `Meter.track` now accepts
  `baseline_cost_usd` / `baseline_model` (which `record_usage` has carried since
  #7 but the SDK never exposed, so production coverage stayed zero outside the
  Hermes sync), and a new **token-reduction counterfactual**:
  `baseline_input_tokens` / `baseline_output_tokens` record the token counts a
  call would have sent *without* the optimization (e.g. the full-context prompt
  a memory recall replaced), priced from the published table at
  `baseline_model` if given, else at the event's own model, with the actual
  cost floored at its own list price so a mis-recorded cost can never inflate
  the saving. Same fields accepted per event on `POST /v1/usage` (negative
  counts rejected 400) and documented in `openapi.yaml`. This is the mechanism
  that lets Perseus record token-reduction savings per call, which the
  model-substitution baseline structurally cannot see (perseus#805).
- **Quantization as a cost-model dimension** (#128). The precision a model is
  served at (fp16 / fp8 / nvfp4 / int8 / int4 / 1bit) is now a first-class lever
  in pricing: `pricing.resolve_precision_multiplier()` and a new
  `quantization=` argument to `pricing.estimate_cost()` scale per-token inference
  cost by a precision multiplier. Multipliers default to **1.0 (no assumed
  savings)** for every tier and for any unrecognized input — so an uncalibrated
  deployment can never over-report savings. Real multipliers are supplied from
  *measured* artifacts (perseus-vault#630) via `pricing.quantization` in
  `~/.ledger/config.yaml`, never from vendor-published claims. See
  [docs/quantization-cost-model.md](docs/quantization-cost-model.md).

### Changed
- **Savings-share default is now 10%** (was 18%). `billing.savings_share_pct`
  defaults to `0.10` and `savings.DEFAULT_RATE_BPS` to `1000`; per-run `--rate`
  and the config key still override. This is the published rate for the
  value-based path; the underlying share math is unchanged.
- **Attribution: Ledger measures, it doesn't save.** Corrected the dashboard
  billboard and all copy to reflect that **Perseus** (routing) + **Vault** (memory)
  are what reduce spend; Ledger meters it and *proves* the savings. The billboard
  now branches on whether a Perseus baseline is present: with one it reads
  *"Perseus saved you $X — verified by Ledger"*; standalone it reads *"Your AI
  spend … flagship-equivalent … N× efficient"* (a tracking/verification stat, no
  "saved" claim), plus a "reconcile against your provider console" nudge. The Free
  tip jar only appears when there are provable ecosystem savings to share. Tier
  copy re-grounded on **verification** (Pro) and **attribution by user/provider**
  (Team). See [docs/three-tier-model.md](docs/three-tier-model.md).

### Fixed
- **303 redirects now carry `Content-Length: 0`** so proxies / in-app webviews
  reliably follow the `Location` (billing checkout / portal buttons no longer
  appear "dead" behind a tunnel).

### Added
- **Three-tier model: Free / Pro / Team (+ Enterprise).** One meter, three ways
  to pay for it — the savings-share is a single lever set per tier
  (`Tier.savings_share`): **suggested** on Free (an optional tip jar), **waived**
  on Pro (the flat $20/mo replaces it), **mandatory** on Team (18% of provable
  savings). New `Tier.per_seat_usd_month` ($10/seat on Team) and
  `Tier.full_reporting`; `pricing.savings_mode()`. Free now meters **unlimited**
  so the savings billboard keeps running (the paywall is reporting *depth*, not
  volume). New **efficiency billboard** on the dashboard (attributed — see
  Changed above) shown on every tier, with a Free-tier **tip jar**
  (`POST /billing/checkout/donate` → one-time Stripe Checkout, recorded as a
  distinct hash-chained `donation` ledger entry). Deep reporting (per-task,
  leakage, export) is gated behind Pro. `ledger bill-savings --apply` now refuses
  to invoice a waived (Pro) or suggested (Free) org without `--force`. `/pricing`
  and the `ledger pricing` CLI show the four-tier ladder. See
  [docs/three-tier-model.md](docs/three-tier-model.md).
- **Efficiency leakage & policy adherence (#8).** The negative half of the
  efficiency story: turns that ran ABOVE the cheapest policy-passing option.
  Meter an event with `optimal_model` / `optimal_cost_usd` (the model the routing
  policy *would* have chosen); the server prices it like the baseline. `ledger
  efficiency` now reports **leaked $** (`Σ max(0, cost − optimal)`) and an
  **adherence rate** (share of policy-covered turns that ran on-policy) alongside
  achieved efficiency — so "you saved $X" is paired with "and left $Y on the
  table on these off-policy turns." Schema v8 (additive: nullable
  `usage_events.optimal_micros`, hash-chained as another optional trailing field
  so pre-v8 chains verify unchanged); `optimal_micros` exported as `optimal_usd`
  for audit; `/v1/usage` and `ledger meter --optimal` accept it. Only events that
  carry an optimal count toward leakage/adherence — the negative signal is never
  fabricated. Roadmap for tiers 2–3 in docs/roadmap-efficiency-leakage.md.
- **Savings-share billing (`ledger savings` / `ledger bill-savings`).** The
  value-based revenue path: bill a share (default 18%, `billing.savings_share_pct`)
  of the money Perseus provably saved a customer, not just the flat subscription.
  Every metered event can carry a `baseline_cost_usd` counterfactual (what the
  same call would have cost without Perseus); the per-event saving is
  `max(0, baseline − cost)` and a period's billable share is the summed savings ×
  the rate. Three properties keep the number defensible: only events with a
  baseline count (never a blanket percentage), the baseline is folded into the
  usage hash chain (so an inflated saving breaks `ledger verify`) and exported as
  `baseline_usd` for independent reconstruction, and coverage is always disclosed.
  `ledger savings` reports the figure read-only; `ledger bill-savings --apply`
  raises a Stripe invoice and records an idempotent `savings_invoices` row (a
  re-run for the same org+period is a no-op). `baseline_cost_usd` is accepted at
  `ledger meter --baseline` and the `/v1/usage` boundary. Free tier now covers up
  to 5 team members (the hybrid model: free to 5, then the $20/mo floor, then
  opt-in savings-share). Schema v7 (additive: nullable `usage_events.baseline_micros`
  chained as an optional trailing field so pre-v7 chains verify unchanged, plus
  the `savings_invoices` table). See BILLING.md §6 and docs/savings-share.md. (#7)
- **Automatic baselines from the Hermes bridge.** `examples/hermes_sync.py` now
  tags each synced session with a `baseline_model` — the flagship the customer
  would have run without Perseus routing — so hosted Ledger prices the
  counterfactual from its published table and records the saving with no manual
  step. Opt in with `LEDGER_BASELINE=flagship` (or `LEDGER_BASELINE_MODEL[S]`); a
  baseline is attached only when the actual model differs from the flagship (so
  un-routed traffic records nothing), and only the model name is sent — never a
  dollar amount — keeping the figure reconstructable. `record_usage` and
  `/v1/usage` accept `baseline_model` and price it via `pricing.resolve_price`;
  `config.savings.baseline_models` is the operator-owned flagship map. (#7)
- **Ledger tamper-evidence (`ledger verify`).** The usage ledger was
  integer-exact and re-queryable but append-only *by convention* only — an
  operator with database access could rewrite a debit undetectably, which the
  verified-savings work (perseus#749) made load-bearing. Every `usage_events`
  row now carries a per-org SHA-256 hash chain (`prev_hash`/`row_hash`) computed
  inside `record_usage`'s transaction, so any modification, deletion, reorder,
  or insertion breaks the chain from that point. `ledger verify` walks the chain
  and reports the first divergence (exit 2 on tampering); `GET /v1/admin/verify`
  and a dashboard "Ledger integrity" tile surface the same. An optional keyed
  HMAC (`ledger.hmac_key` / `LEDGER_CHAIN_HMAC_KEY`), with the key held by the
  customer, gives the two-party property — the operator alone cannot re-chain a
  rewritten history. Schema v6 (additive: nullable columns; rows written before
  the upgrade are reported as an unverifiable "pre-chain" prefix, not
  back-filled). Reuses the Perseus Vault audit-chain design (#460/#463). No
  "tamper-evident" claim ships in public docs until an external crypto review
  covers this. See docs/ledger-integrity.md. (#108)
- **Provider cost fetchers + scheduled close (`ledger close`).** Follow-up to
  the reconciler (#107): the operator no longer supplies the authoritative
  per-provider total by hand. New `ledger_agent.fetchers` pulls a period's real
  spend from each provider's own cost API — OpenAI organization Costs
  (`OPENAI_ADMIN_KEY`), Anthropic cost report (`ANTHROPIC_ADMIN_KEY`), and AWS
  Bedrock via Cost Explorer (`pip install 'ledger-agent[fetchers]'`) — and
  normalizes to the reconciler's `{provider: usd}` shape. `ledger close` runs
  fetch → reconcile in one step (period defaults to the previous month;
  providers default to those with recorded usage), dry-run by default, apply with
  `--apply`. Carries #107's guardrails: a provider whose fetch fails is left
  unreconciled and **never zeroed**, and `close` exits non-zero so a cron job
  surfaces the gap. OpenAI/Anthropic use stdlib urllib (no new hard dependency);
  the offline story is untouched. See docs/reconciliation.md. (#109)
- **Cost reconciliation (`ledger reconcile`).** The wired providers return
  tokens, not dollars, so metered cost is priced from the static table in
  `pricing.py` and flagged `estimated`. The new reconciler trues that up to a
  provider's authoritative billing: give it the provider's own per-provider
  total for a period (from its usage or cost export) and it writes one `adjust`
  ledger entry per provider so the ledger, and the prepaid balance, match the
  real invoice. Idempotent and restatement-safe (each provider+period keyed by
  `reconcile:<period>:<provider>`, written net of prior adjusts), dry-run by
  default, and it never assumes a provider with no supplied total should be
  zeroed. New `ledger_agent.reconcile` module + `ledger reconcile` CLI. See
  docs/reconciliation.md.

### Fixed
- **Per-model spend attribution for mid-session model switches.** Ledger reads
  Hermes' `state.db` for spend. The `sessions` row attributes every token to the
  `(model, billing_provider)` active when the session *started*, so a mid-session
  `/model` switch dumped the whole session's cost onto the initial provider —
  corrupting per-provider spend, and with it the runway projections and the
  runway router's decisions (a provider with hidden burn looks like it has
  infinite runway and gets *more* traffic). Ledger now reads the schema-v17
  `session_model_usage` table (authored + fixed upstream in hermes-agent, issue
  #51607) so spend lands on the provider that actually served each call. Tokens
  come straight from the per-model rows; the session's authoritative
  `actual_cost_usd` is allocated across those rows in proportion to their
  estimated cost (falling back to token weight), so the per-session total is
  preserved exactly and never regresses. Pre-v17 / un-backfilled databases fall
  back to the aggregate `sessions` row. Applied across all three readers — the
  standalone monitor (`ledger.py`), the hosted sync (`examples/hermes_sync.py`),
  and the backfill (`examples/hermes_integration.py`) — with a new canonical,
  unit-tested implementation in `ledger_agent/hermes.py`.

## [1.0.1] — 2026-07-05

**Security-hardening release.** A four-part follow-up to the internal pre-1.0
review, from a fresh 2026-07-05 audit (money/ledger, auth/OIDC, HTTP surface,
deploy posture). No API/contract changes; the `/v1` surface is unchanged. Every
finding was traced to source and covered by tests. Highlights: closed an OIDC
login-CSRF, made all money side effects atomic with the markers guarding them
(no webhook credit-loss / idempotency double-debit under crash+retry), added
security headers + CSV-injection defense, and made the server fail closed rather
than expose an unauthenticated dashboard on a public interface.

### Security
- **Fail closed instead of exposing an unauthenticated dashboard.** The server now
  refuses to bind a non-loopback host (e.g. `--host 0.0.0.0`) when authentication
  is disabled — previously the shipped Docker/compose default (`serve --demo
  --host 0.0.0.0`) and the off-by-default / fail-off-on-misconfig auth could
  publish an open billing console on the network. Override for a trusted network
  with `--allow-insecure` / `LEDGER_ALLOW_INSECURE=1`; the disposable demo image
  sets it explicitly. New `docs/deploy-hardening.md` documents the safe config.
- **Config backups no longer escape the ignore rules.** `.gitignore`/`.dockerignore`
  matched only `*.ledger-bak-*` but the real backup name is `config.yaml.bak-<ts>`,
  which could carry file-sourced secrets into a commit or image; now `config.yaml*`
  and `*.bak-*` are ignored.
- **SMTP `alerts.require_tls` (opt-in)** refuses to send alerts over an
  unencrypted connection (protects alert bodies, not just credentials, from a
  STARTTLS downgrade). **PyYAML** is now a hard requirement at server start
  (fail fast) rather than silently degrading config parsing.

### Changed
- Dependency floors gained upper bounds (`stripe<14`, `reportlab<5`, `PyYAML<7`,
  `pytest<10`) for reproducible builds; the Docker image now runs as a non-root
  user.
- **Ledger atomicity: every money side effect now commits in the same transaction
  as the marker guarding it.** Two crash-window bugs are closed:
  (1) *Webhook silent credit loss / double-reverse* — `mark_stripe_event` committed
  the event claim in a separate transaction from the credit `add_ledger`, so a
  crash between them plus Stripe's retry left the event marked "duplicate" with the
  credit never applied (customer paid, no credit); concurrent refund/dispute events
  for one charge could also double-reverse. `handle_webhook_event` now wraps the
  claim and all side effects in one `db.immediate` (`BEGIN IMMEDIATE`) transaction
  with `commit=False` threaded through `mark_stripe_event`/`add_ledger`/
  `set_org_tier`; any error rolls the whole thing back for a clean retry.
  (2) *`/v1/usage` idempotency double-debit* — the batch committed, but the
  idempotency-key status was flipped in a later separate commit, so a crash in
  between left committed events behind a NULL-status claim that the 120s reclaim
  deleted, letting a retry re-record the batch. The response is now stored inside
  the same `db.immediate` block as the debits. (2026-07-05 security review)
- **Prepaid hard-stop decided in integer micro-dollars.** The `block_over_balance`
  check compared float USD (`balance - cost_usd < 0`); it now uses
  `get_balance_micros`/`usd_to_micros` so sub-micro float error can't let a debit
  slip past zero or wrongly block one. (2026-07-05 security review)
- **OIDC login-CSRF: bind the OAuth flow to the browser that started it.** The
  authorization `state` was held only in a process-global pool, so an attacker
  could complete their own Google sign-in, capture their `code`+`state`, and feed
  a victim's browser a `/auth/callback?code=…&state=…` link — planting an
  attacker-owned session in the victim's browser (the victim would then operate
  inside, and leak API keys to, the attacker's tenant). `/auth/login` now sets a
  short-lived `HttpOnly; SameSite=Lax` `ledger_oauth_state` cookie and
  `handle_callback` requires the callback `state` to match it (constant-time),
  in addition to the existing `_pending` nonce check. (2026-07-05 security review)
- **Security headers on every response.** `_send` now emits `X-Frame-Options: DENY`
  (+ CSP `frame-ancestors 'none'`) so the dashboard can't be framed/clickjacked,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, and a CSP that
  locks `default-src`/`object-src`/`base-uri`/`form-action` to self. Script/style
  keep `'unsafe-inline'` (the dashboard is inline-only); a nonce-based `script-src`
  is a follow-up. (2026-07-05 security review)
- **CSV export formula-injection neutralized.** Tenant-controlled
  `provider`/`model`/`workspace`/`task_type` cells beginning with `= + - @` (or a
  leading tab/CR) are now quote-prefixed in `export.csv`, so a crafted value like
  `=HYPERLINK(...)` can't execute when a teammate opens the export. (2026-07-05)
- **Webhook error/log hardening.** `/webhook/stripe` no longer echoes the raw
  exception text to the caller on a bad signature (returns a generic message, logs
  detail server-side), and the success path logs only the event id/type instead of
  the applied result (org_id/amount/balance). (2026-07-05 security review)

### Changed
- `views.simple_page`'s body parameter renamed to `body_html` with a docstring
  making explicit that it is inserted as raw HTML and callers must pre-escape
  untrusted data (removes a latent XSS foot-gun; no live path today). (2026-07-05)

## [1.0.0] — 2026-06-27

**Ledger 1.0 — the billing loop is closed and the contract is frozen.** The
ledger is now an auditable mirror of Stripe (refunds, disputes, and failed
payments reverse it idempotently); every money- and quota-bearing input is
guarded; ingest and auth are hardened; self-serve export and a token-scoped admin
API are in; and the `/v1` OpenAPI spec plus the DB forward-compatibility policy
are published as the frozen contract Perseus and Perseus Vault build against. An internal
security review (documented in `docs/security-review-2026-06-27.md`) cleared the
money/auth/tenant surfaces; an external review remains the gate before any public
launch.

### Fixed
- **Orphaned in-flight Idempotency-Key no longer 409s forever (review F3, #80).**
  If a request crashed between claiming an `Idempotency-Key` and storing its
  response, the row stayed `status=NULL` and every retry got `409` permanently.
  A claimed-but-unanswered row older than a 2-minute grace window is now
  reclaimable (re-processed), while a *completed* claim is never reclaimed
  (replay preserved). Added `db.purge_idempotency()` to bound the table.

### Added
- **OpenAPI 3.1 spec for `/v1/*` + the forward-compatibility contract (#67).**
  [`openapi.yaml`](openapi.yaml) documents the frozen `/v1` surface (usage ingest,
  spend export, admin) that Perseus/Perseus Vault build against. [`docs/schema.md`](docs/schema.md)
  states the database forward-compat policy (additive-only within 1.x; breaking
  changes need a new major), and [`docs/postgres.md`](docs/postgres.md) records the
  ADR keeping the single-file SQLite backend for 1.0 while documenting the
  Postgres migration shape. `db.SCHEMA_VERSION` bumped to 5 (the
  `ingest_idempotency` table); `init_schema` now refuses to open a database
  written by a newer Ledger, and `db.get_schema_version()` reads the stamped
  version.

### Security
- **Negative token counts can no longer rewind the free-tier meter (#80).** The
  `/v1/usage` boundary validated only that token fields coerce to int, so a
  negative `input_tokens` (with a non-negative `cost_usd`, dodging the #61 guard)
  rewound `tracked_tokens_mtd` — bypassing the Free-tier quota — and corrupted
  `SUM(tokens)` aggregates. Negatives are now rejected with a `400` at the
  boundary and a `ValueError` in `record_usage`.
- **CSRF synchronizer token as defense-in-depth (#58).** State-changing
  dashboard POSTs now accept a per-session CSRF token in addition to the existing
  fail-closed Origin/Referer check: a request passes if it is same-origin **or**
  carries a valid token. This lets through legitimate requests whose
  Origin/Referer a privacy proxy stripped (which the origin check rejects), while
  a forgery — which can't know the token — is still blocked. The token is
  `HMAC-SHA256(session_token, "ledger-csrf-v1")`, derivable only by the cookie
  holder and never leaking the cookie; it's embedded as a hidden `_csrf` field in
  every dashboard/pricing form. The origin check remains the first gate.
- **Per-IP self-serve signup throttle (#59).** The existing global hourly limiter
  and DB-backed daily org cap (#33) are both global, so one abuser could drain
  the whole daily budget and lock out legitimate signups. A new per-IP cap
  (`auth.max_signups_per_ip_per_day`, default 3; in-memory 24h ring) is checked
  before the global limiter. The client IP is the socket peer by default, or the
  first `X-Forwarded-For` hop when `auth.trust_forwarded_for` is set (for running
  behind a trusted reverse proxy). Existing members signing in are never
  throttled — only new-org self-serve signups.

### Fixed
- **Dashboard "Sign out" chip used an undefined `--muted` CSS var (#56).** The
  signed-in user chip and its Sign-out button now use `var(--dim)`, so the text
  renders in the intended dim gray instead of falling back to the inherited color.

### Changed
- **Package version is single-sourced (#57).** `pyproject.toml` now declares
  `dynamic = ["version"]` reading from `ledger_agent.__version__`, so the wheel
  metadata and `ledger version` can no longer drift apart.

### Tests
- **High-risk auth/tenant coverage (#66, part 3 — closes #66).** Added tests for
  the previously-untested money/auth paths: the hand-rolled OIDC **RS256 verifier**
  itself (a real pure-Python RSA-signed token verifies; a tampered payload and a
  non-RS256 `alg` are rejected — every other auth test had set
  `allow_unsigned_tokens`, so the signature math was never exercised); the
  `_authz_org` **cross-tenant `PermissionError`** path; and the
  `allow_negative_balance` **exemption end-to-end** over HTTP. (The #60/#61/#62
  coverage landed with those fixes.)

### Added
- **Token-scoped admin API (#66, part 2).** A new `/v1/admin/*` surface lets an
  operator script tenant management instead of using the CLI/dashboard only:
  `GET/POST /v1/admin/orgs` (list / create), `POST /v1/admin/credits`
  (`grant`/`adjust` ledger entries), and `GET/POST /v1/admin/keys` (list /
  mint — the secret is returned once). Gated by a single `admin.token`
  (env `LEDGER_ADMIN_TOKEN`, masked from saved config, constant-time compared);
  with no token configured the API is disabled and returns `404`.
- **Self-serve spend export + cursor pagination (#66, part 1).** New
  `GET /v1/usage/export.csv` and `export.json` (Bearer-authenticated, org-scoped,
  optional `?since`/`?until` epoch bounds) let a customer pull their own usage
  for their books. List endpoints now paginate with a `?limit&before=<_rowid>`
  cursor: new `GET /api/ledger` and `GET /api/events` return `{items,
  next_before, limit}`, and `GET /api/orgs` accepts `?limit&offset`. The
  underlying `db.ledger_history` / `metering.recent_events` gained a `before`
  cursor.

### Security
- **`/v1/usage` ingest hardening (#65).**
  - **Idempotency-Key.** A retried or duplicated POST used to double-count usage
    and double-debit credit (the inverse of the webhook idempotency from #26).
    The endpoint now accepts an `Idempotency-Key` header, claims it atomically
    with the recording (per-org `ingest_idempotency` table), and replays the
    stored response on a duplicate instead of re-recording.
  - **Per-key rate limit.** A leaked/abusive key could fire unbounded batches; a
    per-key token-bucket limiter (config `ingest.rate_per_min` / `burst`) now
    returns `429` when exceeded.
  - **Monitor-bridge lock-down.** The bridge subprocess now requires the command
    to be an absolute path present in `monitor.allowed_binaries` (fail-closed,
    structured argv, `shell=False`), and when auth is on it only shells out for
    an authenticated request — an unauthenticated dashboard hit no longer
    triggers it.

### Added
- **Estimated costs are flagged `unpriced` when no exact model price exists
  (#64).** Whenever a usage event is metered without an exact `cost_usd` and the
  (provider, model) isn't in the price table, the cost falls back to a
  provider/global default — previously with no signal, so a coarse estimate
  looked authoritative. `MeterResult.unpriced` now carries that signal and it is
  surfaced per-event in the `/v1/usage` response. The price table is expanded to
  current 2026 models (adds `claude-fable-5`, the GPT-5 family, more Gemini, and
  xAI / Mistral / Cohere / Meta providers), carries a dated `PRICE_TABLE_AS_OF`
  stamp shown on the pricing page, and `ModelPrice` can now price reasoning
  tokens separately (defaults to the output rate, so existing estimates are
  unchanged). *Deferred:* persisting `unpriced` onto historical dashboard rows
  (needs a `usage_events` column) and cache-*write* token pricing (needs a new
  event token field) — both noted for a follow-up.

### Fixed
- **Money-correctness cluster (#63) — four independent fixes:**
  - **USD-only is enforced.** The credit ledger stores plain USD micro-dollars
    with no currency dimension, so a non-USD top-up was recorded as the wrong
    number of dollars. A configured `billing.currency` other than `usd` now
    raises a clear `BillingError` instead of silently mis-billing.
  - **`past_due` no longer counts as active Pro.** A subscription in dunning
    used to retain full Pro for the whole retry window; Pro is now kept only
    through `active`/`trialing` (Stripe restores Pro on the next `active`).
  - **Credit checkout amounts are bounded** to a finite $1–$10,000 at the form
    boundary — `inf`/`nan`/a 9-figure typo previously passed straight to Stripe.
  - **Month boundaries are computed in UTC**, matching the UTC-epoch event store,
    so the free-tier quota reset and MTD reports no longer shift by the server's
    UTC offset on a non-UTC host.
- **Batch `/v1/usage` no longer hides prepaid-hard-stop rejections (#62).** The
  multi-event summary reported only the free-tier `blocked` count;
  `over_balance` rejections were absent, so a prepaid org past zero credit could
  get a `200` with events silently dropped. The summary now carries
  `over_balance_blocked`, `free_limit_blocked`, and a `blocked` total covering
  both reasons, and the endpoint returns `402` whenever *nothing* landed —
  including a batch split across both rejection reasons (previously it only 402'd
  when a single reason accounted for the whole batch).

### Added
- **Stripe refunds, disputes, and failed payments now reverse the ledger
  (#60).** The webhook handler previously ignored every reversal event, so a
  refunded or charged-back prepaid top-up left the credit on Ledger's
  append-only ledger forever. New handlers: `charge.refunded` posts a negative
  `refund` entry (converging to the charge's cumulative `amount_refunded`, so
  partial/repeat refunds reverse exactly once); `charge.dispute.created` /
  `charge.dispute.funds_withdrawn` post a negative `adjust` for the disputed
  amount (both events for one dispute converge to a single reversal); and
  `invoice.payment_failed` is recorded as a dunning alert. Top-ups are now keyed
  on the PaymentIntent so a dispute (which carries no customer) maps back to its
  org. Reversals converge to a target amount per Stripe reference on top of the
  existing per-event idempotency, so replays can't double-reverse.

### Security
- **Negative `cost_usd` can no longer mint prepaid credit or bypass the hard-stop
  (#61).** A caller-supplied negative `cost_usd` previously flowed to the ledger
  debit path as `-(-x)` — a *positive* credit delta — and slipped past the prepaid
  hard-stop (a negative cost only raises the projected balance). `record_usage`
  now rejects a negative `cost_usd` with a `ValueError`, and `/v1/usage` returns
  `400` for a negative or non-numeric `cost_usd` before any event is recorded.
  Genuine corrections/credits must go through the explicit adjust/grant/refund
  ledger path, never metering.

### Changed
- **Prepaid credit hard-stop is now ON by default (#28).** `pricing
  .block_over_balance` defaults to `true`, so a prepaid org can no longer debit
  unbounded amounts past a zero balance — `/v1/usage` returns `402` with
  `over_balance` once a charge would go negative. It only affects orgs that have
  actually held credit; pure free-tier tracking is never blocked. Trusted /
  internal orgs can opt into track-only mode with a new per-org
  `allow_negative_balance` flag, toggled via `ledger org allow-negative <org>` /
  `ledger org enforce-balance <org>` (idempotent column migration on existing
  databases).

### Fixed — 1.0 punch-list (#37)
- **`org create` / `workspace create` with no NAME** now exit with a usage
  message instead of crashing in `slugify(None)`.
- **500 responses no longer leak `str(exception)`** — both the GET error page and
  the POST JSON return a generic message plus a short reference id; the full
  exception is logged server-side under that id.
- **Reflected-XSS surface closed** — the 404 handler now HTML-escapes the
  request path before rendering it.
- **Ambiguous-org guard** — state-changing POSTs (billing, API keys) require an
  explicit `org` when the signed-in user belongs to more than one, instead of
  silently acting on the earliest org. Dashboard GETs stay lenient.
- **`api_key_org` throttles `last_used_at`** to at most once per 60s per key,
  removing per-ingest WAL thrash / write contention with the metering txn.
- **`install-claude-hook` backup** copies the pristine original bytes once and no
  longer clobbers that backup (or re-serializes away comments) on re-runs.
- **PyYAML-free config reader** now reads back the block-style lists PyYAML
  writes, so a config saved with PyYAML and re-read without it no longer silently
  resets to defaults.

### Security
- **DB-backed per-day signup cap (#33)** — self-serve org creation now has a
  hard ceiling per rolling 24h (`auth.max_new_orgs_per_day`, default 50),
  counted from the `organizations` table so it survives process restarts —
  unlike the existing in-memory hourly limiter, which it complements. Set to
  `0` to disable.
- **OIDC unsigned-token bypass removed** — signature verification was skipped for
  any id_token whose header segment literally equalled `"hdr"` (a test shim) on
  the production path. It is now gated behind an explicit, default-off
  `auth.allow_unsigned_tokens` flag used only by the test suite. (#37)

## [0.7.0] — 2026-06-24

Security hardening — the second of the two 1.0 launch-gate milestones. Closes
the public-surface findings so open signup + live money can be exposed safely.
(The roadmap's v0.7 exit also calls for an external security-review pass before
public launch; that human gate is separate and not flipped here.)

### Security
- **Request body-size cap (#31)** — `/v1/usage` and `/webhook/stripe` reject
  bodies over 1 MiB with `413`, closing a trivial memory-exhaustion DoS.
- **CSRF protection (#32)** — cookie-authenticated state-changing POSTs are
  same-origin checked (Origin/Referer vs `auth.base_url`); logout is now a POST
  (`GET /auth/logout` returns `405`).
- **Signup abuse controls (#33)** — self-serve signup is rate-limited
  (5/hour, in-memory global). *(Correction, 2026-06-26: the per-day org cap is
  not yet implemented; tracked in the reopened #33.)*
- **Report XSS escaping (#34)** — attacker-controlled names (org, keys, periods)
  are HTML-escaped in the dashboard and HTML/PDF reports.
- **SMTP TLS (#35)** — implicit TLS on port 465 (`SMTP_SSL`) and STARTTLS on
  other ports before any `LOGIN`; no credentials sent in the clear.
- **OIDC JWKS verification (#36)** — Google ID tokens are verified against the
  published JWKS RSA signature (cached 1h) in addition to `aud`/`iss`/`exp`/
  `nonce` claims.

### Polish (#37)
- Strict integer parsing on token fields, `--db` flag wiring, config-file
  backups on write, `email_verified` enforcement, and a YAML-load fallback.

## [0.6.0] — 2026-06-24

Money & concurrency correctness — the first 1.0 launch-gate milestone. Root
cause for most findings: read-modify-write with no atomic transaction under the
threaded, connection-per-request server.

### Fixed
- **Atomic `/v1/usage` (#27)** — validate all events, record them in a single
  transaction, commit once; no partial batches, no double-count.
- **Webhook idempotency (#26)** — insert the dedup row first and apply the
  side-effect only if newly inserted, so retried Stripe events can't double-credit.
- **Concurrency hardening (#30)** — `PRAGMA busy_timeout` + WAL. *(Correction,
  2026-06-26: the atomic `balance_after` and free-tier quota-race fixes were not
  fully completed — the ledger read-modify-write is still non-atomic under
  concurrency; tracked in the reopened #30.)*
- **Trustworthy credit (#29)** — credit prepaid balance from Stripe's
  `amount_total`, never client-supplied metadata.
- **Prepaid hard-stop (#28)** — stop debiting past zero; opt-in `402` when a
  prepaid org is exhausted.
- **Integer micro-dollars (#38)** — all money stored as integer micro-dollars
  (schema migration) to eliminate float drift before the 1.0 schema freeze.

## [0.5.1] — 2026-06-23

### Fixed
- **Ingest blocked behind Cloudflare.** The SDK's remote `Meter` and the Hermes
  sync bridge sent the default `Python-urllib/x.y` User-Agent, which Cloudflare
  (and similar WAFs) hard-block with **error 1010** — so `POST /v1/usage`
  through the public origin failed for any urllib client. Both now send a real
  `User-Agent` (`ledger-agent/<version>`). Caught while dogfooding Hermes.

## [0.5.0] — 2026-06-23

The **usage ingest API** — closes the self-serve loop so a signed-up org can
feed usage into a hosted instance over HTTP, with no SDK or local DB.

### Added
- **`POST /v1/usage`** — Bearer-authed JSON ingest. Meters one event or a JSON
  array (≤1000) via an API key, returns the metered result(s) + month-to-date
  quota. Past the free cap with `pricing.block_over_free_limit` on, it returns
  **402** with an `upgrade_url`.
- **API keys** — per-org `ledger_sk_…` secrets (only a SHA-256 hash is stored;
  the secret is shown once). New `api_keys` table, `db.create_api_key` /
  `api_key_org` / `list_api_keys` / `revoke_api_key`.
- **Dashboard key management** — an API-keys panel (list, create, revoke) with a
  ready-to-paste `curl` snippet; a one-time "key created" page.
- **`ledger keys create|list|revoke`** CLI for self-hosted/local key management.
- **SDK remote mode** — `Meter(remote="https://…", api_key="ledger_sk_…")` (or env
  `LEDGER_REMOTE_URL` + `LEDGER_API_KEY`) sends each `track()` to `/v1/usage`
  instead of a local DB. Auto-detected from env, so the bundled adapters and the
  Claude Code hook report to a hosted instance with no code change. A 402 over
  quota returns a non-recorded result rather than raising (won't break an agent);
  `balance()`/`summary()`/`topup()` stay local-only.

### Changed
- Schema version 3 — adds the `api_keys` table (additive; auto-applied on start).
- `/v1/usage` is a public path (it authenticates by API key, not a session).

## [0.4.0] — 2026-06-23

The **self-serve signup funnel** — Ledger turns into a SaaS strangers can buy
without an operator in the loop.

### Added
- **Open signup** (`auth.allow_signup` / `LEDGER_ALLOW_SIGNUP`). When on, any
  verified Google account that isn't already known gets its *own* new Free-tier
  org as owner. Off by default; the allow-list still takes precedence so
  inviting a teammate and onboarding a stranger stay distinct. See `docs/auth.md`.
- **Free-tier enforcement** in the metering core:
  - Workspace cap (Free = 1) — events tagged with a new workspace fold into the
    org's first workspace instead of creating another; tracking never breaks.
  - Token quota (Free = 10K/mo) — past the cap, events are flagged
    `over_free_limit` (still recorded, so no billing data is dropped). Optional
    hard stop via `pricing.block_over_free_limit`.
  - `metering.tier_status()` — single source of truth for plan limits vs. usage.
- **In-app upgrade nudge** on the dashboard once an org is near (≥75%) or over
  its quota, wired straight to Stripe Checkout.
- **Public `/pricing` page** comparing Free / Pro / Enterprise — the surface the
  nudges point to (reachable without signing in).

### Changed
- `MeterResult` gains `recorded` and `over_free_limit` fields (additive).
- `db.create_org()` accepts `owner_name`.

## [0.3.0] — 2026-06-22

### Added
- **Google OIDC sign-in** for the dashboard and billing endpoints (stdlib only,
  no auth library). Off by default; enable with `auth.enabled` + a Google OAuth
  client. Server-side, revocable sessions (`sessions` table); access is
  allow-listed (existing org members, plus `auth.allowed_emails` /
  `auth.allowed_domain`); the dashboard and APIs are scoped to the signed-in
  user's orgs (`?org=` for a non-member returns 403). See `docs/auth.md`.
- Public-by-default paths when auth is on: `/healthz`, `/webhook/stripe`,
  `/auth/*` — so health checks and Stripe webhooks are never challenged.

### Changed
- Schema version 2 — adds the `sessions` table (additive; auto-applied on start).

## [0.2.0] — 2026-06-21

The **monetization engine** — Ledger becomes the billing layer for AI agents.

### Added
- **`ledger_agent` package** (PyPI `ledger-agent`, console command `ledger`).
- **Multi-tenant model** — organizations → workspaces → users, in SQLite.
- **Usage metering** per provider / model / task-type, with token→cost
  estimation and exact-cost passthrough.
- **Prepaid credit** — append-only ledger that depletes as calls route through;
  balance is the sum of deltas (robust to out-of-order / back-filled inserts).
- **Dark dashboard** at `:8420` (`ledger serve`) — brand `#0c0814`, real-time
  cards, per-workspace budget bars, provider health, cost-per-task, live feed.
  Framework-free (stdlib `http.server`).
- **`ledger serve --demo` / `ledger demo`** — realistic month of sample data.
- **Stripe billing** — Checkout for prepaid credits + the $20/mo Pro plan,
  Customer Portal, and an idempotent webhook handler. Optional + offline-safe.
- **`ledger stripe-setup`** — creates the Pro price in your Stripe account.
- **`ledger install-claude-hook`** — wires Ledger into Claude Code / Codex as a
  Stop hook so every turn meters automatically.
- **Monthly reports** — PDF (reportlab) or print-ready HTML.
- **Alerts** — SMTP low-balance and budget-cap email, de-duped, offline-safe.
- **Pricing tiers** — Free / Pro / Enterprise.
- **Embeddable client** — `from ledger_agent import Meter`.
- **Integrations** — Anthropic / OpenAI / Hermes adapters; runnable examples.
- **Packaging** — `pyproject.toml`, Dockerfile, docker-compose, GHCR + PyPI
  release workflow, expanded CI.

### Unchanged
- The live credit monitor (`ledger.py`) and runway router (`ledger_route.py`)
  are left byte-for-byte intact. The engine bridges to them via subprocess.

## [0.1.0]
- Provider credit & spend monitor (`ledger.py`) and runway-based model router
  (`ledger_route.py`) for Hermes Agent.
