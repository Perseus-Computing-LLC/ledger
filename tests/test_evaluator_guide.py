"""Evaluator-guide facts stay aligned with the Ledger implementation."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_evaluator_guide_describes_the_stdlib_ledger_server():
    guide = (ROOT / "docs" / "EVALUATOR_GUIDE.md").read_text(encoding="utf-8")
    app = (ROOT / "ledger_agent" / "server" / "app.py").read_text(encoding="utf-8")

    assert "stdlib `http.server`" in guide
    assert "FastAPI" not in guide
    assert "http.server" in app
