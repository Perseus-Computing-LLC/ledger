"""Per-receipt governance self-cost field (#239).

Success criteria:
- receipts expose governance overhead (wall/cpu/... per action);
- cumulative governance cost queryable per workspace;
- governance overhead excluded from customer-facing usage/totals;
- existing receipts unchanged when the field is absent.
"""
import time

import pytest

from ledger_agent import db, metering
from ledger_agent.receipts import (
    GOVERNANCE_COST_FIELDS,
    build_governance_cost,
    validate_governance_cost,
)
from ledger_agent.server.api import audit_json

SCOPE = "github:Perseus-Computing-LLC/ledger"


def _record(conn, org_id, external_ref, *, governance_cost=None, provenance=True,
            workspace=None, ts=None):
    common = dict(
        conn=conn, org_id=org_id, provider="openai", model="gpt-fixture",
        task_type="deploy", external_ref=external_ref,
        input_tokens=10, output_tokens=5, cost_usd=0.1,
        workspace=workspace, governance_cost=governance_cost,
        ts=ts if ts is not None else time.time(),
    )
    if provenance:
        return metering.record_usage(
            **common, agent_id="hermes-prod", authority_manifest_ref="auth-1",
            scope_anchor=SCOPE, action_intent_hash="c" * 64,
            action_status="executed",
        )
    return metering.record_usage(**common)


# ── block construction / validation ─────────────────────────────────────────


def test_build_and_validate_governance_cost():
    block = build_governance_cost(
        wall_ms=12, cpu_ms=4, mem_bytes=8192, storage_bytes=2048,
        tokens=0, model_calls=0, approval_waits_ms=0,
    )
    ok, errors = validate_governance_cost(block)
    assert ok, errors
    assert block["schema"] == "perseus-ledger-governance-cost/v1"
    assert block["wall_ms"] == 12
    assert len(block["governance_digest"]) == 64


def test_validate_rejects_negative_and_unknown_fields():
    bad = {
        "schema": "perseus-ledger-governance-cost/v1",
        "wall_ms": -5,
        "governance_digest": "a" * 64,
    }
    ok, errors = validate_governance_cost(bad)
    assert not ok
    assert "governance_wall_ms" in errors

    unknown = {
        "schema": "perseus-ledger-governance-cost/v1",
        "wall_ms": 1, "mystery_field": 2,
        "governance_digest": "a" * 64,
    }
    ok, errors = validate_governance_cost(unknown)
    assert not ok
    assert "governance_mystery_field_unknown" in errors

    with pytest.raises(ValueError):
        build_governance_cost(wall_ms=-1)
    with pytest.raises(ValueError):
        build_governance_cost(bogus=1)


# ── receipt exposure ────────────────────────────────────────────────────────


