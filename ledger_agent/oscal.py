"""Deterministic, hash-only OSCAL projections for Ledger evidence (#259).

The module intentionally owns a small, explicit input contract rather than
accepting arbitrary Ledger rows. It emits the OSCAL 1.2.3 Assessment Results
and POA&M envelopes and keeps Ledger's coverage report beside (not inside) the
OSCAL model. Missing or uncertain evidence is represented as an unsatisfied
assessment posture; it is never promoted to a clean control result.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from functools import lru_cache
from importlib.resources import files
from typing import Any, Iterable, Mapping

import yaml
from jsonschema import Draft7Validator, FormatChecker

OSCAL_VERSION = "1.2.3"
PROJECTION_SCHEMA = "perseus-ledger-oscal-projection/v1"
LEDGER_EVENT_SCHEMA = "perseus-ledger-oscal-event/v1"
ASSESSMENT_RESULTS_SCHEMA_URL = (
    "https://github.com/usnistgov/OSCAL/releases/download/v1.2.3/"
    "oscal_assessment-results_schema.json"
)
POAM_SCHEMA_URL = (
    "https://github.com/usnistgov/OSCAL/releases/download/v1.2.3/"
    "oscal_poam_schema.json"
)

EVIDENCE_STATES = ("observed", "missing", "unknown", "stale", "superseded", "unreported")
FINDING_STATES = ("satisfied", "not-satisfied", "unknown", "not-assessed")
RISK_STATES = ("none", "low", "moderate", "high", "critical", "unknown")
REMEDIATION_STATES = ("complete", "open", "in-progress", "accepted-risk", "unknown")
REVIEW_STATES = ("approved", "rejected", "pending", "unknown")
ATTESTATION_STATES = ("attested", "not-attested", "pending", "unknown")

_OSCAL_UUID_NAMESPACE = uuid.UUID("8e4e3c82-6f4e-5ef1-8c67-49e6e1a6a2e0")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+#%?=&,-]{0,1023}$")
_REF_RESERVED_VALUES = frozenset(
    {
        "false",
        "nan",
        "none",
        "null",
        "raw-sensitive-sentinel",
        "raw_sensitive_sentinel",
        "raw-sentinel",
        "raw_sentinel",
        "undefined",
        "true",
    }
)
_TOKEN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9.\-_]*$")
_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,63}$")
_FORBIDDEN_KEYS = {
    "prompt", "prompts", "memory_body", "memory_bodies", "provider_payload",
    "provider_response", "raw_payload", "tool_arguments", "tool_output",
    "api_key", "authorization", "password", "credential", "secret",
    "private_key", "access_token", "refresh_token", "bearer_token",
}
_EVENT_FIELDS = {
    "schema", "event_id", "control_id", "observed_at", "evidence",
    "observation_state", "finding_state", "risk_state", "remediation_status",
    "human_review", "attestation_state",
}
_EVIDENCE_FIELDS = {"ref", "digest", "state", "superseded_by"}
_STATE_PRIORITY = {
    "observed": 0,
    "superseded": 1,
    "stale": 2,
    "unknown": 3,
    "missing": 4,
    "unreported": 5,
}


def canonical_json(value: Any) -> str:
    """Return the byte-stable JSON representation used for export hashes."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{field} must be a 64-character SHA-256 hex digest")
    return value.lower()


def _text(value: Any, field: str, *, max_len: int = 512) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    if len(value) > max_len:
        raise ValueError(f"{field} exceeds {max_len} characters")
    if "\r" in value or "\n" in value:
        raise ValueError(f"{field} must not contain newlines")
    return value


def _token(value: Any, field: str) -> str:
    value = _text(value, field, max_len=128)
    if not _TOKEN_RE.fullmatch(value):
        raise ValueError(f"{field} must be an OSCAL token")
    return value


def _version(value: Any, field: str) -> str:
    value = _text(value, field, max_len=64)
    if not _VERSION_RE.fullmatch(value):
        raise ValueError(f"{field} must be a bounded version token")
    return value


