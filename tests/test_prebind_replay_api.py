from __future__ import annotations

from ledger_agent import db, metering
from ledger_agent.server.api import replay_receipt_prebind
from test_prebind_receipt import make_prebind


def test_stored_prebind_replay_is_non_mutating(tmp_path):
    conn = db.connect(str(tmp_path / "replay.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "replay-prebind", tier="free")["id"]
    block = make_prebind(boundary_outcome="hold", non_effective_result="not_executed")
    metering.record_usage(
        conn, org_id, provider="openai", model="fixture", external_ref="replay-ref",
        input_tokens=0, output_tokens=0, cost_usd=0.0, prebind=block,
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


# ── authority-trace v2: CVA replay resistance ───────────────────────────────

def _cva_digest(value):
    import hashlib
    import json

    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def test_authority_trace_v2_cva_replay_resistance_and_key_rotation(tmp_path):
    from ledger_agent.cva import CvaGateway, build_cva_statement
    from ledger_agent.receipts import build_prebind_v2

    request = {"action": "deploy", "resource": "prod"}
    context = {"environment": "prod", "risk": "low"}
    policy = lambda attrs, req, ctx: attrs.get("role") == "deployer" and req["resource"] == "prod"
    old_key_registry = {
        "old": {"key_material": b"old-key", "custody": "self_held", "agent_id": "agent-a"},
        "new": {"key_material": b"new-key", "custody": "self_held", "agent_id": "agent-a"},
    }
    block = build_prebind_v2(
        attempted_action="deploy",
        actor_ref="agent-a",
        authority_ref="authority:1",
        trusted_scope="repo:ledger",
        policy_version="policy/v1",
        evidence_hashes=[_cva_digest("evidence")],
        selected_context_digest=_cva_digest("selection"),
        resource_ref="resource:prod",
        boundary_outcome="allow",
        non_effective_result="not_executed",
        replay_id="replay:n1",
        context_hash=_cva_digest(context),
        policy_hash=_cva_digest("policy/v1"),
        request_hash=_cva_digest(request),
        nonce="n1",
        epoch=100,
    )
    statement = build_cva_statement(
        agent_id=block["actor_ref"],
        request_hash=block["request_hash"],
        context_hash=block["context_hash"],
        policy_id=block["policy_version"],
        nonce=block["nonce"],
        timestamp_ms=block["epoch"],
    )
    gateway = CvaGateway()
    kwargs = {
        "principal_key_id": "old",
        "key_registry": old_key_registry,
        "request_payload": request,
        "context_payload": context,
        "attrs": {"role": "deployer"},
        "policy": policy,
        "t_min": 0,
        "t_max": 200,
    }
    assert gateway.accept(statement, **kwargs) == {"accepted": True, "reason": "accepted"}
    assert gateway.accept(statement, **kwargs) == {"accepted": False, "reason": "replay"}

    rotated_registry = {
        "old": {"key_material": b"old-key", "custody": "self_held", "agent_id": "agent-a", "revoked": True},
        "new": {"key_material": b"new-key", "custody": "self_held", "agent_id": "agent-a"},
    }
    old_witness_statement = build_cva_statement(
        agent_id="agent-a",
        request_hash=_cva_digest(request),
        context_hash=_cva_digest(context),
        policy_id="policy/v1",
        nonce="old-after-rotation",
        timestamp_ms=100,
    )
    old_witness = dict(kwargs, key_registry=rotated_registry, principal_key_id="old")
    old_result = gateway.accept(old_witness_statement, **old_witness)
    assert old_result["accepted"] is False
    assert old_result["reason"] == "relation_not_satisfied"
    assert "bind_principal" in old_result["relation_errors"]

    stale_statement = build_cva_statement(
        agent_id="agent-a",
        request_hash=_cva_digest(request),
        context_hash=_cva_digest(context),
        policy_id="policy/v1",
        nonce="stale-context",
        timestamp_ms=100,
    )
    stale_witness = dict(old_witness, principal_key_id="new", context_payload={"environment": "staging", "risk": "low"})
    stale_result = gateway.accept(stale_statement, **stale_witness)
    assert stale_result["accepted"] is False
    assert stale_result["reason"] == "relation_not_satisfied"
    assert "bind_context" in stale_result["relation_errors"]

    fresh_statement = build_cva_statement(
        agent_id="agent-a",
        request_hash=_cva_digest(request),
        context_hash=_cva_digest({"environment": "staging", "risk": "low"}),
        policy_id="policy/v1",
        nonce="fresh-after-rotation",
        timestamp_ms=100,
    )
    fresh_result = gateway.accept(fresh_statement, **stale_witness)
    assert fresh_result == {"accepted": True, "reason": "accepted"}


__all__ = [
    "test_stored_prebind_replay_is_non_mutating",
    "test_authority_trace_v2_cva_replay_resistance_and_key_rotation",
]
