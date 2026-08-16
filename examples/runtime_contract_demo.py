"""Minimal runtime-contract evidence-gating demonstration for Ledger #250."""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Keep the example runnable directly from a clean checkout, before a wheel is
# installed into the selected interpreter.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ledger_agent.trajectory import Trajectory, evaluate_submission


REQUIREMENTS = [
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
        "ref_state": {"source_urls": ["https://arxiv.org/abs/2608.11274"]},
    },
]


def report(title: str, value: dict) -> None:
    print(title)
    print(json.dumps(value, indent=2, sort_keys=True))


def main() -> None:
    trajectory = Trajectory()
    trajectory.append("model_message", {"text": "done"}, timestamp_ms=1)
    report("Evidence-less done claim", evaluate_submission(trajectory, REQUIREMENTS))

    trajectory.append(
        "tool_result",
        {"command": "pytest tests/ -q", "exit_code": 0},
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
    report("Complete evidence chain", evaluate_submission(trajectory, REQUIREMENTS))


if __name__ == "__main__":
    main()
