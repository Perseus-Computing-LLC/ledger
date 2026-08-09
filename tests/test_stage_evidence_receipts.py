"""Tests for stage-aware action receipts and evidence bindings (#219–#224).

Covers:
  #219 — stage-aware action receipts and v2 prebind
  #220 — context and policy hashes
  #221 — served-claim / context-projection evidence
  #222 — degraded evidence states with fail-closed semantics
  #223 — runtime manifest
  #224 — external-artifact prior-action and idempotency bindings
"""
from __future__ import annotations

import hashlib
import json
import time

import pytest

from ledger_agent.prebind import (
    PREBIND_SCHEMA, PREBIND_V2_SCHEMA, STAGE_VALUES,
    build_prebind, build_prebind_v2,
    prebind_digest, replay_prebind, replay_prebind_v2,
    validate_prebind,
)
from ledger_agent.receipts import (
    build_stage_trace, build_served_claim, validate_served_claim,
    evidence_decision, EVIDENCE_STATUS_VALUES, EVIDENCE_POLICY_VALUES,
    build_runtime_manifest, validate_runtime_manifest,
    build_external_artifact_binding, validate_external_artifact_binding,
    check_artifact_idempotent,
    EXECUTION_FAMILY_VALUES, PRIOR_ACTION_STATUS_VALUES,
)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# ── #219: stage-aware v2 prebind ───────────────────────────────────────────

def make_prebind_v2(**overrides):
    trace = build_stage_trace(
        action_key="action:deploy-42",
        stages=[
            {"stage": "proposed", "at": time.time(), "actor": "agent:hermes-prod", "digest": digest("propose")},
            {"stage": "approved", "at": time.time() + 1, "actor": "human:thomas", "digest": digest("approve")},
            {"stage": "executing", "at": time.time() + 2, "actor": "agent:hermes-prod", "digest": digest("execute")},
            {"stage": "completed", "at": time.time() + 3, "actor": "agent:hermes-prod", "digest": digest("complete")},
        ],
        human_intercept={"kind": "approval", "approver": "human:thomas", "decision": "approved"},
    )
    block = build_prebind_v2(
        attempted_action="action:deploy-42",
        actor_ref="agent:hermes-prod",
        authority_ref="authority:manifest-3",
        trusted_scope="github:Perseus-Computing-LLC/ledger",
        policy_version="policy/v3",
        evidence_hashes=[digest("source")],
        selected_context_digest=digest("context-selection"),
        resource_ref="resource:ledger-event-42",
        boundary_outcome="allow",
        non_effective_result="not_executed",
        replay_id="replay:deploy-42",
        stage_trace=trace,
        context_hash=digest("context-v2"),
        policy_hash=digest("policy-v2"),
        uncertainty="low",
    )
    block.update(overrides)
    block["prebind_hash"] = prebind_digest(block)
    return block


def test_v2_prebind_is_validated_with_stage_traces():
    block = make_prebind_v2()
    assert block["schema_version"] == PREBIND_V2_SCHEMA
    assert prebind_digest(block) == block["prebind_hash"]
    valid, errors = validate_prebind(block)
    assert valid, errors
    # Stage trace is present
    assert block["stage_trace"]["schema"] == "perseus-ledger-stage-trace/v1"
    assert len(block["stage_trace"]["stages"]) == 4
    assert block["stage_trace"]["stages"][0]["stage"] == "proposed"
    assert block["stage_trace"]["stages"][-1]["stage"] == "completed"


def test_v2_prebind_validates_stage_trace():
    block = make_prebind_v2()
    # Invalid stage
    block["stage_trace"]["stages"][0]["stage"] = "invalid_stage"
    block["prebind_hash"] = prebind_digest(block)
    valid, errors = validate_prebind(block)
    assert not valid


def test_v2_prebind_rejects_v2_without_stage_trace_if_context_hash_set():
    # v2 blocks can omit stage_trace but must have valid context/policy hashes
    block = make_prebind_v2()
    block.pop("stage_trace")
    block["prebind_hash"] = prebind_digest(block)
    valid, _ = validate_prebind(block)
    assert valid  # stage_trace is optional in v2


def test_v2_prebind_preserves_v1_backward_compatibility():
    # A v1 prebind should still validate
    block = build_prebind(
        attempted_action="action:test",
        actor_ref="agent:test",
        authority_ref="auth:1",
        trusted_scope="scope:test",
        policy_version="p1",
        evidence_hashes=[digest("src")],
        selected_context_digest=digest("ctx"),
        resource_ref="res:1",
        boundary_outcome="hold",
        non_effective_result="not_executed",
        replay_id="rep:1",
    )
    valid, errors = validate_prebind(block)
    assert valid, errors


