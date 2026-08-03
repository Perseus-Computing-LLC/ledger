# Continuous Attestation: Admission vs. Runtime Evidence

> **Perseus resolves. Vault remembers. Ledger proves.** This document turns the
> "prove it later" promise into a continuous one: admission is not a one-time event;
> compliance is a sequence of recorded claims over monitorable behavior.

Related: [Evidence Receipts](evidence-receipts.md) · [Ledger Integrity](ledger-integrity.md) ·
[Schema](schema.md) · [Three-Tier Model](three-tier-model.md)

## 1. The problem

Ledger records *what happened*. That is necessary but not sufficient for governed
autonomous systems: a component that passed admission can later drift, degrade, or be
attacked (stale evidence, superseded policy, dependency drift, adversarial inputs). The
research literature converges on the same answer (Protocol-Driven Development,
arXiv:2605.12981; Proof of Execution, arXiv:2607.05397): **compliance is continuously
attested**, not once admitted. The protocol (contract) remains the durable governing
object; evidence continues to accumulate after deployment.

## 2. Admission vs. continuous attestation

| | Admission (build/decision time) | Continuous attestation (runtime) |
|---|---|---|
| Question | "Is this artifact/action admissible under the contract?" | "Does the deployed behavior still satisfy the monitorable projection of the contract?" |
| Evidence | Signed Evidence Chain: protocol, implementation hash, validator outputs, attestation | Append-only ledger blocks: observations, invariant checks, violations, remediation |
| Failure | No acceptance evidence is produced | Violation block is recorded and becomes structured repair context |
| Authority | Validator decides | Runtime Verification Layer enforces *outside* the artifact (block, quarantine, rate-limit, roll back) |

The static/dynamic distinction is a *boundary condition*, not a defect: build-time evidence
proves compliance relative to preserved validator inputs, validator assumptions, and the
protocol observation model. It is not a perpetual guarantee of future executions.

## 3. The attestation block

Each runtime attestation interval produces one signed, hash-chained evidence block:

```text
E_t = H(E_{t-1}, P, Iv, Rt, At, t)
```

| Field | Meaning | Ledger mapping |
|---|---|---|
| `E_{t-1}` | previous evidence block | `prev_hash` of the prior event |
| `P` | governing protocol/contract | `policy_version` (hash-covered) |
| `Iv` | deployed implementation version | `model_config` / artifact identity |
| `Rt` | runtime observations over interval t | `evidence_hashes` + event facts |
| `At` | attestation decision | the event's action/result + `human_review` |
| `t` | interval identifier | event timestamp/sequence |

The ledger evolves append-only: `L_t = L_{t-1} ∥ E_t`. The deployed instance is
time-indexed by implementation and evidence state: `D_t = (P, Iv, L_t)` — the protocol is
the durable specification while operational evidence evolves.

## 4. Mechanical vs. reasoning provenance

The evidence model separates two primitives (Reasoning Provenance, arXiv:2603.21692):

1. **Mechanical provenance** — what happened: events, hashes, traces, causal lineage.
   This is Ledger's current model (event rows, `prev_hash`/`row_hash` chains).
2. **Structured reasoning provenance** — *why* each action was chosen, what each
   observation concluded, how conclusions shaped strategy, and which evidence supports
   the final verdict. This is a schema-level primitive for population analytics, not an
   optional comment field.

Clients should send both where available: mechanical facts through the existing ingest
contract, and reasoning provenance as hash-covered evidence references (rationale
digests, evidence-support links) — never raw rationale text. Ledger canonicalizes
digests, never payload content.

## 5. Typed attestations with replayable verify

Attestation records carry a claim type so verification is deterministic (Pramāṇa,
arXiv:2605.20312):

- **measurement** — an observed fact; verify against the recorded observation digest.
- **inference** — a derived conclusion; verify against its inputs and the derivation rule.
- **analogy** — a mapping claim; verify against the referenced source.
- **citation** — an attribution; verify against the cited artifact digest.

Each typed claim is linked to a source digest and a deterministic or replayable
`verify()` operation. The ledger records audit-complete emission/suppression/disclosure
semantics: if a claim is emitted, its verification inputs must be reproducible from the
preserved record.

## 6. Independent auditability

Third-party verification is a first-class goal (Context Lineage, arXiv:2509.18415; Aegon,
arXiv:2604.06693):

- Domain-separated event leaves with signed tree heads/checkpoints (Ledger already
  supports external `chain_checkpoints` — see `ledger-integrity.md`).
- Inclusion and consistency proofs so an auditor can verify "this event was recorded"
  and "nothing was retroactively modified" without trusting the operator.
- A proof-server API surface for independent queries.

The existing `/api/audit` verification block (`chain_ok`, `verified_events`, method) is the
starting point; continuous attestation extends it from one-time verification to
periodic, interval-indexed attestation queries.

## 7. Wiring: violation → repair context → re-admission

Runtime attestation failure should close the governance loop (PDD remediation
orchestrator):

1. A violation block is appended (signed, hash-chained).
2. The violation identifies the violated contract clause, relevant telemetry, and
   environment metadata (as hash references).
3. The block becomes structured **repair context** for the next generation/admission
   attempt.
4. Any proposed fix re-enters validation before replacement — admission remains
   necessary, deployed behavior is continuously measured against the monitorable
   projection.

## 8. What this document does *not* claim

- Ledger does not decide policy; it records what happened under which authority and
  evidence. Continuous attestation records attestation *decisions* made by the governing
  controller.
- Attestation covers the **monitorable projection** of the protocol — the observations
  available to the runtime verifier — not an unobservable guarantee about the world.
- "Replayable" is not "reproducible in the world": replay under captured inputs and
  declared dependencies does not guarantee external sources match at replay time.

## References

- Protocol-Driven Development — arXiv:2605.12981 (Dynamic Evidence Ledger; monitorable
  runtime projection; remediation orchestrator)
- Proof of Execution — arXiv:2607.05397 (execution = (C, T, R); validator invariants;
  replay envelope)
- Reasoning Provenance for Autonomous AI Agents — arXiv:2603.21692
- Pramāṇa: Protocol-Layer Claim Verification — arXiv:2605.20312
- Context Lineage Assurance (CT-style Merkle logs) — arXiv:2509.18415
- Aegon: Ledger-Bound Tokens + CT-Style Audit — arXiv:2604.06693
- Sovereign Execution Broker — arXiv:2606.20520 (certificate-bound authority, revocation
  epochs, live-state drift)
