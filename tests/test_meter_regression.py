"""Meter-accuracy regression suite (#20-arc, shape C).

Invarium testing *Plutus*: Plutus's cost attribution is deterministic given fixed
token counts and a pinned price table, so a golden catalog can be blessed and any
drift in pricing / baseline math caught with `compare`. These tests pin:

1. the golden costs themselves (an exact guard — a price-table edit fails here);
2. that a clean re-meter shows no regression; and
3. that a simulated price drift surfaces as a cost regression on exactly the
   affected model, via the success-rate → cost-delta interlock.

Invarium is an optional dev/test dependency; skip cleanly when it's absent. The
suite deliberately does not depend on the (unreleased) cost assertions from
invarium PR #28 — the cost pin is a two-sided exact check, so it runs on the
published invarium.
"""
import os
import sys

import pytest

pytest.importorskip("invarium")

# The example module is the source of truth for the catalog + helpers.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples"))
import invarium_meter_regression as demo  # noqa: E402

from invarium.compare import compare_reports  # noqa: E402


# --- 1. golden costs are exact (a price-table edit fails here) --------------
@pytest.mark.parametrize("w", demo.CATALOG, ids=lambda w: w.model)
def test_golden_cost_and_savings_are_exact(w):
    result = demo.meter_workload(w)
    assert round(result.cost, 6) == w.expected_cost_usd
    assert round(result.metadata["savings_usd"], 6) == w.expected_savings_usd


def test_every_golden_workload_passes_its_pin():
    reports = demo.build_reports()
    assert all(r.success_rate == 100.0 for r in reports)


# --- 2. clean re-meter: no regression ---------------------------------------
def test_clean_re_meter_has_no_regression():
    baseline = [r.to_dict() for r in demo.build_reports()]
    current = [r.to_dict() for r in demo.build_reports()]
    result = compare_reports(current, baseline)
    assert result["regressions"] == []


# --- 3. a price drift is caught as a cost regression, scoped to the model ---
def _drift_haiku_input_to(price: float) -> dict:
    return {"anthropic": {"claude-haiku-4-5": {"input": price, "output": 5.0, "cache_read": 0.10}}}


def test_price_drift_surfaces_as_cost_regression():
    baseline = [r.to_dict() for r in demo.build_reports()]
    current = [r.to_dict() for r in demo.build_reports(pricing_overrides=_drift_haiku_input_to(2.0))]
    result = compare_reports(current, baseline)

    regressed = {r["test_name"] for r in result["regressions"]}
    assert regressed == {"meter[anthropic/claude-haiku-4-5]"}

    reg = result["regressions"][0]
    assert reg["previous_success_rate"] == 100.0
    assert reg["current_success_rate"] == 0.0
    # +$1.00: 1,000,000 input tokens * ($2.00 - $1.00) per 1M.
    assert round(reg["cost_delta"], 6) == 1.00
    assert reg["failure_categories"].get("cost_exceeded") == 1


def test_unaffected_models_do_not_regress():
    baseline = [r.to_dict() for r in demo.build_reports()]
    current = [r.to_dict() for r in demo.build_reports(pricing_overrides=_drift_haiku_input_to(2.0))]
    result = compare_reports(current, baseline)
    regressed = {r["test_name"] for r in result["regressions"]}
    for w in demo.CATALOG:
        if w.model != "claude-haiku-4-5":
            assert demo._test_name(w) not in regressed


def test_each_workload_is_labelled_by_model():
    # Each metered workload carries its model in metadata so a regression is
    # attributable to a specific priced model (and, once shape A lands, to an
    # external_ref on the event itself).
    w = demo.CATALOG[0]
    result = demo.meter_workload(w)
    assert result.metadata["model"] == w.model
