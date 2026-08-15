"""Custody disclosure labels on key_registry entries and authority
manifests (#241).

Acceptance:
- ``custody`` field on key_registry entries and authority manifests,
  drawn from the 1f916 taxonomy (extensible);
- verification output surfaces custody next to every signature result;
- missing/unknown custody is rendered as labeled uncertainty, never
  silently as the strongest case.
"""
import time

import pytest

from ledger_agent import db, evidence_levels, metering
from ledger_agent.keys import (
    CUSTODY_UNKNOWN,
    custody_for_key,
    custody_label,
    is_known_custody,
    normalize_key_registry,
)
from ledger_agent.server.api import audit_json

KEY = b"ledger-test-signing-key-32-bytes!"


# ── taxonomy ────────────────────────────────────────────────────────────────


def test_custody_taxonomy_is_extensible():
    for tier in ("self_held", "platform_held", "household_held", "kms",
                 "hsm", "session_delegated", "threshold(k,n)"):
        assert is_known_custody(tier), tier
    # parameterized forms
    assert is_known_custody("threshold(2,3)")
    assert is_known_custody("threshold( 2 , 3 )") is False  # strict form
    assert is_known_custody("kms(aws-kms)")
    # unknown tiers are labeled, never silently accepted as known
    assert not is_known_custody("my_cousins_laptop")
    assert not is_known_custody("")
    assert not is_known_custody(None)


def test_custody_label_renders_uncertainty():
    assert custody_label(None) == {"custody": CUSTODY_UNKNOWN, "known": False}
    assert custody_label("self_held") == {"custody": "self_held", "known": True}
    assert custody_label("??") == {"custody": "??", "known": False}


def test_normalize_key_registry_accepts_both_shapes():
    legacy = normalize_key_registry({"k1": KEY})
    assert legacy["k1"]["key_material"] == KEY
    assert legacy["k1"]["custody"] == CUSTODY_UNKNOWN
    assert legacy["k1"]["known"] is False

    labeled = normalize_key_registry(
        {"k1": {"key_material": KEY, "custody": "self_held", "label": "ops"}})
    assert labeled["k1"]["custody"] == "self_held"
    assert labeled["k1"]["known"] is True

    with pytest.raises(ValueError):
        normalize_key_registry({"k1": "not-bytes-not-dict"})

    assert custody_for_key(None, "k1") == {"custody": CUSTODY_UNKNOWN,
                                           "known": False}


# ── custody surfaced next to signature results ──────────────────────────────


def _record(conn, org_id, external_ref, *, custody=None):
    return metering.record_usage(
        conn, org_id, provider="openai", model="gpt-fixture",
        task_type="deploy", external_ref=external_ref,
        input_tokens=10, output_tokens=5, cost_usd=0.1, ts=time.time(),
        agent_id="hermes-prod", authority_manifest_ref="auth-1",
        authority_manifest_custody=custody,
        scope_anchor="github:Perseus-Computing-LLC/ledger",
        action_intent_hash="c" * 64, action_status="executed",
    )


def _signed(conn, org_id, external_ref, registry):
    return audit_json(conn, org_id, external_ref=external_ref,
                      key_registry=registry, sign_key_id="ledger-ops")


