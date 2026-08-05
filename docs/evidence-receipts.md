# Perseus Evidence Receipts

For the cross-product mapping from memory lifecycle and recall posture to these
hash-only fields, see [Memory governance and Ledger provenance](memory-governance-provenance.md).
For a local Perseus + Vault + Ledger walkthrough, see
[the local integration guide](local-perseus-vault-ledger.md).

An evidence receipt is a task-scoped, machine-readable view of hash-chained Ledger events. It answers a bounded question:

> For this externally identified task or artifact, which recorded autonomous-system actions exist, what resource allocation accompanied them, and does their containing organization ledger verify?

It is not a claim that the Ledger captured information a client did not send. The first receipt version exposes the immutable event facts already recorded by the compatible usage-ingest contract.

## Request

```text
GET /api/audit?org=<organization-id>&external_ref=<task-or-artifact-id>
```

The endpoint uses the same organization authorization gate as `/api/audit`. The `external_ref` selector is tenant-scoped and is hash-covered on every event that supplies it.

## Response shape

```json
{
  "receipt_version": "perseus-evidence-receipt/v1",
  "organization": {"id": "org_…", "name": "Example"},
  "external_ref": "artifact-42",
  "events": [
    {
      "event_id": "evt_…",
      "ts": 1760000000.0,
      "actor": "usr_…",
      "action": "artifact_review",
      "model_config": {"provider": "openai", "model": "gpt-5.6-terra"},
      "external_ref": "artifact-42",
      "resource_allocation": {
        "input_tokens": 1200,
        "output_tokens": 300,
        "cost_usd": 0.02,
        "estimated": false
      },
      "prev_hash": "…",
      "row_hash": "…"
    }
  ],
  "verification": {
    "chain_ok": true,
    "verified_events": 18,
    "pre_chain_events": 0,
    "unverifiable_events": 0,
    "coverage": {
      "total": 18,
      "verified": 18,
      "unverifiable": 0,
      "status": "complete"
    },
    "method": "sha256",
    "hash_method": "sha256"
  }
}
```

Events are returned in ledger insertion order so each event's `prev_hash` can be compared directly to the prior event's `row_hash` when both belong to the receipt.

## Decision context fields

New events may carry the following optional, hash-covered fields through `POST /v1/usage`:

- `evidence_hashes`: SHA-256 digests for source artifacts; Ledger canonicalizes this as a sorted, de-duplicated list.
- `policy_version`: the policy/configuration identifier in effect for the action.
- `result_hash`: SHA-256 digest of the output artifact or conclusion.
- `human_review`: `approved`, `rejected`, or `corrected`.
- `correction_ref`: opaque correction identifier, required for `corrected` events.
- `action_authorization`: optional authority-manifest reference, trusted scope
  anchor, intent digest, lifecycle status, agent identity, and approval reference
  for an Authorized Action Receipt. When action provenance is supplied, `agent_id`,
  manifest reference, scope anchor, intent digest, and lifecycle status are
  required together; `approval_ref` is required only for `approved`, `denied`,
  and `expired` statuses.

These fields appear in `evidence` and `decision_context` on receipt events. They are optional and trailing in the canonical event form, so they do not alter verification of historical records.

## Interpretation and limits

- `chain_ok` verifies the organization event chain, not only the selected receipt rows.
- `pre_chain_events`/`unverifiable_events` report a leading legacy prefix that
  predates hash chaining. Such a receipt can be chain-intact while its coverage
  is `partial`; `method` identifies the actual SHA-256 or HMAC-SHA256 verifier.
- A task receipt may contain a subset of an organization chain. Its first event can legitimately point to a predecessor outside the selected task.
- `external_ref` is an opaque client-provided correlation identifier. A caller should use a stable artifact or task ID, never a secret.
- A receipt is evidence of Ledger-recorded activity and allocation. It does not establish that every real-world action was reported to Ledger.
- External retention of a checkpoint remains the defense against an operator recomputing a complete chain. See [ledger integrity](ledger-integrity.md).

## Compatibility

No ingest route, package name, state path, API key, or existing event field changed. Clients attach an `external_ref` to `POST /v1/usage` events, then retrieve a receipt through the additive selector above.
