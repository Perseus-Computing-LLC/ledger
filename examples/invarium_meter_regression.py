"""Meter-accuracy regression suite: Invarium testing *Ledger*, not an agent.

Run:  python examples/invarium_meter_regression.py

Shape C of the Invarium arc (docs/invarium-integration.md). The idea: Ledger's
cost attribution is deterministic given fixed token counts and a pinned price
table, so it can be regression-tested exactly like an agent. We `bless` a golden
catalog of workloads (each with a frozen expected cost), and on every change we
re-meter and `compare`. A drift in Ledger's pricing table or baseline-derivation
math shows up as a **cost regression** in the Invarium report.

The mechanism (worth understanding): Invarium's `compare_reports` only flags a
regression when a test's *success rate* drops — so cost drift has to fail an
assertion to be caught. Each workload therefore carries a cost pin: metered cost
must equal its golden value. A pricing change trips that pin → success 100%→0% →
`compare` reports the regression, and surfaces the cost delta alongside it. This
is exactly why shape C pairs the cost assertion with the suite: the assertion is
what makes an attribution drift visible to `bless`/`compare`.

Deterministic: in-memory SQLite, fixed token counts, Ledger's pinned price table.
"""
from __future__ import annotations

from typing import NamedTuple, Optional

from invarium import AgentResult
from invarium.assertions import AssertionRecord
from invarium.compare import compare_reports
from invarium.report import TestReport, TestRun, build_test_report, new_run_id

from ledger_agent import db, metering


class Workload(NamedTuple):
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    baseline_model: Optional[str]
    # Golden values, frozen from Ledger's price table (PRICE_TABLE_AS_OF). A
    # legitimate price-table change is expected to fail this suite until re-blessed.
    expected_cost_usd: float
    expected_savings_usd: float


# The golden catalog: routed model + baseline, one per provider, plus a no-baseline
# cost-only pin. Costs were captured from Ledger and frozen (see the module test).
CATALOG = [
    Workload("anthropic", "claude-haiku-4-5", 1_000_000, 500_000, "claude-opus-4-8", 3.50, 14.00),
    Workload("openai", "gpt-5-mini", 1_000_000, 1_000_000, "gpt-5", 2.25, 9.00),
    Workload("google", "gemini-2.5-flash", 2_000_000, 1_000_000, "gemini-2.5-pro", 3.10, 9.40),
    Workload("anthropic", "claude-sonnet-4-6", 500_000, 200_000, None, 4.50, 0.00),
]


def meter_workload(w: Workload, pricing_overrides: Optional[dict] = None) -> AgentResult:
    """Meter one workload through a fresh in-memory Ledger and return an AgentResult.

    ``cost_usd=None`` forces Ledger to *price from its table* — that pricing logic
    is precisely what this suite regression-tests. The metered cost/baseline/savings
    ride along in metadata so the report and any downstream assertion can see them.
    """
    conn = db.connect(":memory:")
    db.init_schema(conn)
    org_id = db.create_org(conn, "meter-regression", tier="pro")["id"]
    mr = metering.record_usage(
        conn, org_id, provider=w.provider, model=w.model,
        input_tokens=w.input_tokens, output_tokens=w.output_tokens,
        cost_usd=None, baseline_model=w.baseline_model,
        pricing_overrides=pricing_overrides, task_type="regression",
    )
    conn.close()
    return AgentResult(
        input=w.model, final_output="metered", steps=1, cost=mr.cost_usd,
        metadata={
            "model": w.model,
            "baseline_usd": mr.baseline_usd,
            "savings_usd": mr.savings_usd,
        },
    )


def _test_name(w: Workload) -> str:
    return f"meter[{w.provider}/{w.model}]"


def report_for(w: Workload, pricing_overrides: Optional[dict] = None) -> TestReport:
    """A one-run Invarium report for a workload, pinned to its golden cost.

    The cost pin is a two-sided exact check (drift up *or* down fails it). Once
    invarium ships the cost assertions from PR #28, the one-sided budget case can
    be written as ``expect(result).cost_less_than(...)``; a two-sided
    ``cost_within`` is the natural follow-on the report/taxonomy would host.
    """
    result = meter_workload(w, pricing_overrides)
    passed = result.cost is not None and round(result.cost, 6) == w.expected_cost_usd
    record = AssertionRecord(
        name="cost_matches_golden",
        passed=passed,
        message=(
            f"metered ${result.cost} matches golden ${w.expected_cost_usd:.6f}"
            if passed else
            f"cost drift: metered ${result.cost} != golden ${w.expected_cost_usd:.6f}"
        ),
        category=None if passed else "cost_exceeded",
    )
    run = TestRun(
        test_name=_test_name(w), run_id=new_run_id(), result=result,
        assertions=[record], passed=passed,
    )
    return build_test_report(_test_name(w), [run])


def build_reports(pricing_overrides: Optional[dict] = None) -> list[TestReport]:
    return [report_for(w, pricing_overrides) for w in CATALOG]


def main() -> None:
    baseline = [r.to_dict() for r in build_reports()]

    # Simulate a pricing-table drift: someone bumps claude-haiku-4-5 input price
    # from $1.00 to $2.00 per 1M tokens. Nothing else changes.
    drift = {"anthropic": {"claude-haiku-4-5": {"input": 2.0, "output": 5.0, "cache_read": 0.10}}}
    current = [r.to_dict() for r in build_reports(pricing_overrides=drift)]

    clean = compare_reports([r.to_dict() for r in build_reports()], baseline)
    drifted = compare_reports(current, baseline)

    print("Golden catalog blessed:")
    for w in CATALOG:
        print(f"  {w.provider}/{w.model:22} ${w.expected_cost_usd:>7.4f} "
              f"(saves ${w.expected_savings_usd:.2f})")

    print(f"\nRe-meter, no change        -> {clean['summary']}")
    print(f"Re-meter after price bump  -> {drifted['summary']}")
    for reg in drifted["regressions"]:
        print(f"  [REGRESSION] {reg['test_name']}")
        print(f"    success {reg['previous_success_rate']:.0f}% -> {reg['current_success_rate']:.0f}%")
        print(f"    cost delta ${reg['cost_delta']:+.4f}")
        print(f"    categories {reg['failure_categories']}")

    print(
        "\nA one-line price-table edit surfaces as a cost regression on exactly the\n"
        "affected model - Invarium guarding Ledger's attribution, not just agents."
    )


if __name__ == "__main__":
    main()
