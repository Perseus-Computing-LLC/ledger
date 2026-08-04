# Two-layer integrity: dataplane storage root and authority-layer receipts

Status: spec
Date: 2026-08-04
Resolves: #206
Related: [ledger-integrity](ledger-integrity.md) (dataplane) ·
[authorized-action-receipts](authorized-action-receipts.md) (authority layer) ·
[evidence-receipts](evidence-receipts.md) (task-scoped receipt view) ·
[continuous-attestation](continuous-attestation.md) (#201 semantics)

Ledger separates two guarantees that are enforced by different mechanisms.
They are composable, verifiable independently, and **neither implies the
other**. This is the same split the ecosystem documents elsewhere: a dataplane
layer seals security-relevant events into a hash root, and a hosting/authority
layer signs receipts. Storage integrity and authority attestation are distinct
claims; mixing them weakens the audit story because one failing (or one
passing) says nothing about the other.

## Layer 1 — dataplane: storage integrity over the append-only event store

The dataplane layer proves the store did not silently change. It is
implemented by the per-org event hash chain and out-of-band checkpoints
(`docs/ledger-integrity.md`):

- every `usage_events` row carries `prev_hash`/`row_hash` over canonical,
  column-tagged immutable fields; SHA-256 by default, HMAC-SHA256 when a key
  is set;
- verification (`plutus verify`) replays the chain from genesis and fails at
  the first divergence — exit 0 = intact, exit 2 = tampered;
- retained checkpoints (`plutus checkpoint` / `verify-checkpoints`) pin a
  chain head out of band so a rewritten history cannot be re-chained into
  agreement with a head someone else already holds.

The chain head (or a Merkle-style root over it) is a **storage-root claim
only**: it answers "did the store change?" and nothing else.

## Layer 2 — authority layer: receipts signed/attested by the control plane

The authority layer proves who authorized what, under which evidence. It is
implemented by Authorized Action Receipts (AAR, `docs/authorized-action-receipts.md`):

- a receipt binds actor, boundary (org/workspace/scope), evidence references,
  action, and result;
- Vault is the authoritative control plane; Ledger commits only opaque,
  validated references and hashes — never raw prompts, secrets, tool output,
  or policy bodies;
- the receipt's authority bindings are checkable against the control plane's
  manifests and approval state independently of any storage root.

A receipt is an **authority claim only**: it answers "who authorized what"
and nothing about whether the store has since changed.

## Independence

Each layer has its own verifier, and neither verifier replays the other:

| Claim to check | Verifier | Does not require |
|---|---|---|
| Storage did not change | `plutus verify` / `verify-checkpoints` (chain + retained heads) | Replaying receipt signatures; any receipt at all |
| Receipt is genuine and authorized | AAR authority checks (manifest state, approval state, opaque reference validity) | Replaying the storage chain; the chain root |

The two verifiers may run in any order, on different machines, at different
times. A receipt can be verified long after the storage root has rotated
away, and the storage root can be verified with no authority material
present.

## Failure cases are distinguishable

| Case | Dataplane verdict | Authority verdict | Distinguishable outcome |
|---|---|---|---|
| Tampered store, valid receipts | **broken** — chain/checkpoint divergence (`plutus verify` exit 2) | **intact** — receipts still verify against manifests | Storage compromised; authority trail intact. Do not conflate "receipts check out" with "store intact". |
| Valid store, revoked/absent signature | **intact** — chain verifies | **failed** — signature missing or manifest revoked | Store intact; authority compromised/expired. Do not conflate "chain verifies" with "action was authorized". |
| Both intact | intact | intact | The only case where "what happened" and "it was authorized" both hold. |
| Both failed | broken | failed | Either layer independently broken; both must be remediated. |

The receipt record keeps the two verdicts separate: `verify` output never
silently "passes" a receipt-bearing org whose chain is broken, and a receipt's
authority fields are never inferred from chain position.

## Composition

Where both layers apply to the same event, they compose in the obvious way:
the dataplane root proves the event is the event, the authority receipt proves
the event was authorized. Neither claim upgrades the other — in particular, a
valid signature does not repair a broken chain, and an intact chain does not
authorize an unsigned event.

## Non-goals

- No new cryptographic primitive or schema change is required by this spec;
  it canonizes the existing split (chain/checkpoints vs. AAR receipts).
- The two-layer model is not a claim that every recorded event needs an
  authority receipt; receipts attach where authority applies (authorized
  actions), while the dataplane layer covers the whole append-only stream.
