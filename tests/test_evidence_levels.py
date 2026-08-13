"""Evidence levels for receipts (#235).

One test per level, plus the issue's success criteria:
- a sign-then-abort receipt verifies at Attested but NOT Inclusion;
- a Commit receipt's inclusion proof verifies against a durable anchor after
  restart;
- watermark reclamation downgrades Replay while Inclusion stays verifiable;
- a malformed receipt fails at Structural with a stable reason.
"""
import copy
import time

from ledger_agent import db, evidence_levels, metering, prebind
from ledger_agent.server.api import audit_json

KEY = b"ledger-test-signing-key-32-bytes!"
OTHER_KEY = b"other-test-signing-key-32-bytes!"
SCOPE = "github:Perseus-Computing-LLC/ledger"


def _allow_prebind(replay_id: str = "rpl-allow") -> dict:
    return prebind.build_prebind(
        attempted_action="deploy", actor_ref="hermes-prod", authority_ref="auth-1",
        trusted_scope=SCOPE, policy_version="pol/v1", evidence_hashes=["a" * 64],
        selected_context_digest="b" * 64, resource_ref="res-1",
        boundary_outcome="allow", non_effective_result="not_executed",
        replay_id=replay_id,
    )


def _record_commit(conn, org_id, external_ref, *, with_prebind=True,
                   evidence_hashes=None, prebind_block=None):
    """Record an executed (Commit) action with full action provenance."""
    pb = prebind_block if prebind_block is not None else _allow_prebind()
    return metering.record_usage(
        conn, org_id, provider="openai", model="gpt-fixture",
        task_type="deploy", external_ref=external_ref,
        input_tokens=10, output_tokens=5, cost_usd=0.1,
        evidence_hashes=evidence_hashes if evidence_hashes is not None else ["a" * 64],
        policy_version="pol/v1",
        agent_id="hermes-prod", authority_manifest_ref="auth-1",
        scope_anchor=SCOPE, action_intent_hash="c" * 64, action_status="executed",
        prebind=pb if with_prebind else None,
        ts=time.time(),
    )


def _signed_receipt(conn, org_id, external_ref):
    """A ledger-rendered receipt carrying attestation + signature."""
    return audit_json(
        conn, org_id, external_ref=external_ref,
        key_registry={"ledger-ops": KEY}, sign_key_id="ledger-ops",
    )


def _strip_replay_objects(receipt):
    """Simulate watermark reclamation: re-issue without the replay inputs."""
    stripped = copy.deepcopy(receipt)
    stripped.pop("signature", None)
    stripped.pop("attestation", None)
    for event in stripped["events"]:
        event["prebind"] = None
    return stripped


# ── structural ──────────────────────────────────────────────────────────────


