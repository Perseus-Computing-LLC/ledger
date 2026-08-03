from __future__ import annotations

import hashlib
import json

import pytest

from plutus_agent.prebind import (
    PREBIND_SCHEMA,
    build_prebind,
    prebind_digest,
    replay_prebind,
    validate_prebind,
)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def make_prebind(**overrides):
    value = build_prebind(
        attempted_action="action:deploy-42",
        actor_ref="agent:hermes-prod",
        authority_ref="authority:manifest-3",
        trusted_scope="github:Perseus-Computing-LLC/plutus",
        policy_version="policy/v3",
        evidence_hashes=[digest("source")],
        selected_context_digest=digest("context-selection"),
        resource_ref="resource:ledger-event-42",
        boundary_outcome="hold",
        non_effective_result="not_executed",
        replay_id="replay:deploy-42",
    )
    value.update(overrides)
    return value


def test_prebind_is_canonical_hash_bound_and_has_no_raw_payloads():
    block = make_prebind()
    assert block["schema_version"] == PREBIND_SCHEMA
    assert prebind_digest(block) == block["prebind_hash"]
    assert validate_prebind(block) == (True, [])
    serialized = json.dumps(block, sort_keys=True)
    assert "prompt" not in serialized
    assert "raw context" not in serialized
    assert "tool_arguments" not in serialized


def test_prebind_rejects_missing_fields_invalid_hashes_and_raw_fields():
    missing = make_prebind()
    missing.pop("selected_context_digest")
    assert "selected_context_digest" in validate_prebind(missing)[1]

    invalid = make_prebind(evidence_hashes=["not-a-digest"])
    assert "evidence_hashes" in validate_prebind(invalid)[1]

    leaked = make_prebind()
    leaked["prompt"] = "raw prompt"
    valid, errors = validate_prebind(leaked)
    assert not valid
    assert "forbidden_field:prompt" in errors


def test_prebind_rejects_hash_tampering_and_ambiguous_outcomes():
    tampered = make_prebind()
    tampered["boundary_outcome"] = "allow"
    valid, errors = validate_prebind(tampered)
    assert not valid
    assert "prebind_hash" in errors

    ambiguous = make_prebind(boundary_outcome="allow", non_effective_result="executed")
    valid, errors = validate_prebind(ambiguous)
    assert not valid
    assert "outcome_result_mismatch" in errors


def test_replay_is_pure_and_detects_scope_authority_and_evidence_changes():
    prior = make_prebind(boundary_outcome="hold", non_effective_result="not_executed")
    comparison = replay_prebind(
        prior,
        current_authority_ref="authority:manifest-4",
        current_trusted_scope="github:other/repo",
        current_evidence_hashes=[digest("changed-source")],
        current_state={"authority_ok": False, "evidence_current": False, "approval_granted": False},
    )
    assert comparison["replay_id"] == prior["replay_id"]
    assert comparison["admission"] == "not_admitted"
    assert set(comparison["changed_fields"]) >= {"authority_ref", "trusted_scope", "evidence_hashes"}
    assert prior["boundary_outcome"] == "hold"


def test_replay_can_admit_corrected_held_attempt_without_mutating_history():
    prior = make_prebind(boundary_outcome="hold", non_effective_result="not_executed")
    comparison = replay_prebind(
        prior,
        current_state={"authority_ok": True, "evidence_current": True, "approval_granted": True, "action_allowed": True},
    )
    assert comparison["admission"] == "admitted_after_correction"
    assert comparison["replayed_boundary_outcome"] == "allow"
    assert prior["non_effective_result"] == "not_executed"


def test_replay_rejects_tampered_prior_block():
    prior = make_prebind()
    prior["replay_id"] = "replay:tampered"
    with pytest.raises(ValueError, match="invalid prebind"):
        replay_prebind(prior)


def test_prebind_is_persisted_in_hash_chain_and_receipt(tmp_path):
    from plutus_agent import db, metering
    from plutus_agent.server.api import audit_json

    conn = db.connect(str(tmp_path / "prebind.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "prebind-integration", tier="free")["id"]
    block = make_prebind(boundary_outcome="hold", non_effective_result="not_executed")
    result = metering.record_usage(
        conn, org_id, provider="openai", model="fixture", task_type="deploy",
        external_ref="deploy-prebind", input_tokens=1, output_tokens=1,
        cost_usd=0.01, prebind=block,
    )
    assert result.recorded is True
    receipt = audit_json(conn, org_id, external_ref="deploy-prebind")
    assert receipt["events"][0]["prebind"]["prebind_hash"] == block["prebind_hash"]
    assert db.verify_chain(conn, org_id)["ok"] is True
    conn.close()


def test_legacy_usage_without_prebind_remains_unchanged_and_receipt_omits_it(tmp_path):
    from plutus_agent import db, metering
    from plutus_agent.server.api import audit_json

    conn = db.connect(str(tmp_path / "legacy.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "legacy-no-prebind", tier="free")["id"]
    metering.record_usage(
        conn, org_id, provider="openai", model="fixture", external_ref="legacy",
        input_tokens=1, output_tokens=1, cost_usd=0.01,
    )
    receipt = audit_json(conn, org_id, external_ref="legacy")
    assert receipt["events"][0]["prebind"] is None
    assert db.verify_chain(conn, org_id)["ok"] is True
    conn.close()
