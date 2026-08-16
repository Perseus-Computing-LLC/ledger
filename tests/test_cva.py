from __future__ import annotations

import hashlib
import json

from ledger_agent.cva import (
    PROPERTIES,
    CvaGateway,
    build_cva_statement,
    cva_relation_holds,
    is_fresh,
)
from ledger_agent.prebind import (
    build_prebind,
    build_prebind_v2,
    prebind_digest,
    validate_prebind,
)
from ledger_agent.receipts import build_prebind_v2 as build_receipt_prebind_v2


REQUEST = {"action": "deploy", "resource": "prod", "revision": 42}
CONTEXT = {"environment": "prod", "risk": "low"}
KEY = b"agent-a-key-material"


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def policy_for(policy_id="policy/v1", allowed=True):
    def predicate(attrs, request_payload, context_payload):
        return (
            allowed
            and attrs.get("role") == "deployer"
            and request_payload["resource"] == "prod"
            and context_payload["environment"] == "prod"
        )

    predicate.policy_id = policy_id
    return predicate


def make_statement(*, nonce="n1", timestamp_ms=100, policy_id="policy/v1"):
    return build_cva_statement(
        agent_id="agent-a",
        request_hash=digest(REQUEST),
        context_hash=digest(CONTEXT),
        policy_id=policy_id,
        nonce=nonce,
        timestamp_ms=timestamp_ms,
    )


def make_registry(*, agent_id="agent-a", revoked=False):
    return {
        "key-a": {
            "key_material": KEY,
            "custody": "self_held",
            "agent_id": agent_id,
            "revoked": revoked,
        }
    }


def relation(statement, *, request_payload=REQUEST, context_payload=CONTEXT,
             registry=None, policy=None, principal_key_id="key-a"):
    return cva_relation_holds(
        statement,
        principal_key_id=principal_key_id,
        key_registry=registry or make_registry(),
        request_payload=request_payload,
        context_payload=context_payload,
        attrs={"role": "deployer"},
        policy=policy or policy_for(),
    )


def gateway_accept(gateway, statement, *, request_payload=REQUEST,
                   context_payload=CONTEXT, registry=None, policy=None,
                   consumed=None, principal_key_id="key-a", t_min=100, t_max=200):
    return gateway.accept(
        statement,
        principal_key_id=principal_key_id,
        key_registry=registry or make_registry(),
        request_payload=request_payload,
        context_payload=context_payload,
        attrs={"role": "deployer"},
        policy=policy or policy_for(),
        consumed=consumed,
        t_min=t_min,
        t_max=t_max,
    )


def test_cva_relation_accepts_bound_request_context_principal_and_policy():
    valid, errors = relation(make_statement())
    assert valid is True
    assert errors == []


def test_authorization_soundness_rejects_tampered_request_payload():
    valid, errors = relation(make_statement(), request_payload={**REQUEST, "revision": 43})
    assert valid is False
    assert errors == ["bind_request"]


def test_principal_binding_rejects_key_bound_to_another_agent():
    valid, errors = relation(make_statement(), registry=make_registry(agent_id="agent-b"))
    assert valid is False
    assert errors == ["bind_principal"]


def test_request_binding_rejects_different_request_with_same_context():
    valid, errors = relation(make_statement(), request_payload={"action": "delete", "resource": "prod"})
    assert valid is False
    assert "bind_request" in errors


def test_policy_binding_rejects_flipped_predicate_and_policy_identifier():
    statement = make_statement()
    valid, errors = relation(statement, policy=policy_for(allowed=False))
    assert valid is False
    assert errors == ["satisfy_policy"]

    valid, errors = relation(statement, policy=policy_for(policy_id="policy/v2"))
    assert valid is False
    assert "satisfy_policy" in errors


def test_context_binding_rejects_changed_context_payload():
    valid, errors = relation(make_statement(), context_payload={"environment": "staging", "risk": "low"})
    assert valid is False
    assert "bind_context" in errors


def test_replay_resistance_consumes_nonce_once():
    gateway = CvaGateway()
    statement = make_statement()
    first = gateway_accept(gateway, statement)
    second = gateway_accept(gateway, statement)
    assert first == {"accepted": True, "reason": "accepted"}
    assert second == {"accepted": False, "reason": "replay"}


def test_timestamp_window_rejects_stale_and_future_statements():
    gateway = CvaGateway()
    stale = gateway_accept(gateway, make_statement(timestamp_ms=99))
    future = gateway_accept(gateway, make_statement(nonce="n2", timestamp_ms=201))
    assert stale == {"accepted": False, "reason": "stale_timestamp"}
    assert future == {"accepted": False, "reason": "future_timestamp"}


