from __future__ import annotations

import copy
import hashlib
import json

import pytest

from ledger_agent import campaigns


def h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def manifest(**overrides):
    value = campaigns.build_manifest(
        campaign_id="campaign:acceptance-1",
        planned_cells=["cell:a", "cell:b"],
        provider_lanes=["openai/gpt-fixture"],
        config_hash=h("config-v1"),
        fixture_hash=h("fixture-v1"),
        expected_spend_min_micros=100,
        expected_spend_max_micros=500,
        hard_stop_micros=600,
        runaway_guard_micros=550,
        retry_policy="new_version_only",
        continuation_allowed=True,
        action_intent_hash=h("intent-v1"),
        target_commit_hash=h("target-commit"),
    )
    value.update(overrides)
    value["manifest_hash"] = campaigns.manifest_digest(value)
    return value


def check(**overrides):
    value = campaigns.build_check(
        campaign_id="campaign:acceptance-1",
        cell_id="cell:a",
        lane="openai/gpt-fixture",
        status="pass",
        config_hash=h("config-v1"),
        result_hash=h("result-a"),
        evidence_hashes=[h("evidence-a")],
        usage_event_ids=["evt_a"],
    )
    value.update(overrides)
    value["check_hash"] = campaigns.check_digest(value)
    return value


def test_manifest_and_check_are_hash_bound_and_public_safe():
    m = manifest()
    c = check()
    assert campaigns.validate_manifest(m) == (True, [])
    assert campaigns.validate_check(c) == (True, [])
    assert campaigns.manifest_digest(m) == m["manifest_hash"]
    assert campaigns.check_digest(c) == c["check_hash"]
    raw = json.dumps({"manifest": m, "check": c}, sort_keys=True)
    assert "prompt" not in raw
    assert "memory_body" not in raw
    assert "provider_payload" not in raw
    assert "api_key" not in raw


def test_receipt_separates_framework_completion_from_target_failure():
    receipt = campaigns.build_receipt(
        manifest=manifest(),
        checks=[check(status="fail", result_hash=h("failed-result"))],
        framework_status="completed",
        target_status="fail",
        evidence_status="complete",
        finalization_status="complete",
        spent_micros=300,
        remaining_micros=300,
    )
    assert receipt["framework_status"] == "completed"
    assert receipt["target_status"] == "fail"
    assert receipt["verification"]["valid"] is True
    assert receipt["verification"]["verified_pass"] is False
    assert receipt["counts"] == {"planned": 2, "executed": 1, "passed": 0, "failed": 1, "skipped": 0}


def test_all_skipped_is_inconclusive_not_pass():
    receipt = campaigns.build_receipt(
        manifest=manifest(),
        checks=[check(status="skip", result_hash=None, evidence_hashes=[], usage_event_ids=[])],
        framework_status="completed",
        evidence_status="complete",
        finalization_status="complete",
        spent_micros=0,
        remaining_micros=600,
    )
    assert receipt["target_status"] == "inconclusive"
    assert receipt["verification"]["verified_pass"] is False
    assert "no_executed_checks" in receipt["verification"]["reasons"]


def test_framework_error_before_checks_is_not_run():
    receipt = campaigns.build_receipt(
        manifest=manifest(), checks=[], framework_status="error",
        finalization_status="failed", finalization_reason="runner_error",
    )
    assert receipt["target_status"] == "not_run"
    assert receipt["verification"]["verified_pass"] is False
    assert "framework_not_completed" in receipt["verification"]["reasons"]


def test_failed_finalization_downgrades_a_target_pass():
    receipt = campaigns.build_receipt(
        manifest=manifest(), checks=[check()], framework_status="completed",
        target_status="pass", evidence_status="complete",
        finalization_status="failed", finalization_reason="evidence_write_failed",
        spent_micros=200, remaining_micros=400,
    )
    assert receipt["target_status"] == "pass"
    assert receipt["verification"]["valid"] is True
    assert receipt["verification"]["verified_pass"] is False
    assert "finalization_failed" in receipt["verification"]["reasons"]


