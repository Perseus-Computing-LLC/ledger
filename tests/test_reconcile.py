"""Cost reconciliation: true-up estimated cost to a provider's real billing.

Covers plutus_agent.reconcile - the delta math, idempotency, restatement,
the never-assume-zero safety rule, dry-run vs apply, and the input loaders.
"""
import json

import pytest

from plutus_agent import db, metering, reconcile


def _org(tmp_path, credit=100.0):
    conn = db.connect(str(tmp_path / "plutus.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "Acme", tier="pro")["id"]
    if credit:
        db.add_ledger(conn, org_id, credit, "topup", reason="test seed")
    return conn, org_id


def _meter(conn, org_id, provider, cost_usd, n=1, **kw):
    for _ in range(n):
        metering.record_usage(conn, org_id, provider=provider, model="gpt-4o",
                              cost_usd=cost_usd, source="api", **kw)


def _adjusts(conn, org_id):
    return conn.execute(
        "SELECT delta_micros, stripe_ref, reason FROM credit_ledger "
        "WHERE org_id=? AND kind='adjust' ORDER BY rowid", (org_id,)).fetchall()


# --------------------------------------------------------------- delta math ---
def test_dry_run_computes_delta_without_writing(tmp_path):
    conn, org = _org(tmp_path)
    _meter(conn, org, "openai", 1.00, n=5)            # recorded $5, balance $95
    rep = reconcile.reconcile(conn, org, {"openai": 4.00},
                              period_label="2026-07", apply=False)
    item = rep.items[0]
    assert item.provider == "openai"
    assert item.recorded_usd == pytest.approx(5.0)
    assert item.delta_usd == pytest.approx(1.0)        # over-charged -> credit back
    assert item.applied is False
    assert _adjusts(conn, org) == []                   # nothing written
    assert db.get_balance(conn, org) == pytest.approx(95.0)   # unchanged
    assert rep.balance_after_usd == pytest.approx(96.0)       # projected


def test_apply_makes_ledger_match_authoritative(tmp_path):
    conn, org = _org(tmp_path)
    _meter(conn, org, "openai", 1.00, n=5)
    rep = reconcile.reconcile(conn, org, {"openai": 4.00},
                              period_label="2026-07", apply=True)
    rows = _adjusts(conn, org)
    assert len(rows) == 1
    assert rows[0]["delta_micros"] == 1_000_000
    assert rows[0]["stripe_ref"] == "reconcile:2026-07:openai"
    assert rep.total_adjust_usd == pytest.approx(1.0)
    assert db.get_balance(conn, org) == pytest.approx(96.0)


def test_under_charge_debits_the_shortfall(tmp_path):
    conn, org = _org(tmp_path)
    _meter(conn, org, "openai", 1.00, n=5)             # recorded $5
    rep = reconcile.reconcile(conn, org, {"openai": 8.00},
                              period_label="2026-07", apply=True)
    assert rep.total_adjust_usd == pytest.approx(-3.0)  # billed more than metered
    assert db.get_balance(conn, org) == pytest.approx(92.0)  # 95 - 3


def test_idempotent_second_run_is_noop(tmp_path):
    conn, org = _org(tmp_path)
    _meter(conn, org, "openai", 1.00, n=5)
    reconcile.reconcile(conn, org, {"openai": 4.00}, period_label="2026-07", apply=True)
    bal = db.get_balance(conn, org)
    rep2 = reconcile.reconcile(conn, org, {"openai": 4.00}, period_label="2026-07", apply=True)
    assert rep2.total_adjust_usd == pytest.approx(0.0)
    assert len(_adjusts(conn, org)) == 1               # no second adjust
    assert db.get_balance(conn, org) == pytest.approx(bal)


def test_restatement_applies_only_the_increment(tmp_path):
    conn, org = _org(tmp_path)
    _meter(conn, org, "openai", 1.00, n=5)             # recorded $5
    reconcile.reconcile(conn, org, {"openai": 4.00}, period_label="2026-07", apply=True)
    # provider later restates the invoice to $6
    rep = reconcile.reconcile(conn, org, {"openai": 6.00}, period_label="2026-07", apply=True)
    assert rep.total_adjust_usd == pytest.approx(-2.0)  # from +1 already applied to net -1
    # ledger now reflects -$6 for openai: debits -5, adjusts +1 then -2 = -6
    adj = sum(r["delta_micros"] for r in _adjusts(conn, org))
    assert adj == -1_000_000                            # +1e6 - 2e6
    assert db.get_balance(conn, org) == pytest.approx(94.0)  # 100 - 6


def test_missing_provider_is_never_assumed_zero(tmp_path):
    conn, org = _org(tmp_path)
    _meter(conn, org, "openai", 2.00, n=1)
    _meter(conn, org, "anthropic", 3.00, n=1)
    # only openai has an authoritative total; anthropic must be left untouched
    rep = reconcile.reconcile(conn, org, {"openai": 2.00},
                              period_label="2026-07", apply=True)
    assert "anthropic" in rep.unreconciled_providers
    assert all(i.provider != "anthropic" for i in rep.items)
    # anthropic's $3 debit is still there, no refund-to-zero
    assert db.get_balance(conn, org) == pytest.approx(95.0)  # 100 - 2 - 3


def test_authoritative_without_metered_usage_books_full_cost(tmp_path):
    conn, org = _org(tmp_path)
    rep = reconcile.reconcile(conn, org, {"google": 2.50},
                              period_label="2026-07", apply=True)
    item = rep.items[0]
    assert item.recorded_usd == pytest.approx(0.0)
    assert item.delta_usd == pytest.approx(-2.5)
    assert "no metered usage" in item.note
    assert db.get_balance(conn, org) == pytest.approx(97.5)


def test_estimated_events_flagged_in_note(tmp_path):
    conn, org = _org(tmp_path)
    # no cost_usd -> priced from the table -> estimated=True
    metering.record_usage(conn, org, provider="openai", model="gpt-4o",
                          input_tokens=1000, output_tokens=100, source="api")
    rep = reconcile.reconcile(conn, org, {"openai": 0.05}, period_label="2026-07")
    assert rep.items[0].estimated_events == 1
    assert "estimated" in rep.items[0].note


def test_period_window_scopes_usage_by_ts(tmp_path):
    conn, org = _org(tmp_path)
    start, end = reconcile.month_window("2026-07")
    _meter(conn, org, "openai", 1.00, n=1, ts=start + 100)     # inside July
    _meter(conn, org, "openai", 1.00, n=1, ts=start - 100)     # before July
    rep = reconcile.reconcile(conn, org, {"openai": 1.00},
                              period_label="2026-07", start_ts=start, end_ts=end)
    # only the in-window $1 event counts, so it already matches: no adjust
    assert rep.items[0].recorded_usd == pytest.approx(1.0)
    assert rep.items[0].delta_usd == pytest.approx(0.0)


# ------------------------------------------------------------------ loaders ---
def test_load_authoritative_json(tmp_path):
    p = tmp_path / "totals.json"
    p.write_text(json.dumps({"period": "2026-07",
                             "totals": {"openai": 12.34, "anthropic": 5.0}}))
    assert reconcile.load_authoritative(p) == {"openai": 12.34, "anthropic": 5.0}


def test_load_authoritative_flat_json(tmp_path):
    p = tmp_path / "flat.json"
    p.write_text(json.dumps({"openai": 1.5}))
    assert reconcile.load_authoritative(p) == {"openai": 1.5}


def test_load_authoritative_csv(tmp_path):
    p = tmp_path / "totals.csv"
    p.write_text("provider,cost_usd\nopenai,12.34\nanthropic,5.00\n")
    assert reconcile.load_authoritative(p) == {"openai": 12.34, "anthropic": 5.0}


def test_month_window_bounds():
    start, end = reconcile.month_window("2026-12")
    import datetime as dt
    assert dt.datetime.fromtimestamp(start, dt.timezone.utc).month == 12
    assert dt.datetime.fromtimestamp(end, dt.timezone.utc).year == 2027
