"""Issue #260: fail-closed context release/publication receipts."""
from __future__ import annotations

import hashlib

import pytest

from ledger_agent import context_release

NOW = "2026-08-19T12:00:00Z"
LATER = "2026-08-19T13:00:00Z"
SCOPE = "workspace:synthetic/program:alpha"
INTERNAL_DEST = "workspace:synthetic"
EXTERNAL_DEST = "proposal-partner:synthetic"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def make_decision(**overrides):
    values = {
        "decision_id": "release:ctx-1:revision-1",
        "source_ref": "vault:context/ctx-1",
        "source_digest": digest("source-v1"),
        "projection_ref": "vault:projection/ctx-1-safe-v1",
        "projection_digest": digest("projection-v1"),
        "handling_profile": "PUBLIC_SAFE",
        "redaction_receipt_ref": "vault:redaction/ctx-1-v1",
        "redaction_receipt_digest": digest("redaction-v1"),
        "policy_version": "handling-policy/v3",
        "policy_digest": digest("policy-v3"),
        "authority_ref": "authority:operator-1",
        "authority_digest": digest("authority-active-v1"),
        "authority_state": "active",
        "scope_anchor": SCOPE,
        "destination_scope": EXTERNAL_DEST,
        "audience_class": "PROPOSAL_PARTNER",
        "requester": "agent:requester",
        "approver": "operator:approver",
        "capability": "publish.safe_projection",
        "purpose": "proposal_evidence_review",
        "decision_at": NOW,
        "expires_at": "2026-08-19T12:30:00Z",
        "decision_state": "APPROVED_EXTERNAL",
        "classifier_state": "available",
        "redaction_state": "complete",
        "evidence_state": "fresh",
        "revocation_state": "not-revoked",
        "revocation_ref": None,
        "released_artifact_digest": digest("released-v1"),
        "idempotency_key": "release-retry-key-1",
        "publication_revision": 1,
        "previous_decision_hash": None,
        "oscal_evidence_refs": [
            {"ref": "ledger:oscal/assessment-results", "digest": digest("oscal-ar-v1")},
            {"ref": "ledger:oscal/poam", "digest": digest("oscal-poam-v1")},
        ],
    }
    values.update(overrides)
    return context_release.build_context_release_decision(**values)


def admission(decision, **overrides):
    values = {
        "now": NOW,
        "required_scope": SCOPE,
        "required_destination": decision["destination_scope"],
        "external_release": True,
        "expected_projection_digest": decision["projection_digest"],
    }
    values.update(overrides)
    return context_release.evaluate_publication(decision, **values)


def test_decision_is_hash_bound_deterministic_and_schema_closed():
    first = make_decision()
    second = make_decision()

    assert first == second
    assert first["decision_hash"] == context_release.decision_digest(first)
    assert context_release.validate_context_release_decision(first) == (True, [])
    raw = context_release.canonical_json(first)
    assert "prompt" not in raw
    assert "memory_body" not in raw
    assert "provider_payload" not in raw
    assert "credential" not in raw

    tampered = dict(first)
    tampered["projection_digest"] = digest("projection-tampered")
    assert context_release.validate_context_release_decision(tampered)[0] is False


def test_legacy_reader_preserves_hash_validity_and_defaults_new_fields():
    legacy = make_decision()
    legacy.pop("revocation_ref")
    legacy.pop("oscal_evidence_refs")
    legacy["decision_hash"] = context_release.decision_digest(legacy)

    result = context_release.read_context_release_decision(legacy)

    assert result["valid"] is True
    assert result["hash_valid"] is True
    assert set(result["legacy_fields_missing"]) == {"revocation_ref", "oscal_evidence_refs"}
    assert result["decision"]["revocation_ref"] is None
    assert result["decision"]["oscal_evidence_refs"] == []


