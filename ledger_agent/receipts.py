"""Stage-aware action receipts, evidence state, runtime manifests, and
external-artifact bindings for Ledger receipts.

Covers:
  #219 — stage-aware action receipts and uncertainty capture
  #220 — context and policy hashes
  #221 — served-claim/context-projection evidence bindings
  #222 — degraded evidence states with fail-closed semantics
  #223 — cross-runtime evaluation / runtime manifest
  #224 — exact external-artifact prior-action and idempotency bindings

Every model here is hash-only: no raw prompts, tool output, secrets, or
memory bodies. All fields are optional/additive so existing v1 receipts
remain validated.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

# ── shared helpers ──────────────────────────────────────────────────────────

SHA256_HEX = set("0123456789abcdef")


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and value.lower().isalnum() and set(value.lower()) <= SHA256_HEX


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _opt_hash(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if not _is_sha256(value):
        raise ValueError(f"expected 64-char SHA-256 hex digest, got: {value!r}")
    return value.lower()


def _opt_text(value: Optional[str], field: str, max_len: int = 512) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string when supplied")
    if len(value) > max_len:
        raise ValueError(f"{field} exceeds {max_len} characters")
    return value


# ── #219 / #220: stage-aware prebind v2 ─────────────────────────────────────

STAGE_VALUES = {
    "proposed", "approved", "leased", "executing",
    "completed", "failed", "cancelled", "interrupted", "recovered",
}
OUTCOME_VALUES = {"allow", "hold", "deny", "abstain", "interrupt", "recover"}
RESULT_VALUES = {"not_executed", "held", "denied", "abstained", "cancelled", "failed", "executed"}
PREBIND_V2_SCHEMA = "perseus-ledger-prebind/v2"


def build_stage_trace(*, action_key: str, stages: list[dict[str, Any]],
                      uncertainty: Optional[dict[str, Any]] = None,
                      human_intercept: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Build a versioned stage trace for an action.

    ``stages`` is an ordered list of {stage, at, actor, digest, detail}
    records. ``uncertainty`` captures model-estimated confidence where
    available. ``human_intercept`` records approval/review points.
    """
    for s in stages:
        if s.get("stage") not in STAGE_VALUES:
            raise ValueError(f"invalid stage: {s.get('stage')}")
    return {
        "schema": "perseus-ledger-stage-trace/v1",
        "action_key": action_key,
        "stages": stages,
        "uncertainty": uncertainty,
        "human_intercept": human_intercept,
        "digest": _sha({"action_key": action_key, "stages": stages,
                         "uncertainty": uncertainty, "human_intercept": human_intercept}),
    }


def build_prebind_v2(*, attempted_action: str, actor_ref: str, authority_ref: str,
                     trusted_scope: str, policy_version: str,
                     evidence_hashes: list[str], selected_context_digest: str,
                     resource_ref: str, boundary_outcome: str, non_effective_result: str,
                     replay_id: str, approval_ref: Optional[str] = None,
                     stage_trace: Optional[dict[str, Any]] = None,
                     context_hash: Optional[str] = None,
                     policy_hash: Optional[str] = None,
                     uncertainty: Optional[str] = None) -> dict[str, Any]:
    """Build a v2 prebind block with stage-aware fields."""
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
        "stage_trace": stage_trace,
        "context_hash": _opt_hash(context_hash),
        "policy_hash": _opt_hash(policy_hash),
        "uncertainty": uncertainty,
    }
    block["prebind_hash"] = _sha({k: v for k, v in block.items() if k != "prebind_hash"})
    return block


