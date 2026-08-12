"""Regression tests for the classified `ledger.py --json` CI smoke (#231).

Every nonzero exit used to be absorbed as "expected" by the workflow step.
These tests pin the classification contract: only the recognized
unavailable-state-DB case may pass with a nonzero exit; crashes, bad args,
and malformed output must fail loudly.
"""
from __future__ import annotations

import json
import subprocess
import sys

from tools.json_smoke import classify, run_smoke


def _result(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["ledger.py", "--json"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def test_clean_success_passes():
    payload = json.dumps({"providers": [{"provider": "deepseek"}], "generated_at": 1.0})
    code, message = classify(_result(0, stdout=payload))
    assert code == 0
    assert "PASS" in message


def test_exit_zero_with_invalid_json_fails():
    code, message = classify(_result(0, stdout="not json at all"))
    assert code == 1
    assert "not valid JSON" in message


def test_exit_zero_missing_required_keys_fails():
    code, message = classify(_result(0, stdout=json.dumps({"providers": []})))
    assert code == 1
    assert "generated_at" in message


def test_recognized_state_db_missing_passes():
    code, message = classify(_result(1, stderr="ledger note: (state.db not found)"))
    assert code == 0
    assert "recognized unavailable-state-DB" in message


def test_unrecognized_nonzero_exit_fails_not_masked():
    code, message = classify(_result(1, stderr="Traceback (most recent call last):\nboom"))
    assert code == 1
    assert "unrecognized" in message


def test_unrelated_nonzero_exit_fails():
    code, message = classify(_result(2, stderr="usage: ledger.py [--json] ..."))
    assert code == 1


def test_run_smoke_end_to_end(tmp_path):
    """run_smoke() wiring: a real subprocess with a healthy fake monitor."""
    fake = tmp_path / "ledger.py"
    fake.write_text(
        "import json, sys\n"
        "print(json.dumps({'providers': [], 'generated_at': 1.0}))\n"
    )
    code, message = run_smoke(sys.executable, fake)
    assert code == 0
    assert "PASS" in message


def test_run_smoke_fake_crash_fails_loudly(tmp_path):
    fake = tmp_path / "ledger.py"
    fake.write_text("import sys\nsys.stderr.write('Traceback (most recent call last):\\n')\nsys.exit(1)\n")
    code, message = run_smoke(sys.executable, fake)
    assert code == 1
    assert "FAIL" in message
