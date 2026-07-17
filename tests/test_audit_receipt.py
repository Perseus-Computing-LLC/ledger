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
