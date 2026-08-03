# Memory governance and Ledger provenance (#199)

Perseus resolves active context. Perseus Vault owns durable memory and its
lifecycle. Perseus Ledger records what an integration supplies as
hash-covered evidence. These are complementary responsibilities, not one
shared policy engine.

This page defines the boundary for a cross-product integration. It does not
make Ledger a memory store, a retention controller, an admission service, or an
authorization engine.

## The ownership boundary

| Layer | Owns | Does not claim |
|---|---|---|
| **Perseus** | Workspace resolution, the selected recall posture, render identity, and the decision to make a context available to a caller | That a memory was durable, authoritative, or retained by merely rendering a reference |
| **Perseus Vault** | Admission decisions, workspace/visibility checks, retention and history policy, archive/purge behavior, correction and curation, and recall results | That a downstream action occurred or that an action was reported to Ledger |
| **Perseus Ledger** | The supplied event, opaque correlation references, supplied evidence digests, decision context, optional action provenance, context-render bindings, and the per-organization hash chain | Vault lifecycle enforcement, recall ranking, deletion, approval, or facts that were never supplied |

A Ledger receipt therefore answers a bounded question: *which hash-covered
claims and resource facts were supplied for this event, and does the containing
Ledger chain verify?* It cannot answer whether Vault should have retained a
record or whether every action was reported.

## The hash-only projection

A consequential action can carry a compact projection of the context and memory
state that influenced it. Values that identify a record, policy, workspace, or
artifact must be opaque references; content is represented by a full lowercase
SHA-256 digest. The fixed schema value below is not a memory body.

```json
{
  "external_ref": "ref_7c91d2a4",
  "evidence_hashes": [
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  ],
  "policy_version": "policy_4f18c0e2",
  "result_hash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "human_review": "approved",
  "correction_ref": null,
  "context_render_schema": "perseus-context-render-trace/v1",
  "context_render_hash": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "served_memory_provenance_hash": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
  "action_receipt_hash": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
}
```

The projection uses existing Ledger fields:

- `external_ref` is an opaque, tenant-scoped correlation reference. It is the
  selector used by the task-scoped Evidence Receipt.
- `evidence_hashes` is a canonical sorted, de-duplicated list of source or
  decision digests. A Vault admission `record_digest` or `decision_digest`
  may be included when the action depends on that admission result.
- `policy_version` identifies the immutable policy/configuration reference in
  effect. It is a reference, not the policy body.
- `result_hash` identifies the output artifact or conclusion without copying it
  into Ledger.
- `human_review` is the bounded value `approved`, `rejected`, or `corrected`;
  `correction_ref` is required for `corrected` and remains opaque.
- `context_render_schema` is required when any context-render binding is
  supplied. `context_render_hash` binds the rendered context, while
  `served_memory_provenance_hash` binds the hash-only explanation of which
  Vault records were served and why. `action_receipt_hash` can bind an
  upstream control-plane receipt.

Ledger validates the digest-shaped fields and hash-covers every supplied value
in the event chain. It does not dereference any digest, inspect Vault, or infer
missing fields.

## Lifecycle decisions and evidence linkage

The lifecycle remains in Vault. The integration decides which bounded
references and digests are material to a later verification, then supplies
those values to Ledger at the time of the consequential event.

| Vault/Perseus decision | Control-plane owner | Hash-only Ledger linkage | Boundary to preserve |
|---|---|---|---|
| **Admission** — accept, quarantine, suppress, escalate, abstain, or revoke a record | Vault | Include the relevant source/admission digests in `evidence_hashes`; identify the policy with `policy_version`; correlate with `external_ref` | Ledger records the supplied admission evidence. It does not re-evaluate the admission outcome or make a non-authoritative record visible. |
| **Retention** — keep current state, retain history, compact, or apply a bounded retention policy | Vault | Use an opaque policy reference in `policy_version` and, when applicable, a checkpoint or decision digest in `evidence_hashes` | Ledger does not copy Vault history, extend Vault retention, or promise that a retained Ledger event preserves a deleted memory body. |
| **Deletion** — archive with `perseus_vault_forget`, bulk archive with the curation tools, or permanently purge with `perseus_vault_purge` | Vault | For a consequential deletion, bind an action intent/approval projection and the deletion result digest; use `action_status`, `action_intent_hash`, `approval_ref`, `result_hash`, and `external_ref` as applicable | Vault owns the deletion and its authorization. A Ledger event is evidence that a supplied deletion claim was reported, not proof that Ledger deleted or can restore Vault content. |
| **Curation** — correct, supersede, consolidate, promote, demote, or run maintenance | Vault, with any required operator/control-plane approval | Bind the source-set, correction, successor, or maintenance-report digests; use `correction_ref` for a correction and `evidence_hashes` for the supporting set | Ledger does not choose the winning fact, rewrite Vault history, or turn a derived summary into an authoritative source. |
| **Recall posture** — `on_demand`, `relevant`, or an explicit `always` posture | Perseus selects the posture; Vault enforces recall/visibility invariants | Record an opaque posture/policy reference in `policy_version`, the actual `context_render_hash`, the served-memory provenance digest, and the action receipt digest | Ledger records the posture used by the caller. It does not select recall mode, rank memories, or treat a rendered context as durable memory. |