def test_structural_level_verifies_well_formed_receipt(tmp_path):
    conn = db.connect(str(tmp_path / "structural.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "el-structural", tier="free")["id"]
    metering.record_usage(
        conn, org_id, provider="openai", model="gpt-fixture",
        task_type="retrieve", external_ref="task-1",
        input_tokens=10, output_tokens=5, cost_usd=0.1, ts=time.time(),
    )
    receipt = audit_json(conn, org_id, external_ref="task-1")
    evidence = receipt["verification"]["evidence"]
    assert evidence["level"] == "structural"
    assert evidence["levels"] == {
        "structural": True, "attested": False, "replay": False, "inclusion": False,
    }
    assert evidence["reasons"]["replay"] == "replay:no_replayable_inputs"
    assert evidence["reasons"]["inclusion"] == "inclusion:anchor_missing"
    conn.close()


def test_malformed_receipt_fails_structural_with_stable_reason(tmp_path):
    conn = db.connect(str(tmp_path / "malformed.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "el-malformed", tier="free")["id"]
    metering.record_usage(
        conn, org_id, provider="openai", model="gpt-fixture",
        task_type="retrieve", external_ref="task-1",
        input_tokens=10, output_tokens=5, cost_usd=0.1, ts=time.time(),
    )
    receipt = audit_json(conn, org_id, external_ref="task-1")
    assert receipt["verification"]["evidence"]["levels"]["structural"] is True

    # Two events so the inter-event hash link is exercised: the FIRST receipt
    # event may legitimately point at a predecessor outside the task, so a
    # single-event receipt has no link to break.
    metering.record_usage(
        conn, org_id, provider="openai", model="gpt-fixture",
        task_type="retrieve", external_ref="task-1",
        input_tokens=10, output_tokens=5, cost_usd=0.1, ts=time.time(),
    )
    receipt = audit_json(conn, org_id, external_ref="task-1")
    assert len(receipt["events"]) == 2

    broken_hash = copy.deepcopy(receipt)
    broken_hash["events"][0]["row_hash"] = "not-a-hex-digest"
    evidence = evidence_levels.verify_receipt_evidence(conn, org_id, broken_hash)
    assert evidence["levels"]["structural"] is False
    assert evidence["reasons"]["structural"] == "structural:row_hash[0]"
    assert evidence["level"] is None

    broken_link = copy.deepcopy(receipt)
    broken_link["events"][1]["prev_hash"] = "d" * 64
    evidence = evidence_levels.verify_receipt_evidence(conn, org_id, broken_link)
    assert evidence["reasons"]["structural"] == "structural:event_hash_link"

    empty_events = copy.deepcopy(receipt)
    empty_events["events"] = []
    evidence = evidence_levels.verify_receipt_evidence(conn, org_id, empty_events)
    assert evidence["reasons"]["structural"] == "structural:events"

    bad_version = copy.deepcopy(receipt)
    bad_version["receipt_version"] = "perseus-evidence-receipt/v0"
    evidence = evidence_levels.verify_receipt_evidence(conn, org_id, bad_version)
    assert evidence["reasons"]["structural"] == "structural:receipt_version"

    bad_claim = copy.deepcopy(receipt)
    bad_claim["claimed_evidence_level"] = "paranormal"
    evidence = evidence_levels.verify_receipt_evidence(conn, org_id, bad_claim)
    assert evidence["reasons"]["structural"] == "structural:claimed_level"
    conn.close()


def test_receipt_signature_binds_content_and_is_structural(tmp_path):
    conn = db.connect(str(tmp_path / "signature.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "el-signature", tier="free")["id"]
    metering.record_usage(
        conn, org_id, provider="openai", model="gpt-fixture",
        task_type="retrieve", external_ref="task-1",
        input_tokens=10, output_tokens=5, cost_usd=0.1, ts=time.time(),
    )
    receipt = audit_json(conn, org_id, external_ref="task-1")
    signed = evidence_levels.sign_receipt(receipt, key_id="ledger-ops", key=KEY)
    evidence = evidence_levels.verify_receipt_evidence(
        conn, org_id, signed, key_registry={"ledger-ops": KEY},
    )
    assert evidence["levels"]["structural"] is True
    assert evidence["reasons"]["structural"] == "structural:ok"

    tampered = copy.deepcopy(signed)
    tampered["organization"] = {"id": org_id, "name": "tampered"}
    evidence = evidence_levels.verify_receipt_evidence(
        conn, org_id, tampered, key_registry={"ledger-ops": KEY},
    )
    assert evidence["levels"]["structural"] is False
    assert evidence["reasons"]["structural"] == "structural:signature_invalid"

    evidence = evidence_levels.verify_receipt_evidence(
        conn, org_id, signed, key_registry={"someone-else": OTHER_KEY},
    )
    assert evidence["reasons"]["structural"] == "structural:signature_unknown_key"
    conn.close()


# ── attested ────────────────────────────────────────────────────────────────


def test_attested_level_requires_trusted_attestation_of_terminal_stage(tmp_path):
    conn = db.connect(str(tmp_path / "attested.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "el-attested", tier="free")["id"]
    _record_commit(conn, org_id, "commit-1", with_prebind=False)
    receipt = audit_json(conn, org_id, external_ref="commit-1")

    attested = evidence_levels.attest_receipt(
        receipt, key_id="ledger-ops", key=KEY,
        stage="executed", reason="terminal stage attested by ops",
    )
    evidence = evidence_levels.verify_receipt_evidence(
        conn, org_id, attested, key_registry={"ledger-ops": KEY},
    )
    assert evidence["levels"]["attested"] is True
    assert evidence["level"] == "attested"
    assert evidence["levels"]["inclusion"] is False
    assert evidence["inclusion_required"] is True

    bad_sig = copy.deepcopy(attested)
    bad_sig["attestation"]["sig"] = "0" * 64
    evidence = evidence_levels.verify_receipt_evidence(
        conn, org_id, bad_sig, key_registry={"ledger-ops": KEY},
    )
    assert evidence["levels"]["attested"] is False
    assert evidence["reasons"]["attested"] == "attested:bad_signature"

    mismatched = evidence_levels.attest_receipt(
        receipt, key_id="ledger-ops", key=KEY,
        stage="failed", reason="wrong stage",
    )
    evidence = evidence_levels.verify_receipt_evidence(
        conn, org_id, mismatched, key_registry={"ledger-ops": KEY},
    )
    assert evidence["reasons"]["attested"] == "attested:stage_mismatch"
    conn.close()


def test_sign_then_abort_verifies_attested_not_inclusion(tmp_path):
    """A signer produced a receipt before its backing store committed.

    The receipt is signed and attested — verifiable at Attested — but its
    events never landed in durable state, so Inclusion cannot be claimed.
    """
    conn = db.connect(str(tmp_path / "sign-then-abort.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "el-abort", tier="free")["id"]

    receipt = {
        "receipt_version": "perseus-evidence-receipt/v1",
        "organization": {"id": org_id, "name": "aborted-org"},
        "external_ref": "aborted-task",
        "events": [{
            "event_id": "evt_never_committed",
            "ts": time.time(),
            "actor": "usr_ops",
            "action": "deploy",
            "model_config": {"provider": "openai", "model": "gpt-fixture"},
            "external_ref": "aborted-task",
            "evidence": {"source_hashes": []},
            "decision_context": {"policy_version": None, "result_hash": None,
                                 "human_review": None, "correction_ref": None},
            "context_render_binding": {"schema_version": None, "render_hash": None,
                                       "served_memory_provenance_hash": None,
                                       "action_receipt_hash": None},
            "prebind": None,
            "served_claim": None,
            "evidence_status": None,
            "runtime_manifest": None,
            "external_artifact_binding": None,
            "action_authorization": {
                "agent_id": "hermes-prod", "authority_manifest_ref": "auth-1",
                "scope_anchor": SCOPE, "action_intent_hash": "c" * 64,
                "status": "executed", "approval_ref": None,
            },
            "resource_allocation": {"input_tokens": 10, "output_tokens": 5,
                                    "cost_usd": 0.1, "estimated": False},
            "prev_hash": None,
            "row_hash": None,
        }],
    }
    attested = evidence_levels.attest_receipt(
        receipt, key_id="ledger-ops", key=KEY,
        stage="executed", reason="commit attested before durable write",
    )
    signed = evidence_levels.sign_receipt(attested, key_id="ledger-ops", key=KEY)
    evidence = evidence_levels.verify_receipt_evidence(
        conn, org_id, signed, key_registry={"ledger-ops": KEY},
    )

    assert evidence["level"] == "attested"
    assert evidence["levels"]["attested"] is True
    assert evidence["levels"]["inclusion"] is False
    assert evidence["reasons"]["inclusion"].startswith("inclusion:event_missing")
    assert evidence["commit_receipt"] is True
    assert evidence["inclusion_required"] is True
    assert evidence["inclusion_anchor"] is None
    conn.close()


# ── replay ──────────────────────────────────────────────────────────────────


def test_replay_level_reproduces_recorded_transition(tmp_path):
    conn = db.connect(str(tmp_path / "replay.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "el-replay", tier="free")["id"]

    # Commit: retained inputs + pinned versions reproduce the executed
    # transition deterministically.
    _record_commit(conn, org_id, "commit-1")
    receipt = audit_json(conn, org_id, external_ref="commit-1")
    evidence = receipt["verification"]["evidence"]
    assert evidence["levels"]["replay"] is True
    assert evidence["reasons"]["replay"] == "replay:ok"
    assert evidence["level"] == "replay"

    # Deny: a hold bound to an approval reproduces the non-admission.
    hold = prebind.build_prebind(
        attempted_action="deploy", actor_ref="hermes-prod", authority_ref="auth-1",
        trusted_scope=SCOPE, policy_version="pol/v1", evidence_hashes=["a" * 64],
        selected_context_digest="b" * 64, resource_ref="res-1",
        boundary_outcome="hold", non_effective_result="held",
        replay_id="rpl-hold", approval_ref="appr-1",
    )
    metering.record_usage(
        conn, org_id, provider="openai", model="gpt-fixture",
        task_type="deploy", external_ref="deny-1",
        input_tokens=0, output_tokens=0, cost_usd=0.0,
        evidence_hashes=["a" * 64], policy_version="pol/v1",
        prebind=hold, ts=time.time(),
    )
    evidence = audit_json(conn, org_id, external_ref="deny-1")["verification"]["evidence"]
    assert evidence["levels"]["replay"] is True
    assert evidence["level"] == "replay"
    conn.close()


def test_replay_requires_pinned_versions_to_match(tmp_path):
    conn = db.connect(str(tmp_path / "replay-pinned.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "el-replay-pinned", tier="free")["id"]
    # The prebind pinned evidence hashes "a"*64; the event recorded "c"*64.
    _record_commit(conn, org_id, "commit-1", evidence_hashes=["c" * 64])
    receipt = audit_json(conn, org_id, external_ref="commit-1")
    evidence = receipt["verification"]["evidence"]
    assert evidence["levels"]["replay"] is False
    assert evidence["reasons"]["replay"] == "replay:pinned_version_changed[0]"
    conn.close()


def test_watermark_reclamation_downgrades_replay_keeps_inclusion(tmp_path):
    # Part A — no durable anchor: reclaiming the replay inputs downgrades the
    # receipt from Replay to Attested.
    conn = db.connect(str(tmp_path / "reclaim-a.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "el-reclaim-a", tier="free")["id"]
    _record_commit(conn, org_id, "commit-1")
    receipt = _signed_receipt(conn, org_id, "commit-1")
    assert receipt["verification"]["evidence"]["level"] == "replay"

    stripped = _strip_replay_objects(receipt)
    stripped["claimed_evidence_level"] = "replay"
    stripped = evidence_levels.attest_receipt(
        stripped, key_id="ledger-ops", key=KEY,
        stage="executed", reason="ledger-audit-render",
    )
    stripped = evidence_levels.sign_receipt(stripped, key_id="ledger-ops", key=KEY)
    evidence = evidence_levels.verify_receipt_evidence(
        conn, org_id, stripped, key_registry={"ledger-ops": KEY},
    )
    assert evidence["levels"]["replay"] is False
    assert evidence["reasons"]["replay"] == "replay:inputs_reclaimed"
    assert evidence["level"] == "attested"
    assert evidence["downgrades"] == [
        {"from": "replay", "to": "attested", "reason": "replay:inputs_reclaimed"},
    ]
    conn.close()

    # Part B — durable anchor retained: the same reclamation leaves Inclusion
    # verifiable while Replay drops out.
    conn = db.connect(str(tmp_path / "reclaim-b.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "el-reclaim-b", tier="free")["id"]
    _record_commit(conn, org_id, "commit-1")
    cp = db.checkpoint_chain(conn, org_id)
    assert cp is not None
    receipt = _signed_receipt(conn, org_id, "commit-1")
    assert receipt["verification"]["evidence"]["level"] == "inclusion"

    stripped = _strip_replay_objects(receipt)
    stripped = evidence_levels.attest_receipt(
        stripped, key_id="ledger-ops", key=KEY,
        stage="executed", reason="ledger-audit-render",
    )
    stripped = evidence_levels.sign_receipt(stripped, key_id="ledger-ops", key=KEY)
    evidence = evidence_levels.verify_receipt_evidence(
        conn, org_id, stripped, key_registry={"ledger-ops": KEY},
        checkpoints=db.list_checkpoints(conn, org_id),
    )
    assert evidence["levels"]["replay"] is False
    assert evidence["reasons"]["replay"] == "replay:inputs_reclaimed"
    assert evidence["levels"]["inclusion"] is True
    assert evidence["level"] == "inclusion"
    assert evidence["inclusion_anchor"]["checkpoint_id"] == cp["id"]
    conn.close()


# ── inclusion ───────────────────────────────────────────────────────────────


def test_inclusion_commit_receipt_verifies_after_restart(tmp_path):
    path = str(tmp_path / "restart.db")
    conn = db.connect(path)
    db.init_schema(conn)
    org_id = db.create_org(conn, "el-restart", tier="free")["id"]
    _record_commit(conn, org_id, "commit-1")
    cp = db.checkpoint_chain(conn, org_id)
    assert cp is not None

    receipt = _signed_receipt(conn, org_id, "commit-1")
    evidence = receipt["verification"]["evidence"]
    assert evidence["level"] == "inclusion"
    assert evidence["inclusion_anchor"]["status"] == "ok"
    assert evidence["inclusion_anchor"]["head_hash"] == cp["head_hash"]
    conn.close()

    # Restart: a fresh connection must still verify the durable anchor.
    conn = db.connect(path)
    evidence = _signed_receipt(conn, org_id, "commit-1")["verification"]["evidence"]
    assert evidence["level"] == "inclusion"
    assert evidence["inclusion_anchor"]["status"] == "ok"
    assert evidence["inclusion_anchor"]["head_hash"] == cp["head_hash"]
    assert evidence["commit_receipt"] is True
    assert evidence["inclusion_required"] is True
    conn.close()
