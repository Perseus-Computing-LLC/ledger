"""Hash-bound CUI-safe context publication decisions (#260).

This module is a pure contract layer. It records opaque references and digests
for a source, safe projection, redaction/certification receipt, policy,
authority, destination, and released artifact. It does not classify legal CUI,
transport content, or replace Vault policy. External publication is allowed
only when every required evidence and authority predicate is explicit.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

CONTEXT_RELEASE_SCHEMA = "perseus-ledger-context-release-decision/v1"
OUTBOX_RECEIPT_SCHEMA = "perseus-ledger-publication-outbox-receipt/v1"
TOMBSTONE_SCHEMA = "perseus-ledger-publication-tombstone/v1"

HANDLING_PROFILES = (
    "PUBLIC_SAFE", "INTERNAL_ONLY", "CUI_LIKE", "EXPORT_CONTROLLED_SIGNAL",
    "REVIEW_REQUIRED", "UNKNOWN",
)
DECISION_STATES = (
    "PENDING_REVIEW", "APPROVED_INTERNAL", "APPROVED_EXTERNAL", "DENIED",
    "EXPIRED", "REVOKED", "UNAVAILABLE", "TAMPERED",
)
EVIDENCE_STATES = (
    "fresh", "partial", "stale", "missing", "unknown", "unavailable",
    "incomplete", "tampered", "superseded", "unreported",
)
CLASSIFIER_STATES = ("available", "unavailable", "unknown")
REDACTION_STATES = ("complete", "incomplete", "unavailable", "tampered", "unknown")
AUTHORITY_STATES = ("active", "revoked", "expired", "unknown")
REVOCATION_STATES = ("not-revoked", "revoked", "unknown")
AUDIENCE_CLASSES = (
    "INTERNAL_AGENT", "INTERNAL_WORKSPACE", "PROPOSAL_PARTNER", "PUBLIC_SITE",
    "EXTERNAL_EMAIL", "PROGRAM_BOUNDARY",
)
TOMBSTONE_REASONS = (
    "REVOKED", "EXPIRED", "SUPERSEDED", "SCOPE_WITHDRAWN", "TAMPERED",
    "ADMIN_WITHDRAWN",
)

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+#%?=&,-]{0,1023}$")
_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9._:/+,-]{0,127}$")
_OSCAL_UUID_NAMESPACE = uuid.UUID("d7a820cc-6a1e-5cfb-98e9-6c4d3ea4d11f")
_FORBIDDEN_KEYS = {
    "prompt", "prompts", "body", "content", "memory", "memory_body",
    "tool_args", "tool_arguments", "arguments", "response", "provider_payload",
    "provider_response", "raw_payload", "raw_content", "authorization",
    "credential", "credentials", "token", "secret", "password", "private_key",
    "access_token", "refresh_token", "bearer_token",
}
_OPTIONAL_FIELDS = {
    "revocation_ref", "oscal_evidence_refs", "previous_decision_hash",
}
_REQUIRED_FIELDS = {
    "schema", "decision_id", "source_ref", "source_digest", "projection_ref",
    "projection_digest", "handling_profile", "redaction_receipt_ref",
    "redaction_receipt_digest", "policy_version", "policy_digest", "authority_ref",
    "authority_digest", "authority_state", "scope_anchor", "destination_scope",
    "audience_class", "requester", "approver", "capability", "purpose", "decision_at",
    "expires_at", "decision_state", "classifier_state", "redaction_state",
    "evidence_state", "revocation_state", "released_artifact_digest",
    "idempotency_key", "publication_revision", "decision_hash",
}
_ALLOWED_FIELDS = _REQUIRED_FIELDS | _OPTIONAL_FIELDS
_OUTBOX_FIELDS = {
    "schema", "receipt_id", "decision_id", "decision_hash", "source_digest",
    "projection_digest", "released_artifact_digest", "destination_scope",
    "audience_class", "delivered_at", "transport_receipt_ref",
    "transport_receipt_digest", "publication_status", "receipt_hash",
}
_TOMBSTONE_FIELDS = {
    "schema", "tombstone_id", "decision_id", "decision_hash", "source_digest",
    "projection_digest", "released_artifact_digest", "destination_scope", "reason",
    "tombstone_at", "revocation_ref", "content_free", "resurrection_blocked",
    "tombstone_hash",
}
_MAX_OSCAL_EVIDENCE_REFS = 32
_MAX_HISTORY_RECORDS = 256
_MAX_VALIDATION_DEPTH = 64


def _bounded_list(values: Iterable[Any] | None, *, limit: int, field: str) -> list[Any]:
    if values is None:
        return []
    if isinstance(values, (list, tuple)):
        if len(values) > limit:
            raise ValueError(f"{field} exceeds {limit} items")
        return list(values)
    try:
        iterator = iter(values)
    except TypeError as exc:
        raise ValueError(f"{field} must be iterable") from exc
    bounded: list[Any] = []
    for _ in range(limit + 1):
        try:
            bounded.append(next(iterator))
        except StopIteration:
            return bounded
        except Exception as exc:
            raise ValueError(f"{field} iterator failed") from exc
    raise ValueError(f"{field} exceeds {limit} items")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _digest(value: Any, field: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{field} must be a 64-character SHA-256 hex digest")
    return value.lower()


def _text(value: Any, field: str, *, max_len: int = 512, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    if len(value) > max_len:
        raise ValueError(f"{field} exceeds {max_len} characters")
    if "\r" in value or "\n" in value:
        raise ValueError(f"{field} must not contain newlines")
    return value


def _opaque_label(value: Any, field: str, *, max_len: int = 1024, optional: bool = False) -> str | None:
    value = _text(value, field, max_len=max_len, optional=optional)
    if value is None:
        return None
    if not _LABEL_RE.fullmatch(value):
        raise ValueError(f"{field} must be an opaque label/reference without whitespace")
    return value


def _purpose_code(value: Any, field: str, *, optional: bool = False) -> str | None:
    value = _text(value, field, max_len=128, optional=optional)
    if value is None:
        return None
    if not _CODE_RE.fullmatch(value):
        raise ValueError(f"{field} must be a bounded opaque lower-case code")
    return value


def _parse_timestamp(value: Any, field: str) -> datetime:
    value = _text(value, field, max_len=64)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _timestamp(value: Any, field: str) -> str:
    parsed = _parse_timestamp(value, field)
    timespec = "microseconds" if parsed.microsecond else "seconds"
    return parsed.isoformat(timespec=timespec).replace("+00:00", "Z")


def _find_forbidden(
    value: Any,
    path: str = "",
    _seen: set[int] | None = None,
    _depth: int = 0,
) -> list[str]:
    if _depth > _MAX_VALIDATION_DEPTH:
        return [f"depth:{path}"]
    seen = set() if _seen is None else _seen
    errors: list[str] = []
    if isinstance(value, Mapping):
        marker = id(value)
        if marker in seen:
            return [f"cycle:{path}"]
        seen.add(marker)
        try:
            for key, child in value.items():
                key_text = str(key).lower()
                child_path = f"{path}.{key}" if path else str(key)
                if key_text in _FORBIDDEN_KEYS or key_text.startswith("raw_"):
                    errors.append(f"forbidden:{child_path}")
                if key_text == "oscal_evidence_refs" and isinstance(child, list) and len(child) > _MAX_OSCAL_EVIDENCE_REFS:
                    errors.append(f"{child_path}.too_many")
                    continue
                errors.extend(_find_forbidden(child, child_path, seen, _depth + 1))
        finally:
            seen.remove(marker)
    elif isinstance(value, list):
        marker = id(value)
        if marker in seen:
            return [f"cycle:{path}"]
        seen.add(marker)
        try:
            for index, child in enumerate(value):
                errors.extend(_find_forbidden(child, f"{path}[{index}]", seen, _depth + 1))
        finally:
            seen.remove(marker)
    return errors


def _stable_id(kind: str, *parts: str) -> str:
    value = "|".join((CONTEXT_RELEASE_SCHEMA, kind, *parts))
    return str(uuid.uuid5(_OSCAL_UUID_NAMESPACE, value))


def decision_digest(decision: Mapping[str, Any]) -> str:
    return _sha({key: value for key, value in decision.items() if key != "decision_hash"})


def _decision_hash_matches(decision: Mapping[str, Any], claimed: str) -> bool:
    optional_defaults = (
        ("revocation_ref", None),
        ("previous_decision_hash", None),
        ("oscal_evidence_refs", []),
    )
    try:
        if claimed.lower() == decision_digest(decision):
            return True
        for mask in range(1, 1 << len(optional_defaults)):
            legacy = dict(decision)
            for index, (field, default) in enumerate(optional_defaults):
                if mask & (1 << index) and legacy.get(field) == default:
                    legacy.pop(field, None)
            if claimed.lower() == decision_digest(legacy):
                return True
    except (OverflowError, RecursionError, TypeError, ValueError):
        return False
    return False


def _ref_digest_errors(value: Any, path: str) -> list[str]:
    if not isinstance(value, list):
        return [path]
    if len(value) > _MAX_OSCAL_EVIDENCE_REFS:
        return [f"{path}.too_many"]
    errors: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            errors.append(f"{path}[{index}]")
            continue
        errors.extend(_find_forbidden(item, f"{path}[{index}]") )
        if set(item) != {"ref", "digest"}:
            errors.extend(
                f"unknown:{path}[{index}].{key}"
                for key in sorted(set(item) - {"ref", "digest"}, key=str)
            )
        try:
            _opaque_label(item.get("ref"), f"{path}[{index}].ref", max_len=1024)
        except ValueError:
            errors.append(f"{path}[{index}].ref")
        try:
            _digest(item.get("digest"), f"{path}[{index}].digest")
        except ValueError:
            errors.append(f"{path}[{index}].digest")
    return errors


def _decision_errors(decision: Any) -> list[str]:
    if not isinstance(decision, Mapping):
        return ["decision"]
    errors = _find_forbidden(decision)
    unknown_fields = set(decision) - _ALLOWED_FIELDS
    errors.extend(f"unknown:{key}" for key in sorted(unknown_fields, key=str))
    if decision.get("schema") != CONTEXT_RELEASE_SCHEMA:
        errors.append("schema")
    label_fields = (
        ("decision_id", 1024), ("source_ref", 1024), ("projection_ref", 1024),
        ("redaction_receipt_ref", 1024), ("policy_version", 512), ("authority_ref", 1024),
        ("scope_anchor", 1024), ("destination_scope", 1024), ("requester", 512),
        ("approver", 512), ("idempotency_key", 256),
    )
    for field, max_len in label_fields:
        try:
            _opaque_label(decision.get(field), field, max_len=max_len)
        except ValueError:
            errors.append(field)
    try:
        _opaque_label(decision.get("revocation_ref"), "revocation_ref", max_len=1024, optional=True)
    except ValueError:
        errors.append("revocation_ref")
    for field in ("capability", "purpose"):
        try:
            _purpose_code(decision.get(field), field)
        except ValueError:
            errors.append(field)
    for field in (
        "source_digest", "projection_digest", "redaction_receipt_digest", "policy_digest",
        "authority_digest", "released_artifact_digest", "previous_decision_hash",
    ):
        try:
            _digest(decision.get(field), field, optional=field in {"released_artifact_digest", "previous_decision_hash"})
        except ValueError:
            errors.append(field)
    for field in ("decision_at", "expires_at"):
        try:
            _timestamp(decision.get(field), field)
        except ValueError:
            errors.append(field)
    if isinstance(decision.get("decision_at"), str) and isinstance(decision.get("expires_at"), str):
        try:
            if _parse_timestamp(decision["expires_at"], "expires_at") < _parse_timestamp(decision["decision_at"], "decision_at"):
                errors.append("expiry_not_after_decision")
        except ValueError:
            pass
    for field, values in (
        ("handling_profile", HANDLING_PROFILES),
        ("decision_state", DECISION_STATES),
        ("evidence_state", EVIDENCE_STATES),
        ("classifier_state", CLASSIFIER_STATES),
        ("redaction_state", REDACTION_STATES),
        ("authority_state", AUTHORITY_STATES),
        ("revocation_state", REVOCATION_STATES),
        ("audience_class", AUDIENCE_CLASSES),
    ):
        if decision.get(field) not in values:
            errors.append(field)
    revision = decision.get("publication_revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        errors.append("publication_revision")
    errors.extend(_ref_digest_errors(decision.get("oscal_evidence_refs", []), "oscal_evidence_refs"))
    digest = decision.get("decision_hash")
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        errors.append("decision_hash")
    elif not _decision_hash_matches(decision, digest):
        errors.append("decision_hash_mismatch")
    return sorted(set(errors))


def validate_context_release_decision(decision: Mapping[str, Any]) -> tuple[bool, list[str]]:
    try:
        errors = _decision_errors(decision)
    except Exception:
        return False, ["malformed_decision"]
    return not errors, errors


def build_context_release_decision(
    *,
    decision_id: str,
    source_ref: str,
    source_digest: str,
    projection_ref: str,
    projection_digest: str,
    handling_profile: str,
    redaction_receipt_ref: str,
    redaction_receipt_digest: str,
    policy_version: str,
    policy_digest: str,
    authority_ref: str,
    authority_digest: str,
    authority_state: str,
    scope_anchor: str,
    destination_scope: str,
    audience_class: str,
    requester: str,
    approver: str,
    capability: str,
    purpose: str,
    decision_at: str,
    expires_at: str,
    decision_state: str,
    classifier_state: str,
    redaction_state: str,
    evidence_state: str,
    revocation_state: str,
    released_artifact_digest: str | None,
    idempotency_key: str,
    publication_revision: int,
    revocation_ref: str | None = None,
    previous_decision_hash: str | None = None,
    oscal_evidence_refs: Iterable[Mapping[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    if extra:
        forbidden = _find_forbidden(extra)
        if forbidden:
            raise ValueError("invalid context release decision: " + ", ".join(forbidden))
        unknown = sorted(set(extra) - {"decision_hash"})
        if unknown:
            raise ValueError("invalid context release decision: unknown:" + ", ".join(unknown))
    refs = []
    raw_refs = _bounded_list(
        oscal_evidence_refs,
        limit=_MAX_OSCAL_EVIDENCE_REFS,
        field="oscal_evidence_refs",
    )
    for item in raw_refs:
        if not isinstance(item, Mapping):
            raise ValueError("OSCAL evidence references must be objects")
        unknown = set(item) - {"ref", "digest"}
        if unknown:
            names = ", ".join(sorted((str(key) for key in unknown)))
            raise ValueError("invalid OSCAL evidence reference: unknown:" + names)
        refs.append({
            "ref": _opaque_label(item.get("ref"), "oscal_evidence_refs.ref", max_len=1024),
            "digest": _digest(item.get("digest"), "oscal_evidence_refs.digest"),
        })
    refs.sort(key=lambda item: (item["ref"], item["digest"]))
    decision: dict[str, Any] = {
        "schema": CONTEXT_RELEASE_SCHEMA,
        "decision_id": _opaque_label(decision_id, "decision_id", max_len=1024),
        "source_ref": _opaque_label(source_ref, "source_ref", max_len=1024),
        "source_digest": _digest(source_digest, "source_digest"),
        "projection_ref": _opaque_label(projection_ref, "projection_ref", max_len=1024),
        "projection_digest": _digest(projection_digest, "projection_digest"),
        "handling_profile": handling_profile,
        "redaction_receipt_ref": _opaque_label(redaction_receipt_ref, "redaction_receipt_ref", max_len=1024),
        "redaction_receipt_digest": _digest(redaction_receipt_digest, "redaction_receipt_digest"),
        "policy_version": _opaque_label(policy_version, "policy_version", max_len=512),
        "policy_digest": _digest(policy_digest, "policy_digest"),
        "authority_ref": _opaque_label(authority_ref, "authority_ref", max_len=1024),
        "authority_digest": _digest(authority_digest, "authority_digest"),
        "authority_state": authority_state,
        "scope_anchor": _opaque_label(scope_anchor, "scope_anchor", max_len=1024),
        "destination_scope": _opaque_label(destination_scope, "destination_scope", max_len=1024),
        "audience_class": audience_class,
        "requester": _opaque_label(requester, "requester", max_len=512),
        "approver": _opaque_label(approver, "approver", max_len=512),
        "capability": _purpose_code(capability, "capability"),
        "purpose": _purpose_code(purpose, "purpose"),
        "decision_at": _timestamp(decision_at, "decision_at"),
        "expires_at": _timestamp(expires_at, "expires_at"),
        "decision_state": decision_state,
        "classifier_state": classifier_state,
        "redaction_state": redaction_state,
        "evidence_state": evidence_state,
        "revocation_state": revocation_state,
        "revocation_ref": _opaque_label(revocation_ref, "revocation_ref", max_len=1024, optional=True),
        "released_artifact_digest": _digest(released_artifact_digest, "released_artifact_digest", optional=True),
        "idempotency_key": _opaque_label(idempotency_key, "idempotency_key", max_len=256),
        "publication_revision": publication_revision,
        "previous_decision_hash": _digest(previous_decision_hash, "previous_decision_hash", optional=True),
        "oscal_evidence_refs": refs,
    }
    decision["decision_hash"] = decision_digest(decision)
    valid, errors = validate_context_release_decision(decision)
    if not valid:
        raise ValueError("invalid context release decision: " + ", ".join(errors))
    return decision


def read_context_release_decision(value: Mapping[str, Any]) -> dict[str, Any]:
    """Read v1 records while defaulting fields added after the first writer.

    The hash is checked over the exact bytes/fields supplied by the older
    writer before defaults are added. Missing fields are reported to callers;
    publication admission still requires fresh evidence and an explicit
    external-safe evidence reference, so compatibility never becomes an
    authorization upgrade.
    """
    if not isinstance(value, Mapping):
        return {"valid": False, "hash_valid": False, "decision": None, "legacy_fields_missing": []}
    raw = dict(value)
    missing = sorted(field for field in _OPTIONAL_FIELDS if field not in raw)
    errors = _decision_errors(raw)
    hash_valid = "decision_hash_mismatch" not in errors and "decision_hash" not in errors
    normalized = dict(raw)
    normalized.setdefault("revocation_ref", None)
    normalized.setdefault("previous_decision_hash", None)
    normalized.setdefault("oscal_evidence_refs", [])
    # Re-run structural validation without requiring normalized defaults to have
    # the old hash; the original hash was already verified above.
    structural_errors = [error for error in errors if error not in {"decision_hash_mismatch"}]
    return {
        "valid": not structural_errors and hash_valid,
        "hash_valid": hash_valid,
        "decision": normalized,
        "legacy_fields_missing": missing,
    }


def _result(decision: Any, state: str, reason: str, **extra: Any) -> dict[str, Any]:
    try:
        decision_hash = decision.get("decision_hash") if isinstance(decision, Mapping) else None
    except Exception:
        decision_hash = None
    result = {"allowed": False, "state": state, "reason": reason, "decision_hash": decision_hash}
    result.update(extra)
    return result


def _tombstone_matches(decision: Mapping[str, Any], tombstone: Mapping[str, Any]) -> bool:
    return (
        tombstone.get("source_digest") == decision.get("source_digest")
        and tombstone.get("projection_digest") == decision.get("projection_digest")
        and tombstone.get("destination_scope") == decision.get("destination_scope")
    ) or tombstone.get("decision_hash") == decision.get("decision_hash")


def evaluate_publication(
    decision: Mapping[str, Any],
    *,
    now: str,
    required_scope: str,
    required_destination: str,
    external_release: bool,
    expected_projection_digest: str | None = None,
    tombstones: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    valid, errors = validate_context_release_decision(decision)
    if not valid:
        return _result(decision, "TAMPERED", "decision_hash_invalid", validation_errors=errors)
    if type(external_release) is not bool:
        return _result(decision, "DENIED", "invalid_external_release_flag")
    try:
        tombstone_list = _bounded_list(
            tombstones,
            limit=_MAX_HISTORY_RECORDS,
            field="tombstones",
        )
    except ValueError:
        return _result(decision, "TAMPERED", "tombstone_history_too_large")
    try:
        current = _parse_timestamp(now, "now")
        decision_at = _parse_timestamp(decision["decision_at"], "decision_at")
        expires_at = _parse_timestamp(decision["expires_at"], "expires_at")
    except ValueError:
        return _result(decision, "DENIED", "invalid_admission_time")
    for tombstone in tombstone_list:
        try:
            tomb_valid, tomb_errors = _validate_tombstone(
                tombstone,
                require_decision_binding=False,
            )
        except Exception:
            tomb_valid, tomb_errors = False, ["malformed_tombstone"]
        if not tomb_valid:
            return _result(decision, "TAMPERED", "tombstone_invalid", validation_errors=tomb_errors)
        if _tombstone_matches(decision, tombstone):
            return _result(decision, "DENIED", "publication_tombstoned")
    if decision["authority_state"] == "revoked" or decision["revocation_state"] == "revoked" or decision["decision_state"] == "REVOKED":
        return _result(decision, "REVOKED", "authority_revoked")
    if decision["revocation_state"] != "not-revoked":
        return _result(decision, "DENIED", "revocation_state_not_clear")
    if decision["decision_state"] == "TAMPERED":
        return _result(decision, "TAMPERED", "decision_marked_tampered")
    if current >= expires_at:
        return _result(decision, "EXPIRED", "approval_expired")
    if current < decision_at:
        return _result(decision, "DENIED", "decision_not_yet_effective")
    if decision["decision_state"] not in {"APPROVED_INTERNAL", "APPROVED_EXTERNAL"}:
        return _result(decision, "DENIED", "decision_not_approved")
    if external_release and decision["decision_state"] != "APPROVED_EXTERNAL":
        return _result(decision, "DENIED", "external_approval_required")
    if decision["evidence_state"] != "fresh":
        return _result(decision, "DENIED", "evidence_not_fresh")
    if decision["classifier_state"] != "available":
        return _result(decision, "DENIED", "classifier_unavailable")
    if decision["redaction_state"] != "complete":
        return _result(decision, "DENIED", "redaction_" + decision["redaction_state"])
    if not decision["redaction_receipt_ref"] or not decision["redaction_receipt_digest"]:
        return _result(decision, "DENIED", "redaction_evidence_missing")
    if decision["authority_state"] != "active":
        return _result(decision, "DENIED", "authority_not_active")
    if decision["scope_anchor"] != required_scope:
        return _result(decision, "DENIED", "scope_mismatch")
    if decision["destination_scope"] != required_destination:
        return _result(decision, "DENIED", "destination_mismatch")
    if decision["requester"] == decision["approver"]:
        return _result(decision, "DENIED", "requester_approver_not_separate")
    if expected_projection_digest is not None and decision["projection_digest"] != expected_projection_digest:
        return _result(decision, "DENIED", "projection_digest_mismatch")
    if external_release:
        if decision["handling_profile"] != "PUBLIC_SAFE":
            return _result(decision, "DENIED", "handling_profile_not_exportable")
        if decision["audience_class"] in {"INTERNAL_AGENT", "INTERNAL_WORKSPACE"}:
            return _result(decision, "DENIED", "external_destination_required")
        if not decision["released_artifact_digest"]:
            return _result(decision, "DENIED", "released_artifact_missing")
        if not decision["oscal_evidence_refs"]:
            return _result(decision, "DENIED", "oscal_evidence_missing")
        return {
            "allowed": True,
            "state": "APPROVED_EXTERNAL",
            "reason": "authorized_external_release",
            "publication_status": "OUTBOX_PENDING",
            "requires_outbox_receipt": True,
            "decision_hash": decision["decision_hash"],
        }
    return {
        "allowed": True,
        "state": decision["decision_state"],
        "reason": "authorized_internal_visibility",
        "publication_status": "NOT_ATTEMPTED",
        "requires_outbox_receipt": False,
        "decision_hash": decision["decision_hash"],
    }


def outbox_receipt_digest(receipt: Mapping[str, Any]) -> str:
    return _sha({key: value for key, value in receipt.items() if key != "receipt_hash"})


def build_outbox_receipt(
    decision: Mapping[str, Any],
    *,
    tombstones: Iterable[Mapping[str, Any]] | None = None,
    delivered_at: str,
    transport_receipt_ref: str,
    transport_receipt_digest: str,
) -> dict[str, Any]:
    valid, errors = validate_context_release_decision(decision)
    if not valid:
        raise ValueError("cannot create outbox receipt: " + ", ".join(errors))
    if decision["decision_state"] != "APPROVED_EXTERNAL" or not decision["released_artifact_digest"]:
        raise ValueError("outbox receipt requires an approved external release artifact")
    if tombstones is None:
        raise ValueError("outbox receipt requires a tombstone snapshot")
    try:
        tombstone_list = _bounded_list(
            tombstones,
            limit=_MAX_HISTORY_RECORDS,
            field="tombstones",
        )
    except ValueError as exc:
        raise ValueError("invalid tombstone snapshot") from exc
    admission = evaluate_publication(
        decision,
        now=delivered_at,
        required_scope=decision["scope_anchor"],
        required_destination=decision["destination_scope"],
        external_release=True,
        expected_projection_digest=decision["projection_digest"],
        tombstones=tombstone_list,
    )
    if not admission["allowed"]:
        raise ValueError("outbox receipt requires admitted external publication: " + admission["reason"])
    receipt: dict[str, Any] = {
        "schema": OUTBOX_RECEIPT_SCHEMA,
        "receipt_id": _stable_id("outbox", decision["decision_id"], decision["decision_hash"]),
        "decision_id": decision["decision_id"],
        "decision_hash": decision["decision_hash"],
        "source_digest": decision["source_digest"],
        "projection_digest": decision["projection_digest"],
        "released_artifact_digest": decision["released_artifact_digest"],
        "destination_scope": decision["destination_scope"],
        "audience_class": decision["audience_class"],
        "delivered_at": _timestamp(delivered_at, "delivered_at"),
        "transport_receipt_ref": _opaque_label(transport_receipt_ref, "transport_receipt_ref", max_len=1024),
        "transport_receipt_digest": _digest(transport_receipt_digest, "transport_receipt_digest"),
        "publication_status": "DELIVERY_CONFIRMED",
    }
    receipt["receipt_hash"] = outbox_receipt_digest(receipt)
    receipt_valid, receipt_errors = validate_outbox_receipt(receipt, decision=decision)
    if not receipt_valid:
        raise ValueError("invalid outbox receipt: " + ", ".join(receipt_errors))
    return receipt


def _decision_binding_errors(
    record: Mapping[str, Any],
    decision: Mapping[str, Any] | None,
    *,
    record_kind: str,
    require_decision_binding: bool = True,
) -> list[str]:
    if decision is None:
        return ["decision_binding_required"] if require_decision_binding else []
    if not isinstance(decision, Mapping):
        return ["decision_binding"]
    decision_valid, _ = validate_context_release_decision(decision)
    if not decision_valid:
        return ["decision_binding"]
    fields = (
        "decision_id", "decision_hash", "source_digest", "projection_digest",
        "released_artifact_digest", "destination_scope",
    )
    if record_kind == "outbox":
        fields += ("audience_class",)
    if any(record.get(field) != decision.get(field) for field in fields):
        return ["decision_binding"]
    expected_id = _stable_id(record_kind, decision["decision_id"], decision["decision_hash"])
    if record_kind == "tombstone":
        expected_id = _stable_id(
            "tombstone", decision["decision_id"], decision["decision_hash"], record["reason"],
        )
    id_field = "receipt_id" if record_kind == "outbox" else "tombstone_id"
    if record.get(id_field) != expected_id:
        return ["decision_binding"]
    return []


def _self_digest_matches(
    record: Mapping[str, Any],
    field: str,
    digest_fn: Any,
) -> bool:
    try:
        claimed = record.get(field)
        if not isinstance(claimed, str):
            return False
        return claimed == digest_fn(record)
    except Exception:
        return False


def _validate_outbox_receipt(
    receipt: Mapping[str, Any],
    *,
    decision: Mapping[str, Any] | None = None,
) -> tuple[bool, list[str]]:
    if not isinstance(receipt, Mapping):
        return False, ["receipt"]
    errors = _find_forbidden(receipt)
    errors.extend(f"unknown:{key}" for key in sorted(set(receipt) - _OUTBOX_FIELDS))
    if receipt.get("schema") != OUTBOX_RECEIPT_SCHEMA:
        errors.append("schema")
    for field in ("receipt_id", "decision_id", "destination_scope", "transport_receipt_ref"):
        try:
            _opaque_label(receipt.get(field), field, max_len=1024)
        except ValueError:
            errors.append(field)
    if receipt.get("audience_class") not in AUDIENCE_CLASSES:
        errors.append("audience_class")
    for field in ("decision_hash", "source_digest", "projection_digest", "released_artifact_digest", "transport_receipt_digest"):
        try:
            _digest(receipt.get(field), field)
        except ValueError:
            errors.append(field)
    try:
        _timestamp(receipt.get("delivered_at"), "delivered_at")
    except ValueError:
        errors.append("delivered_at")
    if receipt.get("publication_status") != "DELIVERY_CONFIRMED":
        errors.append("publication_status")
    if not _self_digest_matches(receipt, "receipt_hash", outbox_receipt_digest):
        errors.append("receipt_hash")
    errors.extend(_decision_binding_errors(receipt, decision, record_kind="outbox"))
    return not errors, sorted(set(errors))


def validate_outbox_receipt(
    receipt: Mapping[str, Any],
    *,
    decision: Mapping[str, Any] | None = None,
) -> tuple[bool, list[str]]:
    try:
        return _validate_outbox_receipt(receipt, decision=decision)
    except Exception:
        return False, ["malformed_receipt"]


def tombstone_digest(tombstone: Mapping[str, Any]) -> str:
    return _sha({key: value for key, value in tombstone.items() if key != "tombstone_hash"})


def build_publication_tombstone(
    decision: Mapping[str, Any],
    *,
    reason: str,
    tombstone_at: str,
    revocation_ref: str | None = None,
) -> dict[str, Any]:
    valid, errors = validate_context_release_decision(decision)
    if not valid:
        raise ValueError("cannot create tombstone: " + ", ".join(errors))
    if reason not in TOMBSTONE_REASONS:
        raise ValueError("invalid tombstone reason")
    tombstone: dict[str, Any] = {
        "schema": TOMBSTONE_SCHEMA,
        "tombstone_id": _stable_id("tombstone", decision["decision_id"], decision["decision_hash"], reason),
        "decision_id": decision["decision_id"],
        "decision_hash": decision["decision_hash"],
        "source_digest": decision["source_digest"],
        "projection_digest": decision["projection_digest"],
        "released_artifact_digest": decision["released_artifact_digest"],
        "destination_scope": decision["destination_scope"],
        "reason": reason,
        "tombstone_at": _timestamp(tombstone_at, "tombstone_at"),
        "revocation_ref": _opaque_label(revocation_ref, "revocation_ref", max_len=1024, optional=True),
        "content_free": True,
        "resurrection_blocked": True,
    }
    tombstone["tombstone_hash"] = tombstone_digest(tombstone)
    tombstone_valid, tombstone_errors = validate_tombstone(tombstone, decision=decision)
    if not tombstone_valid:
        raise ValueError("invalid tombstone: " + ", ".join(tombstone_errors))
    return tombstone


def _validate_tombstone(
    tombstone: Mapping[str, Any],
    *,
    decision: Mapping[str, Any] | None = None,
    require_decision_binding: bool = True,
) -> tuple[bool, list[str]]:
    if not isinstance(tombstone, Mapping):
        return False, ["tombstone"]
    errors = _find_forbidden(tombstone)
    errors.extend(f"unknown:{key}" for key in sorted(set(tombstone) - _TOMBSTONE_FIELDS))
    if tombstone.get("schema") != TOMBSTONE_SCHEMA:
        errors.append("schema")
    for field in ("tombstone_id", "decision_id", "destination_scope"):
        try:
            _opaque_label(tombstone.get(field), field, max_len=1024)
        except ValueError:
            errors.append(field)
    for field in ("decision_hash", "source_digest", "projection_digest"):
        try:
            _digest(tombstone.get(field), field)
        except ValueError:
            errors.append(field)
    if tombstone.get("released_artifact_digest") is not None:
        try:
            _digest(tombstone["released_artifact_digest"], "released_artifact_digest")
        except ValueError:
            errors.append("released_artifact_digest")
    if tombstone.get("reason") not in TOMBSTONE_REASONS:
        errors.append("reason")
    try:
        _timestamp(tombstone.get("tombstone_at"), "tombstone_at")
    except ValueError:
        errors.append("tombstone_at")
    try:
        _opaque_label(tombstone.get("revocation_ref"), "revocation_ref", max_len=1024, optional=True)
    except ValueError:
        errors.append("revocation_ref")
    if tombstone.get("content_free") is not True or tombstone.get("resurrection_blocked") is not True:
        errors.append("tombstone_flags")
    if not _self_digest_matches(tombstone, "tombstone_hash", tombstone_digest):
        errors.append("tombstone_hash")
    errors.extend(
        _decision_binding_errors(
            tombstone,
            decision,
            record_kind="tombstone",
            require_decision_binding=require_decision_binding,
        )
    )
    return not errors, sorted(set(errors))


def validate_tombstone(
    tombstone: Mapping[str, Any],
    *,
    decision: Mapping[str, Any] | None = None,
) -> tuple[bool, list[str]]:
    try:
        return _validate_tombstone(tombstone, decision=decision)
    except Exception:
        return False, ["malformed_tombstone"]


def check_idempotent_retry(
    decision: Mapping[str, Any], prior_decisions: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    valid, _ = validate_context_release_decision(decision)
    if not valid:
        return {"allowed": False, "reason": "decision_invalid"}
    try:
        prior_list = _bounded_list(
            prior_decisions,
            limit=_MAX_HISTORY_RECORDS,
            field="prior_decisions",
        )
    except ValueError:
        return {"allowed": False, "reason": "prior_history_too_large"}
    for prior in prior_list:
        prior_valid, _ = validate_context_release_decision(prior)
        if not prior_valid:
            return {"allowed": False, "reason": "prior_decision_invalid"}
        if prior.get("idempotency_key") != decision.get("idempotency_key"):
            continue
        if prior.get("decision_hash") == decision.get("decision_hash"):
            return {"allowed": True, "reason": "idempotent_retry"}
        return {"allowed": False, "reason": "idempotency_key_conflict"}
    return {"allowed": True, "reason": "first_attempt"}


def check_publication_order(
    decision: Mapping[str, Any], prior_decisions: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    valid, _ = validate_context_release_decision(decision)
    if not valid:
        return {"allowed": False, "reason": "decision_invalid"}
    try:
        prior_list = _bounded_list(
            prior_decisions,
            limit=_MAX_HISTORY_RECORDS,
            field="prior_decisions",
        )
    except ValueError:
        return {"allowed": False, "reason": "prior_history_too_large"}
    for prior in prior_list:
        prior_valid, _ = validate_context_release_decision(prior)
        if not prior_valid:
            return {"allowed": False, "reason": "prior_decision_invalid"}
    relevant = [
        prior for prior in prior_list
        if prior.get("source_ref") == decision.get("source_ref")
        and prior.get("scope_anchor") == decision.get("scope_anchor")
        and prior.get("destination_scope") == decision.get("destination_scope")
    ]
    by_revision: dict[int, Mapping[str, Any]] = {}
    for prior in relevant:
        revision = prior["publication_revision"]
        if revision in by_revision:
            return {"allowed": False, "reason": "duplicate_revision"}
        by_revision[revision] = prior
    if not by_revision:
        if decision["publication_revision"] != 1:
            return {"allowed": False, "reason": "revision_gap"}
        if decision.get("previous_decision_hash") is not None:
            return {"allowed": False, "reason": "lineage_mismatch"}
        return {"allowed": True, "reason": "first_revision"}

    latest_revision = max(by_revision)
    if set(by_revision) != set(range(1, latest_revision + 1)):
        return {"allowed": False, "reason": "revision_history_incomplete"}
    for revision in range(1, latest_revision + 1):
        current = by_revision[revision]
        if revision == 1:
            if current.get("previous_decision_hash") is not None:
                return {"allowed": False, "reason": "lineage_mismatch"}
        elif current.get("previous_decision_hash") != by_revision[revision - 1].get("decision_hash"):
            return {"allowed": False, "reason": "lineage_mismatch"}

    current_revision = decision["publication_revision"]
    if current_revision in by_revision:
        if decision.get("decision_hash") == by_revision[current_revision].get("decision_hash"):
            return {"allowed": True, "reason": "idempotent_retry"}
        return {"allowed": False, "reason": "out_of_order_publication"}
    if current_revision != latest_revision + 1:
        return {"allowed": False, "reason": "revision_gap"}
    if decision.get("previous_decision_hash") != by_revision[latest_revision].get("decision_hash"):
        return {"allowed": False, "reason": "lineage_mismatch"}
    return {"allowed": True, "reason": "next_revision"}


__all__ = [
    "CONTEXT_RELEASE_SCHEMA", "OUTBOX_RECEIPT_SCHEMA", "TOMBSTONE_SCHEMA",
    "HANDLING_PROFILES", "DECISION_STATES", "EVIDENCE_STATES", "AUDIENCE_CLASSES",
    "canonical_json", "decision_digest", "build_context_release_decision",
    "validate_context_release_decision", "read_context_release_decision",
    "evaluate_publication", "build_outbox_receipt", "outbox_receipt_digest",
    "validate_outbox_receipt", "build_publication_tombstone", "tombstone_digest",
    "validate_tombstone", "check_idempotent_retry", "check_publication_order",
]
