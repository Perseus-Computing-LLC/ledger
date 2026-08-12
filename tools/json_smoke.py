#!/usr/bin/env python3
"""Classified CI smoke for `ledger.py --json` (#231).

The old workflow step ran `ledger.py --json || echo "(state.db unavailable —
expected in CI)"`, which converted EVERY nonzero exit into an "expected"
message — a real crash, bad args, or wrong exit code was silently absorbed.

This tool exits 0 only when the monitor either:
  * exits 0 AND prints valid JSON containing the expected top-level keys
    (the genuine success shape: missing state.db/config degrade to a JSON
    payload with `ledger_error` set, so exit 0 + valid JSON IS the smoke
    assertion), or
  * exits nonzero with the specifically recognized unavailable-state-DB
    message on stderr — the only nonzero exit that is documented as
    acceptable.

Every other outcome fails the workflow loudly.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# ledger.ledger_spend()'s documented note when the state database is missing
# (ledger.py:237 — `return out, "(state.db not found)"`).
RECOGNIZED_STATE_DB_MISSING = re.compile(r"state\.db not found", re.IGNORECASE)

REQUIRED_JSON_KEYS = ("providers", "generated_at")


def classify(result: subprocess.CompletedProcess) -> tuple[int, str]:
    """Classify a `ledger.py --json` subprocess result.

    Returns (exit_code, human-readable message). The unavailable-DB case is
    the ONLY nonzero exit tolerated; everything else fails.
    """
    if result.returncode == 0:
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            return 1, (
                "FAIL: ledger.py --json exited 0 but stdout is not valid JSON "
                f"({exc}); stdout head: {result.stdout[:200]!r}"
            )
        missing = [key for key in REQUIRED_JSON_KEYS if key not in payload]
        if missing:
            return 1, (
                "FAIL: ledger.py --json exited 0 but output is missing required "
                f"key(s): {', '.join(missing)}"
            )
        return 0, (
            "PASS: ledger.py --json emitted valid JSON with "
            + "/".join(REQUIRED_JSON_KEYS)
        )
    if RECOGNIZED_STATE_DB_MISSING.search(result.stderr):
        return 0, (
            "PASS: ledger.py --json exited "
            f"{result.returncode} with the recognized unavailable-state-DB message"
        )
    return 1, (
        "FAIL: ledger.py --json exited "
        f"{result.returncode} with unrecognized output; "
        f"stderr: {result.stderr[:300]!r}; stdout: {result.stdout[:200]!r}"
    )


def run_smoke(python: str = sys.executable,
              ledger_path: Path | None = None) -> tuple[int, str]:
    """Run `ledger.py --json` and classify the result."""
    ledger_path = ledger_path or (REPO_ROOT / "ledger.py")
    try:
        result = subprocess.run(
            [python, str(ledger_path), "--json"],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return 1, "FAIL: ledger.py --json timed out after 60s"
    return classify(result)


def main() -> int:
    code, message = run_smoke()
    print(message)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