def test_v2_prebind_rejects_unknown_fields():
    block = make_prebind_v2()
    block["extra_unknown"] = "bad"
    block["prebind_hash"] = prebind_digest(block)
    valid, errors = validate_prebind(block)
    assert not valid
    assert any("unknown_field" in e for e in errors)


# ── #220: context and policy hashes ────────────────────────────────────────

def test_v2_prebind_carries_context_and_policy_hashes():
    ctx_hash = digest("context-123")
    pol_hash = digest("policy-456")
    block = make_prebind_v2(context_hash=ctx_hash, policy_hash=pol_hash)
    assert block["context_hash"] == ctx_hash
    assert block["policy_hash"] == pol_hash
    valid, _ = validate_prebind(block)
    assert valid


def test_v2_prebind_rejects_invalid_hash_shapes():
    block = make_prebind_v2(context_hash="not-a-hash")
    valid, errors = validate_prebind(block)
    assert not valid
    assert "context_hash" in errors


# ── #219/#220: v2 replay with degraded evidence ────────────────────────────

def test_v2_replay_detects_context_and_policy_changes():
    prior = make_prebind_v2(boundary_outcome="hold", non_effective_result="not_executed")
    comparison = replay_prebind_v2(
        prior,
        current_context_hash=digest("new-context"),
        current_policy_hash=digest("new-policy"),
        current_state={"authority_ok": True, "evidence_current": True, "action_allowed": True},
    )
    assert "context_hash" in comparison["changed_fields"]
    assert "policy_hash" in comparison["changed_fields"]


def test_v2_replay_blocks_on_degraded_evidence():
    prior = make_prebind_v2(boundary_outcome="allow", non_effective_result="not_executed")
    comparison = replay_prebind_v2(
        prior,
        current_state={
            "authority_ok": True, "evidence_current": True,
            "action_allowed": True, "evidence_degraded": True,
            "evidence_policy_degraded_allow": False,
        },
        current_evidence_status="timeout",
    )
    assert comparison["admission"] == "not_admitted"
    assert comparison["replayed_boundary_outcome"] == "hold"
    assert "evidence_degraded" in comparison["reason_codes"]
    assert comparison["evidence_status"] == "timeout"


def test_v2_replay_admits_on_fresh_evidence_with_no_degradation():
    prior = make_prebind_v2(boundary_outcome="allow", non_effective_result="not_executed")
    comparison = replay_prebind_v2(
        prior,
        current_state={"authority_ok": True, "evidence_current": True, "action_allowed": True},
    )
    assert comparison["admission"] == "admitted"
    assert comparison["replayed_boundary_outcome"] == "allow"


# ── #221: served-claim evidence ────────────────────────────────────────────

def test_served_claim_is_hash_bound():
    claim = build_served_claim(
        source_ref="vault:mem-abc",
        event_ref="vault:event-123",
        immutable_span="span:0-100",
        derivation="extractor/v1",
        valid_from=time.time() - 3600,
        authority_ref="auth:manifest-3",
        provenance_class="user_confirmed",
        state="active",
        scope_anchor="github:Perseus-Computing-LLC/ledger",
        projection_digest=digest("projection"),
        retrieval_status="fresh",
        decision_reason="evidence-driven",
    )
    assert claim["schema"] == "perseus-ledger-served-claim/v1"
    assert len(claim["claim_digest"]) == 64
    valid, errors = validate_served_claim(claim)
    assert valid, errors


def test_served_claim_rejects_missing_source():
    claim = build_served_claim(source_ref="", event_ref="evt:1")
    valid, errors = validate_served_claim(claim)
    assert not valid
    assert "served_claim_source_ref" in errors


