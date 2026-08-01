# Authorized Action Receipts

Status: implementation slice
Date: 2026-07-25
Tracks: [perseus-vault#768](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/768) · [ledger#183](https://github.com/Perseus-Computing-LLC/ledger/issues/183) · [hermes-plugin-perseus-vault#4](https://github.com/Perseus-Computing-LLC/hermes-plugin-perseus-vault/issues/4)

## Overview

An Authorized Action Receipt binds a recorded autonomous-system action to the
identity, authority manifest, trusted scope, action intent, and approval state
that governed it. It extends a task-scoped Evidence Receipt; it does not make
Ledger an authorization engine.

Vault is the authoritative control plane. Ledger accepts only opaque, validated
references and hashes and commits them to the organization event chain. This
keeps raw prompts, secrets, tool output, and policy bodies outside the Ledger
while preserving an independently verifiable evidence trail.

## Roles

| System | Responsibility |
|---|---|
| Vault | Store/version/revoke authority manifests; validate scope and capability; enforce approval and action state transitions; issue action/approval references. |
| Hermes plugin | Derive trusted integration scope; request intent before a side effect; block on a Vault denial or required approval; report terminal outcome. |
| Ledger | Hash-cover supplied action provenance alongside resource allocation and render it in a task receipt. |
| Human approver | Authorize, deny, or allow an approval to expire through Vault; never represented by a local Boolean alone. |

## Evidence model

`usage_events` may include this nullable, hash-covered `action_authorization`
projection:

```json
{
  "agent_id": "hermes-prod",
  "authority_manifest_ref": "auth-42@3",
  "scope_anchor": "github:Perseus-Computing-LLC/ledger",
  "action_intent_hash": "sha256 hex digest",
  "status": "executed",
  "approval_ref": null
}
```

All six fields are optional for compatibility. When any action-provenance field
is supplied, Ledger requires `agent_id`, `authority_manifest_ref`,
`scope_anchor`, `action_intent_hash`, and `status`. `action_intent_hash` is a
64-character SHA-256 digest. Approval-decision states (`approved`, `denied`,
`expired`) additionally require `approval_ref`.

Allowed status values:

```text
intent | approval_requested | approved | denied | expired |
executed | failed | cancelled
```

Ledger intentionally does not infer a state transition or decide whether an
approval was required. It records evidence reported by the enforcement plane.

## Receipt behavior

`GET /api/audit?org=<id>&external_ref=<id>` retains
`perseus-evidence-receipt/v1` compatibility. Each event now includes
`action_authorization` with the fields above; historical events render all
fields as `null`.

The action provenance fields are trailing optional canonical fields in the
per-organization chain. Historical rows retain their original hash bytes. A
mutation to an action status, manifest reference, scope anchor, intent digest,
or approval reference makes `verify_chain()` fail from that event onward.

## Vault contract

Vault issue #768 defines the authoritative records:

1. An authority manifest binds a registered `agent_id` to allowed capabilities,
   trusted scope anchors, expiry/revocation, and approval requirements.
2. An `action_intent` validates manifest, capability, agent, workspace, and
   trusted scope before creating an action ID and intent digest.
3. Approval and terminal transitions are append-only keyed-audit events.
4. A scoped lease admits one executor for `{workspace, action_key}` at a time;
   it expires rather than becoming a durable execution claim.
5. Manifest violations fail closed and are independently journaled.

## Privacy and retention

The receipt holds opaque IDs and digests only. It MUST NOT contain credentials,
raw prompts, raw tool output, or unredacted CUI/PII. Transient coordination
belongs in TTL state/leases. Durable action records declare `searchability`:
`full`, `metadata_only`, or `disabled`; Vault owns enforcement of that policy.

## Acceptance criteria

- Ledger validates and hash-covers complete supplied action provenance.
- Existing usage ingest and v1 receipts remain compatible.
- Unit fixtures prove rendering, incomplete-provenance rejection, and tamper
  detection for an action status.
- Vault later proves manifest validation, approval lifecycle, scope binding,
  searchability, and lease race safety before Hermes enforcement is enabled.
