"""Efficiency view (#8): flagship-equivalent value vs actual cost.

Covers ledger_agent.efficiency — the token-derived value figures, the
family inference, the actual-paid basis and the multiple.
"""
import datetime as dt

from ledger_agent import db, metering, efficiency


def _org(tmp_path):
    conn = db.connect(str(tmp_path / "ledger.db"))
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
    assert round(rep["flagship_value_usd"], 2) == 17.50  # opus on same tokens (5/25)
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
    # flagship (deepseek-v4-pro): 10*0.435 + 2*0.87 = 4.35 + 1.74 = 6.09
    assert round(d["flagship_value_usd"], 2) == 6.09
    assert d["basis_usd"] == 0.02
    assert d["efficiency_usd"] == round(6.09 - 0.02, 6)
    assert d["multiple"] == round(6.09 / 0.02, 2)  # 304.5x


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
    assert rep["adherence_pct"] is None
    assert rep["leaked_usd"] == 0.0


def test_leakage_and_adherence(tmp_path):
    # optimal = the cheapest policy-passing option. cost above it = a leak /
    # off-policy turn; cost at-or-below = on-policy.
    conn, org = _org(tmp_path)
    # on-policy: ran exactly at the optimal ($1.00)
    metering.record_usage(conn, org, provider="anthropic", model="claude-haiku-4-5",
                          cost_usd=1.0, optimal_cost_usd=1.0, ts=_ts(1))
    # off-policy: ran the flagship ($5.00) when policy optimal was $1.00 -> leak 4
    metering.record_usage(conn, org, provider="anthropic", model="claude-opus-4-8",
                          cost_usd=5.0, optimal_cost_usd=1.0, ts=_ts(2))
    # no policy target -> not judged for adherence
    metering.record_usage(conn, org, provider="openai", model="gpt-5",
                          cost_usd=2.0, ts=_ts(3))
    d = efficiency.org_efficiency(conn, org, period_label="2026-07").as_dict()
    assert d["policy_events"] == 2
    assert d["on_policy_events"] == 1
    assert d["adherence_pct"] == 50.0
    assert d["leaked_usd"] == 4.0


def test_optimal_model_priced_server_side(tmp_path):
    # Name the policy-optimal model; the server prices the same tokens. Ran opus
    # (cost 17.50 est) when policy-optimal was haiku (3.50) -> leak 14.
    conn, org = _org(tmp_path)
    metering.record_usage(conn, org, provider="anthropic", model="claude-opus-4-8",
                          input_tokens=1_000_000, output_tokens=500_000,
                          cost_usd=None, optimal_model="claude-haiku-4-5", ts=_ts())
    d = efficiency.org_efficiency(conn, org, period_label="2026-07").as_dict()
    # opus est = 5 + 12.5 = 17.50 ; haiku optimal = 1 + 2.5 = 3.50
    assert d["policy_events"] == 1
    assert d["on_policy_events"] == 0
    assert round(d["leaked_usd"], 2) == 14.00


def test_optimal_chained_and_negative_rejected(tmp_path):
    conn, org = _org(tmp_path)
    metering.record_usage(conn, org, provider="anthropic", model="claude-opus-4-8",
                          cost_usd=5.0, optimal_cost_usd=1.0, ts=_ts())
    assert db.verify_chain(conn)["ok"] is True
    import pytest
    with pytest.raises(ValueError):
        metering.record_usage(conn, org, provider="anthropic", model="x",
                              cost_usd=1.0, optimal_cost_usd=-1.0, ts=_ts())
