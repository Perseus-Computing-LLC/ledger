# CVA Contract Specification

Status: implementation slice
Date: 2026-08-16
Resolves: ledger#252 · Consumed by: `ledger_agent/cva.py`, AAR prebind receipts
Related: [Authorized Action Receipts](authorized-action-receipts.md), arXiv [2607.21325](https://arxiv.org/abs/2607.21325), [2605.20704](https://arxiv.org/abs/2605.20704), [2604.07695](https://arxiv.org/abs/2604.07695)

## Scope and non-ZK interpretation

The CVA paper defines authorization as request-bound evidence, not merely
authentication or delegation. Ledger implements the formal shape from
[2607.21325](https://arxiv.org/abs/2607.21325) as a deterministic, hash-bound
contract. It does **not** claim that a Python predicate is a SNARK, that private
attributes are hidden, or that authorization proves runtime execution.

The public statement is `x = (id_i, h_q, h_c, pid_j, n, t)` (paper Eq. 16):

```json
{
  "schema": "perseus-ledger-cva-statement/v1",
  "agent_id": "agent-a",
  "request_hash": "<sha256(canonical request_payload)>",
  "context_hash": "<sha256(canonical context_payload)>",
  "policy_id": "policy/v1",
  "nonce": "n1",
  "timestamp_ms": 100,
  "statement_hash": "<sha256(all fields above)>"
}
```

The Ledger witness is supplied to the verifier as `{principal_key_id,
key_registry, request_payload, context_payload, attrs, policy}`. The relation
is the paper's Eq. 22–24, evaluated as:

```text
R_CVA = BindPrincipal ∧ BindRequest ∧ BindContext ∧ SatisfyPolicy
```

* **BindPrincipal (Eq. 23):** the normalized key registry contains the selected
  key, its `agent_id`/`agent_binding` equals `statement.agent_id`, and the key
  is not revoked. In an AAR, this is `actor_ref` plus the key-registry custody
  label; custody discloses provenance and is not a proof of authority.
* **BindRequest (Eq. 19, property Eq. 30):** the SHA-256 of canonical
  `request_payload` equals `request_hash`. AAR carries the same `request_hash`.
* **BindContext (Eq. 20, property Eqs. 32–36):** the SHA-256 of canonical
  `context_payload` equals `context_hash`; AAR maps this to
  `selected_context_digest` and the optional `context_hash`.
* **SatisfyPolicy (Eq. 21):** the caller-supplied deterministic predicate
  returns the literal `True` for `(attrs, request_payload, context_payload)`.
  `policy_id` is committed in the statement; a predicate may advertise a
  matching `policy_id` for cross-policy rejection. `policy_version` and
  `policy_hash` are the AAR policy projection.

`build_cva_statement` and `build_prebind_v2` use canonical JSON
(`sort_keys=True`, `separators=(',', ':')`) and SHA-256. Relation failures are
reported by conjunct (`bind_principal`, `bind_request`, `bind_context`,
`satisfy_policy`) without short-circuiting.

## Replay contract

The gateway keeps mutable `consumed_nonces` outside the stateless relation.
`is_fresh(n, t, N)` is `n ∉ N ∧ t_min ≤ t ≤ t_max` (paper Eqs. 26,
37–40). `CvaGateway.accept` checks replay, timestamp, and the relation, then
adds the nonce only after full acceptance. A failed relation therefore cannot
burn a nonce. The gateway nonce set is a trusted component, as in the paper's
partially trusted gateway model; it must be durable/serialized correctly when
multiple gateways share an authorization domain. [2605.20704](https://arxiv.org/abs/2605.20704)
provides a related freshness/revocation perspective for agent credentials.

## CVA property matrix

| Property / paper definition | Attack class defeated | Ledger mechanism | Acceptance criterion | Covering test |
|---|---|---|---|---|
| Authorization soundness: no accepted proof without a valid witness, Eqs. 27–28 | Forged or invalid-witness authorization | Hash-bound statement plus all four fail-closed conjuncts | No relation acceptance when any binding/policy check fails | `test_authorization_soundness_rejects_tampered_request_payload` |
| Principal binding: a proof for `id_i` does not verify for `id_k`, Eq. 29 | Cross-principal transfer | Active registry key's agent binding equals `agent_id`; revocation is rejected | Agent B's key cannot satisfy Agent A's statement | `test_principal_binding_rejects_key_bound_to_another_agent` |
| Request binding: `q_i != q'_i` cannot transfer evidence, Eq. 30 | Cross-request transfer | Canonical request hash in CVA statement and AAR `request_hash` | Changed request yields `bind_request` | `test_request_binding_rejects_different_request_with_same_context` |
| Policy binding: `pid_j != pid_k` cannot transfer evidence, Eq. 31 | Cross-policy transfer | `policy_id` is statement-covered; policy predicate/identifier must match | Flipped predicate or advertised policy ID is rejected | `test_policy_binding_rejects_flipped_predicate_and_policy_identifier` |
| Context binding: distinct context commitments do not verify, Eqs. 32–36 | Context substitution at authorization | Canonical context hash plus AAR context projections | Changed context yields `bind_context` | `test_context_binding_rejects_changed_context_payload` |
| Replay resistance: consumed nonce or out-of-window time rejects, Eqs. 37–40 | Proof/nonce reuse and deferred presentation | Gateway nonce set and inclusive timestamp window | First accept succeeds; replay, stale, and future presentations reject | `test_replay_resistance_consumes_nonce_once`; `test_timestamp_window_rejects_stale_and_future_statements` |

The authority-trace section additionally exercises old-key revocation, stale
context, and a fresh post-rotation witness. This complements continuous
delegation/revocation work such as [2604.07695](https://arxiv.org/abs/2604.07695)
without treating that protocol as Ledger's implementation.

## Structural separation and explicit limits

The paper's central open problem is that the following are distinct security
layers (Eq. 52):

```text
Identity Binding ≢ Authorization-Request Binding ≢ Runtime-Execution Binding
```

1. **Identity binding** establishes which principal a key/credential names.
   Ledger's registry binding and AAR `actor_ref` cover this narrow seam.
2. **Authorization-request binding** establishes what request and policy were
   accepted under what context. The AAR prebind is this authorization-request
   boundary: it is proposed/approved evidence before execution, not execution.
3. **Runtime-execution binding** establishes that the exact authorized request
   was the request actually executed. Context drift between verification and
   execution is the TOCTOU gap (`c_tv != c_te`; paper Eqs. 41–43 and 53–54).
   Execution receipts and trajectory evidence must close that seam separately.

A request commitment is not an agent's deliberative state: `Request
Commitment ≠ Internal Agent Intent` (paper Eq. 25). A valid hash proves only
that the committed bytes were presented; it says nothing about hidden chain of
thought, motivation, or normative policy correctness. The receipt path also
does not provide selective disclosure or post-quantum security.

## Falsifiable research agenda

| Hypothesis | Falsification condition | Experiment sketch | Status |
|---|---|---|---|
| **H1 — three-binding separation is implementable.** Distinct Ledger layers can compose prebind → execution receipt → trajectory evidence without conflating claims. | A controlled TOCTOU or substituted-runtime-request case passes despite distinct commitments, or one layer cannot be independently verified. | Generate paired authorized/executed requests, mutate context/action between layers, verify each digest and chain, and measure detection by layer. | Implemented as a contract boundary; empirical end-to-end experiment pending. |
| **H2 — replay resistance survives key rotation without trusted replay sets in the receipt path.** | A rotated/revoked key or previously accepted nonce is accepted, or a failed relation consumes a nonce. | Run concurrent old/new-key windows with duplicate, stale, future, and failed-relation deliveries. Keep the gateway nonce set as the explicit trusted state; receipts carry commitments but do not replace it. | Local sequential tests pass; distributed-state and concurrency falsification pending. |
| **H3 — receipts-first fits interactive latency better than per-request ZK proofs.** | Under a matched workload and threat model, receipts plus deterministic verification miss the latency budget or lose a required confidentiality guarantee relative to ZK. | Benchmark canonical hashing, registry/policy checks, and chain verification against a Groth16 lane at equal request frequency and policy coverage. The paper's framing reports roughly `zkLLM ~180 s/query` versus receipts `<20 ms`; reproduce rather than generalize those numbers. | Position/hypothesis, not a Ledger benchmark. |

The stance is **receipts-first, proofs-where-required**: use the cheap,
inspectable AAR/chain path for ordinary authorization evidence, then add a
proof lane where confidential attributes or disclosure minimization is a real
requirement. The comparison must preserve the paper's warning that proof
latency depends on circuit complexity and authorization frequency
([2607.21325](https://arxiv.org/abs/2607.21325)).

## Selective disclosure / zk-PoC feasibility lane

When attributes must remain confidential, a prover can hold `(sk, attrs,
request_payload, context_payload)` and produce a Groth16 proof over a circuit
that exposes `agent_id`, request/context commitments, `policy_id`, nonce, and
time as public inputs. The gateway verifies `(statement, proof)` and still
performs stateful freshness checks; the verifier does not receive raw `attrs`.
Ledger can retain the statement hash, proof reference/digest, verification-key
identifier, and AAR linkage without storing witness material.

This lane has material caveats. Groth16 requires a circuit-specific trusted
setup/CRS and is not post-quantum secure; policy changes may require circuit
and key governance, and dynamic procedural policies are difficult to encode.
The gateway remains partially trusted for replay state. Context binding and
runtime-execution binding must still be tested outside the circuit. Those
constraints are consistent with the paper's PoC limits and with adjacent
credential/revocation and delegation work ([2605.20704](https://arxiv.org/abs/2605.20704),
[2604.07695](https://arxiv.org/abs/2604.07695)).

## Implementation slice

- `ledger_agent/cva.py` implements the statement, relation, freshness gateway,
  and `PROPERTIES` matrix for arXiv:2607.21325.
- `ledger_agent/receipts.py` adds optional `request_hash`, `nonce`, and `epoch`
  fields to v2 prebind blocks; `prebind_hash` covers them.
- `ledger_agent/prebind.py` validates those fields while accepting old v1/v2
  blocks that omit them.
- `tests/test_cva.py` and the authority-trace v2 section exercise each attack
  class, round trips, tamper rejection, and key rotation.