def test_internal_visibility_cannot_authorize_external_publication():
    internal = make_decision(
        decision_id="release:ctx-1:internal",
        decision_state="APPROVED_INTERNAL",
        destination_scope=INTERNAL_DEST,
        audience_class="INTERNAL_AGENT",
        released_artifact_digest=None,
        idempotency_key="internal-key",
    )

    visible = context_release.evaluate_publication(
        internal,
        now=NOW,
        required_scope=SCOPE,
        required_destination=INTERNAL_DEST,
        external_release=False,
        expected_projection_digest=internal["projection_digest"],
    )
    external = context_release.evaluate_publication(
        internal,
        now=NOW,
        required_scope=SCOPE,
        required_destination=EXTERNAL_DEST,
        external_release=True,
        expected_projection_digest=internal["projection_digest"],
    )

    assert visible["allowed"] is True
    assert external == {
        "allowed": False,
        "state": "DENIED",
        "reason": "external_approval_required",
        "decision_hash": internal["decision_hash"],
    }


def test_approved_external_public_safe_release_requires_outbox_receipt():
    decision = make_decision()
    result = admission(decision)

    assert result["allowed"] is True
    assert result["state"] == "APPROVED_EXTERNAL"
    assert result["publication_status"] == "OUTBOX_PENDING"
    assert result["requires_outbox_receipt"] is True

    receipt = context_release.build_outbox_receipt(
        decision,
        tombstones=[],
        delivered_at="2026-08-19T12:15:00Z",
        transport_receipt_ref="transport:synthetic-1",
        transport_receipt_digest=digest("transport-1"),
    )
    assert receipt == context_release.build_outbox_receipt(
        decision,
        tombstones=[],
        delivered_at="2026-08-19T12:15:00Z",
        transport_receipt_ref="transport:synthetic-1",
        transport_receipt_digest=digest("transport-1"),
    )
    assert receipt["decision_hash"] == decision["decision_hash"]
    assert receipt["released_artifact_digest"] == decision["released_artifact_digest"]
    assert receipt["receipt_hash"] == context_release.outbox_receipt_digest(receipt)
    assert context_release.validate_outbox_receipt(receipt) == (True, [])

    forged = dict(receipt)
    forged["notes"] = "raw prompt content must never enter a receipt"
    forged["receipt_hash"] = context_release.outbox_receipt_digest(forged)
    valid, errors = context_release.validate_outbox_receipt(forged)
    assert valid is False
    assert "unknown:notes" in errors


def test_blocked_handling_profile_and_unknown_classifier_fail_closed():
    cui = make_decision(
        decision_id="release:cui-blocked",
        handling_profile="CUI_LIKE",
        idempotency_key="cui-key",
    )
    unavailable = make_decision(
        decision_id="release:classifier-unavailable",
        classifier_state="unavailable",
        idempotency_key="classifier-key",
    )

    cui_result = admission(cui)
    classifier_result = admission(unavailable)

    assert cui_result["allowed"] is False
    assert cui_result["reason"] == "handling_profile_not_exportable"
    assert classifier_result["allowed"] is False
    assert classifier_result["reason"] == "classifier_unavailable"


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("evidence_state", "missing", "evidence_not_fresh"),
        ("redaction_state", "incomplete", "redaction_incomplete"),
        ("authority_state", "revoked", "authority_revoked"),
    ],
)
def test_missing_or_invalid_evidence_authority_and_redaction_fail_closed(field, value, reason):
    decision = make_decision(**{field: value}, idempotency_key=f"{field}-key")
    result = admission(decision)

    assert result["allowed"] is False
    assert result["reason"] == reason


def test_scope_expiry_requester_separation_and_tamper_are_distinct_blocks():
    scope = admission(make_decision(idempotency_key="scope-key"), required_scope="workspace:wrong")
    expired = admission(
        make_decision(idempotency_key="expired-key", expires_at="2026-08-19T12:00:00Z"),
    )
    same_actor = admission(
        make_decision(idempotency_key="same-actor-key", approver="agent:requester"),
    )
    tampered = dict(make_decision(idempotency_key="tampered-key"))
    tampered["released_artifact_digest"] = digest("forged-artifact")
    tampered_result = admission(tampered)

    assert scope["reason"] == "scope_mismatch"
    assert expired["state"] == "EXPIRED"
    assert expired["reason"] == "approval_expired"
    assert same_actor["reason"] == "requester_approver_not_separate"
    assert tampered_result["state"] == "TAMPERED"
    assert tampered_result["reason"] == "decision_hash_invalid"