def test_failed_relation_does_not_consume_nonce():
    gateway = CvaGateway()
    statement = make_statement()
    rejected = gateway_accept(
        gateway, statement, request_payload={**REQUEST, "revision": 999})
    assert rejected["accepted"] is False
    assert rejected["reason"] == "relation_not_satisfied"
    assert "n1" not in gateway.consumed_nonces

    accepted = gateway_accept(gateway, statement)
    assert accepted == {"accepted": True, "reason": "accepted"}


def test_is_fresh_requires_unused_nonce_and_inclusive_timestamp_window():
    assert is_fresh("n1", 100, set(), 100, 200) is True
    assert is_fresh("n1", 100, {"n1"}, 100, 200) is False
    assert is_fresh("n2", 99, set(), 100, 200) is False
    assert is_fresh("n3", 201, set(), 100, 200) is False


def test_receipt_prebind_carries_request_nonce_epoch_and_hash_covers_them():
    block = build_receipt_prebind_v2(
        attempted_action="deploy",
        actor_ref="agent-a",
        authority_ref="authority:1",
        trusted_scope="repo:ledger",
        policy_version="policy/v1",
        evidence_hashes=[digest("evidence")],
        selected_context_digest=digest("selection"),
        resource_ref="resource:prod",
        boundary_outcome="hold",
        non_effective_result="not_executed",
        replay_id="replay:1",
        context_hash=digest(CONTEXT),
        policy_hash=digest("policy/v1"),
        request_hash=digest(REQUEST),
        nonce="n1",
        epoch="epoch-1",
    )
    assert block["request_hash"] == digest(REQUEST)
    assert block["nonce"] == "n1"
    assert block["epoch"] == "epoch-1"
    assert validate_prebind(block) == (True, [])
    tampered = dict(block, request_hash=digest({"action": "other"}))
    assert prebind_digest(tampered) != block["prebind_hash"]


def test_prebind_validator_accepts_valid_new_fields():
    block = build_prebind_v2(
        attempted_action="deploy",
        actor_ref="agent-a",
        authority_ref="authority:1",
        trusted_scope="repo:ledger",
        policy_version="policy/v1",
        evidence_hashes=[digest("evidence")],
        selected_context_digest=digest("selection"),
        resource_ref="resource:prod",
        boundary_outcome="hold",
        non_effective_result="not_executed",
        replay_id="replay:1",
        request_hash=digest(REQUEST),
        nonce="n1",
        epoch=1,
    )
    assert validate_prebind(block) == (True, [])


def test_prebind_validator_rejects_bad_request_hash_and_nonce():
    block = build_prebind_v2(
        attempted_action="deploy",
        actor_ref="agent-a",
        authority_ref="authority:1",
        trusted_scope="repo:ledger",
        policy_version="policy/v1",
        evidence_hashes=[digest("evidence")],
        selected_context_digest=digest("selection"),
        resource_ref="resource:prod",
        boundary_outcome="hold",
        non_effective_result="not_executed",
        replay_id="replay:1",
        request_hash=digest(REQUEST),
        nonce="n1",
    )
    block["request_hash"] = "not-a-hash"
    block["nonce"] = ""
    block["prebind_hash"] = prebind_digest(block)
    valid, errors = validate_prebind(block)
    assert valid is False
    assert "prebind_request_hash" in errors
    assert "prebind_nonce" in errors


def test_old_style_prebind_without_cva_fields_remains_valid():
    block = build_prebind(
        attempted_action="deploy",
        actor_ref="agent-a",
        authority_ref="authority:1",
        trusted_scope="repo:ledger",
        policy_version="policy/v1",
        evidence_hashes=[digest("evidence")],
        selected_context_digest=digest("selection"),
        resource_ref="resource:prod",
        boundary_outcome="hold",
        non_effective_result="not_executed",
        replay_id="replay:1",
    )
    assert "request_hash" not in block
    assert "nonce" not in block
    assert validate_prebind(block) == (True, [])


def test_cva_statement_schema_hash_and_determinism():
    first = make_statement()
    second = make_statement()
    assert first == second
    assert first["schema"] == "perseus-ledger-cva-statement/v1"
    assert len(first["statement_hash"]) == 64
    changed = make_statement(nonce="n2")
    assert changed["statement_hash"] != first["statement_hash"]


def test_cva_properties_cover_paper_security_matrix():
    names = {entry["name"] for entry in PROPERTIES}
    assert names == {
        "authorization_soundness",
        "principal_binding",
        "request_binding",
        "policy_binding",
        "context_binding",
        "replay_resistance",
    }
    assert all(entry["paper_eq"] for entry in PROPERTIES)