def validate_stage_trace(trace: dict[str, Any]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if trace.get("schema") != "perseus-ledger-stage-trace/v1":
        errors.append("stage_trace_schema")
    if not isinstance(trace.get("action_key"), str) or not trace["action_key"].strip():
        errors.append("stage_trace_action_key")
    stages = trace.get("stages")
    if not isinstance(stages, list) or not stages:
        errors.append("stage_trace_stages")
    else:
        for i, s in enumerate(stages):
            if s.get("stage") not in STAGE_VALUES:
                errors.append(f"stage_trace_stages[{i}].stage")
            if not isinstance(s.get("at"), (int, float)):
                errors.append(f"stage_trace_stages[{i}].at")
    if not _is_sha256(trace.get("digest")):
        errors.append("stage_trace_digest")
    return not errors, sorted(set(errors))


# ── #221: served-claim / context-projection evidence ────────────────────────

SERVED_CLAIM_SCHEMA = "perseus-ledger-served-claim/v1"


def build_served_claim(*, source_ref: str, event_ref: str,
                       immutable_span: Optional[str] = None,
                       derivation: Optional[str] = None,
                       valid_from: Optional[float] = None,
                       valid_until: Optional[float] = None,
                       authority_ref: Optional[str] = None,
                       provenance_class: Optional[str] = None,
                       state: Optional[str] = None,
                       scope_anchor: Optional[str] = None,
                       projection_digest: Optional[str] = None,
                       retrieval_status: Optional[str] = None,
                       decision_reason: Optional[str] = None) -> dict[str, Any]:
    """Build a hash-only served-claim evidence object.

    Binds source/event refs and immutable spans, derivation metadata,
    valid/recorded time, authority/provenance/state, workspace/scope,
    selected projection digest, retrieval status, and decision reason.
    Excludes raw prompts, memory bodies, tool args, and secrets.
    """
    claim: dict[str, Any] = {
        "schema": SERVED_CLAIM_SCHEMA,
        "source_ref": source_ref,
        "event_ref": event_ref,
        "immutable_span": immutable_span,
        "derivation": derivation,
        "valid_from": valid_from,
        "valid_until": valid_until,
        "authority_ref": authority_ref,
        "provenance_class": provenance_class,
        "state": state,
        "scope_anchor": scope_anchor,
        "projection_digest": _opt_hash(projection_digest),
        "retrieval_status": retrieval_status,
        "decision_reason": decision_reason,
    }
    claim["claim_digest"] = _sha(claim)
    return claim


def validate_served_claim(claim: dict[str, Any]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if claim.get("schema") != SERVED_CLAIM_SCHEMA:
        errors.append("served_claim_schema")
    if not isinstance(claim.get("source_ref"), str) or not claim["source_ref"].strip():
        errors.append("served_claim_source_ref")
    if not isinstance(claim.get("event_ref"), str) or not claim["event_ref"].strip():
        errors.append("served_claim_event_ref")
    if not _is_sha256(claim.get("claim_digest")):
        errors.append("served_claim_digest")
    return not errors, sorted(set(errors))


# ── #222: degraded evidence states ──────────────────────────────────────────

EVIDENCE_STATUS_VALUES = {
    "fresh", "partial", "timeout", "unavailable", "empty",
    "stale", "abstain", "review",
}

EVIDENCE_POLICY_VALUES = {"required", "optional", "off"}


def evidence_decision(status: str, policy: str = "required",
                      reason_code: Optional[str] = None) -> dict[str, Any]:
    """Decide action permissibility from evidence status.

    ``required`` policy: fresh only; everything else fails closed.
    ``optional`` policy: fresh/partial pass; timeout/unavailable/stale/empty
    produce abstain; review produces hold.
    ``off`` policy: always passes (explicitly declared only).
    """
    if status not in EVIDENCE_STATUS_VALUES:
        raise ValueError(f"invalid evidence_status: {status}")
    if policy not in EVIDENCE_POLICY_VALUES:
        raise ValueError(f"invalid evidence_policy: {policy}")

    if policy == "required":
        allowed = (status == "fresh")
        outcome = "allow" if allowed else "hold"
    elif policy == "optional":
        if status in ("fresh", "partial"):
            outcome, allowed = "allow", True
        elif status == "review":
            outcome, allowed = "hold", False
        else:
            outcome, allowed = "abstain", False
    else:  # off
        outcome, allowed = "allow", True

    return {
        "schema": "perseus-ledger-evidence-decision/v1",
        "evidence_status": status,
        "evidence_policy": policy,
        "reason_code": reason_code,
        "allowed": allowed,
        "boundary_outcome": outcome,
        "digest": _sha({"status": status, "policy": policy, "reason": reason_code}),
    }


# ── #223: runtime manifest ──────────────────────────────────────────────────

RUNTIME_MANIFEST_SCHEMA = "perseus-ledger-runtime-manifest/v1"

AUTH_MODE_VALUES = {"account", "environment", "local", "none"}
EXECUTION_FAMILY_VALUES = {"stateful", "prompt_only", "wrapper", "local", "hosted"}
RETENTION_CLASS_VALUES = {"ephemeral", "session", "bounded", "full"}


def build_runtime_manifest(*, adapter_name: str, adapter_version: str,
                           capabilities: list[str],
                           model: Optional[str] = None,
                           provider: Optional[str] = None,
                           auth_mode: str = "local",
                           execution_family: str = "local",
                           repository_revision: Optional[str] = None,
                           workspace_scope: Optional[str] = None,
                           scenario_digest: Optional[str] = None,
                           scorer_digest: Optional[str] = None,
                           seed: Optional[int] = None,
                           retention_class: str = "session",
                           timestamps: Optional[dict[str, float]] = None,
                           artifact_digests: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """Build a hash-bound runtime manifest.

    Distinguishes local process, official provider CLI, hosted API,
    wrapper, and stateful agent runtimes. Missing metadata produces
    ``unknown``/``incomplete``, never an implied reproducible claim.
    """
    manifest: dict[str, Any] = {
        "schema": RUNTIME_MANIFEST_SCHEMA,
        "adapter_name": adapter_name,
        "adapter_version": adapter_version,
        "capabilities": sorted(set(capabilities)),
        "model": model,
        "provider": provider,
        "auth_mode": auth_mode,
        "execution_family": execution_family,
        "repository_revision": repository_revision,
        "workspace_scope": workspace_scope,
        "scenario_digest": _opt_hash(scenario_digest),
        "scorer_digest": _opt_hash(scorer_digest),
        "seed": seed,
        "retention_class": retention_class,
        "timestamps": timestamps or {},
        "artifact_digests": artifact_digests or {},
    }
    manifest["manifest_digest"] = _sha(manifest)
    return manifest


def validate_runtime_manifest(manifest: dict[str, Any]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if manifest.get("schema") != RUNTIME_MANIFEST_SCHEMA:
        errors.append("runtime_manifest_schema")
    if not isinstance(manifest.get("adapter_name"), str) or not manifest["adapter_name"].strip():
        errors.append("runtime_manifest_adapter_name")
    if not isinstance(manifest.get("adapter_version"), str) or not manifest["adapter_version"].strip():
        errors.append("runtime_manifest_adapter_version")
    if manifest.get("auth_mode") not in AUTH_MODE_VALUES:
        errors.append("runtime_manifest_auth_mode")
    if manifest.get("execution_family") not in EXECUTION_FAMILY_VALUES:
        errors.append("runtime_manifest_execution_family")
    if manifest.get("retention_class") not in RETENTION_CLASS_VALUES:
        errors.append("runtime_manifest_retention_class")
    if not _is_sha256(manifest.get("manifest_digest")):
        errors.append("runtime_manifest_digest")
    return not errors, sorted(set(errors))


# ── #224: external-artifact binding ─────────────────────────────────────────

EXTERNAL_ARTIFACT_SCHEMA = "perseus-ledger-external-artifact/v1"

PRIOR_ACTION_STATUS_VALUES = {
    "handled", "attempted_failed", "cancelled", "unknown",
    "superseded", "new_version",
}


def build_external_artifact_binding(*, source_system: str, source_type: str,
                                    artifact_id: str,
                                    version_hash: Optional[str] = None,
                                    destination_scope: Optional[str] = None,
                                    resource_scope: Optional[str] = None,
                                    prior_action_status: Optional[str] = None,
                                    prior_receipt_ref: Optional[str] = None,
                                    idempotency_key: str = "",
                                    superseded_artifact_id: Optional[str] = None) -> dict[str, Any]:
    """Build a hash-covered external-artifact binding for receipts.

    Exact IDs are authoritative — text-identical artifacts with different
    IDs are independent. Failed/cancelled prior actions are not completed.
    Cross-destination/agent/workspace reuse is rejected.
    """
    binding: dict[str, Any] = {
        "schema": EXTERNAL_ARTIFACT_SCHEMA,
        "source_system": source_system,
        "source_type": source_type,
        "artifact_id": artifact_id,
        "version_hash": _opt_hash(version_hash),
        "destination_scope": destination_scope,
        "resource_scope": resource_scope,
        "prior_action_status": prior_action_status,
        "prior_receipt_ref": prior_receipt_ref,
        "idempotency_key": idempotency_key,
        "superseded_artifact_id": superseded_artifact_id,
    }
    binding["binding_digest"] = _sha(binding)
    return binding


def validate_external_artifact_binding(binding: dict[str, Any]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if binding.get("schema") != EXTERNAL_ARTIFACT_SCHEMA:
        errors.append("artifact_schema")
    if not isinstance(binding.get("source_system"), str) or not binding["source_system"].strip():
        errors.append("artifact_source_system")
    if not isinstance(binding.get("source_type"), str) or not binding["source_type"].strip():
        errors.append("artifact_source_type")
    if not isinstance(binding.get("artifact_id"), str) or not binding["artifact_id"].strip():
        errors.append("artifact_id")
    if binding.get("prior_action_status") is not None and binding["prior_action_status"] not in PRIOR_ACTION_STATUS_VALUES:
        errors.append("artifact_prior_action_status")
    if not _is_sha256(binding.get("binding_digest")):
        errors.append("artifact_digest")
    return not errors, sorted(set(errors))


def check_artifact_idempotent(binding: dict[str, Any],
                              prior_bindings: list[dict[str, Any]]) -> dict[str, Any]:
    """Pre-action gating helper for exact external artifacts.

    ``prior_bindings`` is the set of previously recorded bindings for the
    same artifact_id. Returns a decision object describing whether the
    current action should proceed.
    """
    for prior in prior_bindings:
        if prior.get("artifact_id") != binding.get("artifact_id"):
            continue
        # Same ID, same version → idempotency violation
        if prior.get("version_hash") == binding.get("version_hash"):
            if prior.get("destination_scope") != binding.get("destination_scope"):
                return {
                    "allowed": False,
                    "reason": "scope_mismatch",
                    "detail": "same artifact/version on different destination scope",
                }
            if prior.get("prior_action_status") == "handled":
                return {
                    "allowed": False,
                    "reason": "duplicate",
                    "detail": "artifact already handled with same version",
                }
        # Different version → new version is independently actionable
        else:
            return {
                "allowed": True,
                "reason": "new_version",
                "detail": f"new version {binding.get('version_hash')} supersedes {prior.get('version_hash')}",
            }

    # No prior binding found → allowed
    return {"allowed": True, "reason": "first_action", "detail": "no prior action on this artifact"}



# ── #237: belief-context evidence block ─────────────────────────────────────

BELIEF_CONTEXT_SCHEMA = "perseus-ledger-belief-context/v1"
BELIEF_KINDS = ("believed", "assumed", "ignored")
BELIEF_ENTRY_MAX_STATEMENT = 512


def _belief_entries(entries: Any, kind: str) -> list[dict[str, Any]]:
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise ValueError(f"{kind} must be a list of entries")
    out: list[dict[str, Any]] = []
    for e in entries:
        if not isinstance(e, dict):
            raise ValueError(f"{kind} entries must be objects")
        statement = e.get("statement")
        if not isinstance(statement, str) or not statement.strip():
            raise ValueError(f"{kind} entries require a non-empty statement")
        if len(statement) > BELIEF_ENTRY_MAX_STATEMENT:
            raise ValueError(f"{kind} statement exceeds {BELIEF_ENTRY_MAX_STATEMENT} characters")
        weight = e.get("weight")
        if weight is not None and (not isinstance(weight, (int, float)) or not (0 <= weight <= 1)):
            raise ValueError(f"{kind} weight must be a number between 0 and 1")
        refs = e.get("evidence_refs")
        if refs is None:
            refs = []
        if not isinstance(refs, list) or any(not _is_sha256(r) for r in refs):
            raise ValueError(f"{kind} evidence_refs must be a list of 64-char SHA-256 hex digests")
        out.append({
            "statement": statement,
            "weight": weight,
            "evidence_refs": sorted(set(r.lower() for r in refs)),
        })
    return out


def build_belief_context(*, believed: Any = None, assumed: Any = None,
                         ignored: Any = None, confidence: Optional[float] = None,
                         source: str = "agent", summary: Optional[str] = None) -> dict[str, Any]:
    """Build a hash-bound belief-context evidence block (#237).

    Records the decision-time beliefs an action was predicated on:
    ``believed`` / ``assumed`` / ``ignored`` entry lists with optional weights
    and evidence refs. Hash-only — statements are belief claims, never raw
    prompts, tool output, or memory bodies.
    """
    if confidence is not None and (not isinstance(confidence, (int, float)) or not (0 <= confidence <= 1)):
        raise ValueError("confidence must be a number between 0 and 1")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source must be a non-empty string")
    if summary is not None and (not isinstance(summary, str) or not summary.strip() or len(summary) > 512):
        raise ValueError("summary must be a non-empty string of at most 512 characters")
    block: dict[str, Any] = {
        "schema": BELIEF_CONTEXT_SCHEMA,
        "believed": _belief_entries(believed, "believed"),
        "assumed": _belief_entries(assumed, "assumed"),
        "ignored": _belief_entries(ignored, "ignored"),
        "confidence": confidence,
        "source": source,
        "summary": summary,
    }
    block["belief_digest"] = _sha({k: v for k, v in block.items() if k != "belief_digest"})
    return block


def validate_belief_context(block: dict[str, Any]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if not isinstance(block, dict):
        return False, ["belief_schema"]
    if block.get("schema") != BELIEF_CONTEXT_SCHEMA:
        errors.append("belief_schema")
    for kind in BELIEF_KINDS:
        entries = block.get(kind)
        if entries is None:
            continue
        if not isinstance(entries, list):
            errors.append(f"belief_{kind}_list")
            continue
        for i, e in enumerate(entries):
            if not isinstance(e, dict) or not isinstance(e.get("statement"), str) \
                    or not e["statement"].strip():
                errors.append(f"belief_{kind}[{i}].statement")
            w = e.get("weight") if isinstance(e, dict) else None
            if w is not None and (not isinstance(w, (int, float)) or not (0 <= w <= 1)):
                errors.append(f"belief_{kind}[{i}].weight")
            refs = e.get("evidence_refs", []) if isinstance(e, dict) else []
            if not isinstance(refs, list) or any(not _is_sha256(r) for r in refs):
                errors.append(f"belief_{kind}[{i}].evidence_refs")
    conf = block.get("confidence")
    if conf is not None and (not isinstance(conf, (int, float)) or not (0 <= conf <= 1)):
        errors.append("belief_confidence")
    if not isinstance(block.get("source"), str) or not block["source"].strip():
        errors.append("belief_source")
    digest = block.get("belief_digest")
    if not _is_sha256(digest):
        errors.append("belief_digest")
    elif digest != _sha({k: v for k, v in block.items() if k != "belief_digest"}):
        errors.append("belief_digest_mismatch")
    return not errors, sorted(set(errors))


# ── #239: governance self-cost ──────────────────────────────────────────────

GOVERNANCE_COST_SCHEMA = "perseus-ledger-governance-cost/v1"
GOVERNANCE_COST_FIELDS = (
    "wall_ms", "cpu_ms", "mem_bytes", "storage_bytes",
    "tokens", "model_calls", "approval_waits_ms",
)


def build_governance_cost(**fields: Any) -> dict[str, Any]:
    """Build a governance self-cost block (#239).

    Only supplied (non-None) fields are recorded; every value is a
    non-negative number. Internal telemetry — excluded from customer-facing
    usage/totals. The research question from #239 (inside vs outside the
    signed bytes) resolves to INSIDE: the block is chain-covered at ingest
    and covered by the receipt HMAC, so governance overhead is as
    tamper-evident as the action it governs.
    """
    block: dict[str, Any] = {"schema": GOVERNANCE_COST_SCHEMA}
    for field in GOVERNANCE_COST_FIELDS:
        value = fields.get(field)
        if value is None:
            continue
        if not isinstance(value, (int, float)) or value < 0:
            raise ValueError(f"governance_cost.{field} must be a non-negative number")
        block[field] = value
    unknown = set(fields) - set(GOVERNANCE_COST_FIELDS)
    if unknown:
        raise ValueError("unknown governance_cost fields: " + ", ".join(sorted(unknown)))
    block["governance_digest"] = _sha({k: v for k, v in block.items() if k != "governance_digest"})
    return block


def validate_governance_cost(block: dict[str, Any]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if not isinstance(block, dict):
        return False, ["governance_schema"]
    if block.get("schema") != GOVERNANCE_COST_SCHEMA:
        errors.append("governance_schema")
    unknown = set(block) - set(GOVERNANCE_COST_FIELDS) - {"schema", "governance_digest"}
    for field in sorted(unknown):
        errors.append(f"governance_{field}_unknown")
    for field in GOVERNANCE_COST_FIELDS:
        value = block.get(field)
        if value is None:
            continue
        if not isinstance(value, (int, float)) or value < 0:
            errors.append(f"governance_{field}")
    digest = block.get("governance_digest")
    if not _is_sha256(digest):
        errors.append("governance_digest")
    elif digest != _sha({k: v for k, v in block.items() if k != "governance_digest"}):
        errors.append("governance_digest_mismatch")
    return not errors, sorted(set(errors))

__all__ = [
    # #219/#220
    "PREBIND_V2_SCHEMA", "build_prebind_v2", "build_stage_trace",
    "validate_stage_trace", "STAGE_VALUES",
    # #221
    "SERVED_CLAIM_SCHEMA", "build_served_claim", "validate_served_claim",
    # #222
    "EVIDENCE_STATUS_VALUES", "EVIDENCE_POLICY_VALUES",
    "evidence_decision",
    # #223
    "RUNTIME_MANIFEST_SCHEMA", "build_runtime_manifest",
    "validate_runtime_manifest", "EXECUTION_FAMILY_VALUES",
    # #224
    "EXTERNAL_ARTIFACT_SCHEMA", "build_external_artifact_binding",
    "validate_external_artifact_binding", "check_artifact_idempotent",
    "PRIOR_ACTION_STATUS_VALUES",
    # #237
    "BELIEF_CONTEXT_SCHEMA", "BELIEF_KINDS", "build_belief_context",
    "validate_belief_context",
    # #239
    "GOVERNANCE_COST_SCHEMA", "GOVERNANCE_COST_FIELDS",
    "build_governance_cost", "validate_governance_cost",
]