For a derived curation result, hash the final result and its supporting set
after all destination scope and policy metadata are assembled. Do not copy a
source admission envelope onto a transformed record.

## A concrete evidence flow

1. **Vault decides.** Vault validates source identity, scope, trust, time, and
   relevance, then creates or updates its own admission evidence and lifecycle
   state. Quarantined, suppressed, escalated, abstained, and revoked outcomes
   remain non-authoritative under Vault's rules.
2. **Perseus resolves.** Perseus uses the configured recall posture and
   workspace scope to ask Vault for the context needed for the current task.
   The render path can produce a versioned, hash-only trace of the served
   memory references and reasons.
3. **The action boundary binds.** The action runner keeps the raw context and
   memory bodies in their owning systems. It computes the render/result
   digests, retains opaque control-plane references, and decides whether the
   action is allowed to proceed.
4. **Ledger records.** The runner sends the normal usage event to
   `POST /v1/usage`, adding only the optional hash/reference fields supported by
   the current contract. The optional fields are trailing and preserve
   compatibility for events that do not carry them.
5. **The receipt is checked.** A caller retrieves
   `GET /api/audit?org=<org-id>&external_ref=<ref-id>`, checks the receipt
   version and every expected binding, and requires `verification.chain_ok`.
   A retained external checkpoint is stronger than an anchor stored only in
   the same operator-controlled database; see [Ledger integrity](ledger-integrity.md).

The final receipt is evidence of Ledger-recorded activity. It is not evidence
that the upstream runner reported every action or that Vault's lifecycle policy
was correct.

## Retention, deletion, and curation without overclaiming

Use these rules when designing a connector or an operator runbook:

1. **Record the policy reference, not the policy body.** A policy change should
   produce a new opaque `policy_version`. The event that used it can then be
   compared with the policy artifact held by the control plane.
2. **Record decisions at the decision boundary.** If admission, retention,
   deletion, or curation changes whether a later action can occur, emit an
   event after the control plane has decided and before the action is claimed as
   complete. A later receipt cannot reconstruct an unreported decision.
3. **Hash the supporting set.** A source-set digest, result digest, or externally
   retained checkpoint lets a verifier detect substitution without sending the
   source bytes to Ledger.
4. **Keep erasure semantics explicit.** Vault may permanently purge a body or
   history. Keep only the references and digests that the applicable retention
   policy permits. A surviving Ledger row proves the supplied event, not the
   erased bytes.
5. **Treat degraded recall as a different posture.** Local fallback, an empty
   result, an unavailable Vault process, and a healthy Vault with no matches are
   different states. If a consequential action requires durable Vault recall,
   hold or abstain when the required integration is unavailable; do not let a
   generic empty result become a provenance claim.
6. **Verify before making a claim.** `chain_ok` proves the Ledger chain it
   verifies. It does not validate a digest against an external artifact, so the
   verifier must also resolve the permitted external reference and recompute
   the expected digest in the owning system.

## Privacy boundary

The cross-product projection MUST NOT contain credentials, raw prompts, raw
memory bodies, raw tool arguments, or raw action results. It may contain only:

- bounded status/enum values;
- opaque IDs and correlation references; and
- full SHA-256 digests of source, context, result, policy, checkpoint, or
  control-plane artifacts.

If a value cannot be safely represented as an opaque reference or digest, keep
it in Vault or in the owning control plane and do not send it to Ledger.

## Related contracts

- [Evidence Receipts](evidence-receipts.md) — task-scoped receipt shape and
  hash-covered decision context.
- [Authorized Action Receipts](authorized-action-receipts.md) — Vault-owned
  authority and approval boundary with Ledger-side provenance.
- [Local Perseus + Vault + Ledger integration](local-perseus-vault-ledger.md) —
  copy-pasteable local wiring and degraded-state checks.
