from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from ledger_agent.trajectory import (
    EVENT_KINDS,
    GENESIS_HASH,
    ComposedGate,
    Trajectory,
    evaluate_submission,
    find_evidence_chain,
    trajectory_root_hash,
    verify_evidence,
)


ROOT = Path(__file__).resolve().parents[1]


def make_trajectory() -> Trajectory:
    trajectory = Trajectory()
    trajectory.append("model_message", {"text": "done"}, timestamp_ms=1)
    return trajectory


def evidence_requirements() -> list[dict]:
    return [
        {
            "property": "test_suite_passes",
            "verifier": "test_run",
            "ref_state": {"expected_pass": True},
        },
        {
            "property": "log_contains",
            "verifier": "log_capture",
            "ref_state": {"marker": "ALL TESTS PASSED"},
        },
        {
            "property": "citation_real",
            "verifier": "citation_lookup",
            "ref_state": {"source_urls": {"https://arxiv.org/abs/2608.11274"}},
        },
    ]


def add_evidence(trajectory: Trajectory) -> None:
    trajectory.append(
        "tool_result",
        {"exit_code": 0, "command": "pytest tests/ -q"},
        timestamp_ms=2,
    )
    trajectory.append(
        "tool_result",
        {"log_text": "pytest: ALL TESTS PASSED"},
        timestamp_ms=3,
    )
    trajectory.append(
        "citation_lookup",
        {"cited_url": "https://arxiv.org/abs/2608.11274"},
        timestamp_ms=4,
    )


def test_genesis_append_and_verify_chain_happy_path():
    trajectory = Trajectory()
    assert trajectory.head_hash == GENESIS_HASH

    event = trajectory.append("tool_call", {"name": "pytest"}, timestamp_ms=100)

    assert event["kind"] == "tool_call"
    assert event["timestamp_ms"] == 100
    assert event["prev_hash"] == GENESIS_HASH
    assert len(event["hash"]) == 64
    assert trajectory.verify_chain() == (True, "ok")


def test_unknown_kind_is_rejected():
    trajectory = Trajectory()
    with pytest.raises(ValueError, match="unknown event kind"):
        trajectory.append("unknown_kind", {}, timestamp_ms=1)
    assert "model_message" in EVENT_KINDS


def test_tampering_invalidates_the_tampered_event_and_chain_suffix():
    trajectory = Trajectory()
    trajectory.append("tool_call", {"name": "pytest"}, timestamp_ms=1)
    trajectory.append("tool_result", {"exit_code": 0}, timestamp_ms=2)
    trajectory.append("commit", {"commit_sha": "a" * 40}, timestamp_ms=3)
    original_suffix_hash = trajectory.events[2]["hash"]

    trajectory.events[0]["payload"]["name"] = " 다른"

    valid, reason = trajectory.verify_chain()
    assert valid is False
    assert reason == "hash_mismatch"
    assert trajectory.events[2]["hash"] == original_suffix_hash


def test_round_trip_is_deterministic_and_diff_stable():
    trajectory = Trajectory()
    trajectory.append("file_read", {"path": "src/main.py"}, timestamp_ms=1)
    trajectory.append("file_write", {"path": "src/main.py", "bytes": 3}, timestamp_ms=2)

    first = trajectory.to_dict()
    second = trajectory.to_dict()
    restored = Trajectory.from_dict(json.loads(json.dumps(first, sort_keys=True)))

    assert first == second
    assert restored.to_dict() == first
    assert json.dumps(first, sort_keys=True, separators=(",", ":")) == json.dumps(
        second, sort_keys=True, separators=(",", ":")
    )


def test_known_verifiers_distinguish_hard_accept_from_reject():
    event = {"kind": "tool_result", "payload": {"exit_code": 0}}
    assert verify_evidence(event, "test_suite_passes", "test_run", {"expected_pass": True}) == "accept"
    assert verify_evidence(event, "test_suite_passes", "test_run", {"expected_pass": False}) == "reject"
    assert verify_evidence(event, "test_suite_passes", "test_run", {"expected_pass": True, "deterministic": False}) == "soft"


