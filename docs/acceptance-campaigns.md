# Acceptance Campaigns and Benchmark Guard Contract

Status: implemented contract
Date: 2026-08-17
Resolves: ledger#256 · ledger#257
Related: `docs/evidence-receipts.md` (#235), `docs/authorized-action-receipts.md` (#197), `docs/schema.md`

## Overview

Ledger usage receipts describe individual actions. An acceptance campaign describes
whether a multi-check runner completed with intact evidence and what the tested
target did. These are independent facts: a healthy framework can record a real
target failure, while a broken or evidence-losing framework cannot produce a
verified pass.

Benchmark campaigns also carry an economic and continuation boundary. Planned
cells, provider/model lanes, configuration and fixture commitments, spend
limits, checkpoint lineage, and correction attempts are recorded as hash-only
projections. Individual usage receipts remain the accounting and tamper-evident
source of spend.

Ledger records supplied evidence. It does not certify a target, authorize paid
work, or infer facts that a runner did not send.

## Compact public-safe example

The following projection contains identifiers, statuses, counts, costs, and
commitments only:

```json
{
  "campaign_id": "campaign:recon-1061",
  "framework_status": "completed",
  "target_status": "fail",
  "budget_status": "within_guard",
  "evidence_status": "complete",
  "counts": {"planned": 4, "executed": 4, "passed": 3, "failed": 1, "skipped": 0},
  "spent_micros": 184200,
  "manifest_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "receipt_hash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
}
```

## Versioned public objects

The schemas are implemented in `ledger_agent/campaigns.py`:

| Object | Schema | Purpose |
|---|---|---|
| Manifest | `perseus-ledger-acceptance-campaign/v1` | Immutable plan and commitments |
| Check | `perseus-ledger-acceptance-check/v1` | One cell result/skip/error |
| Binding | `perseus-ledger-campaign-binding/v1` | Usage-to-cell attribution |
| Receipt | `perseus-ledger-acceptance-receipt/v1` | Final campaign envelope |

Every object has a canonical SHA-256 digest. Unknown fields and forbidden raw
material are rejected. Forbidden material includes prompts, memory bodies,
provider payloads, credentials, authorization values, and tool arguments.

## Independent status axes

`framework_status` is one of `completed`, `error`, `interrupted`, or `cancelled`.
`target_status` is one of `pass`, `fail`, `inconclusive`, or `not_run`.

The required interpretation is:

- `completed` + `fail` is a valid completed failure campaign.
- A framework error before any check produces `target_status=not_run`.
- A completed campaign with no executed checks is `inconclusive`, never pass.
- A check-level provider/schema error is retained as an explicit failed evidence
  path; it cannot become a verified pass.

`budget_status` is `not_configured`, `within_guard`, `stopped`, or `overrun`.
`evidence_status` is `pending`, `complete`, `incomplete`, or `invalid`.
`finalization_status` is `pending`, `complete`, or `failed`.

`verification.verified_pass` is true only when the receipt digest, manifest,
checks, framework, target, budget, evidence, and finalization all verify. A
valid receipt with `target_status=fail` remains useful evidence but has
`verified_pass=false`.

## Manifest and check commitments

A manifest binds:

- unique planned cell IDs and provider/model lane labels;
- configuration and fixture SHA-256 commitments;
- optional target commit/build/runtime identity;
- expected spend range, integer-micro-dollar hard stop, and runaway guard;
- retry policy and whether continuation is allowed;
- optional action-intent commitment and whether evidence is required.

A check binds its cell, lane, configuration, status, result/evidence hashes,
usage-event IDs, checkpoint reference, and attempt lineage. A second attempt
requires `continuation=true`, the immediately prior attempt, a new configuration
commitment, and a new action-intent commitment. Completed cells cannot be silently
replayed.

## Spend and durable state

Campaign manifests, checks, and final receipts are stored in the additive
`acceptance_campaigns` and `acceptance_checks` SQLite tables. A campaign-bound
usage event stores nullable `campaign_id` and a canonical
`campaign_binding_json`/`campaign_binding_hash`; those fields extend the existing
per-organization usage hash chain only when present, preserving historical
canonical bytes.

Budget admission is integer-micro-dollar arithmetic. The spend read and event
insert run under `db.immediate()`. A proposed event crossing the runaway guard or
hard stop is rejected before insertion. The campaign is durably marked
`budget_status=stopped` with a bounded reason code and remaining guard. No
partial overrun usage event is accepted.

The normal lifecycle is:

1. `POST /v1/campaigns` stores or idempotently replays the manifest.
2. `POST /v1/usage` records bound usage events.
3. `POST /v1/campaigns/checks` stores immutable cell results.
4. `POST /v1/campaigns/finalize` stores the final receipt.
5. `GET /v1/campaigns?campaign_id=...` returns the public projection.

The local SDK and MCP `ledger_record` accept the same optional binding. Existing
usage calls without a binding retain their prior behavior.

## Verification and non-goals

`ledger_agent.server.api.campaign_json` rehydrates the manifest, checks, receipt,
spend count, and independent verification after restart. `/api/audit` accepts a
campaign selector, while an event-scoped audit includes the hash-only campaign
binding when present.

This contract does not:

- certify the target system or its external environment;
- authorize provider calls, paid benchmarks, or AAR actions;
- store prompts, memory bodies, provider responses, or secrets;
- replace individual action receipts or the existing usage hash chain;
- make a failed target result positive because the framework completed.

The complete regression battery covers clean completion, target failure,
all-skipped/inconclusive, not-run, interruption, cancellation, failed
finalization, budget stops, malformed bindings, duplicate cells, correction
lineage, restart/readback, HTTP, SDK, MCP, and legacy receipt compatibility.
