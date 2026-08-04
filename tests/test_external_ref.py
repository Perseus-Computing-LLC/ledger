"""Per-task attribution ref on usage_events (#20-arc, shape A).

`external_ref` links a metered event back to the task that produced it (e.g. an
Invarium task_id), so a billed saving can be joined to the exact task. It is a
nullable, hash-chained *optional trailing* column — these tests pin the schema
bump, the round-trip, the `events_by_ref` join, and (load-bearing) that a NULL
ref reproduces the pre-v10 canonical form so existing chains still verify.
"""
import datetime as dt

import pytest

from plutus_agent import db, metering


def _org(tmp_path, tier="pro"):
    conn = db.connect(str(tmp_path / "plutus.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "Acme", tier=tier, owner_email="a@b.co")["id"]
    return conn, org_id


def _ts(day=10):
    return dt.datetime(2026, 7, day, 12, 0, tzinfo=dt.timezone.utc).timestamp()


def _meter(conn, org_id, cost, external_ref=None, baseline=None, ts=None):
    return metering.record_usage(
        conn, org_id, provider="anthropic", model="claude-haiku-4-5",
        cost_usd=cost, baseline_cost_usd=baseline, external_ref=external_ref,
        ts=ts if ts is not None else _ts())


# --- schema / migration -----------------------------------------------------
def test_schema_is_v10_and_has_external_ref(tmp_path):
    conn, _ = _org(tmp_path)
    assert db.get_schema_version(conn) == 17
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(usage_events)")}
    assert "external_ref" in cols


def test_external_ref_index_exists(tmp_path):
    conn, _ = _org(tmp_path)
    idx = {r["name"] for r in conn.execute("PRAGMA index_list(usage_events)")}
    assert "ix_usage_extref" in idx


def test_init_schema_is_idempotent(tmp_path):
    conn, _ = _org(tmp_path)
    db.init_schema(conn)  # second run must not raise or double-add
    assert db.get_schema_version(conn) == 17


# --- round-trip + join ------------------------------------------------------
def test_external_ref_round_trips(tmp_path):
    conn, org = _org(tmp_path)
    r = _meter(conn, org, cost=1.0, external_ref="q3-summary")
    assert r.external_ref == "q3-summary"
    row = conn.execute("SELECT external_ref FROM usage_events WHERE id=?",
                       (r.event_id,)).fetchone()
    assert row["external_ref"] == "q3-summary"


def test_external_ref_defaults_to_null(tmp_path):
    conn, org = _org(tmp_path)
    r = _meter(conn, org, cost=1.0)
    row = conn.execute("SELECT external_ref FROM usage_events WHERE id=?",
                       (r.event_id,)).fetchone()
    assert row["external_ref"] is None


def test_events_by_ref_joins_task_to_events(tmp_path):
    conn, org = _org(tmp_path)
    _meter(conn, org, cost=1.0, external_ref="task-A", baseline=4.0)
    _meter(conn, org, cost=0.5, external_ref="task-A")
    _meter(conn, org, cost=2.0, external_ref="task-B")
    rows = db.events_by_ref(conn, org, "task-A")
    assert len(rows) == 2
    assert all(r["external_ref"] == "task-A" for r in rows)
    assert db.events_by_ref(conn, org, "missing") == []


def test_external_ref_surfaces_in_export(tmp_path):
    conn, org = _org(tmp_path)
    _meter(conn, org, cost=1.0, external_ref="q3-summary")
    ev = db.export_events(conn, org)[0]
    assert ev["external_ref"] == "q3-summary"


# --- hash chain: backward compatibility + tamper-evidence -------------------
def test_null_external_ref_reproduces_pre_v10_canonical_form():
    # The load-bearing invariant: a NULL ref must NOT change the row hash, or
    # every chain written before v10 would fail to verify.
    base = {
        "id": "evt_1", "org_id": "org_1", "workspace_id": None,
        "provider": "anthropic", "model": "claude-haiku-4-5", "task_type": "general",
        "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
        "reasoning_tokens": 0, "cost_micros": 1_000_000, "estimated": 0,
        "source": "api", "ts": 1_752_000_000.0,
        "baseline_micros": None, "optimal_micros": None,
    }
    without = db.compute_row_hash(None, base)
    with_null = db.compute_row_hash(None, {**base, "external_ref": None})
    assert without == with_null
    # ...but a real ref changes the hash (it's folded in / tamper-evident).
    with_ref = db.compute_row_hash(None, {**base, "external_ref": "task-A"})
    assert with_ref != without


def test_chain_verifies_across_mixed_refs(tmp_path):
    conn, org = _org(tmp_path)
    _meter(conn, org, cost=1.0)                          # no ref
    _meter(conn, org, cost=1.0, external_ref="task-A", baseline=4.0)
    _meter(conn, org, cost=1.0, external_ref="task-B")
    result = db.verify_chain(conn, org)
    assert result["ok"] is True
    assert result["orgs"][0]["verified"] == 3


def test_tampering_with_external_ref_breaks_the_chain(tmp_path):
    conn, org = _org(tmp_path)
    r = _meter(conn, org, cost=1.0, external_ref="task-A", baseline=4.0)
    # Re-point the billed saving to a different task after the fact.
    conn.execute("UPDATE usage_events SET external_ref='task-B' WHERE id=?",
                 (r.event_id,))
    conn.commit()
    result = db.verify_chain(conn, org)
    assert result["ok"] is False


# --- bridge wiring (optional invarium dep) ----------------------------------
def test_bridge_passes_task_id_as_external_ref(tmp_path):
    pytest.importorskip("invarium")
    from invarium import AgentResult, ToolCall
    from plutus_agent.integrations.invarium import meter_agent_result

    conn, org = _org(tmp_path)
    result = AgentResult(
        input="q", final_output="done",
        tool_calls=[ToolCall("answer", success=True)], steps=1, cost=0.6,
        metadata={"task_id": "q3-summary", "baseline_cost_usd": 4.0},
    )
    # Even an unverified task records its attribution ref (only savings are gated).
    mr = meter_agent_result(conn, org, result, verified=False,
                            provider="anthropic", task_type="q3_summary")
    assert mr.external_ref == "q3-summary"
    assert mr.savings_usd == 0.0
    assert len(db.events_by_ref(conn, org, "q3-summary")) == 1