def test_soft_evidence_includes_done_model_message_and_unknown_verifiers():
    done = {"kind": "model_message", "payload": {"text": "done"}}
    assert verify_evidence(done, "test_suite_passes", "test_run", {"expected_pass": True}) == "soft"
    assert verify_evidence(done, "anything", "missing_verifier", {}) == "soft"


def test_all_shipped_evidence_verifiers_cover_required_payload_shapes():
    assert verify_evidence(
        {"kind": "citation_lookup", "payload": {"cited_url": "https://example.test/source"}},
        "citation_real",
        "citation_lookup",
        {"source_urls": {"https://example.test/source"}},
    ) == "accept"
    assert verify_evidence(
        {"kind": "file_write", "payload": {"diff": "@@ -1 +1 @@\n-old\n+new"}},
        "diff_matches",
        "file_diff",
        {"expected_hunk": "@@ -1 +1 @@\n-old\n+new"},
    ) == "accept"
    assert verify_evidence(
        {"kind": "shell_exec", "payload": {"exit_code": 127}},
        "exit_code_captured",
        "shell_exec",
        {},
    ) == "accept"
    assert verify_evidence(
        {"kind": "commit", "payload": {"commit_sha": "a" * 40}},
        "commit_matches",
        "commit",
        {"expected_commit_sha": "a" * 40},
    ) == "accept"
    assert verify_evidence(
        {"kind": "screenshot", "payload": {"image_sha256": "b" * 64}},
        "screenshot_matches",
        "screenshot",
        {"expected_image_sha256": "b" * 64},
    ) == "accept"
    assert verify_evidence(
        {"kind": "human_approval", "payload": {"approved_by": "human:alice"}},
        "human_approved",
        "human_approval",
        {"approval_ref": "human:alice"},
    ) == "accept"


def test_find_evidence_chain_reports_found_and_unmet_requirements():
    trajectory = make_trajectory()
    add_evidence(trajectory)

    found, chain_events, unmet = find_evidence_chain(trajectory, evidence_requirements())

    assert found is True
    assert {event["kind"] for event in chain_events} == {
        "tool_result",
        "citation_lookup",
    }
    assert unmet == []

    found, chain_events, unmet = find_evidence_chain(
        trajectory,
        evidence_requirements() + [{"property": "commit_matches", "verifier": "commit", "ref_state": {"expected_commit_sha": "c" * 40}}],
    )
    assert found is False
    assert len(chain_events) == 3
    assert unmet[0]["property"] == "commit_matches"


def test_submission_gate_rejects_evidence_less_done_claim():
    report = evaluate_submission(make_trajectory(), evidence_requirements())

    assert report["accepted"] is False
    assert report["decision"] == "rejected_missing_evidence"
    assert report["evidence_chain"] == []
    assert len(report["unmet_requirements"]) == 3


def test_submission_gate_accepts_complete_evidence_chain():
    trajectory = make_trajectory()
    add_evidence(trajectory)

    report = evaluate_submission(trajectory, evidence_requirements())

    assert report["accepted"] is True
    assert report["decision"] == "accepted_with_evidence"
    assert len(report["evidence_chain"]) == 3
    assert report["unmet_requirements"] == []


def test_no_shell_exec_monitor_requires_prior_human_approval():
    safe = Trajectory()
    safe.append("human_approval", {"approved_by": "human:alice"}, timestamp_ms=1)
    safe.append("shell_exec", {"command": "pytest", "exit_code": 0}, timestamp_ms=2)

    unsafe = Trajectory()
    unsafe.append("shell_exec", {"command": "rm -rf /", "exit_code": 0}, timestamp_ms=1)

    monitor = {"name": "no_shell_exec_without_prior_human_approval"}
    assert ComposedGate(monitors=[monitor]).evaluate(safe)["accepted"] is True
    violation = ComposedGate(monitors=[monitor]).evaluate(unsafe)
    assert violation["accepted"] is False
    assert violation["monitors"][0]["reason"] == "shell_exec_without_prior_human_approval"


