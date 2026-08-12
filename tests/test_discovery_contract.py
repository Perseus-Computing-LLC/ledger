"""Collection-contract test for the root monitor suite (#229).

The engine matrix must run the DEFAULT pytest invocation (`python -m pytest`,
which uses `testpaths` from pyproject.toml) so that the root-level
`test_ledger.py` monitor suite is exercised on every matrix leg. If someone
reverts the CI command to `pytest tests/`, the root monitor silently drops
out of matrix coverage again — this test fails first.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_default_collection_includes_root_monitor_suite():
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, (
        "pytest --collect-only failed; stderr tail:\n" + result.stderr[-2000:]
    )
    assert "test_ledger.py" in result.stdout, (
        "root test_ledger.py is NOT collected by the default pytest "
        "invocation (testpaths from pyproject.toml). The engine matrix must "
        "run `python -m pytest` (not `pytest tests/`) so the root monitor "
        "suite has matrix coverage."
    )
