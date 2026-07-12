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

## Independent verification — externally-retained checkpoints (#120)

The keyed MAC closes re-chaining *only* when the operator does not hold the key.
In the common single-party, self-hosted deployment the operator holds it, so an
operator with database access can still edit a row and recompute the whole chain
from genesis — `plutus verify` reports **intact**. The internal chain is
tamper-evident only relative to a *trusted head*, and nothing pins the head.

A checkpoint pins one. It escrows a head the chain provably reached:

| field           | meaning                                                     |
|-----------------|-------------------------------------------------------------|
| `org_id`        | the org this anchor belongs to                              |
| `through_rowid` | the `usage_events.rowid` the head covers                    |
| `head_hash`     | the `row_hash` of the event at `through_rowid`              |
| `event_count`   | chained events at or below `through_rowid`                  |
| `mode`          | `sha256` or `hmac-sha256`                                   |
| `sig`           | `HMAC-SHA256(key, canonical(org_id,through_rowid,head_hash,event_count,mode))`, when a key is set |
| `ts`, `id`      | capture time and a unique id                                |

The point is *where the anchor lives*: the customer retains it **out of band**
(a git commit, an emailed receipt, an S3 object-lock bucket, a countersignature)
where the operator cannot rewrite it. Verification then requires the live DB to
*reproduce* that exact `head_hash` and covered `event_count` at `through_rowid`.
An operator who rewrote earlier history cannot reproduce a head the customer
already holds, so the recompute that fools `plutus verify` is caught.

```bash
plutus checkpoint                      # record an anchor per org; prints JSON to retain
plutus checkpoint --org <id> --json    # a single org, machine-readable

plutus verify-checkpoints --file anchor.json   # replay a retained anchor; exit 2 on divergence
plutus verify-checkpoints --file -             # read anchors from stdin
plutus verify-checkpoints                      # fall back to DB-stored anchors (weaker; see below)
```

`verify-checkpoints` reports a per-anchor `status`: `ok`, `head_mismatch`
(history rewritten), `count_mismatch` (events added/removed below the anchor),
`missing` (the anchored row was deleted or lost its hash), `chain_broken` (the
chain does not self-verify up to the anchor), or `bad_signature` (the anchor
itself was altered). Exit is `0` when every anchor reproduces, `2` otherwise.

Signing is optional but recommended for the two-party case: with a key set, a
key-holder can confirm the retained anchor was not itself forged. Omitting
`--file` verifies the anchors stored in the same database — this catches
accidental corruption but is **weaker**, since an operator could rewrite those
rows too. The strong guarantee requires a `--file` copy held out of band.

## Migration & scope

- **Schema v6, additive.** `prev_hash`/`row_hash` are nullable and added by the
  standard column migration. Rows written before the upgrade keep `NULL` hashes;
  `verify` counts them as a `pre_chain` prefix and reports them as
  **unverifiable (pre-upgrade)** rather than back-filling a hash it can't attest.
  The chain starts fresh at the upgrade.
- **Schema v9, additive.** The `chain_checkpoints` table (#120) is created by the
  standard migration; `UNIQUE(org_id, through_rowid)` makes re-checkpointing the
  same head idempotent, so a periodic capture never errors. Existing databases
  gain the table on next open with no effect on prior rows.
- **Scope.** The chain covers `usage_events` — the source of the savings
  dollars. The `credit_ledger` (top-ups/adjusts) is a separate integrity surface
  and is not chained here.

## Guardrail

**No public document may claim Plutus is "tamper-evident" until this ships _and_
an external cryptographic review covers it** (the SOW drafted for the Perseus
Vault audit-chain review). Until then, savings statements carry the caveat the
harness/one-pager already print: the ledger is *re-queryable* and now
*hash-chained*, with the external review pending.
