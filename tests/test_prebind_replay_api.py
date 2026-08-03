from __future__ import annotations

from plutus_agent import db, metering
from plutus_agent.server.api import replay_receipt_prebind
from test_prebind_receipt import make_prebind


def test_stored_prebind_replay_is_non_mutating(tmp_path):
    conn = db.connect(str(tmp_path / "replay.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "replay-prebind", tier="free")["id"]
    block = make_prebind(boundary_outcome="hold", non_effective_result="not_executed")
    metering.record_usage(
        conn, org_id, provider="openai", model="fixture", external_ref="replay-ref",
        input_tokens=1, output_tokens=1, cost_usd=0.01, prebind=block,
    )
    before = conn.execute("SELECT COUNT(*) AS n FROM usage_events").fetchone()["n"]
    replay = replay_receipt_prebind(
        conn, org_id, "replay-ref",
        current_state={"authority_ok": True, "evidence_current": True,
                       "approval_granted": True, "action_allowed": True},
    )
    after = conn.execute("SELECT COUNT(*) AS n FROM usage_events").fetchone()["n"]
    assert replay["admission"] == "admitted_after_correction"
    assert before == after == 1
    conn.close()


__all__ = ["test_stored_prebind_replay_is_non_mutating"]