def test_labeled_registry_surfaces_custody_next_to_signatures(tmp_path):
    conn = db.connect(str(tmp_path / "custody.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "custody-org", tier="free")["id"]
    _record(conn, org_id, "task-1")
    registry = {"ledger-ops": {"key_material": KEY, "custody": "self_held"}}
    receipt = _signed(conn, org_id, "task-1", registry)
    evidence = receipt["verification"]["evidence"]
    assert evidence["signature_custody"] == {"custody": "self_held",
                                             "known": True}
    assert evidence["attestation_custody"] == {"custody": "self_held",
                                               "known": True}
    # the labeled entry still resolves to signing material
    ok, reason = evidence_levels.verify_receipt_signature(receipt, registry)
    assert ok, reason
    conn.close()


def test_legacy_bytes_registry_renders_labeled_uncertainty(tmp_path):
    conn = db.connect(str(tmp_path / "legacy.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "legacy-org", tier="free")["id"]
    _record(conn, org_id, "task-1")
    receipt = _signed(conn, org_id, "task-1", {"ledger-ops": KEY})
    evidence = receipt["verification"]["evidence"]
    # authenticity verifies; custody is honest labeled uncertainty
    assert evidence["signature_custody"] == {"custody": CUSTODY_UNKNOWN,
                                             "known": False}
    assert evidence["levels"]["structural"] is True
    conn.close()


def test_unknown_custody_tier_is_never_silently_strongest(tmp_path):
    conn = db.connect(str(tmp_path / "unknown.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "unknown-org", tier="free")["id"]
    _record(conn, org_id, "task-1")
    registry = {"ledger-ops": {"key_material": KEY,
                               "custody": "threshold(5,9)"}}
    receipt = _signed(conn, org_id, "task-1", registry)
    evidence = receipt["verification"]["evidence"]
    assert evidence["signature_custody"] == {"custody": "threshold(5,9)",
                                             "known": True}
    # a garbage tier is recorded verbatim but graded as uncertainty
    receipt2 = _signed(conn, org_id, "task-1",
                       {"ledger-ops": {"key_material": KEY,
                                       "custody": "taped_under_desk"}})
    evidence2 = receipt2["verification"]["evidence"]
    assert evidence2["signature_custody"] == {"custody": "taped_under_desk",
                                              "known": False}
    conn.close()


def test_unsigned_receipt_has_no_signature_custody(tmp_path):
    conn = db.connect(str(tmp_path / "unsigned.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "unsigned-org", tier="free")["id"]
    _record(conn, org_id, "task-1")
    receipt = audit_json(conn, org_id, external_ref="task-1")
    evidence = receipt["verification"]["evidence"]
    assert evidence["signature_custody"] is None
    assert evidence["attestation_custody"] is None
    conn.close()


# ── authority manifest custody ──────────────────────────────────────────────


def test_authority_manifest_custody_recorded_and_surfaced(tmp_path):
    conn = db.connect(str(tmp_path / "manifest.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "manifest-org", tier="free")["id"]
    _record(conn, org_id, "task-1", custody="household_held")
    receipt = audit_json(conn, org_id, external_ref="task-1")
    auth = receipt["events"][0]["action_authorization"]
    assert auth["authority_manifest_custody"] == "household_held"
    evidence = receipt["verification"]["evidence"]
    assert evidence["authority_manifest_custody"] == {
        "value": "household_held", "known": True}
    conn.close()


def test_missing_manifest_custody_is_labeled_uncertainty(tmp_path):
    conn = db.connect(str(tmp_path / "no-manifest.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "no-manifest-org", tier="free")["id"]
    _record(conn, org_id, "task-1", custody=None)
    receipt = audit_json(conn, org_id, external_ref="task-1")
    auth = receipt["events"][0]["action_authorization"]
    assert "authority_manifest_custody" not in auth  # absent stays absent
    evidence = receipt["verification"]["evidence"]
    assert evidence["authority_manifest_custody"] == {
        "value": CUSTODY_UNKNOWN, "known": False}
    conn.close()


def test_manifest_custody_is_chain_covered(tmp_path):
    conn = db.connect(str(tmp_path / "chain.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "chain-org", tier="free")["id"]
    _record(conn, org_id, "task-1", custody="self_held")
    row = conn.execute(
        "SELECT authority_manifest_custody, row_hash FROM usage_events "
        "WHERE org_id=?", (org_id,)).fetchone()
    assert row["authority_manifest_custody"] == "self_held"
    assert row["row_hash"] is not None
    # tampering with the custody label breaks the chain
    conn.execute(
        "UPDATE usage_events SET authority_manifest_custody='hsm' WHERE org_id=?",
        (org_id,))
    chain = db.verify_chain(conn, org_id=org_id)
    org_entry = next(o for o in chain["orgs"] if o["org_id"] == org_id)
    assert org_entry["status"] == "broken"
    conn.close()
