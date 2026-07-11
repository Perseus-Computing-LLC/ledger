# Ledger integrity — usage-event tamper-evidence (#108)

Plutus stores usage in an integer-exact, independently re-queryable ledger
(`SUM(usage_events.cost_micros)`). That makes the dollars *reproducible* — but,
on its own, the table was append-only **by convention** only. Nothing stopped an
operator with database access from rewriting a debit, deleting an event, or
inserting a fabricated one. The verified-savings work (perseus#749: dollars read
straight from this ledger) made that gap load-bearing: self-measured savings on
a mutable ledger is exactly the attribution-dispute failure mode a savings-share
billing model can't tolerate.

This feature makes tampering **detectable**.

## How it works

Every `usage_events` row carries two columns:

| column      | meaning                                                          |
|-------------|------------------------------------------------------------------|
| `prev_hash` | the `row_hash` of the previous event **for the same org**        |
| `row_hash`  | `H(prev_hash-or-genesis ‖ canonical(row))`                       |

- **Per-org chain.** Each organization has its own independent chain (the
  two-party billing unit — a customer verifies their own stream). Ordering is by
  SQLite `rowid`, which is monotonic with insertion; `usage_events` is
  append-only, so `rowid` order *is* insertion order.
- **Canonical row.** The digest covers the immutable event columns — `id`,
  `org_id`, `workspace_id`, `provider`, `model`, `task_type`, the four token
  counts, `cost_micros`, `estimated`, `source`, `ts` — each tagged with its
  column name so a value can't migrate across columns without changing the hash.
- **Written at ingest.** The hash is computed inside `record_usage`, in the same
  transaction as the insert. The HTTP server wraps ingest in `BEGIN IMMEDIATE`
  (`db.immediate`), so reading the chain head and inserting the new row can't
  interleave with another writer — the chain is correct under concurrency and
  across a multi-event batch.

Any edit, delete, reorder, or insert changes a `row_hash` or orphans a
`prev_hash`, and verification fails from that point on.

## Verifying

```bash
plutus verify                # all orgs; exit 0 = intact, exit 2 = tampered
plutus verify --org <id>     # a single org
plutus verify --json         # machine-readable report
```

Also exposed as `GET /v1/admin/verify` (admin-token; returns HTTP 200 with
`{"ok": false, ...}` on divergence so a monitor can alert on the body) and as a
**Ledger integrity** tile on the dashboard.

The report is per-org: `events`, `verified`, `pre_chain`, `status`
(`ok`/`broken`/`empty`), and `first_divergence` (the event id, rowid, and a
human-readable reason) when broken.

## Keyed MAC (two-party mode)

By default the chain is plain SHA-256 — enough to detect an *accidental* or
*external* edit, and it keeps the offline/self-hosted story intact with zero
configuration. But a plain hash chain can be silently **re-chained** by the same
operator who edited a row: recompute every downstream hash and verification
passes again.

To close that, set a secret the operator does **not** control alone:

```bash
export PLUTUS_CHAIN_HMAC_KEY='<secret held by the customer>'
# or: ledger.hmac_key in config.yaml
```

With a key set, `row_hash` is HMAC-SHA256. Only a holder of the key can produce
a chain that verifies, so an operator without it cannot re-chain a rewritten
history. `plutus verify --hmac-key <secret>` accepts the key explicitly (e.g.
for a customer-side audit). This is the property behind any "auditable by both
parties" statement.

## Migration & scope

- **Schema v6, additive.** `prev_hash`/`row_hash` are nullable and added by the
  standard column migration. Rows written before the upgrade keep `NULL` hashes;
  `verify` counts them as a `pre_chain` prefix and reports them as
  **unverifiable (pre-upgrade)** rather than back-filling a hash it can't attest.
  The chain starts fresh at the upgrade.
- **Scope.** The chain covers `usage_events` — the source of the savings
  dollars. The `credit_ledger` (top-ups/adjusts) is a separate integrity surface
  and is not chained here.

## Guardrail

**No public document may claim Plutus is "tamper-evident" until this ships _and_
an external cryptographic review covers it** (the SOW drafted for the Perseus
Vault audit-chain review). Until then, savings statements carry the caveat the
harness/one-pager already print: the ledger is *re-queryable* and now
*hash-chained*, with the external review pending.
