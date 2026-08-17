"""Hash-only acceptance campaign contracts and fail-closed budget helpers.

A campaign is an envelope around individual Ledger usage/action receipts.  The
module is deliberately persistence-agnostic: it validates the public contract,
computes deterministic commitments, and keeps framework integrity separate from
the target's acceptance result.  ``ledger_agent.db`` owns durable storage.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

CAMPAIGN_MANIFEST_SCHEMA = "perseus-ledger-acceptance-campaign/v1"
CAMPAIGN_CHECK_SCHEMA = "perseus-ledger-acceptance-check/v1"
CAMPAIGN_RECEIPT_SCHEMA = "perseus-ledger-acceptance-receipt/v1"
CAMPAIGN_BINDING_SCHEMA = "perseus-ledger-campaign-binding/v1"

FRAMEWORK_STATUS_VALUES = {"completed", "error", "interrupted", "cancelled"}
TARGET_STATUS_VALUES = {"pass", "fail", "inconclusive", "not_run"}
CHECK_STATUS_VALUES = {"pass", "fail", "skip", "error"}
BUDGET_STATUS_VALUES = {"not_configured", "within_guard", "stopped", "overrun"}
EVIDENCE_STATUS_VALUES = {"pending", "complete", "incomplete", "invalid"}
FINALIZATION_STATUS_VALUES = {"pending", "complete", "failed"}
RETRY_POLICY_VALUES = {"none", "same_config", "new_version_only", "explicit_continuation"}


class CampaignBudgetError(ValueError):
    """A proposed campaign usage event crossed a durable spend guard."""

    def __init__(self, campaign_id: str, reason: str, remaining_micros: int):
        self.campaign_id = campaign_id
        self.reason = reason
        self.remaining_micros = remaining_micros
        super().__init__(
            f"campaign budget guard: {reason} (remaining_micros={remaining_micros})"
        )


_SHA256_HEX = set("0123456789abcdef")
_FORBIDDEN_KEYS = {
    "prompt", "memory_body", "memory_bodies", "provider_payload",
    "provider_response", "raw_payload", "tool_arguments", "tool_output",
    "api_key", "authorization", "password", "credential", "secret",
    "private_key", "access_token", "refresh_token", "bearer_token",
}

_MANIFEST_FIELDS = {
    "schema", "campaign_id", "planned_cells", "provider_lanes", "config_hash",
    "fixture_hash", "target_ref", "target_commit_hash", "target_build_hash",
    "runtime_identity", "expected_spend_min_micros", "expected_spend_max_micros",
    "hard_stop_micros", "runaway_guard_micros", "retry_policy",
    "continuation_allowed", "action_intent_hash", "evidence_required",
    "manifest_hash",
}
_CHECK_FIELDS = {
    "schema", "campaign_id", "cell_id", "lane", "status", "config_hash",
    "attempt", "continuation", "parent_attempt", "action_intent_hash",
    "result_hash", "evidence_hashes", "usage_event_ids", "checkpoint_ref",
    "reason_code", "check_hash",
}
_RECEIPT_FIELDS = {
    "schema", "campaign_id", "manifest_hash", "framework_status", "target_status",
    "budget_status", "evidence_status", "finalization_status", "finalization_reason",
    "counts", "check_hashes", "target_identity", "evidence_bundle_hash",
    "protected_state_before_hash", "protected_state_after_hash", "cleanup_manifest_hash",
    "spent_micros", "remaining_micros", "stop_reason", "last_checkpoint_ref",
    "verification", "receipt_hash",
}
_BINDING_FIELDS = {
    "schema", "campaign_id", "cell_id", "lane", "config_hash", "attempt",
    "continuation", "parent_attempt", "action_intent_hash", "checkpoint_ref",
    "binding_hash",
}


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str) and len(value) == 64
        and set(value.lower()) <= _SHA256_HEX
    )


def _text(value: Any, field: str, *, required: bool = False, max_len: int = 256) -> list[str]:
    if value is None and not required:
        return []
    if not isinstance(value, str) or not value.strip():
        return [field]
    if len(value) > max_len:
        return [f"{field}_too_long"]
    return []


def _nonnegative(value: Any, field: str, *, required: bool = False) -> list[str]:
    if value is None and not required:
        return []
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return [field]
    return []


def _list_of_text(value: Any, field: str, *, required: bool = False) -> list[str]:
    if value is None and not required:
        return []
    if not isinstance(value, list) or not value:
        return [field]
    errors: list[str] = []
    normalized: set[str] = set()
    for item in value:
        errors.extend(_text(item, field))
        if isinstance(item, str):
            normalized.add(item)
    if len(normalized) != len(value):
        errors.append(f"{field}_duplicate")
    return errors


def _forbidden(value: Any, path: str = "") -> list[str]:
    errors: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key).lower()
            child_path = f"{path}.{key}" if path else str(key)
            if key_text in _FORBIDDEN_KEYS:
                errors.append(f"forbidden_field:{child_path}")
            errors.extend(_forbidden(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            errors.extend(_forbidden(child, f"{path}[{index}]"))
    return errors


def _unknown(value: dict[str, Any], allowed: set[str]) -> list[str]:
    return [f"unknown:{key}" for key in sorted(set(value) - allowed)]


def _digest_without(value: dict[str, Any], field: str) -> str:
    return _sha({key: item for key, item in value.items() if key != field})


def manifest_digest(manifest: dict[str, Any]) -> str:
    return _digest_without(manifest, "manifest_hash")


def check_digest(check: dict[str, Any]) -> str:
    return _digest_without(check, "check_hash")


def receipt_digest(receipt: dict[str, Any]) -> str:
    return _sha({
        key: value for key, value in receipt.items()
        if key not in {"receipt_hash", "verification"}
    })


def binding_digest(binding: dict[str, Any]) -> str:
    return _digest_without(binding, "binding_hash")


def build_binding(*, campaign_id: str, cell_id: str, lane: str,
                  config_hash: str, attempt: int = 1,
                  continuation: bool = False,
                  parent_attempt: Optional[int] = None,
                  action_intent_hash: Optional[str] = None,
                  checkpoint_ref: Optional[str] = None,
                  **extra: Any) -> dict[str, Any]:
    """Build the hash-only attribution carried by a usage event."""
    binding: dict[str, Any] = {
        "schema": CAMPAIGN_BINDING_SCHEMA,
        "campaign_id": campaign_id,
        "cell_id": cell_id,
        "lane": lane,
        "config_hash": config_hash.lower() if isinstance(config_hash, str) else config_hash,
        "attempt": attempt,
        "continuation": continuation,
        "parent_attempt": parent_attempt,
        "action_intent_hash": action_intent_hash.lower() if isinstance(action_intent_hash, str) else action_intent_hash,
        "checkpoint_ref": checkpoint_ref,
    }
    binding.update(extra)
    binding["binding_hash"] = binding_digest(binding)
    valid, errors = validate_binding(binding)
    if not valid:
        raise ValueError("invalid campaign binding: " + ", ".join(errors))
    return binding


def validate_binding(binding: dict[str, Any]) -> tuple[bool, list[str]]:
    if not isinstance(binding, dict):
        return False, ["binding"]
    errors = _unknown(binding, _BINDING_FIELDS) + _forbidden(binding)
    if binding.get("schema") != CAMPAIGN_BINDING_SCHEMA:
        errors.append("schema")
    for field in ("campaign_id", "cell_id", "lane"):
        errors.extend(_text(binding.get(field), field, required=True))
    if not _is_sha256(binding.get("config_hash")):
        errors.append("config_hash")
    errors.extend(_nonnegative(binding.get("attempt"), "attempt", required=True))
    if binding.get("attempt") == 0:
        errors.append("attempt")
    if not isinstance(binding.get("continuation"), bool):
        errors.append("continuation")
    if binding.get("parent_attempt") is not None:
        errors.extend(_nonnegative(binding.get("parent_attempt"), "parent_attempt"))
    for field in ("action_intent_hash",):
        value = binding.get(field)
        if value is not None and not _is_sha256(value):
            errors.append(field)
    errors.extend(_text(binding.get("checkpoint_ref"), "checkpoint_ref"))
    if binding.get("attempt") == 1 and (binding.get("continuation") or binding.get("parent_attempt") is not None):
        errors.append("initial_attempt_lineage")
    if binding.get("attempt", 0) > 1:
        if not binding.get("continuation") or binding.get("parent_attempt") != binding["attempt"] - 1:
            errors.append("continuation_lineage")
        if not binding.get("action_intent_hash"):
            errors.append("continuation_action_intent_required")
    digest = binding.get("binding_hash")
    if not _is_sha256(digest):
        errors.append("binding_hash")
    elif digest != binding_digest(binding):
        errors.append("binding_hash_mismatch")
    return not errors, sorted(set(errors))


def build_manifest(*, campaign_id: str, planned_cells: list[str],
                   provider_lanes: list[str], config_hash: str, fixture_hash: str,
                   target_ref: Optional[str] = None,
                   target_commit_hash: Optional[str] = None,
                   target_build_hash: Optional[str] = None,
                   runtime_identity: Optional[str] = None,
                   expected_spend_min_micros: Optional[int] = None,
                   expected_spend_max_micros: Optional[int] = None,
                   hard_stop_micros: Optional[int] = None,
                   runaway_guard_micros: Optional[int] = None,
                   retry_policy: str = "none", continuation_allowed: bool = False,
                   action_intent_hash: Optional[str] = None,
                   evidence_required: bool = True) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "schema": CAMPAIGN_MANIFEST_SCHEMA,
        "campaign_id": campaign_id,
        "planned_cells": list(planned_cells),
        "provider_lanes": list(provider_lanes),
        "config_hash": config_hash.lower() if isinstance(config_hash, str) else config_hash,
        "fixture_hash": fixture_hash.lower() if isinstance(fixture_hash, str) else fixture_hash,
        "target_ref": target_ref,
        "target_commit_hash": target_commit_hash.lower() if isinstance(target_commit_hash, str) else target_commit_hash,
        "target_build_hash": target_build_hash.lower() if isinstance(target_build_hash, str) else target_build_hash,
        "runtime_identity": runtime_identity,
        "expected_spend_min_micros": expected_spend_min_micros,
        "expected_spend_max_micros": expected_spend_max_micros,
        "hard_stop_micros": hard_stop_micros,
        "runaway_guard_micros": runaway_guard_micros,
        "retry_policy": retry_policy,
        "continuation_allowed": continuation_allowed,
        "action_intent_hash": action_intent_hash.lower() if isinstance(action_intent_hash, str) else action_intent_hash,
        "evidence_required": evidence_required,
    }
    manifest["manifest_hash"] = manifest_digest(manifest)
    valid, errors = validate_manifest(manifest)
    if not valid:
        raise ValueError("invalid campaign manifest: " + ", ".join(errors))
    return manifest


def validate_manifest(manifest: dict[str, Any]) -> tuple[bool, list[str]]:
    if not isinstance(manifest, dict):
        return False, ["manifest"]
    errors = _unknown(manifest, _MANIFEST_FIELDS) + _forbidden(manifest)
    if manifest.get("schema") != CAMPAIGN_MANIFEST_SCHEMA:
        errors.append("schema")
    errors.extend(_text(manifest.get("campaign_id"), "campaign_id", required=True))
    errors.extend(_list_of_text(manifest.get("planned_cells"), "planned_cells", required=True))
    errors.extend(_list_of_text(manifest.get("provider_lanes"), "provider_lanes", required=True))
    for field in ("config_hash", "fixture_hash", "target_commit_hash", "target_build_hash", "action_intent_hash"):
        value = manifest.get(field)
        if value is not None and not _is_sha256(value):
            errors.append(field)
    for field in ("target_ref", "runtime_identity"):
        errors.extend(_text(manifest.get(field), field))
    for field in ("expected_spend_min_micros", "expected_spend_max_micros", "hard_stop_micros", "runaway_guard_micros"):
        errors.extend(_nonnegative(manifest.get(field), field))
    low = manifest.get("expected_spend_min_micros")
    high = manifest.get("expected_spend_max_micros")
    hard = manifest.get("hard_stop_micros")
    runaway = manifest.get("runaway_guard_micros")
    if low is not None and high is not None and low > high:
        errors.append("expected_spend_range")
    if high is not None and hard is not None and hard < high:
        errors.append("hard_stop_below_expected_max")
    if runaway is not None and hard is not None and runaway > hard:
        errors.append("runaway_guard_above_hard_stop")
    if manifest.get("retry_policy") not in RETRY_POLICY_VALUES:
        errors.append("retry_policy")
    if not isinstance(manifest.get("continuation_allowed"), bool):
        errors.append("continuation_allowed")
    if not isinstance(manifest.get("evidence_required"), bool):
        errors.append("evidence_required")
    digest = manifest.get("manifest_hash")
    if not _is_sha256(digest):
        errors.append("manifest_hash")
    elif digest != manifest_digest(manifest):
        errors.append("manifest_hash_mismatch")
    return not errors, sorted(set(errors))


def build_check(*, campaign_id: str, cell_id: str, lane: str, status: str,
                config_hash: str, attempt: int = 1, continuation: bool = False,
                parent_attempt: Optional[int] = None,
                action_intent_hash: Optional[str] = None,
                result_hash: Optional[str] = None,
                evidence_hashes: Optional[list[str]] = None,
                usage_event_ids: Optional[list[str]] = None,
                checkpoint_ref: Optional[str] = None,
                reason_code: Optional[str] = None) -> dict[str, Any]:
    check: dict[str, Any] = {
        "schema": CAMPAIGN_CHECK_SCHEMA,
        "campaign_id": campaign_id,
        "cell_id": cell_id,
        "lane": lane,
        "status": status,
        "config_hash": config_hash.lower() if isinstance(config_hash, str) else config_hash,
        "attempt": attempt,
        "continuation": continuation,
        "parent_attempt": parent_attempt,
        "action_intent_hash": action_intent_hash.lower() if isinstance(action_intent_hash, str) else action_intent_hash,
        "result_hash": result_hash.lower() if isinstance(result_hash, str) else result_hash,
        "evidence_hashes": sorted(set(evidence_hashes or [])),
        "usage_event_ids": list(usage_event_ids or []),
        "checkpoint_ref": checkpoint_ref,
        "reason_code": reason_code,
    }
    check["check_hash"] = check_digest(check)
    valid, errors = validate_check(check)
    if not valid:
        raise ValueError("invalid campaign check: " + ", ".join(errors))
    return check


def validate_check(check: dict[str, Any]) -> tuple[bool, list[str]]:
    if not isinstance(check, dict):
        return False, ["check"]
    errors = _unknown(check, _CHECK_FIELDS) + _forbidden(check)
    if check.get("schema") != CAMPAIGN_CHECK_SCHEMA:
        errors.append("schema")
    for field in ("campaign_id", "cell_id", "lane"):
        errors.extend(_text(check.get(field), field, required=True))
    if check.get("status") not in CHECK_STATUS_VALUES:
        errors.append("status")
    if not _is_sha256(check.get("config_hash")):
        errors.append("config_hash")
    errors.extend(_nonnegative(check.get("attempt"), "attempt", required=True))
    if check.get("attempt") == 0:
        errors.append("attempt")
    if not isinstance(check.get("continuation"), bool):
        errors.append("continuation")
    parent = check.get("parent_attempt")
    if parent is not None:
        errors.extend(_nonnegative(parent, "parent_attempt"))
    for field in ("action_intent_hash", "result_hash"):
        value = check.get(field)
        if value is not None and not _is_sha256(value):
            errors.append(field)
    evidence = check.get("evidence_hashes")
    if not isinstance(evidence, list) or any(not _is_sha256(item) for item in evidence):
        errors.append("evidence_hashes")
    usage = check.get("usage_event_ids")
    if not isinstance(usage, list) or any(_text(item, "usage_event_id") for item in usage):
        errors.append("usage_event_ids")
    for field in ("checkpoint_ref", "reason_code"):
        errors.extend(_text(check.get(field), field))
    if check.get("status") in {"pass", "fail"} and not _is_sha256(check.get("result_hash")):
        errors.append("result_hash_required")
    if check.get("status") == "error" and not check.get("reason_code"):
        errors.append("reason_code_required")
    if check.get("attempt") == 1 and (check.get("continuation") or check.get("parent_attempt") is not None):
        errors.append("initial_attempt_lineage")
    if check.get("attempt", 0) > 1:
        if not check.get("continuation") or check.get("parent_attempt") != check["attempt"] - 1:
            errors.append("continuation_lineage")
        if not check.get("action_intent_hash"):
            errors.append("continuation_action_intent_required")
    digest = check.get("check_hash")
    if not _is_sha256(digest):
        errors.append("check_hash")
    elif digest != check_digest(check):
        errors.append("check_hash_mismatch")
    return not errors, sorted(set(errors))


def validate_attempt_lineage(checks: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    by_cell: dict[str, list[dict[str, Any]]] = {}
    for item in checks:
        valid, item_errors = validate_check(item)
        if not valid:
            errors.extend(item_errors)
        by_cell.setdefault(item.get("cell_id", ""), []).append(item)
    for items in by_cell.values():
        items.sort(key=lambda item: item.get("attempt", 0))
        for index, item in enumerate(items):
            if index == 0:
                if item.get("attempt") != 1:
                    errors.append("attempt_sequence")
                continue
            prior = items[index - 1]
            if item.get("attempt") != prior.get("attempt", 0) + 1:
                errors.append("attempt_sequence")
            if prior.get("status") == "pass" and not item.get("continuation"):
                errors.append("completed_cell_duplicate")
            if item.get("continuation"):
                if item.get("config_hash") == prior.get("config_hash"):
                    errors.append("continuation_config_unchanged")
                if item.get("action_intent_hash") == prior.get("action_intent_hash"):
                    errors.append("continuation_action_unchanged")
    return not errors, sorted(set(errors))


def _target_status(framework_status: str, checks: list[dict[str, Any]]) -> str:
    executed = [item for item in checks if item.get("status") != "skip"]
    if framework_status != "completed":
        return "not_run" if not executed else "inconclusive"
    if not executed:
        return "inconclusive"
    if any(item.get("status") == "fail" for item in executed):
        return "fail"
    if any(item.get("status") == "error" for item in executed):
        return "inconclusive"
    return "pass"


def build_receipt(*, manifest: dict[str, Any], checks: list[dict[str, Any]],
                  framework_status: str, target_status: Optional[str] = None,
                  budget_status: Optional[str] = None,
                  evidence_status: str = "pending",
                  finalization_status: str = "complete",
                  finalization_reason: Optional[str] = None,
                  target_identity: Optional[dict[str, Any]] = None,
                  evidence_bundle_hash: Optional[str] = None,
                  protected_state_before_hash: Optional[str] = None,
                  protected_state_after_hash: Optional[str] = None,
                  cleanup_manifest_hash: Optional[str] = None,
                  spent_micros: int = 0, remaining_micros: Optional[int] = None,
                  stop_reason: Optional[str] = None,
                  last_checkpoint_ref: Optional[str] = None) -> dict[str, Any]:
    valid, errors = validate_manifest(manifest)
    if not valid:
        raise ValueError("invalid campaign manifest: " + ", ".join(errors))
    if framework_status not in FRAMEWORK_STATUS_VALUES:
        raise ValueError("invalid framework_status")
    if _nonnegative(spent_micros, "spent_micros", required=True):
        raise ValueError("spent_micros must be a non-negative integer")
    if remaining_micros is not None and _nonnegative(remaining_micros, "remaining_micros"):
        raise ValueError("remaining_micros must be a non-negative integer")
    if evidence_status not in EVIDENCE_STATUS_VALUES:
        raise ValueError("invalid evidence_status")
    if finalization_status not in FINALIZATION_STATUS_VALUES:
        raise ValueError("invalid finalization_status")
    for item in checks:
        valid, errors = validate_check(item)
        if not valid:
            raise ValueError("invalid campaign check: " + ", ".join(errors))
        if item["campaign_id"] != manifest["campaign_id"]:
            raise ValueError("check campaign_id does not match manifest")
        if item["cell_id"] not in manifest["planned_cells"]:
            raise ValueError("check cell_id is not planned")
    checks = sorted(checks, key=lambda item: (item["cell_id"], item["attempt"]))
    valid, errors = validate_attempt_lineage(checks)
    if not valid:
        raise ValueError("invalid campaign attempt lineage: " + ", ".join(errors))
    if any(item.get("attempt", 1) > 1 for item in checks) and not manifest.get("continuation_allowed"):
        raise ValueError("campaign continuation is not allowed by the manifest")
    derived_target_status = _target_status(framework_status, checks)
    if target_status is None:
        target_status = derived_target_status
    elif target_status != derived_target_status:
        raise ValueError("target_status does not match framework/check outcomes")
    if target_status not in TARGET_STATUS_VALUES:
        raise ValueError("invalid target_status")
    if budget_status is None:
        if manifest.get("hard_stop_micros") is None:
            budget_status = "not_configured"
        elif spent_micros > manifest["hard_stop_micros"]:
            budget_status = "overrun"
        else:
            budget_status = "within_guard"
    if budget_status not in BUDGET_STATUS_VALUES:
        raise ValueError("invalid budget_status")
    if remaining_micros is None and manifest.get("hard_stop_micros") is not None:
        remaining_micros = max(0, manifest["hard_stop_micros"] - spent_micros)
    counts = {
        "planned": len(manifest["planned_cells"]),
        "executed": sum(item["status"] != "skip" for item in checks),
        "passed": sum(item["status"] == "pass" for item in checks),
        "failed": sum(item["status"] in {"fail", "error"} for item in checks),
        "skipped": sum(item["status"] == "skip" for item in checks),
    }
    receipt: dict[str, Any] = {
        "schema": CAMPAIGN_RECEIPT_SCHEMA,
        "campaign_id": manifest["campaign_id"],
        "manifest_hash": manifest["manifest_hash"],
        "framework_status": framework_status,
        "target_status": target_status,
        "budget_status": budget_status,
        "evidence_status": evidence_status,
        "finalization_status": finalization_status,
        "finalization_reason": finalization_reason,
        "counts": counts,
        "check_hashes": [item["check_hash"] for item in checks],
        "target_identity": target_identity or {},
        "evidence_bundle_hash": evidence_bundle_hash,
        "protected_state_before_hash": protected_state_before_hash,
        "protected_state_after_hash": protected_state_after_hash,
        "cleanup_manifest_hash": cleanup_manifest_hash,
        "spent_micros": spent_micros,
        "remaining_micros": remaining_micros,
        "stop_reason": stop_reason,
        "last_checkpoint_ref": last_checkpoint_ref,
    }
    receipt["receipt_hash"] = receipt_digest(receipt)
    receipt["verification"] = verify_campaign_receipt(receipt, manifest=manifest, checks=checks)
    return receipt


def verify_campaign_receipt(receipt: dict[str, Any], *, manifest: Optional[dict[str, Any]] = None,
                            checks: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    reasons: list[str] = []
    if not isinstance(receipt, dict):
        return {"valid": False, "verified_pass": False, "reasons": ["receipt"]}
    reasons.extend(_unknown(receipt, _RECEIPT_FIELDS))
    reasons.extend(_forbidden(receipt))
    if receipt.get("schema") != CAMPAIGN_RECEIPT_SCHEMA:
        reasons.append("schema")
    digest = receipt.get("receipt_hash")
    if not _is_sha256(digest):
        reasons.append("receipt_hash")
    elif digest != receipt_digest(receipt):
        reasons.append("receipt_hash_mismatch")
    if manifest is not None:
        ok, manifest_errors = validate_manifest(manifest)
        if not ok:
            reasons.extend("manifest:" + item for item in manifest_errors)
        elif receipt.get("manifest_hash") != manifest.get("manifest_hash"):
            reasons.append("manifest_hash_mismatch")
    if checks is not None:
        ok, check_errors = validate_attempt_lineage(checks)
        if not ok:
            reasons.extend("checks:" + item for item in check_errors)
        expected = [item.get("check_hash") for item in checks]
        if receipt.get("check_hashes") != expected:
            reasons.append("check_hashes_mismatch")
    framework = receipt.get("framework_status")
    target = receipt.get("target_status")
    budget = receipt.get("budget_status")
    evidence = receipt.get("evidence_status")
    finalization = receipt.get("finalization_status")
    if framework not in FRAMEWORK_STATUS_VALUES:
        reasons.append("framework_status")
    if target not in TARGET_STATUS_VALUES:
        reasons.append("target_status")
    if budget not in BUDGET_STATUS_VALUES:
        reasons.append("budget_status")
    if evidence not in EVIDENCE_STATUS_VALUES:
        reasons.append("evidence_status")
    if finalization not in FINALIZATION_STATUS_VALUES:
        reasons.append("finalization_status")
    if _nonnegative(receipt.get("spent_micros"), "spent_micros", required=True):
        reasons.append("spent_micros")
    if receipt.get("remaining_micros") is not None and _nonnegative(receipt.get("remaining_micros"), "remaining_micros"):
        reasons.append("remaining_micros")
    counts = receipt.get("counts")
    count_keys = ("planned", "executed", "passed", "failed", "skipped")
    if not isinstance(counts, dict) or any(not isinstance(counts.get(key), int) for key in count_keys):
        reasons.append("counts")
        counts = {}
    if checks is not None:
        expected_counts = {
            "planned": len(manifest["planned_cells"]) if manifest is not None else counts.get("planned", 0),
            "executed": sum(item.get("status") != "skip" for item in checks),
            "passed": sum(item.get("status") == "pass" for item in checks),
            "failed": sum(item.get("status") in {"fail", "error"} for item in checks),
            "skipped": sum(item.get("status") == "skip" for item in checks),
        }
        if any(counts.get(key) != value for key, value in expected_counts.items()):
            reasons.append("counts_mismatch")
        expected_target = _target_status(framework, checks)
        if target != expected_target:
            reasons.append("target_status_mismatch")
        if target == "pass":
            for item in checks:
                if item.get("status") != "pass":
                    continue
                if manifest is not None and manifest.get("evidence_required") and not item.get("evidence_hashes"):
                    reasons.append("evidence_missing")
                if not item.get("usage_event_ids"):
                    reasons.append("usage_binding_missing")
    status_reasons: list[str] = []
    if framework != "completed":
        status_reasons.append("framework_not_completed")
    if counts.get("executed", 0) == 0:
        status_reasons.append("no_executed_checks")
    if target != "pass":
        status_reasons.append("target_not_pass")
    if budget in {"stopped", "overrun"}:
        status_reasons.append("budget_not_within_guard")
    if evidence != "complete":
        status_reasons.append("evidence_not_complete")
    if finalization != "complete":
        status_reasons.append("finalization_failed" if finalization == "failed" else "finalization_pending")
    reasons_all = sorted(set(reasons + status_reasons))
    return {
        "valid": not reasons,
        "verified_pass": not reasons and not status_reasons,
        "reasons": reasons_all,
        "integrity_ok": not reasons,
    }


def admit_spend(manifest: dict[str, Any], *, spent_micros: int, proposed_micros: int) -> dict[str, Any]:
    valid, errors = validate_manifest(manifest)
    if not valid:
        raise ValueError("invalid campaign manifest: " + ", ".join(errors))
    if spent_micros < 0 or proposed_micros < 0:
        raise ValueError("spend values must be non-negative")
    if manifest.get("runaway_guard_micros") is not None:
        remaining = manifest["runaway_guard_micros"] - spent_micros
        if proposed_micros > remaining:
            return {"allowed": False, "reason": "runaway_guard_exceeded", "remaining_micros": max(0, remaining)}
    if manifest.get("hard_stop_micros") is not None:
        remaining = manifest["hard_stop_micros"] - spent_micros
        if proposed_micros > remaining:
            return {"allowed": False, "reason": "hard_stop_exceeded", "remaining_micros": max(0, remaining)}
    hard = manifest.get("hard_stop_micros")
    return {
        "allowed": True,
        "reason": "within_guard",
        "remaining_micros": (hard - spent_micros - proposed_micros) if hard is not None else None,
    }


def record_usage(conn, org_id: str, *, campaign_binding: dict, **kwargs):
    """Record one campaign-bound usage event with atomic budget-stop custody.

    The normal metering function remains the low-level writer. This wrapper owns
    the transaction and, on a rejected spend, records the durable stop state in
    a second transaction so the failed attempt cannot disappear with the rolled
    back usage insert.
    """
    from . import db, metering
    try:
        with db.immediate(conn):
            return metering.record_usage(
                conn, org_id, campaign_binding=campaign_binding,
                commit=False, **kwargs,
            )
    except CampaignBudgetError as exc:
        with db.immediate(conn):
            db.mark_campaign_budget_stop(
                conn, org_id, exc.campaign_id,
                remaining_micros=exc.remaining_micros,
                reason=exc.reason,
            )
        raise


__all__ = [
    "CAMPAIGN_MANIFEST_SCHEMA", "CAMPAIGN_CHECK_SCHEMA", "CAMPAIGN_RECEIPT_SCHEMA",
    "CampaignBudgetError",
    "FRAMEWORK_STATUS_VALUES", "TARGET_STATUS_VALUES", "CHECK_STATUS_VALUES",
    "BUDGET_STATUS_VALUES", "EVIDENCE_STATUS_VALUES", "FINALIZATION_STATUS_VALUES",
    "CAMPAIGN_BINDING_SCHEMA", "build_binding", "validate_binding", "binding_digest",
    "build_manifest", "validate_manifest", "manifest_digest", "build_check",
    "validate_check", "check_digest", "validate_attempt_lineage", "build_receipt",
    "verify_campaign_receipt", "receipt_digest", "admit_spend",
]
