# Database schema & forward-compatibility policy

Ledger stores all state in a single SQLite file (`~/.ledger/ledger.db` by
default). This document is the **1.0 forward-compatibility contract** for that
schema — the database half of the frozen contract whose API half is
[`openapi.yaml`](../openapi.yaml).

## Schema version

`ledger_agent.db.SCHEMA_VERSION` is an integer bumped on every schema change and
stamped into the `meta` table key `schema_version` on `init_schema()`. The
runtime currently declares **`SCHEMA_VERSION=22`**. Read the stored value with
`db.get_schema_version(conn)`; a fresh database is stamped with the runtime
value after all additive migrations have run.

| Version | Change |
|---|---|
| 4 | Converts money to integer micro-dollars: `cost_micros`, `delta_micros`, `balance_after_micros`, and `monthly_budget_micros`; also adds `allow_negative_balance`. |
| 5 | Adds the `ingest_idempotency` table (per-org `Idempotency-Key` store, #65). |
| 6 | Adds nullable `usage_events.prev_hash`/`row_hash` — the per-org tamper-evidence hash chain (#108). Rows written before the upgrade stay `NULL` ("pre-chain", unverifiable); they are never back-filled. See `docs/ledger-integrity.md`. |
| 7 | Adds nullable `usage_events.baseline_micros` (savings-share counterfactual) and the `savings_invoices` table (per-org/period savings-share billing, #7). The baseline is hash-covered when present. |
| 8 | Adds nullable `usage_events.optimal_micros` — the efficiency-leakage counterfactual (cheapest policy-passing option; actual above it = missed savings/off-policy, #8). It is hash-covered when present. |
| 9 | Adds the `chain_checkpoints` table — externally-retained tamper-evidence checkpoints that make the hash chain independently verifiable (#120). Idempotent per `(org_id, through_rowid)`. See `docs/ledger-integrity.md`. |
| 10 | Adds nullable `usage_events.external_ref` and `ix_usage_extref` for per-task/per-question attribution. |
| 11 | Adds nullable `usage_events.cache_write_tokens` for provider cache-creation billing. |
| 12 | Adds nullable `usage_events.user_id`, `users.active`, and `ix_usage_user` for team/seat attribution. |
| 13 | Adds nullable `organizations.stripe_subscription_id` for subscription seat synchronization. |
| 14 | Adds `api_keys.scope`, `api_keys.event_count`, `api_keys.rotation_of`, and the `ingest_health` table for scoped key rotation and source diagnostics (#150). |
| 15 | Adds nullable hash-covered decision evidence: `evidence_hashes`, `policy_version`, `result_hash`, `human_review`, and `correction_ref`. |
| 16 | Adds nullable hash-covered Authorized Action Receipt provenance: `agent_id`, `authority_manifest_ref`, `scope_anchor`, `action_intent_hash`, `action_status`, and `approval_ref`. |
| 17 | Adds nullable hash-covered context/render/resource/prebind commitments: `context_render_schema`, `context_render_hash`, `served_memory_provenance_hash`, `action_receipt_hash`, `resource_constraints_version`, `resource_constraints_hash`, `prebind_json`, and `prebind_hash`; also adds `reconciliation_note` for union-recovered usage records. |
| 18 | Adds nullable stage-aware receipt and evidence-binding fields (#219–#224): `served_claim_json`/`served_claim_hash`, `evidence_status`, `runtime_manifest_json`/`runtime_manifest_hash`, and `external_artifact_json`/`external_artifact_hash`. |
| 19 | Adds nullable hash-covered belief-context evidence (#237): `belief_context_json`/`belief_context_hash` — decision-time `believed`/`assumed`/`ignored` claims, HMAC-covered in receipts, reported at the attested evidence level when present. |
| 20 | Adds nullable hash-covered governance self-cost (#239): `governance_cost_json`/`governance_cost_hash` — internal telemetry (wall/cpu/mem/storage/tokens/model_calls/approval waits), excluded from customer-facing usage and billing totals. |
| 21 | Adds nullable hash-covered behavior-snapshot receipt pin (#238): `behavior_snapshot_json`/`behavior_snapshot_hash` — the sha256 of a canonical agent-run snapshot, re-verifiable with `ledger diff --require-target-digest`. |
| 22 | Adds nullable hash-covered custody disclosure for the referenced authority manifest (#241): `authority_manifest_custody` — 1f916 taxonomy label; missing/unknown custody renders as labeled uncertainty in verification output. |

## The contract (within the 1.0 major line)

1. **Additive only.** Schema changes are limited to **new tables** and **new
   columns that are nullable or have a default**. Existing columns are never
   renamed, retyped, or dropped, and no column gains a `NOT NULL` without a
   default. This keeps an older reader working against a newer database.
2. **Money columns are append-only and integer.** `credit_ledger` is an
   append-only ledger; balances are `SUM(delta_micros)` (see
   [`BILLING.md`](../BILLING.md)). The micro-dollar representation does not change
   in 1.x.
3. **Migrations are idempotent and forward-only.** `init_schema()` is safe to run
   on every startup: `CREATE TABLE IF NOT EXISTS` creates any missing tables, and
   `_migrate_add_columns()` `ALTER`s in any missing columns. There is no
   down-migration — restore from a backup to roll back.
4. **Forward-incompat is refused, not guessed.** Opening a database whose stored
   `schema_version` is **greater** than the running package supports raises
   rather than risk corrupting money data with old code. Upgrade the package.
5. **Breaking changes require a new major.** Anything that violates (1)–(2) — a
   dropped/renamed column, a money-representation change, a semantic change to an
   existing column — ships only in a Ledger 2.0 with an explicit, documented
   migration, and bumps `SCHEMA_VERSION` across the corresponding range.

## Concurrency note

The single-file design serializes writes via `BEGIN IMMEDIATE`
(`db.immediate()`), which is correct but caps `/v1/usage` at one writer at a
time. The horizontal-scale path is documented separately in
[`postgres.md`](postgres.md); it is intentionally **out of scope for 1.0** and is
designed to preserve this contract.
