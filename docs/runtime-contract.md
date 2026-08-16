# Runtime-contract enforcement

Ledger issue #250 adds a small, dependency-free runtime contract for agent
trajectories. The design follows *Agent Safety Should Be a Runtime Contract*,
arXiv:2608.11274 (especially its evidence-gated completion and compositional
gating arguments): <https://arxiv.org/abs/2608.11274>.

The contract is intentionally **additive**. It does not change Ledger's
prebind, receipt, HTTP, or OpenAPI schemas. A caller can put the trajectory root
hash in an existing `evidence_hashes` collection when it wants to bind a
submission to the observed trajectory.

## Trajectory schema

`ledger_agent.trajectory.Trajectory` represents a trajectory
`τ = (e_1, ..., e_T)`. Every event has the shape below. The serialized envelope
has `schema: "perseus-ledger-trajectory/v1"`, an `events` list, and a
calculated `head_hash`.

| Field | Type | Meaning |
| --- | --- | --- |
| `kind` | string | One of `tool_call`, `tool_result`, `file_read`, `file_write`, `shell_exec`, `commit`, `screenshot`, `citation_lookup`, `human_approval`, or `model_message`. Unknown kinds are rejected. |
| `timestamp_ms` | integer | Event time in Unix milliseconds. |
| `payload` | object | Structured event data. It is copied on append and must be JSON-serializable. |
| `prev_hash` | 64-character hex string | The preceding event hash. The first event points to `sha256("genesis")`. |
| `hash` | 64-character hex string | `sha256(canonical_json(kind, timestamp_ms, payload, prev_hash))`. |

Canonical JSON uses sorted keys, compact separators (`(',', ':')`), and UTF-8.
The event's own `hash` is excluded from the covered fields. Consequently,
changing an event makes that event fail verification and makes every unchanged
successor fail its predecessor link. This is the tamper-evident suffix
property needed by the runtime contract in arXiv:2608.11274.

```python
from ledger_agent.trajectory import Trajectory

trajectory = Trajectory()
trajectory.append("tool_call", {"name": "pytest"})
trajectory.append("tool_result", {"exit_code": 0})
assert trajectory.verify_chain() == (True, "ok")
serialized = trajectory.to_dict()
restored = Trajectory.from_dict(serialized)
assert restored.to_dict() == serialized
```

`Trajectory.from_dict` preserves event-level defects so a caller can inspect a
suspect record; `verify_chain()` is the explicit check and is fail-closed.
`Trajectory.head_hash` is the genesis hash for an empty trajectory and the last
stored event hash otherwise.

## Payload shapes used by the evidence registry

The evidence verifier registry is deterministic. Each verifier receives
`(event, property, ref_state)` and returns exactly `accept`, `reject`, or
`soft`.

| Verifier | Required property | Payload / reference state |
| --- | --- | --- |
| `test_run` | `test_suite_passes` | `payload.exit_code == 0` and `ref_state.expected_pass is True`. |
| `citation_lookup` | `citation_real` | `payload.cited_url` (also `url`/`source_url` accepted) is a member of `ref_state.source_urls`. |
| `file_diff` | `diff_present` or `diff_matches` | `payload.diff`/`diff_text`/`hunk`/`patch`; `diff_matches` requires equality with `ref_state.expected_hunk`, while `diff_present` requires a non-empty matching hunk when one is supplied. |
| `log_capture` | `log_contains` | `ref_state.marker` is a non-empty substring of `payload.log_text`. |
| `screenshot` | `screenshot_matches` (or the equivalent screenshot property) | `payload.image_sha256 == ref_state.expected_image_sha256`. |
| `human_approval` | an approval property | `payload.approved_by` is non-empty and equals `ref_state.approval_ref` when that reference is supplied. |
| `shell_exec` | an execution property | `payload.exit_code` is captured as an integer, whether zero or non-zero. Captured failure is evidence of execution, not evidence of success. |
| `commit` | a commit property | `payload.commit_sha == ref_state.expected_commit_sha`. |

An unknown or missing verifier is `soft`, never `accept`. A verifier marked
non-deterministic by its event or reference state is also `soft`. In particular,
a `model_message` that says `done` is soft evidence and cannot satisfy a
load-bearing requirement. This is the key false-completion distinction in
arXiv:2608.11274: a completion claim is not an observation that the claimed
work occurred.

## Evidence chains and the submission gate

A requirement is a small object such as:

```python
{
    "property": "test_suite_passes",
    "verifier": "test_run",
    "ref_state": {"expected_pass": True},
}
```

`find_evidence_chain(trajectory, requirements)` searches the observed events.
It returns `(found, chain_events, unmet_requirements)`. Every requirement must
have an event whose registered verifier returns hard `accept`; `reject` and
`soft` do not count. A single event may establish more than one explicitly
requested property, but the returned event list is de-duplicated.

`evaluate_submission(trajectory, requirements)` is the fail-closed contract:

```json
{
  "accepted": true,
  "decision": "accepted_with_evidence",
  "evidence_chain": ["..."],
  "unmet_requirements": []
}
```

If the trajectory is invalid or any requirement is unmet, the decision is
`rejected_missing_evidence`; it is never inferred from a terminal model
message. This operationalizes the evidence-gated completion rule discussed in
arXiv:2608.11274.