def test_tombstone_blocks_resurrection_but_allows_a_new_projection():
    decision = make_decision()
    tombstone = context_release.build_publication_tombstone(
        decision,
        reason="REVOKED",
        tombstone_at=LATER,
        revocation_ref="authority:revocation-1",
    )
    blocked = admission(decision, now=LATER, tombstones=[tombstone])
    new_projection = make_decision(
        decision_id="release:ctx-1:revision-2",
        projection_ref="vault:projection/ctx-1-safe-v2",
        projection_digest=digest("projection-v2"),
        released_artifact_digest=digest("released-v2"),
        publication_revision=2,
        previous_decision_hash=decision["decision_hash"],
        expires_at="2026-08-19T13:30:00Z",
        idempotency_key="release-retry-key-2",
    )
    allowed = admission(new_projection, now=LATER)

    assert tombstone["content_free"] is True
    assert tombstone["resurrection_blocked"] is True
    assert blocked["allowed"] is False
    assert blocked["reason"] == "publication_tombstoned"
    assert allowed["allowed"] is True
    assert context_release.validate_tombstone(tombstone) == (True, [])

    forged = dict(tombstone)
    forged["notes"] = "raw prompt content must never enter a tombstone"
    forged["tombstone_hash"] = context_release.tombstone_digest(forged)
    valid, errors = context_release.validate_tombstone(forged)
    assert valid is False
    assert "unknown:notes" in errors


def test_retry_is_idempotent_and_changed_payload_cannot_reuse_key():
    decision = make_decision()
    same = context_release.check_idempotent_retry(decision, [decision])
    changed = make_decision(
        decision_id="release:ctx-1:changed",
        released_artifact_digest=digest("different-artifact"),
    )
    conflict = context_release.check_idempotent_retry(changed, [decision])

    assert same == {"allowed": True, "reason": "idempotent_retry"}
    assert conflict == {"allowed": False, "reason": "idempotency_key_conflict"}


def test_publication_order_requires_contiguous_revision_and_lineage():
    first = make_decision()
    second = make_decision(
        decision_id="release:ctx-1:revision-2",
        projection_ref="vault:projection/ctx-1-safe-v2",
        projection_digest=digest("projection-v2"),
        released_artifact_digest=digest("released-v2"),
        publication_revision=2,
        previous_decision_hash=first["decision_hash"],
        idempotency_key="release-retry-key-2",
    )
    gap = make_decision(
        decision_id="release:ctx-1:revision-3",
        projection_ref="vault:projection/ctx-1-safe-v3",
        projection_digest=digest("projection-v3"),
        released_artifact_digest=digest("released-v3"),
        publication_revision=4,
        previous_decision_hash=first["decision_hash"],
        idempotency_key="release-retry-key-4",
    )
    wrong_parent = make_decision(
        decision_id="release:ctx-1:revision-2-wrong-parent",
        projection_ref="vault:projection/ctx-1-safe-v2b",
        projection_digest=digest("projection-v2b"),
        released_artifact_digest=digest("released-v2b"),
        publication_revision=2,
        previous_decision_hash=digest("wrong-parent"),
        idempotency_key="release-retry-key-2b",
    )

    assert context_release.check_publication_order(first, []) == {"allowed": True, "reason": "first_revision"}
    assert context_release.check_publication_order(second, [first]) == {"allowed": True, "reason": "next_revision"}
    assert context_release.check_publication_order(gap, [first]) == {"allowed": False, "reason": "revision_gap"}
    assert context_release.check_publication_order(wrong_parent, [first]) == {"allowed": False, "reason": "lineage_mismatch"}


def test_admission_compares_timezone_offsets_by_instant_not_lexical_order():
    decision = make_decision(
        decision_at="2026-08-19T13:00:00+01:00",
        expires_at="2026-08-19T13:30:00+01:00",
        idempotency_key="offset-time-key",
    )

    decision["decision_at"] = "2026-08-19T13:00:00+01:00"
    decision["expires_at"] = "2026-08-19T13:30:00+01:00"
    decision["decision_hash"] = context_release.decision_digest(decision)

    result = admission(decision, now=NOW)

    assert result["allowed"] is True


