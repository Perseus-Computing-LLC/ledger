"""Optional hash-bound pre-action assurance and pure replay comparison.

Supports two schema versions:
  - ``perseus-ledger-prebind/v1`` — original (legacy)
  - ``perseus-ledger-prebind/v2`` — stage-aware (#219) with context/policy
    hashes (#220), stage traces, and uncertainty capture
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

PREBIND_SCHEMA = "perseus-ledger-prebind/v1"
PREBIND_V2_SCHEMA = "perseus-ledger-prebind/v2"
OUTCOMES = {"allow", "hold", "deny", "abstain", "interrupt", "recover"}
NON_EFFECTIVE_RESULTS = {"not_executed", "held", "denied", "abstained", "cancelled", "failed"}
FORBIDDEN_KEYS = {"prompt", "context", "content", "body", "body_json", "tool_arguments", "arguments", "result", "response", "token", "secret", "password", "api_key"}
_REQUIRED = (
    "attempted_action", "actor_ref", "authority_ref", "trusted_scope", "policy_version",
    "evidence_hashes", "selected_context_digest", "resource_ref", "boundary_outcome",
    "non_effective_result", "replay_id",
)
_V2_FIELDS = {"stage_trace", "context_hash", "policy_hash", "uncertainty", "request_hash", "nonce", "epoch"}
STAGE_VALUES = {"proposed", "approved", "leased", "executing", "completed", "failed", "cancelled", "interrupted", "recovered"}


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _is_hash(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value.lower())


def _optional_request_fields(request_hash: str | None, nonce: str | None,
                            epoch: int | str | None) -> tuple[str | None, str | None, int | str | None]:
    if request_hash is not None and not _is_hash(request_hash):
        raise ValueError("request_hash must be a 64-character SHA-256 hex digest")
    if nonce is not None and (not isinstance(nonce, str) or not nonce.strip()):
        raise ValueError("nonce must be a non-empty string")
    if epoch is not None and (isinstance(epoch, bool) or not isinstance(epoch, (int, str))):
        raise ValueError("epoch must be an integer or string")
    return (request_hash.lower() if request_hash is not None else None, nonce, epoch)


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
    schema_ver = block.get("schema_version")
    if schema_ver not in (PREBIND_SCHEMA, PREBIND_V2_SCHEMA):
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
    # The receipts.py v2 builder predates stage_refs and intentionally omits
    # it; when present, retain the strict validation used by prebind.py.
    if stage_refs is not None and (
        not isinstance(stage_refs, list)
        or any(not isinstance(value, str) or not value for value in stage_refs)
    ):
        errors.append("stage_refs")

    # v2-specific validation (#219, #220)
    if schema_ver == PREBIND_V2_SCHEMA:
        stage_trace = block.get("stage_trace")
        if stage_trace is not None:
            if not isinstance(stage_trace, dict):
                errors.append("stage_trace_not_dict")
            else:
                if stage_trace.get("schema") != "perseus-ledger-stage-trace/v1":
                    errors.append("stage_trace_schema")
                stages = stage_trace.get("stages")
                if not isinstance(stages, list) or not stages:
                    errors.append("stage_trace_stages_empty")
                else:
                    for i, s in enumerate(stages):
                        if not isinstance(s, dict):
                            errors.append(f"stage_trace_stages[{i}]_not_dict")
                            continue
                        if s.get("stage") not in STAGE_VALUES:
                            errors.append(f"stage_trace_stages[{i}].stage")
                        if not isinstance(s.get("at"), (int, float)):
                            errors.append(f"stage_trace_stages[{i}].at")
        for field in ("context_hash", "policy_hash"):
            v = block.get(field)
            if v is not None and not _is_hash(v):
                errors.append(field)
        uncertainty = block.get("uncertainty")
        if uncertainty is not None and not isinstance(uncertainty, str):
            errors.append("uncertainty_not_string")
        request_hash = block.get("request_hash")
        if request_hash is not None and not _is_hash(request_hash):
            errors.append("prebind_request_hash")
        nonce = block.get("nonce")
        if nonce is not None and (not isinstance(nonce, str) or not nonce.strip()):
            errors.append("prebind_nonce")
        epoch = block.get("epoch")
        if epoch is not None and (isinstance(epoch, bool) or not isinstance(epoch, (int, str))):
            errors.append("prebind_epoch")

    supplied = block.get("prebind_hash")
    if not _is_hash(supplied) or supplied != prebind_digest(block):
        errors.append("prebind_hash")
    allowed = set(_REQUIRED) | {"schema_version", "approval_ref", "stage_refs", "prebind_hash"}
    if schema_ver == PREBIND_V2_SCHEMA:
        allowed |= _V2_FIELDS
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
    approval_required = prior.get("approval_ref") is not None
    approved = bool(state.get("approval_granted", not approval_required))
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


def build_prebind_v2(*, attempted_action: str, actor_ref: str, authority_ref: str,
                     trusted_scope: str, policy_version: str,
                     evidence_hashes: list[str], selected_context_digest: str,
                     resource_ref: str, boundary_outcome: str, non_effective_result: str,
                     replay_id: str, approval_ref: str | None = None,
                     stage_refs: list[str] | None = None,
                     stage_trace: dict[str, Any] | None = None,
                     context_hash: str | None = None,
                     policy_hash: str | None = None,
                     uncertainty: str | None = None,
                     request_hash: str | None = None,
                     nonce: str | None = None,
                     epoch: int | str | None = None) -> dict[str, Any]:
    """Build a v2 prebind with stage-aware fields and context/policy hashes."""
    request_hash, nonce, epoch = _optional_request_fields(request_hash, nonce, epoch)
    block: dict[str, Any] = {
        "schema_version": PREBIND_V2_SCHEMA,
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
        "stage_trace": stage_trace,
        "context_hash": context_hash,
        "policy_hash": policy_hash,
        "uncertainty": uncertainty,
        "request_hash": request_hash,
        "nonce": nonce,
        "epoch": epoch,
    }
    block["prebind_hash"] = prebind_digest(block)
    return block


def replay_prebind_v2(prior: Mapping[str, Any], *,
                      current_authority_ref: str | None = None,
                      current_trusted_scope: str | None = None,
                      current_evidence_hashes: list[str] | None = None,
                      current_policy_version: str | None = None,
                      current_context_hash: str | None = None,
                      current_policy_hash: str | None = None,
                      current_evidence_status: str | None = None,
                      current_state: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Replay a v2 prebind with stage-aware and evidence-degradation awareness."""
    valid, errors = validate_prebind(prior)
    if not valid:
        raise ValueError("invalid prebind: " + ", ".join(errors))
    if prior.get("schema_version") != PREBIND_V2_SCHEMA:
        raise ValueError("prebind is not v2, use replay_prebind for v1")
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
    if current_context_hash is not None and current_context_hash != prior.get("context_hash"):
        changed.append("context_hash")
    if current_policy_hash is not None and current_policy_hash != prior.get("policy_hash"):
        changed.append("policy_hash")

    authority_ok = bool(state.get("authority_ok", not any(field in changed for field in ("authority_ref", "trusted_scope"))))
    evidence_current = bool(state.get("evidence_current", not bool(state.get("evidence_stale"))))
    evidence_degraded = bool(state.get("evidence_degraded", False))
    approval_required = prior.get("approval_ref") is not None
    approved = bool(state.get("approval_granted", not approval_required))
    action_allowed = bool(state.get("action_allowed", authority_ok and evidence_current))
    admitted = authority_ok and evidence_current and approved and action_allowed and not evidence_degraded

    if evidence_degraded and not state.get("evidence_policy_degraded_allow", False):
        admission = "not_admitted"
        outcome = "hold"
        reason = "evidence_degraded"
    elif admitted:
        admission = "admitted_after_correction" if prior["boundary_outcome"] in {"hold", "deny", "abstain", "recover", "interrupt"} else "admitted"
        outcome = "allow"
        reason = ""
    else:
        admission = "not_admitted"
        outcome = "hold" if not authority_ok or not evidence_current else "deny"
        reason = ""

    result: dict[str, Any] = {
        "schema_version": "perseus-ledger-replay/v2",
        "replay_id": prior["replay_id"],
        "prior_prebind_hash": prior["prebind_hash"],
        "changed_fields": changed,
        "admission": admission,
        "replayed_boundary_outcome": outcome,
        "non_mutating": True,
        "reason_codes": (["authority_changed"] if "authority_ref" in changed or "trusted_scope" in changed else [])
        + (["evidence_changed"] if "evidence_hashes" in changed else [])
        + (["policy_changed"] if "policy_version" in changed else [])
        + (["context_changed"] if "context_hash" in changed else [])
        + (["policy_hash_changed"] if "policy_hash" in changed else [])
        + (["evidence_degraded"] if evidence_degraded else []),
    }
    if reason:
        result["rejection_reason"] = reason
    if current_evidence_status:
        result["evidence_status"] = current_evidence_status
    return result


__all__ = ["PREBIND_SCHEMA", "PREBIND_V2_SCHEMA", "STAGE_VALUES",
           "build_prebind", "build_prebind_v2", "prebind_digest",
           "replay_prebind", "replay_prebind_v2", "validate_prebind"]
