# CUI-safe context release decisions

`ledger_agent.context_release` is a pure, hash-only evidence contract for
separating internal agent visibility from external publication. It does not
classify legal CUI, make an ATO/RMF/CMMC decision, authorize a customer, or
transport content. Vault remains the policy/projection authority; Ledger binds
the decision to evidence that a caller supplies.

## Decision contract

`build_context_release_decision()` binds:

- opaque source and safe-projection references plus their SHA-256 digests;
- a redaction/certification receipt reference and digest;
- policy version/digest and authority reference/digest/state;
- workspace/program scope and the exact destination scope/audience class;
- requester, separate approver, capability, purpose, decision/expiry times;
- handling profile, classifier state, redaction state, evidence state, and
  decision state;
- released-artifact digest, idempotency key, publication revision, prior
  decision hash, and optional OSCAL evidence references.

The canonical hash is SHA-256 over sorted-key, compact JSON with
`decision_hash` excluded. References, identities, scopes, capabilities,
purposes, and idempotency keys are bounded opaque labels/codes rather than
free-form payload fields; whitespace-bearing purpose text and recursively
named raw fields are rejected. Unknown fields and recursively detected raw
payload fields are rejected. A changed projection, policy, authority,
redaction receipt, or released artifact therefore cannot reuse an earlier
decision hash.

Timestamps are normalized to UTC and compared as parsed instants, including
fractional seconds; admission does not use lexical ordering for expiry.

The reader accepts older v1 records that omitted optional fields by verifying
the hash over the exact older field set, then reporting the missing fields and
adding read-time defaults. The normalized record remains structurally
revalidatable against its legacy hash basis. Read compatibility never upgrades
authorization: publication admission still requires fresh evidence and explicit
external-safe references.

## Fail-closed admission

`evaluate_publication()` returns a structured decision and never treats
unknown or absent evidence as permission.

Internal visibility may use `APPROVED_INTERNAL`. External publication requires
all of the following:

- `APPROVED_EXTERNAL`;
- `PUBLIC_SAFE` handling profile;
- available classifier and complete redaction;
- fresh evidence and a present redaction receipt;
- active authority and exact scope/destination matches;
- `not-revoked` revocation state;
- requester/approver separation;
- unexpired approval;
- released-artifact digest and at least one OSCAL evidence reference.

Missing, partial, stale, unavailable, incomplete, unknown, superseded, or
 tampered evidence blocks publication. Scope mismatch, expiry, revocation,
 classifier failure, redaction failure, and a changed decision hash have
 distinct blocking reason codes.

## Outbox and tombstone lifecycle

An approved external decision returns `OUTBOX_PENDING` and requires a durable
`build_outbox_receipt()` result bound to the decision hash, projection digest,
released-artifact digest, destination, and transport receipt digest. Ledger does
not implement the transport.

`build_publication_tombstone()` creates a content-free, hash-bound tombstone
for expiry, revocation, supersession, scope withdrawal, tamper, or
administrative withdrawal. Every direct outbox write must receive a tombstone
snapshot bounded to 256 entries; omission or overflow fails closed. Admission
checks the tombstone hash and blocks both
the original decision and any later decision that reuses the same source /
projection / destination tuple. A new safe projection must carry a new digest,
revision, and prior-decision lineage.

`check_idempotent_retry()` permits an exact same-hash retry but rejects reuse
of an idempotency key with a changed payload. `check_publication_order()`
requires contiguous revisions and the exact previous decision hash, blocking
out-of-order publication.

## OSCAL linkage

Issue #259's deterministic OSCAL Assessment Results and POA&M exports can be
referenced through `oscal_evidence_refs` as up to 32 opaque references plus
export hashes. Ledger records the linkage; it does not claim that an OSCAL
artifact is an ATO, an AO approval, RMF completion, CMMC certification, or legal
handling determination.
