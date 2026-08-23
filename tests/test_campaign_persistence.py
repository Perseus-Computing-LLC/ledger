from __future__ import annotations

import hashlib
import json

import pytest

from ledger_agent import campaigns, db, metering
from ledger_agent.server.api import campaign_json


def h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def make_manifest(campaign_id="campaign:persist-1"):
    return campaigns.build_manifest(
        campaign_id=campaign_id,
        planned_cells=["cell:a", "cell:b"],
        provider_lanes=["fixture/lane"],
        config_hash=h("config"),
        fixture_hash=h("fixture"),
        hard_stop_micros=1000,
        continuation_allowed=True,
        retry_policy="new_version_only",
        action_intent_hash=h("intent"),
    )


def make_check(status="pass", campaign_id="campaign:persist-1", usage_event_ids=None):
    if usage_event_ids is None:
        usage_event_ids = ["evt_1"] if status == "pass" else []
    return campaigns.build_check(
        campaign_id=campaign_id, cell_id="cell:a", lane="fixture/lane",
        status=status, config_hash=h("config"), result_hash=h("result") if status == "pass" else None,
        evidence_hashes=[h("evidence")] if status == "pass" else [],
        usage_event_ids=usage_event_ids,
        reason_code=None if status == "pass" else "skipped_by_fixture",
    )


def record_bound_usage(conn, org_id, campaign_id):
    binding = campaigns.build_binding(
        campaign_id=campaign_id, cell_id="cell:a", lane="fixture/lane",
        config_hash=h("config"),
    )
    result = metering.record_usage(
        conn, org_id, provider="fixture", model="fixture", cost_usd=0.0001,
        campaign_binding=binding,
    )
    return result.event_id


def test_campaign_manifest_check_and_receipt_survive_restart(tmp_path):
    path = str(tmp_path / "campaign.db")
    conn = db.connect(path)
    db.init_schema(conn)
    org_id = db.create_org(conn, "campaign-org", tier="pro")["id"]
    manifest = make_manifest()
    stored = db.create_campaign(conn, org_id, manifest)
    assert stored["id"] == manifest["campaign_id"]
    assert stored["manifest_hash"] == manifest["manifest_hash"]
    assert json.loads(stored["manifest_json"]) == manifest
    event_id = record_bound_usage(conn, org_id, manifest["campaign_id"])
    check = make_check(campaign_id=manifest["campaign_id"], usage_event_ids=[event_id])
    db.record_campaign_check(conn, org_id, check)
    receipt = campaigns.build_receipt(
        manifest=manifest, checks=[check], framework_status="completed",
        target_status="pass", evidence_status="complete", spent_micros=100,
    )
    db.finalize_campaign(conn, org_id, receipt)
    conn.close()

    conn = db.connect(path)
    db.init_schema(conn)
    loaded = db.get_campaign(conn, org_id, manifest["campaign_id"])
    assert loaded["framework_status"] == "completed"
    assert loaded["target_status"] == "pass"
    assert loaded["receipt_hash"] == receipt["receipt_hash"]
    assert json.loads(loaded["receipt_json"]) == receipt
    assert db.list_campaign_checks(conn, org_id, manifest["campaign_id"])[0]["check_hash"] == check["check_hash"]
    assert db.campaign_spend_micros(conn, manifest["campaign_id"]) == 100
    assert db.get_schema_version(conn) == db.SCHEMA_VERSION
    conn.close()


def test_campaign_create_and_finalize_are_idempotent_but_conflicts_fail_closed(tmp_path):
    conn = db.connect(str(tmp_path / "idempotent.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "idempotent-org", tier="free")["id"]
    manifest = make_manifest("campaign:idempotent")
    assert db.create_campaign(conn, org_id, manifest)["manifest_hash"] == manifest["manifest_hash"]
    assert db.create_campaign(conn, org_id, manifest)["manifest_hash"] == manifest["manifest_hash"]
    changed = make_manifest("campaign:idempotent")
    changed["fixture_hash"] = h("other-fixture")
    changed["manifest_hash"] = campaigns.manifest_digest(changed)
    with pytest.raises(ValueError, match="manifest conflict"):
        db.create_campaign(conn, org_id, changed)
    conn.close()


def test_missing_usage_cannot_finalize_a_verified_pass(tmp_path):
    conn = db.connect(str(tmp_path / "missing-usage.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "missing-usage-org", tier="pro")["id"]
    manifest = make_manifest("campaign:missing-usage")
    db.create_campaign(conn, org_id, manifest)
    check = make_check(campaign_id=manifest["campaign_id"], usage_event_ids=["evt_missing"])
    db.record_campaign_check(conn, org_id, check)
    receipt = campaigns.build_receipt(
        manifest=manifest, checks=[check], framework_status="completed",
        target_status="pass", evidence_status="complete",
    )
    with pytest.raises(ValueError, match="usage_missing"):
        db.finalize_campaign(conn, org_id, receipt)
    assert db.get_campaign(conn, org_id, manifest["campaign_id"])["receipt_hash"] is None
    conn.close()


def test_campaign_scope_is_enforced_for_checks_and_reads(tmp_path):
    conn = db.connect(str(tmp_path / "scope.db"))
    db.init_schema(conn)
    org_a = db.create_org(conn, "scope-a", tier="free")["id"]
    org_b = db.create_org(conn, "scope-b", tier="free")["id"]
    manifest = make_manifest("campaign:scope")
    db.create_campaign(conn, org_a, manifest)
    with pytest.raises(ValueError, match="campaign not found"):
        db.record_campaign_check(conn, org_b, make_check())
    assert db.get_campaign(conn, org_b, manifest["campaign_id"]) is None
    conn.close()


def test_campaign_json_is_public_safe_and_verifiable(tmp_path):
    conn = db.connect(str(tmp_path / "summary.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "summary-org", tier="pro")["id"]
    manifest = make_manifest("campaign:summary")
    db.create_campaign(conn, org_id, manifest)
    event_id = record_bound_usage(conn, org_id, manifest["campaign_id"])
    check = make_check(campaign_id=manifest["campaign_id"], usage_event_ids=[event_id])
    db.record_campaign_check(conn, org_id, check)
    receipt = campaigns.build_receipt(
        manifest=manifest, checks=[check], framework_status="completed",
        target_status="pass", evidence_status="complete", spent_micros=100,
    )
    db.finalize_campaign(conn, org_id, receipt)
    summary = campaign_json(conn, org_id, manifest["campaign_id"])
    assert summary["manifest"]["manifest_hash"] == manifest["manifest_hash"]
    assert summary["checks"][0]["check_hash"] == check["check_hash"]
    assert summary["receipt"]["receipt_hash"] == receipt["receipt_hash"]
    assert summary["verification"]["verified_pass"] is True
    assert "prompt" not in json.dumps(summary)
    conn.close()
