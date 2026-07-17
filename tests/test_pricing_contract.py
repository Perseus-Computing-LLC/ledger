import pytest

from plutus_agent import pricing


def test_free_contract_is_ten_seats_with_five_percent_recommendation():
    tier = pricing.tier("free")
    assert tier.seats == 10
    assert tier.audit_access is True
    assert tier.donation_bps == 500
    assert pricing.recommended_donation_usd("free", 100.0) == 5.0
    assert pricing.recommended_donation_usd("free", 0.0) == 0.0


def test_team_contract_starts_at_eleven_seats_and_costs_twenty_each():
    tier = pricing.tier("team")
    assert tier.min_seats == 11
    assert tier.per_seat_usd_month == 20.0
    assert pricing.seat_charge_usd("team", 11) == 220.0
    with pytest.raises(ValueError, match="at least 11"):
        pricing.seat_charge_usd("team", 10)


def test_enterprise_uses_ten_percent_verified_savings_share():
    tier = pricing.tier("enterprise")
    assert tier.savings_share == "mandatory"
    assert tier.savings_share_bps == 1000
    assert tier.audit_access is True


def test_free_audit_access_is_independent_of_full_reporting():
    tier = pricing.tier("free")
    assert tier.full_reporting is False
    assert tier.audit_access is True
