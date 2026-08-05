import pytest

from plutus_agent import db, metering
from plutus_agent.server.api import audit_json
import time


def test_audit_receipt_is_available_to_free_and_recommends_five_percent(tmp_path):
    conn = db.connect(str(tmp_path / "audit.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "free-audit", tier="free")["id"]
    metering.record_usage(
        conn, org_id, provider="openai", model="gpt-fixture",
        input_tokens=10, output_tokens=10, cost_usd=1.0,
        baseline_cost_usd=11.0, ts=time.time(),
    )
    receipt = audit_json(conn, org_id)
    assert receipt["audit_access"] is True
    assert receipt["recommended_donation_bps"] == 500
    assert receipt["recommended_donation_usd"] == 0.5
    assert "ledger_integrity" in receipt
    assert "savings" in receipt
    conn.close()


def test_evidence_receipt_links_external_ref_to_hash_chained_events(tmp_path):
    conn = db.connect(str(tmp_path / "evidence.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "evidence-receipt", tier="free")["id"]
    first = metering.record_usage(
        conn, org_id, provider="openai", model="gpt-fixture",
        task_type="retrieve", external_ref="task-42",
        input_tokens=10, output_tokens=5, cost_usd=0.1, ts=time.time(),
    )
    second = metering.record_usage(
        conn, org_id, provider="openai", model="gpt-fixture",
        task_type="draft", external_ref="task-42",
        input_tokens=5, output_tokens=10, cost_usd=0.2, ts=time.time(),
    )

    receipt = audit_json(conn, org_id, external_ref="task-42")

    assert receipt["receipt_version"] == "perseus-evidence-receipt/v1"
    assert receipt["external_ref"] == "task-42"
    assert receipt["verification"]["chain_ok"] is True
    assert [event["event_id"] for event in receipt["events"]] == [
        first.event_id, second.event_id,
    ]
    assert receipt["events"][1]["prev_hash"] == receipt["events"][0]["row_hash"]
    assert receipt["events"][0]["resource_allocation"]["cost_usd"] == 0.1
    conn.close()


def test_authorized_action_provenance_is_hash_covered_and_rendered(tmp_path):
    conn = db.connect(str(tmp_path / "authorized-action.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "authorized-action", tier="free")["id"]
    intent_hash = "c" * 64
    metering.record_usage(
        conn, org_id, provider="openai", model="gpt-fixture",
        task_type="deploy", external_ref="deploy-42",
        input_tokens=10, output_tokens=5, cost_usd=0.1,
        agent_id="hermes-prod", authority_manifest_ref="auth-42@3",
        scope_anchor="github:Perseus-Computing-LLC/plutus",
        action_intent_hash=intent_hash, action_status="executed",
    )

    receipt = audit_json(conn, org_id, external_ref="deploy-42")
    event = receipt["events"][0]
    assert event["action_authorization"] == {
        "agent_id": "hermes-prod",
        "authority_manifest_ref": "auth-42@3",
        "scope_anchor": "github:Perseus-Computing-LLC/plutus",
        "action_intent_hash": intent_hash,
        "status": "executed",
        "approval_ref": None,
    }
    conn.execute("UPDATE usage_events SET action_status='failed' WHERE id=?", (event["event_id"],))
    assert db.verify_chain(conn, org_id)["ok"] is False
    conn.close()


def test_action_provenance_requires_complete_authority_context(tmp_path):
    conn = db.connect(str(tmp_path / "bad-authorized-action.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "bad-authorized-action", tier="free")["id"]
    try:
        metering.record_usage(
            conn, org_id, provider="openai", model="gpt-fixture",
            cost_usd=0.1, agent_id="hermes-prod", action_status="executed",
        )
    except ValueError as exc:
        assert "required together" in str(exc)
    else:
        raise AssertionError("incomplete action provenance must be rejected")
    conn.close()


def test_agent_only_action_provenance_is_rejected(tmp_path):
    conn = db.connect(str(tmp_path / "agent-only-authority.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "agent-only-authority", tier="free")["id"]
    with pytest.raises(ValueError, match="required together"):
        metering.record_usage(
            conn, org_id, provider="openai", model="gpt-fixture",
            cost_usd=0.1, agent_id="hermes-prod",
        )
    conn.close()


def test_evidence_receipt_includes_hash_covered_decision_context(tmp_path):
    conn = db.connect(str(tmp_path / "decision-context.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "decision-context", tier="free")["id"]
    source_hash = "a" * 64
    result_hash = "b" * 64
    metering.record_usage(
        conn, org_id, provider="openai", model="gpt-fixture",
        task_type="recommend", external_ref="decision-9",
        input_tokens=10, output_tokens=5, cost_usd=0.1,
        evidence_hashes=[source_hash], policy_version="routing-policy/v3",
        result_hash=result_hash, human_review="corrected",
        correction_ref="correction-9", ts=time.time(),
    )

    receipt = audit_json(conn, org_id, external_ref="decision-9")
    event = receipt["events"][0]

    assert event["evidence"]["source_hashes"] == [source_hash]
    assert event["decision_context"] == {
        "policy_version": "routing-policy/v3",
        "result_hash": result_hash,
        "human_review": "corrected",
        "correction_ref": "correction-9",
    }
    conn.execute("UPDATE usage_events SET policy_version='tampered' WHERE id=?",
                 (event["event_id"],))
    assert db.verify_chain(conn, org_id)["ok"] is False
    conn.close()


def test_context_render_binding_is_hash_covered_and_receipt_safe(tmp_path):
    conn = db.connect(str(tmp_path / "context-render-binding.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "context-render-binding", tier="free")["id"]
    render_hash = "d" * 64
    provenance_hash = "e" * 64
    action_receipt_hash = "f" * 64
    metering.record_usage(
        conn, org_id, provider="openai", model="gpt-fixture",
        task_type="acceptance", external_ref="acceptance-42",
        input_tokens=10, output_tokens=5, cost_usd=0.1,
        context_render_schema="perseus-context-render-trace/v1",
        context_render_hash=render_hash,
        served_memory_provenance_hash=provenance_hash,
        action_receipt_hash=action_receipt_hash,
    )

    receipt = audit_json(conn, org_id, external_ref="acceptance-42")
    event = receipt["events"][0]
    assert event["context_render_binding"] == {
        "schema_version": "perseus-context-render-trace/v1",
        "render_hash": render_hash,
        "served_memory_provenance_hash": provenance_hash,
        "action_receipt_hash": action_receipt_hash,
    }
    assert "raw context" not in str(event)
    conn.execute("UPDATE usage_events SET context_render_hash='0' WHERE id=?", (event["event_id"],))
    assert db.verify_chain(conn, org_id)["ok"] is False
    conn.close()


def test_audit_exposes_partial_coverage_and_actual_hash_method(tmp_path):
    conn = db.connect(str(tmp_path / "partial-audit.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "partial-audit", tier="free")["id"]
    metering.record_usage(
        conn, org_id, provider="openai", model="gpt-fixture",
        input_tokens=1, output_tokens=1, cost_usd=0.01,
    )
    legacy = conn.execute(
        "SELECT id FROM usage_events WHERE org_id=? ORDER BY rowid LIMIT 1",
        (org_id,),
    ).fetchone()
    conn.execute("UPDATE usage_events SET prev_hash=NULL, row_hash=NULL WHERE id=?",
                 (legacy["id"],))
    conn.commit()
    metering.record_usage(
        conn, org_id, provider="openai", model="gpt-fixture",
        external_ref="partial-task", input_tokens=1, output_tokens=1,
        cost_usd=0.01,
    )

    receipt = audit_json(conn, org_id, external_ref="partial-task")
    verification = receipt["verification"]
    assert verification["method"] == "sha256"
    assert verification["hash_method"] == "sha256"
    assert verification["pre_chain_events"] == 1
    assert verification["unverifiable_events"] == 1
    assert verification["coverage"]["status"] == "partial"

    summary = audit_json(conn, org_id)
    assert summary["verification"]["method"] == "sha256"
    assert summary["verification"]["unverifiable_events"] == 1
    conn.close()


def test_audit_reports_hmac_sha256_method(tmp_path):
    conn = db.connect(str(tmp_path / "hmac-audit.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "hmac-audit", tier="free")["id"]
    key = b"customer-held-secret"
    metering.record_usage(
        conn, org_id, provider="openai", model="gpt-fixture",
        external_ref="hmac-task", input_tokens=1, output_tokens=1,
        cost_usd=0.01, chain_hmac_key=key,
    )

    receipt = audit_json(conn, org_id, hmac_key=key, external_ref="hmac-task")
    assert receipt["verification"]["method"] == "hmac-sha256"
    assert receipt["verification"]["hash_method"] == "hmac-sha256"
    assert audit_json(conn, org_id, hmac_key=key)["verification"]["method"] == "hmac-sha256"
    conn.close()
