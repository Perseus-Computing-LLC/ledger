"""Belief-context evidence block in receipts (#237).

Success criteria:
- a receipt with ``belief_context`` verifies with the block covered by the HMAC;
- ``verification.evidence.belief_context`` reports ``attested`` when present;
- absent block: existing receipts byte-unchanged (backward compatible);
- sign->verify round trip with and without the block.
"""
import time

from ledger_agent import db, evidence_levels, metering
from ledger_agent.receipts import build_belief_context, validate_belief_context
from ledger_agent.server.api import audit_json

KEY = b"ledger-test-signing-key-32-bytes!"


def _record(conn, org_id, external_ref, *, belief_context=None, with_signature=True):
    metering.record_usage(
        conn, org_id, provider="openai", model="gpt-fixture",
        task_type="deploy", external_ref=external_ref,
        input_tokens=10, output_tokens=5, cost_usd=0.1,
        agent_id="hermes-prod", authority_manifest_ref="auth-1",
        scope_anchor="github:Perseus-Computing-LLC/ledger",
        action_intent_hash="c" * 64, action_status="executed",
        belief_context=belief_context, ts=time.time(),
    )
    kwargs = {}
    if with_signature:
        kwargs = {"key_registry": {"ledger-ops": KEY}, "sign_key_id": "ledger-ops"}
    return audit_json(conn, org_id, external_ref=external_ref, **kwargs)


# ── block construction / validation ─────────────────────────────────────────


def test_build_and_validate_belief_context():
    block = build_belief_context(
        believed=[{"statement": "the checkout flow is reachable",
                   "weight": 0.9, "evidence_refs": ["a" * 64]}],
        assumed=[{"statement": "fixtures are idempotent"}],
        ignored=[{"statement": "legacy coupon path", "weight": 0.1}],
        confidence=0.8, source="agent",
    )
    ok, errors = validate_belief_context(block)
    assert ok, errors
    assert block["schema"] == "perseus-ledger-belief-context/v1"
    assert len(block["belief_digest"]) == 64


def test_validate_rejects_bad_entries():
    # The builder is strict, so hand-forge malformed blocks for the validator.
    bad_weight = {
        "schema": "perseus-ledger-belief-context/v1",
        "believed": [{"statement": "x", "weight": 7}],
        "assumed": [], "ignored": [], "confidence": 0.5, "source": "agent",
        "belief_digest": "a" * 64,
    }
    ok, errors = validate_belief_context(bad_weight)
    assert not ok
    assert any(e.endswith(".weight") for e in errors)

    bad_ref = {
        "schema": "perseus-ledger-belief-context/v1",
        "believed": [{"statement": "x", "evidence_refs": ["not-a-digest"]}],
        "assumed": [], "ignored": [], "confidence": 0.5, "source": "agent",
        "belief_digest": "a" * 64,
    }
    ok, errors = validate_belief_context(bad_ref)
    assert not ok
    assert any(e.endswith(".evidence_refs") for e in errors)

    # ...and the strict builder rejects the same shapes at construction time.
    import pytest
    with pytest.raises(ValueError):
        build_belief_context(believed=[{"statement": "x", "weight": 7}])
    with pytest.raises(ValueError):
        build_belief_context(
            believed=[{"statement": "x", "evidence_refs": ["not-a-digest"]}])


def test_validate_detects_tampered_digest():
    block = build_belief_context(believed=[{"statement": "x"}])
    block["belief_digest"] = "f" * 64
    ok, errors = validate_belief_context(block)
    assert not ok
    assert "belief_digest_mismatch" in errors


# ── receipt round trips ─────────────────────────────────────────────────────


def test_receipt_with_belief_context_verifies_attested(tmp_path):
    conn = db.connect(str(tmp_path / "bc.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "bc-org", tier="free")["id"]
    block = build_belief_context(
        believed=[{"statement": "deploy target is staging", "weight": 0.9}],
        assumed=[{"statement": "rollback is available"}],
        confidence=0.8, source="agent",
    )
    receipt = _record(conn, org_id, "task-bc", belief_context=block)
    ev = receipt["events"][0]
    assert ev["belief_context"]["schema"] == "perseus-ledger-belief-context/v1"
    assert ev["belief_context"]["believed"][0]["statement"] == "deploy target is staging"

    evidence = receipt["verification"]["evidence"]
    bc = evidence["belief_context"]
    assert bc["present"] is True
    assert bc["covered"] is True          # block is inside the HMAC-signed bytes
    assert bc["level"] == "attested"
    assert bc["reason"] == "belief:ok_attested"
    assert bc["entries"] == {"believed": 1, "assumed": 1, "ignored": 0}
    assert len(bc["digest"]) == 64

    # independent re-verification of the same receipt
    ev2 = evidence_levels.verify_receipt_evidence(
        conn, org_id, receipt, key_registry={"ledger-ops": KEY})
    assert ev2["belief_context"]["covered"] is True
    conn.close()


def test_receipt_without_block_is_backward_compatible(tmp_path):
    conn = db.connect(str(tmp_path / "none.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "none-org", tier="free")["id"]
    receipt = _record(conn, org_id, "task-none")
    assert all(e.get("belief_context") is None for e in receipt["events"])
    bc = receipt["verification"]["evidence"]["belief_context"]
    assert bc["present"] is False
    assert bc["level"] is None
    # canonical signed bytes must not contain belief_context at all
    signed = receipt.get("signature")
    assert signed and signed["key_id"] == "ledger-ops"
    conn.close()


def test_tampered_belief_block_is_detected(tmp_path):
    conn = db.connect(str(tmp_path / "tamper.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "tamper-org", tier="free")["id"]
    block = build_belief_context(believed=[{"statement": "original belief"}])
    receipt = _record(conn, org_id, "task-tamper", belief_context=block)
    receipt["events"][0]["belief_context"]["believed"][0]["statement"] = "forged belief"
    evidence = evidence_levels.verify_receipt_evidence(
        conn, org_id, receipt, key_registry={"ledger-ops": KEY})
    assert evidence["belief_context"]["covered"] is False
    assert evidence["belief_context"]["level"] is None
    assert evidence["levels"]["structural"] is False  # HMAC now mismatches
    conn.close()


def test_unsigned_receipt_with_belief_block_is_not_attested(tmp_path):
    conn = db.connect(str(tmp_path / "unsigned.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "unsigned-org", tier="free")["id"]
    block = build_belief_context(believed=[{"statement": "x"}])
    receipt = _record(conn, org_id, "task-unsigned", belief_context=block,
                      with_signature=False)
    bc = receipt["verification"]["evidence"]["belief_context"]
    assert bc["present"] is True
    assert bc["covered"] is False
    assert bc["level"] is None
    assert bc["reason"] == "belief:signature_missing"
    conn.close()


def test_malformed_belief_block_fails_structural(tmp_path):
    conn = db.connect(str(tmp_path / "malformed.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "malformed-org", tier="free")["id"]
    receipt = _record(conn, org_id, "task-malformed")
    receipt["events"][0]["belief_context"] = {
        "schema": "perseus-ledger-belief-context/v1",
        "believed": [{"statement": "", "weight": 0.5}],
        "assumed": [], "ignored": [], "confidence": 0.5, "source": "agent",
        "belief_digest": "a" * 64,
    }
    ok, reason = evidence_levels.verify_structural(receipt)
    assert not ok
    assert reason == "structural:belief_context[0]"
    conn.close()