def test_explicit_block_lands_in_receipt_and_is_signed(tmp_path):
    conn = db.connect(str(tmp_path / "gc.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "gc-org", tier="free")["id"]
    block = build_governance_cost(wall_ms=12, cpu_ms=4, storage_bytes=2048)
    _record(conn, org_id, "task-gc", governance_cost=block)
    receipt = audit_json(conn, org_id, external_ref="task-gc",
                         key_registry={"ledger-ops": b"ledger-test-signing-key-32-bytes!"},
                         sign_key_id="ledger-ops")
    event = receipt["events"][0]
    assert event["governance_cost"]["wall_ms"] == 12
    assert event["governance_cost"]["cpu_ms"] == 4
    # research question resolution: the block is INSIDE the signed bytes —
    # tampering with it must break the receipt HMAC.
    from ledger_agent import evidence_levels
    ok, reason = evidence_levels.verify_receipt_signature(
        receipt, {"ledger-ops": b"ledger-test-signing-key-32-bytes!"})
    assert ok, reason
    receipt["events"][0]["governance_cost"]["wall_ms"] = 999
    ok, reason = evidence_levels.verify_receipt_signature(
        receipt, {"ledger-ops": b"ledger-test-signing-key-32-bytes!"})
    assert not ok
    conn.close()


def test_auto_measurement_when_provenance_present(tmp_path):
    conn = db.connect(str(tmp_path / "auto.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "auto-org", tier="free")["id"]
    _record(conn, org_id, "task-auto", governance_cost=None)
    receipt = audit_json(conn, org_id, external_ref="task-auto")
    block = receipt["events"][0]["governance_cost"]
    assert block is not None
    assert block["wall_ms"] >= 0 and block["cpu_ms"] >= 0
    assert set(block) >= {"wall_ms", "cpu_ms", "governance_digest"}
    conn.close()


def test_absent_block_leaves_receipts_unchanged(tmp_path):
    conn = db.connect(str(tmp_path / "none.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "none-org", tier="free")["id"]
    _record(conn, org_id, "task-none", provenance=False)
    receipt = audit_json(conn, org_id, external_ref="task-none")
    assert receipt["events"][0].get("governance_cost") is None
    conn.close()


# ── cumulative query per workspace ──────────────────────────────────────────


def test_totals_and_by_workspace(tmp_path):
    conn = db.connect(str(tmp_path / "totals.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "totals-org", tier="pro")["id"]
    db.create_workspace(conn, org_id, "ws-a", 10.0)
    db.create_workspace(conn, org_id, "ws-b", 10.0)
    t0 = 1_700_000_000.0
    _record(conn, org_id, "t-1", workspace="ws-a",
            governance_cost=build_governance_cost(wall_ms=10, cpu_ms=3, tokens=2),
            ts=t0)
    _record(conn, org_id, "t-2", workspace="ws-a",
            governance_cost=build_governance_cost(wall_ms=5, cpu_ms=1),
            ts=t0 + 60)
    _record(conn, org_id, "t-3", workspace="ws-b",
            governance_cost=build_governance_cost(storage_bytes=100),
            ts=t0 + 120)
    _record(conn, org_id, "t-4", provenance=False, ts=t0 + 180)  # no block

    totals = metering.governance_cost_totals(conn, org_id)
    assert totals["events"] == 3
    assert totals["totals"]["wall_ms"] == 15
    assert totals["totals"]["cpu_ms"] == 4
    assert totals["totals"]["tokens"] == 2
    assert totals["totals"]["storage_bytes"] == 100

    by_ws = {r["workspace_name"]: r for r in metering.governance_cost_by_workspace(conn, org_id)}
    assert set(by_ws) == {"ws-a", "ws-b"}
    assert by_ws["ws-a"]["events"] == 2
    assert by_ws["ws-a"]["totals"]["wall_ms"] == 15
    assert by_ws["ws-b"]["totals"]["storage_bytes"] == 100

    windowed = metering.governance_cost_totals(conn, org_id, since=t0 + 60)
    assert windowed["events"] == 2
    assert windowed["totals"]["wall_ms"] == 5
    conn.close()


# ── excluded from customer-facing totals ────────────────────────────────────


def test_governance_cost_never_touches_usage_aggregates(tmp_path):
    conn = db.connect(str(tmp_path / "excluded.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "excl-org", tier="pro")["id"]
    with_gov = metering.record_usage(
        conn, org_id, provider="openai", model="gpt-fixture",
        task_type="deploy", input_tokens=100, output_tokens=50,
        cost_usd=1.0, ts=time.time(),
        governance_cost=build_governance_cost(wall_ms=50, tokens=9999),
    )
    tokens_after = metering.tracked_tokens_mtd(conn, org_id)
    assert tokens_after == 150  # 9999 governance tokens are NOT usage tokens
    # the recorded cost is the action's cost, not governance cost
    row = conn.execute(
        "SELECT cost_micros FROM usage_events WHERE id=?", (with_gov.event_id,)
    ).fetchone()
    assert row["cost_micros"] == db.usd_to_micros(1.0)
    conn.close()
