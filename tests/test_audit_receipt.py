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