def test_validation_rejects_tampering_unknown_fields_and_raw_payloads():
    tampered = manifest()
    tampered["planned_cells"] = ["cell:forged"]
    assert campaigns.validate_manifest(tampered)[0] is False
    unknown = manifest()
    unknown["unexpected"] = "nope"
    assert "unknown:unexpected" in campaigns.validate_manifest(unknown)[1]
    leaked = manifest()
    leaked["prompt"] = "do not persist"
    valid, errors = campaigns.validate_manifest(leaked)
    assert not valid
    assert "forbidden_field:prompt" in errors


def test_correction_attempt_requires_new_lineage_and_config():
    prior = check(status="fail", attempt=1, result_hash=h("failed"))
    resumed = check(
        cell_id="cell:a", status="pass", attempt=2,
        continuation=True, parent_attempt=1,
        config_hash=h("config-v2"), action_intent_hash=h("intent-v2"),
        result_hash=h("repaired"), usage_event_ids=["evt_b"],
    )
    assert campaigns.validate_check(prior)[0]
    assert campaigns.validate_check(resumed)[0]
    assert campaigns.validate_attempt_lineage([prior, resumed]) == (True, [])
    invalid = copy.deepcopy(resumed)
    invalid["config_hash"] = prior["config_hash"]
    invalid["check_hash"] = campaigns.check_digest(invalid)
    assert "continuation_config_unchanged" in campaigns.validate_attempt_lineage([prior, invalid])[1]


def test_budget_admission_is_fail_closed_at_runaway_and_hard_stop():
    assert campaigns.admit_spend(manifest(), spent_micros=500, proposed_micros=50)["allowed"] is True
    runaway = campaigns.admit_spend(manifest(), spent_micros=500, proposed_micros=51)
    assert runaway == {"allowed": False, "reason": "runaway_guard_exceeded", "remaining_micros": 50}
    hard = campaigns.admit_spend(
        manifest(runaway_guard_micros=None), spent_micros=500, proposed_micros=101,
    )
    assert hard == {"allowed": False, "reason": "hard_stop_exceeded", "remaining_micros": 100}
    with pytest.raises(ValueError, match="non-negative"):
        campaigns.admit_spend(manifest(), spent_micros=0, proposed_micros=-1)


def test_verified_pass_rejects_forged_target_status_and_counts():
    failed = check(status="fail", result_hash=h("failed"))
    receipt = campaigns.build_receipt(
        manifest=manifest(), checks=[failed], framework_status="completed",
        target_status="fail", evidence_status="complete",
        finalization_status="complete", spent_micros=1,
    )
    forged = copy.deepcopy(receipt)
    forged["target_status"] = "pass"
    forged["counts"]["failed"] = 0
    forged["counts"]["passed"] = 1
    forged["receipt_hash"] = campaigns.receipt_digest(forged)
    verification = campaigns.verify_campaign_receipt(
        forged, manifest=manifest(), checks=[failed],
    )
    assert verification["valid"] is False
    assert verification["verified_pass"] is False
    assert "target_status_mismatch" in verification["reasons"]
    assert "counts_mismatch" in verification["reasons"]


def test_continuation_requires_manifest_permission():
    prior = check(status="fail", result_hash=h("failed"))
    resumed = check(
        cell_id="cell:a", status="pass", attempt=2, continuation=True,
        parent_attempt=1, config_hash=h("config-v2"),
        action_intent_hash=h("intent-v2"), result_hash=h("repaired"),
        usage_event_ids=["evt_b"],
    )
    with pytest.raises(ValueError, match="continuation is not allowed"):
        campaigns.build_receipt(
            manifest=manifest(continuation_allowed=False),
            checks=[prior, resumed], framework_status="completed",
            evidence_status="complete",
        )