def _timestamp(value: Any, field: str) -> str:
    value = _text(value, field, max_len=64)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    parsed = parsed.astimezone(timezone.utc)
    timespec = "microseconds" if parsed.microsecond else "seconds"
    return parsed.isoformat(timespec=timespec).replace("+00:00", "Z")


def _opaque_ref(value: Any, field: str) -> str:
    value = _text(value, field, max_len=1024)
    if not _REF_RE.fullmatch(value) or value.casefold() in _REF_RESERVED_VALUES:
        raise ValueError(f"{field} must be an opaque reference without whitespace or reserved scalar markers")
    return value


def _find_forbidden(value: Any, path: str = "") -> list[str]:
    errors: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key).lower()
            child_path = f"{path}.{key}" if path else str(key)
            if key_text in _FORBIDDEN_KEYS:
                errors.append(f"forbidden:{child_path}")
            errors.extend(_find_forbidden(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            errors.extend(_find_forbidden(child, f"{path}[{index}]"))
    return errors


def _stable_uuid(kind: str, *parts: str) -> str:
    identity = "|".join((PROJECTION_SCHEMA, kind, *parts))
    return str(uuid.uuid5(_OSCAL_UUID_NAMESPACE, identity))


def _property(name: str, value: str) -> dict[str, str]:
    return {"name": name, "value": value}


def _event_errors(event: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(event, Mapping):
        return ["event must be an object"]
    errors.extend(_find_forbidden(event))
    unknown = sorted(set(event) - _EVENT_FIELDS)
    errors.extend(f"unknown:{field}" for field in unknown)
    if event.get("schema") != LEDGER_EVENT_SCHEMA:
        errors.append("schema")
    try:
        _opaque_ref(event.get("event_id"), "event_id")
    except ValueError:
        errors.append("event_id")
    try:
        _token(event.get("control_id"), "control_id")
    except ValueError:
        errors.append("control_id")
    try:
        _timestamp(event.get("observed_at"), "observed_at")
    except ValueError:
        errors.append("observed_at")
    evidence = event.get("evidence")
    if not isinstance(evidence, list):
        errors.append("evidence")
    else:
        for index, item in enumerate(evidence):
            if not isinstance(item, Mapping):
                errors.append(f"evidence[{index}]")
                continue
            errors.extend(f"evidence[{index}].{error}" for error in _evidence_errors(item))
    for field, values in (
        ("observation_state", EVIDENCE_STATES),
        ("finding_state", FINDING_STATES),
        ("risk_state", RISK_STATES),
        ("remediation_status", REMEDIATION_STATES),
        ("human_review", REVIEW_STATES),
        ("attestation_state", ATTESTATION_STATES),
    ):
        if event.get(field) not in values:
            errors.append(field)
    return sorted(set(errors))


def _evidence_errors(item: Any) -> list[str]:
    if not isinstance(item, Mapping):
        return ["object"]
    errors = _find_forbidden(item)
    errors.extend(f"unknown:{field}" for field in sorted(set(item) - _EVIDENCE_FIELDS))
    try:
        _opaque_ref(item.get("ref"), "ref")
    except ValueError:
        errors.append("ref")
    try:
        _digest(item.get("digest"), "digest")
    except ValueError:
        errors.append("digest")
    if item.get("state") not in EVIDENCE_STATES:
        errors.append("state")
    if item.get("superseded_by") is not None:
        try:
            _opaque_ref(item["superseded_by"], "superseded_by")
        except ValueError:
            errors.append("superseded_by")
    return sorted(set(errors))


def build_ledger_event(
    *,
    event_id: str,
    control_id: str,
    observed_at: str,
    evidence: list[Mapping[str, Any]],
    observation_state: str,
    finding_state: str,
    risk_state: str,
    remediation_status: str,
    human_review: str,
    attestation_state: str,
) -> dict[str, Any]:
    """Build the explicit, hash-only input contract for one Ledger event."""
    event: dict[str, Any] = {
        "schema": LEDGER_EVENT_SCHEMA,
        "event_id": _opaque_ref(event_id, "event_id"),
        "control_id": _token(control_id, "control_id"),
        "observed_at": _timestamp(observed_at, "observed_at"),
        "evidence": [],
        "observation_state": observation_state,
        "finding_state": finding_state,
        "risk_state": risk_state,
        "remediation_status": remediation_status,
        "human_review": human_review,
        "attestation_state": attestation_state,
    }
    if not isinstance(evidence, list):
        raise ValueError("evidence must be a list")
    for item in evidence:
        if not isinstance(item, Mapping):
            raise ValueError("evidence entries must be objects")
        evidence_errors = _evidence_errors(item)
        if evidence_errors:
            raise ValueError("invalid evidence entry: " + ", ".join(evidence_errors))
        normalized = {
            "ref": _opaque_ref(item.get("ref"), "evidence.ref"),
            "digest": _digest(item.get("digest"), "evidence.digest"),
            "state": item.get("state"),
            "superseded_by": item.get("superseded_by"),
        }
        if normalized["superseded_by"] is not None:
            normalized["superseded_by"] = _opaque_ref(
                normalized["superseded_by"], "evidence.superseded_by"
            )
        event["evidence"].append(normalized)
    validate_ledger_event(event)
    return event


def validate_ledger_event(event: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """Validate an input event, raising a stable ``ValueError`` on failure."""
    errors = _event_errors(event)
    if errors:
        raise ValueError("invalid Ledger OSCAL event: " + ", ".join(errors))
    return True, []


def _event_state(event: Mapping[str, Any]) -> str:
    states = [event["observation_state"]]
    states.extend(item["state"] for item in event["evidence"])
    return max(states, key=lambda state: _STATE_PRIORITY[state])


def _control_posture(events: list[Mapping[str, Any]]) -> tuple[str, bool]:
    if not events:
        return "unreported", False
    state = max((_event_state(event) for event in events), key=lambda value: _STATE_PRIORITY[value])
    clean = (
        state == "observed"
        and all(event["evidence"] for event in events)
        and all(
            item["state"] == "observed"
            for event in events
            for item in event["evidence"]
        )
        and all(event["finding_state"] == "satisfied" for event in events)
        and all(event["risk_state"] == "none" for event in events)
        and all(event["remediation_status"] == "complete" for event in events)
        and all(event["human_review"] == "approved" for event in events)
        and all(event["attestation_state"] == "attested" for event in events)
    )
    return state, clean


def _coverage(
    expected: list[str], grouped: Mapping[str, list[Mapping[str, Any]]],
) -> tuple[dict[str, Any], dict[str, tuple[str, bool]]]:
    posture = {control: _control_posture(grouped.get(control, [])) for control in expected}
    observed = [control for control in expected if grouped.get(control)]
    clean = [control for control in expected if posture[control][1]]
    states = {
        state: [control for control in expected if posture[control][0] == state]
        for state in EVIDENCE_STATES
    }
    status = "complete" if len(observed) == len(expected) else ("none" if not observed else "partial")
    return {
        "expected_controls": expected,
        "observed_controls": observed,
        "clean_controls": clean,
        "unreported_controls": states["unreported"],
        "evidence_states": states,
        "status": status,
    }, posture


def _metadata(title: str, document_version: str, last_modified: str) -> dict[str, str]:
    return {
        "title": title,
        "last-modified": last_modified,
        "version": document_version,
        "oscal-version": OSCAL_VERSION,
    }


def _evidence_refs(events: list[Mapping[str, Any]]) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    for event in events:
        for item in event["evidence"]:
            refs.append({
                "description": (
                    f"Opaque Ledger evidence reference; state={item['state']}; "
                    "content is intentionally not included."
                ),
                "href": item["ref"],
            })
    return sorted(refs, key=lambda item: (item["href"], item["description"]))


def _observation(
    control_id: str,
    events: list[Mapping[str, Any]],
    state: str,
    clean: bool,
    assessment_end: str,
) -> dict[str, Any]:
    observed_at = max(
        (event["observed_at"] for event in events),
        key=lambda value: datetime.fromisoformat(value.replace("Z", "+00:00")),
        default=assessment_end,
    )
    digests = sorted({item["digest"] for event in events for item in event["evidence"]})
    observation = {
        "uuid": _stable_uuid("observation", control_id),
        "title": f"Ledger evidence posture for {control_id}",
        "description": (
            f"Ledger evidence state for control {control_id}: {state}. "
            f"Clean projection: {'yes' if clean else 'no'}. "
            "This is a bounded evidence projection, not an assessor or authorization decision."
        ),
        "props": [
            _property("ledger-control-id", control_id),
            _property("ledger-evidence-state", state),
            _property("ledger-clean-projection", "true" if clean else "false"),
            *[_property("ledger-evidence-digest", digest) for digest in digests],
        ],
        "methods": ["EXAMINE"],
        "types": ["control-objective"],
        "collected": observed_at,
    }
    evidence_refs = _evidence_refs(events)
    if evidence_refs:
        observation["relevant-evidence"] = evidence_refs
    return observation


def _finding(
    control_id: str,
    observation_uuid: str,
    state: str,
    clean: bool,
    events: list[Mapping[str, Any]],
) -> dict[str, Any]:
    target_state = "satisfied" if clean else "not-satisfied"
    reason = "pass" if clean else "other"
    return {
        "uuid": _stable_uuid("finding", control_id),
        "title": f"Ledger evidence finding for {control_id}",
        "description": (
            f"Control {control_id} has Ledger evidence state {state}. "
            f"The projected state is {target_state}; missing, unknown, stale, "
            "superseded, and unreported evidence never become a clean result. "
            "This finding is evidence for assessment and adjudication only."
        ),
        "props": [
            _property("ledger-evidence-state", state),
            _property("ledger-finding-state", ",".join(sorted({event["finding_state"] for event in events})) or "unknown"),
            _property("ledger-risk-state", ",".join(sorted({event["risk_state"] for event in events})) or "unknown"),
            _property("ledger-remediation-status", ",".join(sorted({event["remediation_status"] for event in events})) or "unknown"),
            _property("ledger-human-review", ",".join(sorted({event["human_review"] for event in events})) or "unknown"),
            _property("ledger-attestation-state", ",".join(sorted({event["attestation_state"] for event in events})) or "unknown"),
        ],
        "target": {
            "type": "objective-id",
            "target-id": control_id,
            "status": {"state": target_state, "reason": reason},
        },
        "related-observations": [{"observation-uuid": observation_uuid}],
    }


def _poam_item(
    control_id: str,
    finding_uuid: str,
    state: str,
    clean: bool,
) -> dict[str, Any]:
    if clean:
        title = "No unresolved Ledger evidence posture item"
        description = (
            f"Control {control_id} has a clean Ledger evidence projection for the supplied "
            "window. This entry is not a certification, authorization, or AO decision."
        )
    else:
        title = f"Resolve Ledger evidence posture for {control_id}"
        description = (
            f"Control {control_id} has evidence state {state}. The owning assessor or system "
            "owner must adjudicate the evidence posture; Ledger does not infer an ATO, RMF "
            "completion, or compliance certification."
        )
    return {
        "uuid": _stable_uuid("poam-item", control_id),
        "title": title,
        "description": description,
        "props": [
            _property("ledger-control-id", control_id),
            _property("ledger-evidence-state", state),
            _property("ledger-clean-projection", "true" if clean else "false"),
        ],
        "related-findings": [{"finding-uuid": finding_uuid}],
    }


def _validate_projection_inputs(
    events: Iterable[Mapping[str, Any]],
    system_id: str,
    ssp_ref: str,
    assessment_plan_ref: str,
    expected_control_ids: Iterable[str],
    assessment_start: str,
    assessment_end: str,
    document_version: str,
) -> tuple[list[dict[str, Any]], str, str, str, list[str], str, str, str]:
    normalized_events: list[dict[str, Any]] = []
    for raw_event in events:
        if not isinstance(raw_event, Mapping):
            validate_ledger_event(raw_event)
        event = dict(raw_event)
        validate_ledger_event(event)
        event["event_id"] = _opaque_ref(event["event_id"], "event_id")
        event["control_id"] = _token(event["control_id"], "control_id")
        event["observed_at"] = _timestamp(event["observed_at"], "observed_at")
        normalized_evidence = []
        for raw_item in event["evidence"]:
            item = dict(raw_item)
            item["ref"] = _opaque_ref(item["ref"], "evidence.ref")
            item["digest"] = _digest(item["digest"], "evidence.digest")
            if item.get("superseded_by") is not None:
                item["superseded_by"] = _opaque_ref(item["superseded_by"], "evidence.superseded_by")
            normalized_evidence.append(item)
        event["evidence"] = normalized_evidence
        normalized_events.append(event)
    normalized_events.sort(key=lambda event: (event["control_id"], event["event_id"]))
    system_id = _opaque_ref(system_id, "system_id")
    ssp_ref = _opaque_ref(ssp_ref, "ssp_ref")
    assessment_plan_ref = _opaque_ref(assessment_plan_ref, "assessment_plan_ref")
    controls = sorted({_token(control, "control_id") for control in expected_control_ids})
    if not controls:
        raise ValueError("expected_control_ids must not be empty")
    unexpected = sorted({event["control_id"] for event in normalized_events} - set(controls))
    if unexpected:
        raise ValueError(
            "event control ids are outside expected_control_ids: " + ", ".join(unexpected)
        )
    start = _timestamp(assessment_start, "assessment_start")
    end = _timestamp(assessment_end, "assessment_end")
    if start > end:
        raise ValueError("assessment_start must not be after assessment_end")
    document_version = _version(document_version, "document_version")
    return normalized_events, system_id, ssp_ref, assessment_plan_ref, controls, start, end, document_version


def project_oscal(
    events: Iterable[Mapping[str, Any]],
    *,
    system_id: str,
    ssp_ref: str,
    assessment_plan_ref: str,
    expected_control_ids: Iterable[str],
    assessment_start: str,
    assessment_end: str,
    document_version: str = "1.0.0",
) -> dict[str, Any]:
    """Project synthetic Ledger evidence into deterministic OSCAL artifacts.

    ``events`` are sorted before aggregation, so event iteration order cannot
    alter identifiers, coverage, serialized content, or export hashes.
    """
    (
        normalized_events, system_id, ssp_ref, assessment_plan_ref, controls,
        start, end, document_version,
    ) = _validate_projection_inputs(
        events, system_id, ssp_ref, assessment_plan_ref, expected_control_ids,
        assessment_start, assessment_end, document_version,
    )
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in normalized_events:
        if event["control_id"] in controls:
            grouped[event["control_id"]].append(event)
    coverage, posture = _coverage(controls, grouped)

    observations: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    poam_items: list[dict[str, Any]] = []
    for control_id in controls:
        state, clean = posture[control_id]
        observation = _observation(control_id, grouped.get(control_id, []), state, clean, end)
        finding = _finding(control_id, observation["uuid"], state, clean, grouped.get(control_id, []))
        observations.append(observation)
        findings.append(finding)
        if not clean or len(controls) == 1:
            poam_items.append(_poam_item(control_id, finding["uuid"], state, clean))
    if not poam_items:
        poam_items.append(_poam_item(controls[0], findings[0]["uuid"], posture[controls[0]][0], True))

    reviewed_controls = {
        "control-selections": [{"include-controls": [{"control-id": control} for control in controls]}],
    }
    result_uuid = _stable_uuid("result", system_id, assessment_plan_ref, start, end)
    result = {
        "uuid": result_uuid,
        "title": "Ledger evidence assessment results",
        "description": (
            "Deterministic projection of Ledger-recorded evidence for the supplied "
            "system boundary and assessment window. It is evidence for assessment "
            "and adjudication, not an ATO, AO approval, RMF completion, or certification."
        ),
        "start": start,
        "end": end,
        "props": [
            _property("ledger-system-id", system_id),
            _property("ledger-coverage-status", coverage["status"]),
            _property("ledger-observed-control-count", str(len(coverage["observed_controls"]))),
            _property("ledger-expected-control-count", str(len(coverage["expected_controls"]))),
        ],
        "reviewed-controls": reviewed_controls,
        "observations": observations,
        "findings": findings,
    }
    assessment_document = {
        "$schema": ASSESSMENT_RESULTS_SCHEMA_URL,
        "assessment-results": {
            "uuid": _stable_uuid("assessment-results", system_id, assessment_plan_ref, start, end),
            "metadata": _metadata(
                "Ledger Evidence Assessment Results", document_version, end
            ),
            "import-ap": {"href": assessment_plan_ref},
            "results": [result],
        },
    }
    poam_document = {
        "$schema": POAM_SCHEMA_URL,
        "plan-of-action-and-milestones": {
            "uuid": _stable_uuid("poam", system_id, ssp_ref, start, end),
            "metadata": _metadata("Ledger Evidence POA&M", document_version, end),
            "import-ssp": {"href": ssp_ref},
            "system-id": {
                "identifier-type": "https://ietf.org/rfc/rfc4122",
                "id": system_id,
            },
            "observations": observations,
            "findings": findings,
            "poam-items": poam_items,
        },
    }
    for model, document in (("assessment_results", assessment_document), ("poam", poam_document)):
        valid, errors = validate_oscal_document(
            document, "assessment-results" if model == "assessment_results" else "poam"
        )
        if not valid:
            raise ValueError(f"generated OSCAL {model} is invalid: {', '.join(errors)}")
    return {
        "schema": PROJECTION_SCHEMA,
        "oscal_version": OSCAL_VERSION,
        "assessment_results": assessment_document,
        "poam": poam_document,
        "coverage": coverage,
        "export_hashes": {
            "assessment_results": hashlib.sha256(canonical_json(assessment_document).encode("utf-8")).hexdigest(),
            "poam": hashlib.sha256(canonical_json(poam_document).encode("utf-8")).hexdigest(),
        },
    }


@lru_cache(maxsize=2)
def _official_schema(model: str) -> dict[str, Any]:
    filenames = {
        "assessment-results": "oscal_assessment-results_schema.json",
        "poam": "oscal_poam_schema.json",
    }
    filename = filenames.get(model)
    if filename is None:
        raise ValueError("model must be 'assessment-results' or 'poam'")
    schema = json.loads(
        files("ledger_agent").joinpath("schemas", filename).read_text(encoding="utf-8")
    )

    # OSCAL 1.2.3 expresses letter/number classes with Unicode-property
    # escapes. Python's stdlib ``re`` (used by jsonschema) cannot compile
    # those escapes; generated identifiers are deliberately ASCII OSCAL
    # tokens, so normalize only the equivalent local validator patterns.
    def normalize_patterns(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "pattern" and isinstance(child, str):
                    value[key] = child.replace(r"\p{L}", "[A-Za-z]").replace(r"\p{N}", "[0-9]")
                else:
                    normalize_patterns(child)
        elif isinstance(value, list):
            for child in value:
                normalize_patterns(child)

    normalize_patterns(schema)
    return schema


def validate_oscal_document(document: Any, model: str) -> tuple[bool, list[str]]:
    """Validate a document against the packaged official OSCAL 1.2.3 schema."""
    try:
        schema = _official_schema(model)
        validator = Draft7Validator(schema, format_checker=FormatChecker())
        errors = sorted(validator.iter_errors(document), key=lambda error: list(error.absolute_path))
    except (OSError, json.JSONDecodeError, ValueError):
        return False, ["schema-unavailable"]
    result: list[str] = []
    for error in errors:
        path = ".".join(str(part) for part in error.absolute_path) or "$"
        # Do not return jsonschema's value-bearing message: malformed input
        # must not echo a raw prompt, document, or credential into logs.
        result.append(f"{path}:{error.validator or 'invalid'}")
    return not result, result


def serialize(document: Mapping[str, Any], format: str = "json") -> str:
    """Serialize an OSCAL document as deterministic JSON or YAML."""
    if format == "json":
        return json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if format == "yaml":
        return yaml.safe_dump(document, allow_unicode=True, sort_keys=True, default_flow_style=False)
    raise ValueError("format must be 'json' or 'yaml'")


__all__ = [
    "OSCAL_VERSION", "PROJECTION_SCHEMA", "LEDGER_EVENT_SCHEMA",
    "ASSESSMENT_RESULTS_SCHEMA_URL", "POAM_SCHEMA_URL",
    "EVIDENCE_STATES", "build_ledger_event", "validate_ledger_event",
    "project_oscal", "validate_oscal_document", "canonical_json", "serialize",
]