def test_file_write_monitor_enforces_allowed_path_reference():
    trajectory = Trajectory()
    trajectory.append("file_write", {"path": "/workspace/project/result.txt"}, timestamp_ms=1)

    gate = ComposedGate(
        monitors=[
            {
                "name": "no_file_write_outside_allowed_paths",
                "ref_state": {"allowed_paths": {"/workspace/project"}},
            }
        ]
    )
    assert gate.evaluate(trajectory)["accepted"] is True

    trajectory.events[0]["payload"]["path"] = "/etc/passwd"
    violation = gate.evaluate(trajectory)
    assert violation["accepted"] is False
    assert violation["monitors"][0]["reason"] == "file_write_outside_allowed_paths"


def test_composed_gate_accepts_when_all_monitors_and_gates_pass():
    trajectory = make_trajectory()
    trajectory.append("human_approval", {"approved_by": "human:alice"}, timestamp_ms=2)
    trajectory.append("shell_exec", {"command": "pytest", "exit_code": 0}, timestamp_ms=3)
    add_evidence(trajectory)

    composed = ComposedGate(
        monitors=[{"name": "no_shell_exec_without_prior_human_approval"}],
        gates=[evidence_requirements()],
    )
    report = composed.evaluate(trajectory)

    assert report["accepted"] is True
    assert report["decision"] == "accepted_with_evidence"
    assert report["unmet_requirements"] == []


def test_composed_gate_rejects_when_a_monitor_is_violated():
    trajectory = make_trajectory()
    trajectory.append("shell_exec", {"command": "pytest", "exit_code": 0}, timestamp_ms=2)
    add_evidence(trajectory)

    composed = ComposedGate(
        monitors=[{"name": "no_shell_exec_without_prior_human_approval"}],
        gates=[evidence_requirements()],
    )
    report = composed.evaluate(trajectory)

    assert report["accepted"] is False
    assert report["decision"] == "rejected_composition"
    assert report["monitors"][0]["held"] is False


def test_composed_gate_rejects_when_one_evidence_gate_is_unmet():
    trajectory = make_trajectory()
    trajectory.append("human_approval", {"approved_by": "human:alice"}, timestamp_ms=2)
    trajectory.append("shell_exec", {"command": "pytest", "exit_code": 0}, timestamp_ms=3)
    add_evidence(trajectory)

    composed = ComposedGate(
        monitors=[{"name": "no_shell_exec_without_prior_human_approval"}],
        gates=[evidence_requirements() + [{"property": "commit_matches", "verifier": "commit", "ref_state": {"expected_commit_sha": "d" * 40}}]],
    )
    report = composed.evaluate(trajectory)

    assert report["accepted"] is False
    assert report["decision"] == "rejected_composition"
    assert report["gates"][0]["unmet_requirements"][0]["property"] == "commit_matches"


def test_trajectory_root_hash_changes_when_an_event_is_appended():
    trajectory = Trajectory()
    before = trajectory_root_hash(trajectory)
    trajectory.append("model_message", {"text": "done"}, timestamp_ms=1)
    after = trajectory_root_hash(trajectory)

    assert before != after
    assert after == hashlib.sha256(trajectory.head_hash.encode("ascii")).hexdigest()


def test_runtime_contract_demo_runs_end_to_end():
    completed = subprocess.run(
        [sys.executable, str(ROOT / "examples" / "runtime_contract_demo.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Evidence-less done claim" in completed.stdout
    assert "Complete evidence chain" in completed.stdout
    assert '"accepted": false' in completed.stdout
    assert '"accepted": true' in completed.stdout
