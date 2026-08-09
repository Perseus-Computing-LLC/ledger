"""Accuracy-gated savings via the Invarium bridge (docs/invarium-integration.md).

The bridge forwards a task's counterfactual baseline to Ledger *only when the
task passed its Invarium behavioral contract*. A cheaper-but-wrong run therefore
books $0 savings. These tests pin that gate and the "never bill a broken run"
invariant it protects.

Invarium is an optional dev/test dependency, so skip cleanly if absent.
"""
import pytest

invarium = pytest.importorskip("invarium")

from invarium import AgentResult, ToolCall, expect

from ledger_agent import db, savings
from ledger_agent.integrations.invarium import meter_agent_result


def _org(tmp_path, tier="pro"):
    conn = db.connect(str(tmp_path / "ledger.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "Acme", tier=tier, owner_email="a@b.co")["id"]
    return conn, org_id


def _good():
    return AgentResult(
        input="Summarize the Q3 revenue doc",
        final_output="Q3 revenue was $4.2M, up 18% QoQ. Grounded in reports/q3.md.",
        tool_calls=[ToolCall("retrieve", success=True), ToolCall("answer", success=True)],
        steps=2,
        cost=1.00,
        metadata={"task_id": "q3-summary", "baseline_cost_usd": 4.00},
    )


def _regressed():
    # Skips retrieve, claims success, and is *cheaper* than the good run.
    return AgentResult(
        input="Summarize the Q3 revenue doc",
        final_output="Q3 revenue grew strongly. Analysis completed successfully.",
        tool_calls=[ToolCall("answer", success=True)],
        steps=1,
        cost=0.60,
        metadata={"task_id": "q3-summary", "baseline_cost_usd": 4.00},
    )


def _verify(result) -> bool:
    check = expect(result, collect=True)
    check.used_tool("retrieve")
    check.used_tools_in_order(["retrieve", "answer"])
    check.did_not_claim_confirmation_without_tool("retrieve")
    try:
        check.verify()
        return True
    except AssertionError:
        return False


def _meter(conn, org_id, result, verified):
    return meter_agent_result(
        conn, org_id, result, verified=verified,
        provider="anthropic", model="claude-haiku-4-5", task_type="q3_summary",
    )


# --- the gate ---------------------------------------------------------------
def test_verified_task_books_its_saving(tmp_path):
    conn, org = _org(tmp_path)
    r = _meter(conn, org, _good(), verified=True)
    assert r.baseline_usd == 4.00
    assert r.savings_usd == 3.00


def test_unverified_task_books_zero_even_when_cheaper(tmp_path):
    conn, org = _org(tmp_path)
    r = _meter(conn, org, _regressed(), verified=False)
    # Baseline withheld because the behavioral contract failed...
    assert r.baseline_usd is None
    # ...so no savings are booked, despite cost ($0.60) < baseline ($4.00).
    assert r.savings_usd == 0.0
    assert r.cost_usd == 0.60  # cost is still metered — we track spend, just not the claim


def test_invarium_verdict_drives_the_gate(tmp_path):
    # The verdict comes from Invarium, not hand-set: good passes, regressed fails.
    assert _verify(_good()) is True
    assert _verify(_regressed()) is False


def test_gating_keeps_period_savings_honest(tmp_path):
    conn, org = _org(tmp_path)
    for task in (_good(), _regressed()):
        _meter(conn, org, task, verified=_verify(task))
    agg = savings.period_savings(conn, org)
    # Both events are metered; only the verified one contributes to savings.
    assert agg["total_events"] == 2
    assert agg["billable_events"] == 1
    assert db.micros_to_usd(agg["gross_savings_micros"]) == 3.00


def test_missing_cost_is_rejected(tmp_path):
    conn, org = _org(tmp_path)
    bad = _good()
    bad.cost = None
    with pytest.raises(ValueError, match="cost is None"):
        _meter(conn, org, bad, verified=True)
