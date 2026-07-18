from __future__ import annotations

from tools.public_launch_smoke import _expect_text


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
