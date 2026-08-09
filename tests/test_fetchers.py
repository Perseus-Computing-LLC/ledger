"""Provider cost fetchers + scheduled close (#109).

Covers the response parsers (against captured sample shapes), the HTTP fetchers
with an injected opener (no network), the never-assume-zero orchestration, and
`reconcile.close_period` (fetch → reconcile) in dry-run and apply.
"""
import pytest

from ledger_agent import db, fetchers, metering, reconcile


# --------------------------------------------------------------- parsers ------
def test_parse_openai_costs_sums_usd():
    payload = {"data": [
        {"results": [{"amount": {"value": 1.25, "currency": "usd"}}]},
        {"results": [{"amount": {"value": 2.75, "currency": "usd"}},
                     {"amount": {"value": 0.50, "currency": "usd"}}]},
    ], "has_more": False}
    assert fetchers.parse_openai_costs(payload) == pytest.approx(4.50)


def test_parse_openai_costs_rejects_non_usd():
    payload = {"data": [{"results": [{"amount": {"value": 1.0, "currency": "eur"}}]}]}
    with pytest.raises(fetchers.FetchError):
        fetchers.parse_openai_costs(payload)


def test_parse_openai_costs_rejects_bad_shape():
    with pytest.raises(fetchers.FetchError):
        fetchers.parse_openai_costs({"nope": 1})


def test_parse_anthropic_cost_report_string_amounts():
    payload = {"data": [
        {"results": [{"amount": "3.10", "currency": "USD"}]},
        {"results": [{"amount": "0.90", "currency": "USD"}]},
    ], "has_more": False}
    assert fetchers.parse_anthropic_cost_report(payload) == pytest.approx(4.00)


def test_parse_aws_cost_explorer():
    resp = {"ResultsByTime": [
        {"Total": {"UnblendedCost": {"Amount": "12.34", "Unit": "USD"}}},
        {"Total": {"UnblendedCost": {"Amount": "0.66", "Unit": "USD"}}},
    ]}
    assert fetchers.parse_aws_cost_explorer(resp) == pytest.approx(13.00)


# --------------------------------------------------------------- fetchers -----
def test_fetch_openai_with_injected_opener():
    calls = []

    def opener(url, headers):
        calls.append(url)
        assert headers["Authorization"] == "Bearer sk-admin"
        return {"data": [{"results": [{"amount": {"value": 5.0, "currency": "usd"}}]}],
                "has_more": False}

    total = fetchers.fetch_openai(0, 100, api_key="sk-admin", opener=opener)
    assert total == pytest.approx(5.0)
    assert calls and "start_time=0" in calls[0]


def test_fetch_openai_paginates():
    pages = [
        {"data": [{"results": [{"amount": {"value": 1.0, "currency": "usd"}}]}],
         "has_more": True, "next_page": "p2"},
        {"data": [{"results": [{"amount": {"value": 2.0, "currency": "usd"}}]}],
         "has_more": False},
    ]
    seq = iter(pages)

    def opener(url, headers):
        return next(seq)

    assert fetchers.fetch_openai(0, 100, api_key="k", opener=opener) == pytest.approx(3.0)


