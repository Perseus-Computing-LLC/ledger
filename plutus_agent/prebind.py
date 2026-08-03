"""Optional hash-bound pre-action assurance and pure replay comparison."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

PREBIND_SCHEMA = "perseus-ledger-prebind/v1"
OUTCOMES = {"allow", "hold", "deny", "abstain", "interrupt", "recover"}
NON_EFFECTIVE_RESULTS = {"not_executed", "held", "denied", "abstained", "cancelled", "failed"}
FORBIDDEN_KEYS = {"prompt", "context", "content", "body", "body_json", "tool_arguments", "arguments", "result", "response", "token", "secret", "password", "api_key"}
_REQUIRED = (
    "attempted_action", "actor_ref", "authority_ref", "trusted_scope", "policy_version",
    "evidence_hashes", "selected_context_digest", "resource_ref", "boundary_outcome",
    "non_effective_result", "replay_id",
)


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _is_hash(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value.lower())


def _scan(value: Any, errors: list[str]) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            lowered = str(key).lower()
            if lowered in FORBIDDEN_KEYS or lowered.startswith("raw_"):
                errors.append(f"forbidden_field:{key}")
            _scan(child, errors)
    elif isinstance(value, list):
        for child in value:
            _scan(child, errors)


def prebind_digest(block: Mapping[str, Any]) -> str:
    return _sha({key: value for key, value in block.items() if key != "prebind_hash"})


def build_prebind(*, attempted_action: str, actor_ref: str, authority_ref: str, trusted_scope: str,
                  policy_version: str, evidence_hashes: list[str], selected_context_digest: str,
                  resource_ref: str, boundary_outcome: str, non_effective_result: str,
                  replay_id: str, approval_ref: str | None = None,
                  stage_refs: list[str] | None = None) -> dict[str, Any]:
    block: dict[str, Any] = {
        "schema_version": PREBIND_SCHEMA,
        "attempted_action": attempted_action,
        "actor_ref": actor_ref,
        "authority_ref": authority_ref,
        "trusted_scope": trusted_scope,
        "policy_version": policy_version,
        "evidence_hashes": sorted(set(evidence_hashes)),
        "selected_context_digest": selected_context_digest,
        "resource_ref": resource_ref,
        "boundary_outcome": boundary_outcome,
        "non_effective_result": non_effective_result,
        "replay_id": replay_id,
        "approval_ref": approval_ref,
        "stage_refs": list(stage_refs or []),
    }
    block["prebind_hash"] = prebind_digest(block)
    return block


def validate_prebind(block: Mapping[str, Any]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if not isinstance(block, Mapping):
        return False, ["prebind"]
    _scan(block, errors)
    if block.get("schema_version") != PREBIND_SCHEMA:
        errors.append("schema_version")
    for field in _REQUIRED:
        if field not in block or not isinstance(block[field], str) or not block[field].strip():
            if field != "evidence_hashes":
                errors.append(field)
    hashes = block.get("evidence_hashes")
    if not isinstance(hashes, list) or not hashes or any(not _is_hash(value) for value in hashes):
        errors.append("evidence_hashes")
    if not _is_hash(block.get("selected_context_digest")):
        errors.append("selected_context_digest")
    if block.get("boundary_outcome") not in OUTCOMES:
        errors.append("boundary_outcome")
    if block.get("non_effective_result") not in NON_EFFECTIVE_RESULTS:
        errors.append("non_effective_result")
    if block.get("boundary_outcome") == "allow" and block.get("non_effective_result") == "executed":
        errors.append("outcome_result_mismatch")
    if block.get("boundary_outcome") != "allow" and block.get("non_effective_result") == "executed":
        errors.append("outcome_result_mismatch")
    stage_refs = block.get("stage_refs")
    if not isinstance(stage_refs, list) or any(not isinstance(value, str) or not value for value in stage_refs):
        errors.append("stage_refs")
    supplied = block.get("prebind_hash")
    if not _is_hash(supplied) or supplied != prebind_digest(block):
        errors.append("prebind_hash")
    allowed = set(_REQUIRED) | {"schema_version", "approval_ref", "stage_refs", "prebind_hash"}
    for key in set(block) - allowed:
        errors.append(f"unknown_field:{key}")
    return not errors, sorted(set(errors))


def replay_prebind(prior: Mapping[str, Any], *, current_authority_ref: str | None = None,
                   current_trusted_scope: str | None = None, current_evidence_hashes: list[str] | None = None,
                   current_policy_version: str | None = None, current_state: Mapping[str, Any] | None = None) -> dict[str, Any]:
    valid, errors = validate_prebind(prior)
    if not valid:
        raise ValueError("invalid prebind: " + ", ".join(errors))
    state = dict(current_state or {})
    changed: list[str] = []
    if current_authority_ref is not None and current_authority_ref != prior["authority_ref"]:
        changed.append("authority_ref")
    if current_trusted_scope is not None and current_trusted_scope != prior["trusted_scope"]:
        changed.append("trusted_scope")
    normalized = None if current_evidence_hashes is None else sorted(set(current_evidence_hashes))
    if normalized is not None and normalized != prior["evidence_hashes"]:
        changed.append("evidence_hashes")
    if current_policy_version is not None and current_policy_version != prior["policy_version"]:
        changed.append("policy_version")
    authority_ok = bool(state.get("authority_ok", not any(field in changed for field in ("authority_ref", "trusted_scope"))))
    evidence_current = bool(state.get("evidence_current", not bool(state.get("evidence_stale"))))
    approved = bool(state.get("approval_granted", prior.get("approval_ref") is not None))
    action_allowed = bool(state.get("action_allowed", authority_ok and evidence_current))
    admitted = authority_ok and evidence_current and approved and action_allowed
    if admitted:
        admission = "admitted_after_correction" if prior["boundary_outcome"] in {"hold", "deny", "abstain", "recover", "interrupt"} else "admitted"
        outcome = "allow"
    else:
        admission = "not_admitted"
        outcome = "hold" if not authority_ok or not evidence_current else "deny"
    return {
        "schema_version": "perseus-ledger-replay/v1",
        "replay_id": prior["replay_id"],
        "prior_prebind_hash": prior["prebind_hash"],
        "changed_fields": changed,
        "admission": admission,
        "replayed_boundary_outcome": outcome,
        "non_mutating": True,
        "reason_codes": (["authority_changed"] if "authority_ref" in changed or "trusted_scope" in changed else [])
        + (["evidence_changed"] if "evidence_hashes" in changed else [])
        + (["policy_changed"] if "policy_version" in changed else []),
    }


__all__ = ["PREBIND_SCHEMA", "build_prebind", "prebind_digest", "replay_prebind", "validate_prebind"]
