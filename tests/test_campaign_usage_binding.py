from __future__ import annotations

import hashlib

import pytest

from ledger_agent import campaigns, db, metering
from ledger_agent.server.api import audit_json


def h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def make_manifest():
    return campaigns.build_manifest(
        campaign_id="campaign:usage-1", planned_cells=["cell:a"],
        provider_lanes=["fixture/lane"], config_hash=h("config-v1"),
        fixture_hash=h("fixture"), hard_stop_micros=100,
        runaway_guard_micros=100, continuation_allowed=True,
        action_intent_hash=h("intent-v1"),
    )


def make_binding(attempt=1, config="config-v1", **kwargs):
    return campaigns.build_binding(
        campaign_id="campaign:usage-1", cell_id="cell:a", lane="fixture/lane",
        config_hash=h(config), attempt=attempt, **kwargs,
    )


def test_campaign_binding_is_hash_bound_and_raw_payload_free():
    binding = make_binding()
    assert campaigns.validate_binding(binding) == (True, [])
    assert campaigns.binding_digest(binding) == binding["binding_hash"]
    with pytest.raises(ValueError, match="forbidden"):
        campaigns.build_binding(
            campaign_id="campaign:usage-1", cell_id="cell:a", lane="fixture/lane",
            config_hash=h("config-v1"), metadata={"prompt": "hidden"},
        )


def test_campaign_usage_round_trips_through_chain_and_audit(tmp_path):
    conn = db.connect(str(tmp_path / "usage.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "usage-org", tier="pro")["id"]
    manifest = make_manifest()
    db.create_campaign(conn, org_id, manifest)
    binding = make_binding()
    with db.immediate(conn):
        result = metering.record_usage(
            conn, org_id, provider="openai", model="fixture", task_type="benchmark",
            external_ref="cell:a", input_tokens=2, output_tokens=1, cost_usd=0.00005,
            campaign_binding=binding, commit=False,
        )
    assert result.recorded is True
    assert result.campaign_id == manifest["campaign_id"]
    assert db.campaign_spend_micros(conn, manifest["campaign_id"]) == 50
    receipt = audit_json(conn, org_id, external_ref="cell:a")
    assert receipt["events"][0]["campaign_binding"] == binding
    assert receipt["verification"]["chain_ok"] is True
    conn.close()


def test_campaign_budget_stop_writes_no_overrun_event(tmp_path):
    conn = db.connect(str(tmp_path / "budget.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "budget-org", tier="pro")["id"]
    manifest = make_manifest()
    db.create_campaign(conn, org_id, manifest)
    with db.immediate(conn):
        metering.record_usage(
            conn, org_id, provider="openai", model="fixture", task_type="benchmark",
            input_tokens=1, output_tokens=1, cost_usd=0.0001,
            campaign_binding=make_binding(), commit=False,
        )
    before = conn.execute("SELECT COUNT(*) AS n FROM usage_events").fetchone()["n"]
    with pytest.raises(ValueError, match="campaign budget guard"):
        with db.immediate(conn):
            metering.record_usage(
                conn, org_id, provider="openai", model="fixture", task_type="benchmark",
                input_tokens=1, output_tokens=1, cost_usd=0.000001,
                campaign_binding=make_binding(), commit=False,
            )
    after = conn.execute("SELECT COUNT(*) AS n FROM usage_events").fetchone()["n"]
    assert after == before == 1
    assert db.campaign_spend_micros(conn, manifest["campaign_id"]) == 100
    conn.close()


def test_sdk_local_campaign_binding_uses_serialized_budget_guard(tmp_path):
    from ledger_agent.client import Meter
    meter = Meter(org="sdk-campaign-org", tier="pro", db_path=str(tmp_path / "sdk.db"))
    manifest = campaigns.build_manifest(
        campaign_id="campaign:sdk-1", planned_cells=["cell:a"],
        provider_lanes=["fixture/lane"], config_hash=h("config-v1"),
        fixture_hash=h("fixture"), hard_stop_micros=100,
        action_intent_hash=h("intent-v1"),
    )
    db.create_campaign(meter.conn, meter.org_id, manifest)
    result = meter.track(
        "openai", model="fixture", task_type="benchmark", input_tokens=1,
        output_tokens=1, cost_usd=0.0001,
        campaign_binding=campaigns.build_binding(
            campaign_id="campaign:sdk-1", cell_id="cell:a", lane="fixture/lane",
            config_hash=h("config-v1"),
        ),
    )
    assert result.recorded is True
    assert db.campaign_spend_micros(meter.conn, "campaign:sdk-1") == 100
    meter.close()


def test_campaign_wrapper_durably_records_budget_stop(tmp_path):
    conn = db.connect(str(tmp_path / "stop.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "stop-org", tier="pro")["id"]
    manifest = make_manifest()
    db.create_campaign(conn, org_id, manifest)
    campaigns.record_usage(
        conn, org_id, campaign_binding=make_binding(), provider="openai",
        model="fixture", task_type="benchmark", input_tokens=1,
        output_tokens=1, cost_usd=0.0001,
    )
    with pytest.raises(campaigns.CampaignBudgetError):
        campaigns.record_usage(
            conn, org_id, campaign_binding=make_binding(), provider="openai",
            model="fixture", task_type="benchmark", input_tokens=1,
            output_tokens=1, cost_usd=0.000001,
        )
    row = db.get_campaign(conn, org_id, manifest["campaign_id"])
    assert row["budget_status"] == "stopped"
    assert row["stop_reason"] == "runaway_guard_exceeded"
    assert db.campaign_usage_count(conn, manifest["campaign_id"]) == 1
    conn.close()
