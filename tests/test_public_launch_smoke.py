from __future__ import annotations

from unittest.mock import patch

from tools.public_launch_smoke import SmokeFailure, _expect_text, main


# --- _expect_text contract (pre-existing) ---

def test_pricing_contract_accepts_canonical_public_terms():
    _expect_text(
        "Free up to 10 seats · optional 5% donation · Team $20/seat/month · Enterprise",
        "/pricing", "Free", "10", "5%", "Team", "$20", "Enterprise",
    )


def test_pricing_contract_reports_missing_terms():
    try:
        _expect_text("Free and Team", "/pricing", "Enterprise")
    except RuntimeError as exc:
        assert "/pricing: missing contract terms: Enterprise" in str(exc)
    else:
        raise AssertionError("missing pricing term was not rejected")


# --- main() fail-closed behavior (#230) ---

def test_main_fails_closed_without_base_url(capsys, monkeypatch):
    """A scheduled smoke must not exit 0 while configured to smoke nothing."""
    monkeypatch.delenv("LEDGER_SMOKE_BASE_URL", raising=False)
    monkeypatch.delenv("LEDGER_SMOKE_ADMIN_TOKEN", raising=False)
    assert main() == 1
    out = capsys.readouterr().out
    assert "LEDGER_SMOKE_BASE_URL" in out
    assert "RESULT=FAIL" in out


def test_main_fails_closed_without_admin_token(capsys, monkeypatch):
    """The authenticated contracts are part of the smoke; a missing token is
    a configuration failure, not a reason to skip half the contract."""
    monkeypatch.setenv("LEDGER_SMOKE_BASE_URL", "https://example.invalid")
    monkeypatch.delenv("LEDGER_SMOKE_ADMIN_TOKEN", raising=False)
    assert main() == 1
    out = capsys.readouterr().out
    assert "LEDGER_SMOKE_ADMIN_TOKEN" in out
    assert "RESULT=FAIL" in out


def test_main_transport_failure_fails(capsys, monkeypatch):
    monkeypatch.setenv("LEDGER_SMOKE_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("LEDGER_SMOKE_ADMIN_TOKEN", "t")
    with patch(
        "tools.public_launch_smoke._health_contract",
        side_effect=SmokeFailure("GET /healthz: transport error (TimeoutError)"),
    ):
        assert main() == 1
    out = capsys.readouterr().out
    assert "transport error" in out
    assert "RESULT=FAIL" in out


def test_main_bad_status_fails(capsys, monkeypatch):
    monkeypatch.setenv("LEDGER_SMOKE_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("LEDGER_SMOKE_ADMIN_TOKEN", "t")
    with patch("tools.public_launch_smoke._health_contract"), \
         patch(
            "tools.public_launch_smoke._pricing_contract",
            side_effect=SmokeFailure("/pricing: HTTP 500, expected 200"),
         ):
        assert main() == 1
    out = capsys.readouterr().out
    assert "HTTP 500" in out
    assert "RESULT=FAIL" in out


def test_main_success_prints_pass(capsys, monkeypatch):
    monkeypatch.setenv("LEDGER_SMOKE_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("LEDGER_SMOKE_ADMIN_TOKEN", "t")
    with patch("tools.public_launch_smoke._health_contract"), \
         patch("tools.public_launch_smoke._pricing_contract"), \
         patch("tools.public_launch_smoke._authenticated_contract"):
        assert main() == 0
    out = capsys.readouterr().out
    assert "RESULT=PASS" in out