def test_fetch_openai_missing_key(monkeypatch):
    monkeypatch.delenv("OPENAI_ADMIN_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(fetchers.FetchError):
        fetchers.fetch_openai(0, 100)


def test_fetch_anthropic_with_injected_opener():
    def opener(url, headers):
        assert headers["x-api-key"] == "adm"
        assert "starting_at=" in url
        return {"data": [{"results": [{"amount": "2.00", "currency": "USD"}]}],
                "has_more": False}

    assert fetchers.fetch_anthropic(0, 100, api_key="adm", opener=opener) == pytest.approx(2.0)


def test_fetch_aws_no_boto3_is_clear_error():
    # No injected client and boto3 not expected in the test env → clear FetchError.
    with pytest.raises(fetchers.FetchError):
        fetchers.fetch_aws_bedrock(0, 100, client=None)


def test_fetch_aws_with_injected_client():
    class FakeCE:
        def get_cost_and_usage(self, **kw):
            assert kw["Filter"]["Dimensions"]["Values"] == ["Amazon Bedrock"]
            return {"ResultsByTime": [
                {"Total": {"UnblendedCost": {"Amount": "7.50", "Unit": "USD"}}}]}

    assert fetchers.fetch_aws_bedrock(0, 100, client=FakeCE()) == pytest.approx(7.50)


# --------------------------------------------------- orchestration: never zero --
def test_fetch_authoritative_mix_success_and_error():
    reg = {
        "openai": lambda s, e: 10.0,
        "anthropic": lambda s, e: (_ for _ in ()).throw(fetchers.FetchError("no key")),
    }
    totals, errors = fetchers.fetch_authoritative(
        ["openai", "anthropic", "unknownprov"], 0, 100, fetchers=reg)
    assert totals == {"openai": 10.0}          # only the real figure
    assert "anthropic" in errors               # failed fetch is NOT zeroed
    assert "unknownprov" in errors             # no fetcher → error, not total
    assert "anthropic" not in totals


# ------------------------------------------------------------ close_period -----
def _org(tmp_path, credit=1000.0):
    conn = db.connect(str(tmp_path / "ledger.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "Acme", tier="pro")["id"]
    if credit:
        db.add_ledger(conn, org_id, credit, "topup", reason="seed")
    return conn, org_id


def _meter(conn, org_id, provider, cost, n=1, ts=None):
    for _ in range(n):
        metering.record_usage(conn, org_id, provider=provider, model="m",
                              cost_usd=cost, ts=ts)


def test_previous_month_label():
    # 2026-03-04 -> previous month 2026-02; January wraps to prior December.
    import datetime as dt
    mar = dt.datetime(2026, 3, 4, tzinfo=dt.timezone.utc).timestamp()
    assert reconcile.previous_month_label(mar) == "2026-02"
    jan = dt.datetime(2026, 1, 15, tzinfo=dt.timezone.utc).timestamp()
    assert reconcile.previous_month_label(jan) == "2025-12"


def _july_ts():
    import datetime as dt
    return dt.datetime(2026, 7, 15, tzinfo=dt.timezone.utc).timestamp()


def test_close_period_dry_run(tmp_path):
    conn, org = _org(tmp_path)
    _meter(conn, org, "openai", 1.0, n=5, ts=_july_ts())   # recorded $5
    reg = {"openai": lambda s, e: 4.0}                      # provider billed $4
    out = reconcile.close_period(conn, org, "2026-07", providers=["openai"],
                                 apply=False, fetchers=reg)
    assert out["fetched"] == {"openai": 4.0}
    assert out["fetch_errors"] == {}
    item = out["items"][0]
    assert item["delta_usd"] == pytest.approx(1.0)          # over-charged -> credit back
    assert db.get_balance(conn, org) == pytest.approx(995.0)  # unchanged (dry run)


def test_close_period_apply_writes_adjust(tmp_path):
    conn, org = _org(tmp_path)
    _meter(conn, org, "openai", 1.0, n=5, ts=_july_ts())
    reg = {"openai": lambda s, e: 4.0}
    out = reconcile.close_period(conn, org, "2026-07", providers=["openai"],
                                 apply=True, fetchers=reg)
    assert out["applied"] is True
    # ledger now reflects the authoritative $4 for openai this period
    assert db.get_balance(conn, org) == pytest.approx(996.0)
    # idempotent: re-running with the same total is a no-op
    out2 = reconcile.close_period(conn, org, "2026-07", providers=["openai"],
                                  apply=True, fetchers=reg)
    assert out2["total_adjust_usd"] == pytest.approx(0.0)
    assert db.get_balance(conn, org) == pytest.approx(996.0)


def test_close_period_failed_fetch_left_unreconciled(tmp_path):
    conn, org = _org(tmp_path)
    _meter(conn, org, "openai", 1.0, n=3, ts=_july_ts())   # recorded $3
    bal_before = db.get_balance(conn, org)
    reg = {"openai": lambda s, e: (_ for _ in ()).throw(fetchers.FetchError("down"))}
    out = reconcile.close_period(conn, org, "2026-07", providers=["openai"],
                                 apply=True, fetchers=reg)
    assert "openai" in out["fetch_errors"]
    assert out["fetched"] == {}
    assert out["items"] == []                              # nothing reconciled
    assert db.get_balance(conn, org) == pytest.approx(bal_before)  # NOT zeroed


def test_close_period_auto_detects_providers(tmp_path):
    conn, org = _org(tmp_path)
    _meter(conn, org, "openai", 1.0, n=2, ts=_july_ts())
    _meter(conn, org, "anthropic", 2.0, n=1, ts=_july_ts())
    seen = []
    reg = {
        "openai": lambda s, e: seen.append("openai") or 2.0,
        "anthropic": lambda s, e: seen.append("anthropic") or 2.0,
    }
    out = reconcile.close_period(conn, org, "2026-07", apply=False, fetchers=reg)
    assert set(out["providers_requested"]) == {"openai", "anthropic"}
    assert set(seen) == {"openai", "anthropic"}
