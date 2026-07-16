"""Savings-share billing (#7): the value-based revenue path.

Covers plutus_agent.savings + the metering/db/stripe pieces it plugs into:
the per-event baseline capture and clamp, period aggregation over only the
events that carry a baseline, the 18% share math, hash-chain tamper-evidence of
the baseline, idempotent billing, and the mocked Stripe invoice path.
"""
import datetime as dt

import pytest

from plutus_agent import db, metering, savings


def _org(tmp_path, tier="pro"):
    conn = db.connect(str(tmp_path / "plutus.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "Acme", tier=tier, owner_email="a@b.co")["id"]
    return conn, org_id


def _ts(day=10):
    return dt.datetime(2026, 7, day, 12, 0, tzinfo=dt.timezone.utc).timestamp()


def _meter(conn, org_id, cost, baseline=None, ts=None, provider="openai"):
    return metering.record_usage(
        conn, org_id, provider=provider, model="gpt-5", cost_usd=cost,
        baseline_cost_usd=baseline, ts=ts if ts is not None else _ts())


# ------------------------------------------------------------ per-event ------
def test_saving_is_baseline_minus_cost(tmp_path):
    conn, org = _org(tmp_path)
    r = _meter(conn, org, cost=1.0, baseline=4.0)
    assert r.baseline_usd == 4.0
    assert r.savings_usd == 3.0


def test_no_baseline_means_zero_saving(tmp_path):
    conn, org = _org(tmp_path)
    r = _meter(conn, org, cost=5.0, baseline=None)
    assert r.baseline_usd is None
    assert r.savings_usd == 0.0


def test_baseline_model_priced_server_side(tmp_path):
    # Name a baseline model instead of a dollar amount: the server prices the
    # same tokens from the published table. haiku routed, opus baseline.
    conn, org = _org(tmp_path)
    r = metering.record_usage(
        conn, org, provider="anthropic", model="claude-haiku-4-5",
        input_tokens=1_000_000, output_tokens=500_000, cost_usd=None,
        baseline_model="claude-opus-4-8", ts=_ts())
    assert round(r.cost_usd, 2) == 3.50       # haiku: 1*1.0 + 0.5*5.0
    assert round(r.baseline_usd, 2) == 52.50  # opus:  1*15 + 0.5*75
    assert round(r.savings_usd, 2) == 49.00


def test_underrecorded_cost_floored_at_actual_model_price(tmp_path):
    # A broken (too-low) recorded cost must NOT inflate the saving: the actual is
    # floored at the actual model's list price on the same tokens. sonnet routed,
    # opus baseline, 1M in / 0.5M out, but a bogus $0.10 recorded cost.
    conn, org = _org(tmp_path)
    r = metering.record_usage(
        conn, org, provider="anthropic", model="claude-sonnet-5",
        input_tokens=1_000_000, output_tokens=500_000, cost_usd=0.10,
        baseline_model="claude-opus-4-8", ts=_ts())
    # opus est = 15 + 37.5 = 52.50 ; sonnet floor = 3 + 7.5 = 10.50
    # saving = 52.50 - max(0.10, 10.50) = 42.00  (NOT 52.40 from the $0.10)
    assert round(r.savings_usd, 2) == 42.00


def test_explicit_baseline_cost_beats_model(tmp_path):
    # If both are given, the explicit USD figure wins (no server pricing).
    conn, org = _org(tmp_path)
    r = metering.record_usage(
        conn, org, provider="anthropic", model="claude-haiku-4-5",
        input_tokens=1000, output_tokens=1000, cost_usd=1.0,
        baseline_cost_usd=9.0, baseline_model="claude-opus-4-8", ts=_ts())
    assert r.baseline_usd == 9.0


def test_baseline_below_cost_clamps_to_zero(tmp_path):
    conn, org = _org(tmp_path)
    r = _meter(conn, org, cost=3.0, baseline=1.0)
    assert r.savings_usd == 0.0  # never negative


def test_zero_cost_event_not_billable(tmp_path):
    # A baseline-carrying event with no recorded cost ($0) must NOT bill its full
    # baseline as savings — that's an unprovable/phantom saving (data gap or free
    # model). It counts toward coverage but not billable gross.
    conn, org = _org(tmp_path)
    _meter(conn, org, cost=1.0, baseline=4.0, ts=_ts(1))   # billable: +3.00
    _meter(conn, org, cost=0.0, baseline=9.0, ts=_ts(2))   # $0 cost: excluded
    rep = savings.savings_share_report(conn, org, "2026-07").as_dict()
    assert rep["gross_savings_usd"] == 3.0        # only the $1-cost event
    assert rep["covered_events"] == 2             # both carried a baseline
    assert rep["billable_events"] == 1            # only one is billable
    assert any("excluded" in n for n in rep["notes"])


def test_negative_baseline_rejected(tmp_path):
    conn, org = _org(tmp_path)
    with pytest.raises(ValueError):
        _meter(conn, org, cost=1.0, baseline=-1.0)


# ------------------------------------------------------------ aggregation ----
def test_period_gross_sums_only_events_with_a_baseline(tmp_path):
    conn, org = _org(tmp_path)
    _meter(conn, org, cost=1.0, baseline=4.0, ts=_ts(1))   # +3.00
    _meter(conn, org, cost=2.0, baseline=2.5, ts=_ts(2))   # +0.50
    _meter(conn, org, cost=5.0, baseline=None, ts=_ts(3))  # +0 (no baseline)
    _meter(conn, org, cost=3.0, baseline=1.0, ts=_ts(4))   # +0 (clamped)

    rep = savings.savings_share_report(conn, org, "2026-07").as_dict()
    assert rep["gross_savings_usd"] == 3.5
    assert rep["billable_share_usd"] == round(3.5 * 0.10, 6)  # 0.35
    assert rep["covered_events"] == 3
    assert rep["total_events"] == 4
    assert rep["coverage_pct"] == 75.0


def test_events_outside_period_are_excluded(tmp_path):
    conn, org = _org(tmp_path)
    _meter(conn, org, cost=1.0, baseline=4.0, ts=_ts(10))  # July
    # a June event with a big baseline must NOT bleed into July
    june = dt.datetime(2026, 6, 15, tzinfo=dt.timezone.utc).timestamp()
    _meter(conn, org, cost=1.0, baseline=100.0, ts=june)

    rep = savings.savings_share_report(conn, org, "2026-07").as_dict()
    assert rep["gross_savings_usd"] == 3.0


# ------------------------------------------------------------ tamper chain ---
def test_baseline_is_hash_chained(tmp_path):
    conn, org = _org(tmp_path)
    _meter(conn, org, cost=1.0, baseline=4.0, ts=_ts(1))
    _meter(conn, org, cost=5.0, baseline=None, ts=_ts(2))  # no-baseline row too
    assert db.verify_chain(conn)["ok"] is True

    # Inflating a recorded baseline must break the chain (savings can't be forged)
    conn.execute("UPDATE usage_events SET baseline_micros=99000000 "
                 "WHERE baseline_micros IS NOT NULL")
    conn.commit()
    v = db.verify_chain(conn)
    assert v["ok"] is False
    assert v["orgs"][0]["status"] == "broken"


def test_no_baseline_rows_verify_like_pre_v7(tmp_path):
    # A chain of only no-baseline events must verify — proves the optional field
    # is omitted from the canonical form (byte-identical to the pre-#7 chain).
    conn, org = _org(tmp_path)
    for i in range(3):
        _meter(conn, org, cost=1.0 + i, baseline=None, ts=_ts(i + 1))
    assert db.verify_chain(conn)["ok"] is True


# ------------------------------------------- token-reduction baseline (#134) -
def test_baseline_tokens_priced_at_own_model(tmp_path):
    # The reduced call sent 100K input tokens; the counterfactual would have
    # sent 1M at the same model. opus input is $15/1M, output $75/1M.
    conn, org = _org(tmp_path)
    r = metering.record_usage(
        conn, org, provider="anthropic", model="claude-opus-4-8",
        input_tokens=100_000, output_tokens=10_000, cost_usd=None,
        baseline_input_tokens=1_000_000, baseline_output_tokens=10_000,
        ts=_ts())
    # actual: 0.1*15 + 0.01*75 = 2.25; baseline: 1*15 + 0.01*75 = 15.75
    assert round(r.cost_usd, 2) == 2.25
    assert round(r.savings_usd, 2) == 13.50
    assert round(r.baseline_usd, 2) == 15.75
    assert db.verify_chain(conn)["ok"] is True


def test_baseline_tokens_with_baseline_model(tmp_path):
    # Counterfactual counts priced at a DIFFERENT model: token reduction and
    # model substitution in one saving. haiku actual, opus counterfactual.
    conn, org = _org(tmp_path)
    r = metering.record_usage(
        conn, org, provider="anthropic", model="claude-haiku-4-5",
        input_tokens=100_000, output_tokens=0, cost_usd=None,
        baseline_input_tokens=1_000_000, baseline_output_tokens=0,
        baseline_model="claude-opus-4-8", ts=_ts())
    # actual: 0.1*1.0 = 0.10; baseline: 1*15 = 15.00
    assert round(r.cost_usd, 2) == 0.10
    assert round(r.savings_usd, 2) == 14.90


def test_baseline_tokens_underrecorded_cost_floored(tmp_path):
    # A corrupt/too-low cost_usd must not inflate a token-reduction saving:
    # the actual is floored at its own list price over the ACTUAL counts.
    conn, org = _org(tmp_path)
    r = metering.record_usage(
        conn, org, provider="anthropic", model="claude-opus-4-8",
        input_tokens=100_000, output_tokens=10_000, cost_usd=0.01,
        baseline_input_tokens=1_000_000, baseline_output_tokens=10_000,
        ts=_ts())
    # floor 2.25 beats the asserted 0.01: saving is 15.75 - 2.25, not - 0.01
    assert round(r.savings_usd, 2) == 13.50


def test_baseline_tokens_below_actual_clamp_to_zero(tmp_path):
    # Counterfactual smaller than the actual call books zero, never negative.
    conn, org = _org(tmp_path)
    r = metering.record_usage(
        conn, org, provider="anthropic", model="claude-opus-4-8",
        input_tokens=1_000_000, output_tokens=0, cost_usd=None,
        baseline_input_tokens=1_000, baseline_output_tokens=0, ts=_ts())
    assert r.savings_usd == 0.0


def test_negative_baseline_tokens_rejected(tmp_path):
    conn, org = _org(tmp_path)
    with pytest.raises(ValueError):
        metering.record_usage(
            conn, org, provider="openai", model="gpt-5",
            input_tokens=100, baseline_input_tokens=-1, ts=_ts())


def test_meter_track_passes_baselines_through(tmp_path):
    # #134: the SDK exposes all three baseline forms on the embedded path.
    from plutus_agent.client import Meter
    m = Meter(org="Acme SDK", db_path=str(tmp_path / "sdk.db"), create=True)
    r = m.track("anthropic", model="claude-opus-4-8",
                input_tokens=100_000, output_tokens=10_000,
                baseline_input_tokens=1_000_000,
                baseline_output_tokens=10_000)
    assert round(r.savings_usd, 2) == 13.50
    r2 = m.track("anthropic", model="claude-haiku-4-5",
                 input_tokens=1_000_000, output_tokens=500_000,
                 baseline_model="claude-opus-4-8")
    assert round(r2.savings_usd, 2) == 49.00
    r3 = m.track("openai", model="gpt-5", cost_usd=1.0, baseline_cost_usd=4.0)
    assert r3.savings_usd == 3.0
    assert db.verify_chain(m.conn)["ok"] is True


# ------------------------------------------------------------ share math -----
def test_share_math_integer_exact():
    # 1/3 dollar saved at 18% — rounds deterministically, no float drift
    assert savings._share_micros(0, 1800) == 0
    assert savings._share_micros(1_000_000, 1800) == 180_000        # $1 -> $0.18
    assert savings._share_micros(3_333_333, 1800) == round(3_333_333 * 0.18)


def test_rate_from_config():
    assert savings.rate_bps_from_config({}) == 1000
    assert savings.rate_bps_from_config({"billing": {"savings_share_pct": 0.25}}) == 2500
    with pytest.raises(ValueError):
        savings.rate_bps_from_config({"billing": {"savings_share_pct": 1.5}})


# ------------------------------------------------------------ billing --------
class _FakeStripe:
    available = True

    def __init__(self):
        self.calls = []

    def create_savings_invoice(self, conn, org_id, amount_usd, period_label,
                               description=""):
        self.calls.append((org_id, amount_usd, period_label))
        return {"id": f"in_test_{period_label}", "url": "https://pay", "status": "open"}


def test_dry_run_writes_nothing(tmp_path):
    conn, org = _org(tmp_path)
    _meter(conn, org, cost=1.0, baseline=4.0)
    out = savings.bill_savings_share(conn, org, "2026-07", apply=False)
    assert out["status"] == "dry_run"
    assert db.get_savings_invoice(conn, org, "2026-07") is None


def test_apply_records_and_invoices(tmp_path):
    conn, org = _org(tmp_path)
    _meter(conn, org, cost=1.0, baseline=11.0)  # $10 saved -> $1.00 share (clears min_charge)
    fake = _FakeStripe()
    out = savings.bill_savings_share(conn, org, "2026-07", apply=True,
                                     stripe_client=fake)
    assert out["status"] == "invoiced"
    assert out["stripe_invoice_id"] == "in_test_2026-07"
    assert len(fake.calls) == 1
    row = db.get_savings_invoice(conn, org, "2026-07")
    assert row["status"] == "invoiced"
    assert row["amount_usd"] == round(10.0 * 0.10, 6)


def test_apply_is_idempotent(tmp_path):
    conn, org = _org(tmp_path)
    _meter(conn, org, cost=1.0, baseline=11.0)  # $10 saved -> $1.00 share (clears min_charge)
    fake = _FakeStripe()
    savings.bill_savings_share(conn, org, "2026-07", apply=True, stripe_client=fake)
    again = savings.bill_savings_share(conn, org, "2026-07", apply=True, stripe_client=fake)
    assert again["status"] == "already_invoiced"
    assert len(fake.calls) == 1  # never billed twice
    rows = conn.execute(
        "SELECT COUNT(*) n FROM savings_invoices WHERE org_id=?", (org,)).fetchone()
    assert rows["n"] == 1


def test_below_minimum_records_without_stripe(tmp_path):
    conn, org = _org(tmp_path)
    # tiny saving: $0.10 saved -> $0.018 share, below the $0.50 default floor
    _meter(conn, org, cost=1.0, baseline=1.10)
    fake = _FakeStripe()
    out = savings.bill_savings_share(conn, org, "2026-07", apply=True,
                                     stripe_client=fake, min_charge_usd=0.50)
    assert out["applied"] is True
    assert out["stripe_invoice_id"] is None
    assert len(fake.calls) == 0  # no sub-dollar Stripe invoice
    assert db.get_savings_invoice(conn, org, "2026-07")["status"] == "pending"


def test_stripe_absent_records_pending(tmp_path):
    conn, org = _org(tmp_path)
    _meter(conn, org, cost=1.0, baseline=4.0)
    out = savings.bill_savings_share(conn, org, "2026-07", apply=True,
                                     stripe_client=None)
    assert out["applied"] is True
    assert out["status"] == "pending"
    assert db.get_savings_invoice(conn, org, "2026-07")["stripe_invoice_id"] is None


# ------------------------------------------------------------ HTTP ingest ----
def test_http_ingest_records_and_rejects_baseline(tmp_path):
    """The /v1/usage boundary accepts baseline_cost_usd and rejects a negative."""
    import json as _json
    import threading
    import urllib.error
    import urllib.request
    from plutus_agent.config import DEFAULT_CONFIG
    from plutus_agent.server import app

    dbpath = str(tmp_path / "http.db")
    conn = db.connect(dbpath)
    db.init_schema(conn)
    org_id = db.create_org(conn, "HttpCo", tier="pro")["id"]
    _, key = db.create_api_key(conn, org_id)
    db.add_ledger(conn, org_id, 10.0, "topup")
    conn.close()

    ctx = app._Ctx(dict(DEFAULT_CONFIG), dbpath, demo=False)
    httpd = app._Server(("127.0.0.1", 0), app.Handler, ctx)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        def _post(payload):
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/usage",
                data=_json.dumps(payload).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {key}"}, method="POST")
            return urllib.request.urlopen(req, timeout=5)

        # valid baseline records savings
        resp = _post({"provider": "openai", "model": "gpt-5",
                      "cost_usd": 1.0, "baseline_cost_usd": 4.0})
        body = _json.loads(resp.read())
        # single-event response flattens the event fields to the top level
        assert body["savings_usd"] == 3.0

        # negative baseline -> 400
        with pytest.raises(urllib.error.HTTPError) as cm:
            _post({"provider": "openai", "cost_usd": 1.0, "baseline_cost_usd": -2.0})
        assert cm.value.code == 400
    finally:
        httpd.shutdown()
        httpd.server_close()

    conn = db.connect(dbpath)
    agg = savings.period_savings(conn, org_id)
    assert agg["gross_savings_micros"] == 3_000_000
    conn.close()
