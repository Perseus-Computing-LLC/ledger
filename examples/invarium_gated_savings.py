"""Accuracy-gated savings: Invarium verdict → Ledger meter.

Run:  python examples/invarium_gated_savings.py

The story in one screen:

* Two agent tasks are metered. Both are *cheaper* than their $4.00 baseline.
* Task A is correct — it grounded its answer (``retrieve`` -> ``answer``).
* Task B is a regression — it skipped ``retrieve``, answered from its prior, and
  claimed success anyway. It is even *cheaper* than A.
* Naively metering both would book savings on B too — paying out a share on a
  wrong-but-cheap answer. That is exactly the failure mode that erodes the
  "fewer dollars *at higher accuracy*" claim.
* Invarium judges behavior; Ledger books savings only for the verified task. B's
  baseline is withheld, so B contributes $0 to billable savings.

Everything is deterministic (in-memory SQLite, no LLM, no network).
"""
from __future__ import annotations

from invarium import AgentResult, ToolCall, expect

from ledger_agent import db, savings
from ledger_agent.integrations.invarium import meter_agent_result


# --- proposed Invarium primitive (design doc shape C) -------------------------
# Invarium already defines the `cost_exceeded` failure category but ships no
# assertion that emits it. Until `expect(result).cost_within(...)` lands upstream
# (offered alongside issue #26), express the cost check as a plain assertion — the
# same "works today" pattern used for context_hash in the Perseus docs example.
def cost_within(result: AgentResult, baseline_usd: float, *, max_fraction: float) -> bool:
    """True if actual cost is at most ``max_fraction`` of the baseline."""
    return result.cost is not None and result.cost <= baseline_usd * max_fraction


# --- the agent tasks (deterministic stand-ins for real Perseus-run agents) ----
def _good_task() -> AgentResult:
    """Correct: grounds the answer by retrieving before answering."""
    return AgentResult(
        input="Summarize the Q3 revenue doc",
        final_output="Q3 revenue was $4.2M, up 18% QoQ. Grounded in reports/q3.md.",
        tool_calls=[
            ToolCall(name="retrieve", args={"path": "reports/q3.md"}, success=True),
            ToolCall(name="answer", success=True),
        ],
        steps=2,
        cost=1.00,
        metadata={"task_id": "q3-summary", "baseline_cost_usd": 4.00},
    )


def _regressed_task() -> AgentResult:
    """Regression: skips retrieval, answers from prior, still claims success.

    Cheaper than the good task ($0.60 < $1.00) — so a naive meter would book an
    even *larger* saving on the wrong answer.
    """
    return AgentResult(
        input="Summarize the Q3 revenue doc",
        final_output="Q3 revenue grew strongly. Analysis completed successfully.",
        tool_calls=[ToolCall(name="answer", success=True)],
        steps=1,
        cost=0.60,
        metadata={"task_id": "q3-summary", "baseline_cost_usd": 4.00},
    )


def _verify(result: AgentResult) -> bool:
    """Run the Invarium behavioral contract + cost check. Returns pass/fail."""
    check = expect(result, collect=True)
    check.used_tool("retrieve")
    check.used_tools_in_order(["retrieve", "answer"])
    check.did_not_claim_confirmation_without_tool("retrieve")
    check.did_not_error()
    try:
        check.verify()
    except AssertionError:
        return False
    # cost gate (proposed upstream primitive): must be well under baseline
    return cost_within(result, result.metadata["baseline_cost_usd"], max_fraction=0.9)


def run(conn, org_id: str, *, gated: bool) -> None:
    """Meter both tasks. With ``gated`` the baseline follows the Invarium verdict."""
    for task in (_good_task(), _regressed_task()):
        verified = _verify(task)
        # The contrast: gated respects the verdict; naive forces verified=True.
        meter_agent_result(
            conn, org_id, task,
            verified=verified if gated else True,
            provider="anthropic", model="claude-haiku-4-5",
            task_type="q3_summary", source="invarium",
        )


def _period(conn, org_id: str) -> dict:
    agg = savings.period_savings(conn, org_id)  # all events, clock-independent
    return {
        "gross_savings_usd": db.micros_to_usd(agg["gross_savings_micros"]),
        "events_with_baseline": agg["billable_events"],
        "events_total": agg["total_events"],
    }


def main() -> None:
    for gated in (False, True):
        conn = db.connect(":memory:")
        db.init_schema(conn)
        org_id = db.create_org(conn, "Demo Co", tier="pro", owner_email="a@demo.co")["id"]
        run(conn, org_id, gated=gated)
        p = _period(conn, org_id)
        label = "GATED (Invarium verdict)" if gated else "NAIVE (meter everything)"
        print(f"\n{label}")
        print(f"  events metered        : {p['events_total']}")
        print(f"  events booking savings: {p['events_with_baseline']}")
        print(f"  gross billable savings: ${p['gross_savings_usd']:.2f}")
        conn.close()

    print(
        "\nThe naive run books $6.40 of savings - $3.40 of it on a wrong answer.\n"
        "The gated run books $3.00 - only the verified task. Accuracy-gating keeps\n"
        "the savings figure honest: you never bill a share of a cheap-but-broken run."
    )


if __name__ == "__main__":
    main()
