"""Efficiency view (#8): flagship-equivalent value vs actual cost.

Covers plutus_agent.efficiency — the token-derived value figures, the
family inference, the actual-paid basis and the multiple.
"""
import datetime as dt

from plutus_agent import db, metering, efficiency


def _org(tmp_path):
    conn = db.connect(str(tmp_path / "plutus.db"))
    db.init_schema(conn)
    return conn, db.create_org(conn, "Acme", tier="pro", owner_email="a@b.co")["id"]


def _ts(day=10):
    return dt.datetime(2026, 7, day, tzinfo=dt.timezone.utc).timestamp()


def test_family_inference_from_model_name():
    assert efficiency.family_of("unknown", "claude-haiku-4-5") == "anthropic"
    assert efficiency.family_of("", "deepseek/deepseek-v4-flash") == "deepseek"
    assert efficiency.family_of("", "gemma-4-26b") == "google"
    assert efficiency.family_of("openai", None) == "openai"


def test_flagship_value_exceeds_list_when_routed(tmp_path):
    # A cheap model run: list value (haiku) << flagship value (opus).
    conn, org = _org(tmp_path)
    metering.record_usage(conn, org, provider="anthropic", model="claude-haiku-4-5",
                          input_tokens=1_000_000, output_tokens=500_000,
                          cost_usd=3.50, ts=_ts())
    rep = efficiency.org_efficiency(conn, org, period_label="2026-07").as_dict()
    assert round(rep["list_value_usd"], 2) == 3.50       # haiku list
    assert round(rep["flagship_value_usd"], 2) == 52.50  # opus on same tokens
    assert rep["events"] == 1


def test_local_model_value_at_near_zero_cost(tmp_path):
    # The DeepSeek story: real tokens, tiny actual paid -> big efficiency multiple.
    conn, org = _org(tmp_path)
    # deepseek-v4-flash run: list value from tokens, but only $0.02 actually paid
    metering.record_usage(conn, org, provider="deepseek", model="deepseek-v4-flash",
                          input_tokens=10_000_000, output_tokens=2_000_000,
                          cost_usd=0.02, ts=_ts())
    rep = efficiency.org_efficiency(conn, org, period_label="2026-07",
                                    actual_paid_usd=0.02)
    d = rep.as_dict()
    # flagship (deepseek-v4-pro): 10*0.55 + 2*2.19 = 5.5 + 4.38 = 9.88
    assert round(d["flagship_value_usd"], 2) == 9.88
    assert d["basis_usd"] == 0.02
    assert d["efficiency_usd"] == round(9.88 - 0.02, 6)
    assert d["multiple"] == round(9.88 / 0.02, 2)  # 494x


def test_actual_paid_overrides_metered_basis(tmp_path):
    conn, org = _org(tmp_path)
    metering.record_usage(conn, org, provider="deepseek", model="deepseek-v4-pro",
                          input_tokens=1_000_000, output_tokens=0,
                          cost_usd=100.0, ts=_ts())  # bogus recorded cost
    # without actual_paid, basis = metered ($100)
    r1 = efficiency.org_efficiency(conn, org, period_label="2026-07")
    assert r1.basis_usd == 100.0
    # with console truth, basis = actual paid
    r2 = efficiency.org_efficiency(conn, org, period_label="2026-07",
                                   actual_paid_usd=0.55)
    assert r2.basis_usd == 0.55


def test_empty_period(tmp_path):
    conn, org = _org(tmp_path)
    rep = efficiency.org_efficiency(conn, org, period_label="2026-07").as_dict()
    assert rep["events"] == 0
    assert rep["flagship_value_usd"] == 0.0
    assert rep["multiple"] is None