def test_raw_sensitive_fields_are_rejected_at_the_builder_boundary():
    with pytest.raises(ValueError, match="forbidden"):
        context_release.build_context_release_decision(
            **{
                **make_decision(),
                "raw_payload": "must not be persisted",
            }
        )

    with pytest.raises(ValueError, match="purpose"):
        purpose_values = make_decision(idempotency_key="free-text-key")
        purpose_values.pop("schema")
        context_release.build_context_release_decision(
            **{
                **purpose_values,
                "purpose": "raw prompt content",
            }
        )


def test_decision_revocation_ref_and_evidence_reference_count_are_bounded():
    decision = make_decision()
    forged = dict(decision)
    forged["revocation_ref"] = {"raw": "private revocation detail"}
    forged["decision_hash"] = context_release.decision_digest(forged)
    valid, errors = context_release.validate_context_release_decision(forged)
    assert valid is False
    assert "revocation_ref" in errors

    refs = [
        {"ref": f"ledger:oscal/{index}", "digest": digest(f"oscal-{index}")}
        for index in range(33)
    ]
    with pytest.raises(ValueError, match="oscal_evidence_refs"):
        make_decision(idempotency_key="too-many-refs", oscal_evidence_refs=refs)


@pytest.mark.parametrize("external_release", [0, 1, "false", None])
def test_admission_rejects_non_boolean_external_release_flags(external_release):
    result = context_release.evaluate_publication(
        make_decision(),
        now=NOW,
        required_scope=SCOPE,
        required_destination=EXTERNAL_DEST,
        external_release=external_release,
        expected_projection_digest=digest("projection-v1"),
    )
    assert result["allowed"] is False
    assert result["reason"] == "invalid_external_release_flag"


def test_outbox_builder_rechecks_external_admission_before_creating_receipt():
    revoked = make_decision(
        authority_state="revoked",
        idempotency_key="revoked-outbox-key",
    )
    with pytest.raises(ValueError, match="authority_revoked"):
        context_release.build_outbox_receipt(
            revoked,
            tombstones=[],
            delivered_at=LATER,
            transport_receipt_ref="transport:revoked",
            transport_receipt_digest=digest("transport-revoked"),
        )


def test_outbox_builder_requires_tombstone_snapshot_and_blocks_tombstoned_decision():
    decision = make_decision(idempotency_key="tombstone-outbox-key")
    tombstone = context_release.build_publication_tombstone(
        decision,
        reason="REVOKED",
        tombstone_at=NOW,
        revocation_ref="authority:revocation-outbox",
    )
    with pytest.raises(ValueError, match="tombstone snapshot"):
        context_release.build_outbox_receipt(
            decision,
            delivered_at="2026-08-19T12:15:00Z",
            transport_receipt_ref="transport:missing-snapshot",
            transport_receipt_digest=digest("transport-missing-snapshot"),
        )
    with pytest.raises(ValueError, match="publication_tombstoned"):
        context_release.build_outbox_receipt(
            decision,
            tombstones=[tombstone],
            delivered_at="2026-08-19T12:15:00Z",
            transport_receipt_ref="transport:tombstoned",
            transport_receipt_digest=digest("transport-tombstoned"),
        )


def test_evidence_reference_generator_is_bounded_before_materialization():
    consumed = []

    def references():
        for index in range(1000):
            consumed.append(index)
            yield {"ref": f"ledger:oscal/{index}", "digest": digest(f"oscal-{index}")}

    with pytest.raises(ValueError, match="oscal_evidence_refs"):
        make_decision(idempotency_key="generator-bound-key", oscal_evidence_refs=references())
    assert len(consumed) <= 33


def test_history_iterables_are_bounded_fail_closed():
    decision = make_decision(idempotency_key="history-bound-key")
    prior = (make_decision(idempotency_key=f"prior-{index}") for index in range(257))
    assert context_release.check_idempotent_retry(decision, prior) == {
        "allowed": False,
        "reason": "prior_history_too_large",
    }
    tombstones = (context_release.build_publication_tombstone(
        decision,
        reason="REVOKED",
        tombstone_at=NOW,
        revocation_ref=f"authority:revocation-{index}",
    ) for index in range(257))
    result = admission(decision, tombstones=tombstones)
    assert result["allowed"] is False
    assert result["reason"] == "tombstone_history_too_large"