def test_served_claim_is_persisted_in_receipt(tmp_path):
    from ledger_agent import db, metering
    from ledger_agent.server.api import audit_json

    conn = db.connect(str(tmp_path / "served-claim.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "served-claim-org", tier="free")["id"]

    claim = build_served_claim(
        source_ref="vault:mem-abc",
        event_ref="vault:event-123",
        retrieval_status="fresh",
    )
    metering.record_usage(
        conn, org_id, provider="openai", model="fixture",
        external_ref="claim-test", input_tokens=1, output_tokens=1,
        cost_usd=0.01, served_claim=claim,
    )
    receipt = audit_json(conn, org_id, external_ref="claim-test")
    assert receipt["events"][0]["served_claim"]["claim_digest"] == claim["claim_digest"]
    conn.close()


def test_served_claim_rejects_invalid_dict(tmp_path):
    from ledger_agent import db, metering
    conn = db.connect(str(tmp_path / "bad-claim.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "bad-claim", tier="free")["id"]
    with pytest.raises(ValueError, match="invalid served_claim"):
        metering.record_usage(
            conn, org_id, provider="openai", model="fixture",
            input_tokens=1, output_tokens=1, cost_usd=0.01,
            served_claim={"schema": "wrong"},
        )
    conn.close()


# ── #222: degraded evidence states ─────────────────────────────────────────

def test_evidence_decision_required_fresh_only():
    for status in ("partial", "timeout", "unavailable", "empty", "stale", "abstain", "review"):
        d = evidence_decision(status, "required")
        assert d["allowed"] is False, f"{status} should not be allowed under required"
        assert d["boundary_outcome"] in ("hold", "abstain")

    d = evidence_decision("fresh", "required")
    assert d["allowed"] is True
    assert d["boundary_outcome"] == "allow"


def test_evidence_decision_optional_allows_partial():
    d = evidence_decision("partial", "optional")
    assert d["allowed"] is True
    d2 = evidence_decision("timeout", "optional")
    assert d2["allowed"] is False
    assert d2["boundary_outcome"] == "abstain"


def test_evidence_decision_off_always_allows():
    for status in EVIDENCE_STATUS_VALUES:
        d = evidence_decision(status, "off")
        assert d["allowed"] is True


def test_evidence_decision_rejects_invalid_status():
    with pytest.raises(ValueError, match="invalid evidence_status"):
        evidence_decision("bogus", "required")


def test_evidence_decision_rejects_invalid_policy():
    with pytest.raises(ValueError, match="invalid evidence_policy"):
        evidence_decision("fresh", "bogus")


def test_evidence_status_is_persisted_in_receipt(tmp_path):
    from ledger_agent import db, metering
    from ledger_agent.server.api import audit_json

    conn = db.connect(str(tmp_path / "evidence-status.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "evidence-status-org", tier="free")["id"]

    metering.record_usage(
        conn, org_id, provider="openai", model="fixture",
        external_ref="degraded-test", input_tokens=1, output_tokens=1,
        cost_usd=0.01, evidence_status="timeout",
    )
    receipt = audit_json(conn, org_id, external_ref="degraded-test")
    assert receipt["events"][0]["evidence_status"] == "timeout"

    # Unknown evidence_status is rejected
    with pytest.raises(ValueError, match="evidence_status must be one of"):
        metering.record_usage(
            conn, org_id, provider="openai", model="fixture",
            input_tokens=1, output_tokens=1, cost_usd=0.01,
            evidence_status="bogus",
        )
    conn.close()


# ── #223: runtime manifest ─────────────────────────────────────────────────

def test_runtime_manifest_is_hash_bound():
    manifest = build_runtime_manifest(
        adapter_name="hermes-agent",
        adapter_version="2.0.0",
        capabilities=["persistent_state", "seeded_memory", "filesystem", "tools"],
        model="gpt-5.6-luna",
        provider="openai",
        auth_mode="account",
        execution_family="stateful",
        repository_revision="abc123def",
        workspace_scope="github:Perseus-Computing-LLC/ledger",
        seed=42,
        retention_class="full",
        timestamps={"started": time.time()},
    )
    assert manifest["schema"] == "perseus-ledger-runtime-manifest/v1"
    assert len(manifest["manifest_digest"]) == 64
    valid, errors = validate_runtime_manifest(manifest)
    assert valid, errors


def test_runtime_manifest_rejects_missing_required():
    manifest = {"schema": "perseus-ledger-runtime-manifest/v1"}
    valid, errors = validate_runtime_manifest(manifest)
    assert not valid
    assert "runtime_manifest_adapter_name" in errors


def test_runtime_manifest_rejects_invalid_auth_mode():
    manifest = build_runtime_manifest(
        adapter_name="test", adapter_version="1.0", capabilities=[],
    )
    manifest["auth_mode"] = "invalid"
    manifest["manifest_digest"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    valid, errors = validate_runtime_manifest(manifest)
    assert not valid
    assert "runtime_manifest_auth_mode" in errors


def test_runtime_manifest_distinguishes_execution_families():
    for family in EXECUTION_FAMILY_VALUES:
        manifest = build_runtime_manifest(
            adapter_name="test", adapter_version="1.0",
            capabilities=[], execution_family=family,
        )
        assert manifest["execution_family"] == family


def test_runtime_manifest_is_persisted_in_receipt(tmp_path):
    from ledger_agent import db, metering
    from ledger_agent.server.api import audit_json

    conn = db.connect(str(tmp_path / "runtime-manifest.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "runtime-org", tier="free")["id"]

    manifest = build_runtime_manifest(
        adapter_name="hermes-agent", adapter_version="2.0.0",
        capabilities=["persistent_state"],
        seed=42,
    )
    metering.record_usage(
        conn, org_id, provider="openai", model="fixture",
        external_ref="runtime-test", input_tokens=1, output_tokens=1,
        cost_usd=0.01, runtime_manifest=manifest,
    )
    receipt = audit_json(conn, org_id, external_ref="runtime-test")
    assert receipt["events"][0]["runtime_manifest"]["manifest_digest"] == manifest["manifest_digest"]
    assert receipt["events"][0]["runtime_manifest"]["seed"] == 42
    conn.close()


# ── #224: external-artifact binding ────────────────────────────────────────

def test_external_artifact_is_hash_bound():
    binding = build_external_artifact_binding(
        source_system="github",
        source_type="issue",
        artifact_id="Perseus-Computing-LLC/ledger#42",
        version_hash=digest("v1"),
        destination_scope="action:deploy",
        resource_scope="github:Perseus-Computing-LLC/ledger",
        prior_action_status="unknown",
        idempotency_key="idem-deploy-42",
    )
    assert binding["schema"] == "perseus-ledger-external-artifact/v1"
    assert len(binding["binding_digest"]) == 64
    valid, errors = validate_external_artifact_binding(binding)
    assert valid, errors


def test_external_artifact_rejects_missing_id():
    binding = build_external_artifact_binding(
        source_system="github", source_type="pr", artifact_id="",
    )
    valid, errors = validate_external_artifact_binding(binding)
    assert not valid
    assert "artifact_id" in errors


def test_external_artifact_idempotency_prevents_duplicate():
    binding = build_external_artifact_binding(
        source_system="github", source_type="issue",
        artifact_id="issue-42",
        version_hash=digest("v1"),
        destination_scope="dest-A",
        idempotency_key="key-1",
    )
    prior = [
        {
            "artifact_id": "issue-42",
            "version_hash": digest("v1"),
            "destination_scope": "dest-A",
            "prior_action_status": "handled",
        }
    ]
    result = check_artifact_idempotent(binding, prior)
    assert result["allowed"] is False
    assert result["reason"] == "duplicate"


def test_external_artifact_idempotency_allows_new_version():
    binding = build_external_artifact_binding(
        source_system="github", source_type="issue",
        artifact_id="issue-42",
        version_hash=digest("v2"),
        destination_scope="dest-A",
        idempotency_key="key-2",
    )
    prior = [
        {
            "artifact_id": "issue-42",
            "version_hash": digest("v1"),
            "destination_scope": "dest-A",
            "prior_action_status": "handled",
        }
    ]
    result = check_artifact_idempotent(binding, prior)
    assert result["allowed"] is True
    assert result["reason"] == "new_version"


def test_external_artifact_idempotency_rejects_cross_destination():
    binding = build_external_artifact_binding(
        source_system="github", source_type="issue",
        artifact_id="issue-42",
        version_hash=digest("v1"),
        destination_scope="dest-B",
        idempotency_key="key-3",
    )
    prior = [
        {
            "artifact_id": "issue-42",
            "version_hash": digest("v1"),
            "destination_scope": "dest-A",
            "prior_action_status": "handled",
        }
    ]
    result = check_artifact_idempotent(binding, prior)
    assert result["allowed"] is False
    assert result["reason"] == "scope_mismatch"


def test_external_artifact_idempotency_allows_first_action():
    binding = build_external_artifact_binding(
        source_system="github", source_type="issue",
        artifact_id="issue-new",
        version_hash=digest("v1"),
        idempotency_key="key-new",
    )
    result = check_artifact_idempotent(binding, [])
    assert result["allowed"] is True
    assert result["reason"] == "first_action"


def test_external_artifact_allows_retry_after_failed():
    binding = build_external_artifact_binding(
        source_system="github", source_type="issue",
        artifact_id="issue-42",
        version_hash=digest("v1"),
        destination_scope="dest-A",
        idempotency_key="retry-1",
    )
    prior = [
        {
            "artifact_id": "issue-42",
            "version_hash": digest("v1"),
            "destination_scope": "dest-A",
            "prior_action_status": "attempted_failed",
        }
    ]
    result = check_artifact_idempotent(binding, prior)
    assert result["allowed"] is True  # Failed prior action; new attempt allowed


def test_external_artifact_is_persisted_in_receipt(tmp_path):
    from ledger_agent import db, metering
    from ledger_agent.server.api import audit_json

    conn = db.connect(str(tmp_path / "artifact.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "artifact-org", tier="free")["id"]

    binding = build_external_artifact_binding(
        source_system="github", source_type="issue",
        artifact_id="issue-42",
        version_hash=digest("v1"),
        idempotency_key="idem-1",
    )
    metering.record_usage(
        conn, org_id, provider="openai", model="fixture",
        external_ref="artifact-test", input_tokens=1, output_tokens=1,
        cost_usd=0.01, external_artifact=binding,
    )
    receipt = audit_json(conn, org_id, external_ref="artifact-test")
    assert receipt["events"][0]["external_artifact_binding"]["binding_digest"] == binding["binding_digest"]
    assert receipt["events"][0]["external_artifact_binding"]["artifact_id"] == "issue-42"
    conn.close()


# ── chain integrity ────────────────────────────────────────────────────────

def test_v18_fields_preserve_chain_integrity(tmp_path):
    """All new v18 fields are optional and chain-preserving."""
    from ledger_agent import db, metering

    conn = db.connect(str(tmp_path / "chain-v18.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "chain-v18", tier="free")["id"]

    # Event without v18 fields (legacy)
    metering.record_usage(
        conn, org_id, provider="openai", model="fixture",
        input_tokens=1, output_tokens=1, cost_usd=0.01,
    )
    chain1 = db.verify_chain(conn, org_id)
    assert chain1["ok"] is True

    # Event with all v18 fields
    claim = build_served_claim(source_ref="a", event_ref="b")
    manifest = build_runtime_manifest(
        adapter_name="test", adapter_version="1.0", capabilities=[],
    )
    artifact = build_external_artifact_binding(
        source_system="test", source_type="test", artifact_id="x",
    )
    metering.record_usage(
        conn, org_id, provider="openai", model="fixture",
        input_tokens=1, output_tokens=1, cost_usd=0.01,
        served_claim=claim,
        evidence_status="fresh",
        runtime_manifest=manifest,
        external_artifact=artifact,
    )
    chain2 = db.verify_chain(conn, org_id)
    assert chain2["ok"] is True

    # Event without v18 fields again (legacy continuity)
    metering.record_usage(
        conn, org_id, provider="openai", model="fixture",
        input_tokens=1, output_tokens=1, cost_usd=0.01,
    )
    chain3 = db.verify_chain(conn, org_id)
    assert chain3["ok"] is True
    conn.close()


def test_v2_prebind_in_chain_preserves_integrity(tmp_path):
    from ledger_agent import db, metering
    from ledger_agent.server.api import audit_json

    conn = db.connect(str(tmp_path / "prebind-v2-chain.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "prebind-v2-chain", tier="free")["id"]

    block = make_prebind_v2(boundary_outcome="allow", non_effective_result="not_executed")
    metering.record_usage(
        conn, org_id, provider="openai", model="fixture",
        external_ref="v2-prebind", input_tokens=1, output_tokens=1,
        cost_usd=0.01, prebind=block,
    )
    receipt = audit_json(conn, org_id, external_ref="v2-prebind")
    assert receipt["events"][0]["prebind"]["schema_version"] == PREBIND_V2_SCHEMA
    assert receipt["events"][0]["prebind"]["stage_trace"] is not None
    assert db.verify_chain(conn, org_id)["ok"] is True
    conn.close()


def test_legacy_events_render_with_nulls_for_v18_fields(tmp_path):
    from ledger_agent import db, metering
    from ledger_agent.server.api import audit_json

    conn = db.connect(str(tmp_path / "legacy-v18.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "legacy-v18", tier="free")["id"]

    metering.record_usage(
        conn, org_id, provider="openai", model="fixture",
        external_ref="legacy", input_tokens=1, output_tokens=1,
        cost_usd=0.01,
    )
    receipt = audit_json(conn, org_id, external_ref="legacy")
    event = receipt["events"][0]
    assert event["served_claim"] is None
    assert event["evidence_status"] is None
    assert event["runtime_manifest"] is None
    assert event["external_artifact_binding"] is None
    conn.close()
