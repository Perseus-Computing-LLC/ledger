"""Issue #259: deterministic, hash-only OSCAL projections."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft7Validator

from ledger_agent import oscal


FIXTURES = Path(__file__).parent / "fixtures" / "oscal"


def _schema(name: str) -> dict:
    schema = json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    # NIST's schema uses Unicode-property escapes (\\p{L}/\\p{N}) that are
    # valid in the schema's regex dialect but are not accepted by Python's
    # stdlib ``re``. The generated fixture uses ASCII OSCAL tokens, so narrow
    # only those equivalent assertions for the local Draft-07 validator.
    def normalize_patterns(value):
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


def _assert_schema_valid(document: dict, name: str) -> None:
    errors = sorted(Draft7Validator(_schema(name)).iter_errors(document), key=lambda e: list(e.path))
    assert not errors, "\n".join(f"{list(error.path)}: {error.message}" for error in errors)


def _event(
    event_id: str,
    control_id: str,
    *,
    evidence_state: str = "observed",
    finding_state: str = "satisfied",
    human_review: str = "approved",
    attestation_state: str = "attested",
) -> dict:
    return oscal.build_ledger_event(
        event_id=event_id,
        control_id=control_id,
        observed_at="2026-08-19T12:00:00Z",
        evidence=[
            {
                "ref": f"ledger:evidence/{event_id}",
                "digest": hashlib.sha256(event_id.encode()).hexdigest(),
                "state": evidence_state,
            },
        ],
        observation_state=evidence_state,
        finding_state=finding_state,
        risk_state="none",
        remediation_status="complete",
        human_review=human_review,
        attestation_state=attestation_state,
    )


def _project(events: list[dict]) -> dict:
    return oscal.project_oscal(
        events,
        system_id="system:synthetic-ledger",
        ssp_ref="https://example.test/oscal/ssp.json",
        assessment_plan_ref="https://example.test/oscal/assessment-plan.json",
        expected_control_ids=["ac-2", "ac-3", "ac-4"],
        assessment_start="2026-08-19T00:00:00Z",
        assessment_end="2026-08-19T23:59:59Z",
        document_version="1.0.0",
    )


def test_projection_is_deterministic_and_validates_against_nist_schemas():
    events = [
        _event("evt-ac-2", "ac-2"),
        _event("evt-ac-3", "ac-3", evidence_state="unknown", finding_state="unknown"),
    ]

    first = _project(events)
    second = _project(events)

    assert first == second
    _assert_schema_valid(first["assessment_results"], "oscal_assessment-results_schema.json")
    _assert_schema_valid(first["poam"], "oscal_poam_schema.json")
    assert first["oscal_version"] == "1.2.3"
    assert first["coverage"] == {
        "expected_controls": ["ac-2", "ac-3", "ac-4"],
        "observed_controls": ["ac-2", "ac-3"],
        "clean_controls": ["ac-2"],
        "unreported_controls": ["ac-4"],
        "evidence_states": {
            "observed": ["ac-2"],
            "unknown": ["ac-3"],
            "missing": [],
            "stale": [],
            "superseded": [],
            "unreported": ["ac-4"],
        },
        "status": "partial",
    }

    for model in ("assessment_results", "poam"):
        document = first[model]
        canonical = oscal.canonical_json(document)
        assert first["export_hashes"][model] == hashlib.sha256(canonical.encode()).hexdigest()
        assert json.loads(oscal.serialize(document, "json")) == document
        assert yaml.safe_load(oscal.serialize(document, "yaml")) == document

    result = first["assessment_results"]["assessment-results"]["results"][0]
    findings = {finding["target"]["target-id"]: finding for finding in result["findings"]}
    assert findings["ac-2"]["target"]["status"]["state"] == "satisfied"
    assert findings["ac-3"]["target"]["status"]["state"] == "not-satisfied"
    assert findings["ac-4"]["target"]["status"]["state"] == "not-satisfied"


def test_production_validator_rejects_schema_invalid_documents_without_echoing_values():
    malformed = deepcopy(_project([_event("evt-schema", "ac-2")])["assessment_results"])
    root = malformed["assessment-results"]
    root["uuid"] = "not-a-uuid"
    root["import-ap"] = {"href": ""}
    root["results"] = [{}]

    valid, errors = oscal.validate_oscal_document(malformed, "assessment-results")

    assert valid is False
    assert errors
    assert all("not-a-uuid" not in error for error in errors)

    invalid_time = deepcopy(_project([_event("evt-format", "ac-2")])["assessment_results"])
    invalid_time["assessment-results"]["metadata"]["last-modified"] = "not-a-date"
    valid, errors = oscal.validate_oscal_document(invalid_time, "assessment-results")
    assert valid is False
    assert errors
    assert all("not-a-date" not in error for error in errors)


def test_missing_unknown_stale_superseded_and_unreported_never_become_clean():
    events = [
        _event("evt-missing", "ac-2", evidence_state="missing", finding_state="satisfied"),
        _event("evt-stale", "ac-3", evidence_state="stale", finding_state="satisfied"),
    ]
    projected = oscal.project_oscal(
        events,
        system_id="system:synthetic-ledger",
        ssp_ref="ssp:synthetic",
        assessment_plan_ref="ap:synthetic",
        expected_control_ids=["ac-2", "ac-3", "ac-4", "ac-5"],
        assessment_start="2026-08-19T00:00:00Z",
        assessment_end="2026-08-19T23:59:59Z",
    )

    assert projected["coverage"]["clean_controls"] == []
    assert projected["coverage"]["evidence_states"]["missing"] == ["ac-2"]
    assert projected["coverage"]["evidence_states"]["stale"] == ["ac-3"]
    assert projected["coverage"]["evidence_states"]["unreported"] == ["ac-4", "ac-5"]
    assert all(
        finding["target"]["status"]["state"] == "not-satisfied"
        for finding in projected["assessment_results"]["assessment-results"]["results"][0]["findings"]
    )


def test_input_contract_rejects_invalid_or_raw_evidence():
    with pytest.raises(ValueError, match="digest"):
        oscal.build_ledger_event(
            event_id="evt-invalid",
            control_id="ac-2",
            observed_at="2026-08-19T12:00:00Z",
            evidence=[{"ref": "ledger:evidence/raw", "digest": "not-a-digest", "state": "observed"}],
            observation_state="observed",
            finding_state="satisfied",
            risk_state="none",
            remediation_status="complete",
            human_review="approved",
            attestation_state="attested",
        )

    with pytest.raises(ValueError, match="forbidden"):
        oscal.validate_ledger_event(
            {
                **_event("evt-raw", "ac-2"),
                "prompt": "must never enter an OSCAL export",
            }
        )

    with pytest.raises(ValueError, match="event_id"):
        raw_event_id = _event("evt-raw-id", "ac-2")
        raw_event_id["event_id"] = "RAW EVENT\nSECRET"
        oscal.validate_ledger_event(raw_event_id)

    with pytest.raises(ValueError, match="ref"):
        raw_ref_event = _event("evt-raw-ref", "ac-2")
        raw_ref_event["evidence"][0]["ref"] = "raw prompt content must not be copied"
        oscal.validate_ledger_event(raw_ref_event)

    with pytest.raises(ValueError, match="timezone"):
        oscal.build_ledger_event(
            event_id="evt-naive",
            control_id="ac-2",
            observed_at="2026-08-19T12:00:00",
            evidence=[],
            observation_state="unreported",
            finding_state="unknown",
            risk_state="unknown",
            remediation_status="unknown",
            human_review="unknown",
            attestation_state="unknown",
        )


def test_projection_normalizes_offset_timestamps_before_ordering():
    event = _event("evt-offset", "ac-2")
    event["observed_at"] = "2026-08-19T12:00:00+01:00"

    projected = _project([event])

    observation = projected["assessment_results"]["assessment-results"]["results"][0]["observations"][0]
    assert observation["collected"] == "2026-08-19T11:00:00Z"


def test_observed_without_retained_evidence_is_not_clean():
    event = _event("evt-empty", "ac-2")
    event["evidence"] = []
    projected = _project([event])

    assert projected["coverage"]["clean_controls"] == []
    finding = projected["assessment_results"]["assessment-results"]["results"][0]["findings"][0]
    assert finding["target"]["status"]["state"] == "not-satisfied"


def test_round_trip_and_stable_ids_do_not_depend_on_input_order():
    events = [_event("evt-ac-3", "ac-3"), _event("evt-ac-2", "ac-2")]
    reversed_events = list(reversed(events))
    first = _project(events)
    second = _project(reversed_events)

    assert first == second
    observations = first["assessment_results"]["assessment-results"]["results"][0]["observations"]
    assert [observation["props"][0]["value"] for observation in observations] == [
        "ac-2", "ac-3", "ac-4",
    ]
    assert oscal.validate_oscal_document(first["assessment_results"], "assessment-results") == (True, [])
    assert oscal.validate_oscal_document(first["poam"], "poam") == (True, [])