## Compositional gating proposition

`ComposedGate` combines deterministic trajectory monitors with one or more
independent evidence gates. The built-in monitors are:

* `no_shell_exec_without_prior_human_approval`: every `shell_exec` must be
  preceded by an approved `human_approval` event;
* `no_file_write_outside_allowed_paths`: every `file_write` path must be within
  a caller-provided `ref_state["allowed_paths"]` root.

The preferred API is:

```python
from ledger_agent.trajectory import ComposedGate

gate = ComposedGate(
    monitors=[{"name": "no_shell_exec_without_prior_human_approval"}],
    gates=[requirements],
)
report = gate.evaluate(trajectory)
```

The gate accepts iff every monitor holds and every evidence gate finds a full
chain. Requirement sets across evidence gates must be disjoint; overlap is
rejected rather than silently allowing interference.

**Proposition (compositional runtime enforcement, adapted from §4.3 of
arXiv:2608.11274).** Let monitors `h_1, ..., h_n` be deterministic finite
state monitors (DFAs) with disjoint observation alphabets, and let `H_1, ...,
H_m` be evidence gates over disjoint requirement sets. Their parallel
composition accepts exactly the trajectories satisfying

```
(h_1 || ... || h_n || H_1 || ... || H_m)(τ)
  = (∧ᵢ φᵢ(τ)) ∧ (∧ⱼ ηⱼ(τ))
```

where `φ_i` is the safety language of monitor `h_i` and `η_j` is a complete
hard-evidence chain for gate `H_j`. Disjoint alphabets make the monitors
non-interfering and permit product evaluation in polynomial time in the
trajectory and monitor sizes. If observations are shared, use an
assume-guarantee contract: each monitor states the events it assumes and the
properties it guarantees, and the composition must discharge the shared-event
obligations. General shared-alphabet DFA composition can require an exponential
state product. Sequential evidence-chain scans and disjoint monitor evaluation
remain polynomial; unrestricted general composition has the usual exponential
worst case. This is the runtime-contract composition boundary described in
arXiv:2608.11274.

## Preventive-face taxonomy

A runtime contract has four complementary faces, rather than treating every
control as a post-hoc audit:

1. **Preventive** — block or hold an action before its side effect (for example,
   the shell-approval and allowed-path monitors).
2. **Detective** — observe and hash what happened (the trajectory chain, logs,
   screenshots, citations, and test results).
3. **Corrective** — stop, roll back, quarantine, or require re-approval after a
   monitor violation or missing evidence.
4. **Structural** — make the safe path the natural path through schemas,
   least authority, explicit references, and cryptographic binding.

The five Saltzer-Schroeder principles adapted to this contract are:

* **Economy of mechanism:** use a small closed event vocabulary, canonical JSON,
  and simple deterministic predicates.
* **Fail-safe defaults:** absent, unknown, rejected, or soft evidence denies a
  submission; callers must prove acceptance.
* **Complete mediation:** evaluate every submission and every monitored side
  effect, not only the first event or the final model message.
* **Open design:** the schema and verifier behavior are inspectable and
  reproducible; security does not depend on hiding the implementation.
* **Separation of privilege:** require independent evidence classes and, where
  appropriate, an independent human approval instead of treating one claim as
  sufficient.

These faces and principles turn the paper's runtime-contract argument into
operational controls while preserving the distinction between an observed
fact and a model assertion (arXiv:2608.11274).

## Six evidence classes for false-completion audits

The paper's false-completion audit motivates collecting multiple evidence
classes. A deployment can require whichever classes are appropriate, but should
not silently substitute a model message for any of them:

1. **Citation grounding** — the cited URL or external source was looked up and
   is in the trusted source set.
2. **Log capture** — the expected marker is present in captured execution output.
3. **Test run** — a concrete test process returned exit code zero against the
   expected reference state.
4. **Human approval** — an identified approver authorized the relevant action.
5. **External state** — a commit, file diff, or other independently inspectable
   state matches the expected reference.
6. **Screenshot** — a captured visual state is bound by its image digest.

The registry's `test_run`, `log_capture`, `citation_lookup`,
`human_approval`, `commit`/`file_diff`, and `screenshot` verifiers implement
these classes as hard evidence where their deterministic reference checks
succeed. This layered evidence is the practical antidote to false completion
identified by arXiv:2608.11274.

## AAR / prebind integration

`trajectory_root_hash(trajectory)` computes
`sha256(trajectory.head_hash)` as a hexadecimal string. Callers may append this
value to an existing prebind or receipt `evidence_hashes` list, for example:

```python
from ledger_agent.trajectory import trajectory_root_hash

root = trajectory_root_hash(trajectory)
# Existing prebind/receipt builder, unchanged:
# evidence_hashes = prior_hashes + [root]
```

This binds the caller's existing evidence block to the observed trajectory
without changing any existing function signature or schema. The root helper is
an integration aid, not a replacement for verifying the trajectory itself.

## Demo

Run the end-to-end example with the repository's configured interpreter:

```bash
/opt/data/venv-ledger/bin/python examples/runtime_contract_demo.py
```

It prints a rejection for the evidence-less `done` claim, then appends a test
run, captured log marker, and citation lookup and prints an accepted report.
