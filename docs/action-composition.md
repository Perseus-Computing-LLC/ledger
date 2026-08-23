# Action-composition admission

Ledger v24 provides a durable, fail-closed contract for admitting a proposed
sequence of trusted actions before an external effect runs. It is a composition
layer, not a replacement for the Perseus Vault authority plane, authorized
action-receipt (AAR) lifecycle, or the backend/tool executor.

## Trust boundary

The embedding authority plane constructs a `TrustedActionRegistry` and a
`CompositionPolicy`. Request bodies can name an action and provide its arguments,
but cannot provide or override the taxonomy, action profile, classification,
impact, cost, risk, policy, or policy version. Unknown tools, aliases, malformed
arguments, ambiguous resource forms, and non-finite values produce an explicit
`review`/`hold` verdict; they never become an allow.

The registry's canonical action profile is the source of truth for:

- canonical tool endpoint and action class;
- normalized resource and allowed argument names;
- data classification, impact, and budget cost; and
- the profile digest committed into each verdict.

`authority_action_id`, `authority_ref`, `workspace_scope`, and
`context_head_digest` are opaque, hash-bound references. Their meaning and
lifecycle remain owned by the authority/AAR and context systems.

## Admission lifecycle

1. A trusted authority issues a signed lineage authorization.
2. `CompositionEngine.start_lineage()` verifies it and creates one durable
   task-lineage state with the policy/taxonomy versions and context head.
3. The caller resolves a candidate through `CompositionEngine.admit()` before
   executing it. The engine serializes the read/check/transition under
   `BEGIN IMMEDIATE`.
4. Only an `allow` verdict may be passed to the effect executor. The verdict is
   idempotent when retried with the same `idempotency_key` and action digest.
5. The caller passes that verdict to `Meter.track(...,
   composition_verdict=verdict)` and, where applicable, includes
   `composition_binding(verdict)` in the prebind. Ledger accepts the usage
   event only when the allow is present in the durable admission table and the
   supplied verdict exactly matches the stored admission (apart from replay
   metadata).

A missing lineage is a hold, not an implicit fresh budget. A reset requires a
new signed reset authorization and creates a new lineage identifier. Session
scoped policies reject a session change; task scoped policies retain the same
state across delegated sessions.

## Policies

`CompositionPolicy` supports both:

- unordered prohibited pairs, normalized so `read + send` and `send + read`
  are the same restriction; and
- ordered prohibited sequences, evaluated against the ordered tail of the
  admitted task history. A sequence such as `read → write → send` can therefore
  deny even when all individual pairs are allowed.

Budget checks are performed against the durable cumulative state. Negative,
non-finite, or caller-claimed costs are rejected; the cost comes only from the
trusted profile. Concurrent admissions cannot both spend the same remaining
budget.

An override is a separately signed, pre-declared authority decision bound to
lineage, action digest, authority reference, policy/taxonomy hashes, workspace,
and context head. Model output, retrieved text, or an arbitrary request field
cannot create an override.

## Hash-only receipts

Composition state stores action history and admission verdicts as safe
projections. Normalized resources are represented by `resource_hash`; raw tool
arguments are used only transiently to resolve the profile and calculate the
action digest. `composition_binding()` contains only schema/version fields,
policy and taxonomy digests, state/action/profile digests, opaque authority and
scope references, verdict outcome, and the composition hash.

The binding is accepted by prebind validation and is carried into the
hash-covered usage event projection. Usage exports expose the same safe
composition projection. No prompt, credential, raw argument, or tool payload is
stored in the composition state or receipt projection.

## HTTP API

An embedding application may inject a trusted engine into `server.app.serve()`
with `composition_engine=...`. `POST /v1/composition/admit` accepts the
request fields described in `openapi.yaml`. A signed `lineage_authorization`
may initialize an absent lineage; without a configured engine the endpoint
returns a fail-closed service error. The endpoint never constructs policy or
taxonomy objects from request data.

## Example

```python
from ledger_agent.composition import CompositionEngine, composition_binding

engine = CompositionEngine(registry=trusted_registry, policy=trusted_policy,
                           authority_key=authority_key)
lineage_auth = engine.issue_lineage_authorization(
    org_id=org_id, task_lineage_id="task-42", session_id="session-a",
    workspace_scope="prod", authority_action_id="aar-42",
    authority_ref="vault/authority/v1", context_head_digest=context_digest,
)
engine.start_lineage(
    conn, org_id=org_id, task_lineage_id="task-42", session_id="session-a",
    workspace_scope="prod", authority_action_id="aar-42",
    authority_ref="vault/authority/v1", context_head_digest=context_digest,
    authorization=lineage_auth,
)
verdict = engine.admit(
    conn, org_id=org_id, task_lineage_id="task-42", session_id="session-a",
    workspace_scope="prod", authority_action_id="aar-42",
    authority_ref="vault/authority/v1", context_head_digest=context_digest,
    action_id="action-1", tool_endpoint="vault.read",
    arguments={"resource": "dataset:customer-record"},
    idempotency_key="action-1",
)
if verdict["outcome"] != "allow":
    raise RuntimeError("do not execute a non-allow verdict")

# Bind the same hash-only result to the prebind/evidence path and usage event.
binding = composition_binding(verdict)
```

The `authority_key` in this example is held by trusted infrastructure; it is
not a model-controlled value and must never be placed in a receipt or log.
