from __future__ import annotations

import json
import math
import threading

import pytest

from ledger_agent import db, metering
from ledger_agent.composition import (
    ACTION_PROFILE_SCHEMA,
    ACTION_TAXONOMY_SCHEMA,
    COMPOSITION_POLICY_SCHEMA,
    COMPOSITION_VERDICT_SCHEMA,
    CompositionEngine,
    CompositionPolicy,
    CompositionError,
    ActionProfile,
    TrustedActionRegistry,
    composition_binding,
    validate_verdict,
)
from ledger_agent.server.api import audit_json


AUTHORITY_KEY = b"composition-test-authority"
CONTEXT_HASH = "c" * 64


def _registry(*, with_aliases: bool = False) -> TrustedActionRegistry:
    profiles = [
        ActionProfile(
            tool_endpoint="data.read",
            action_class="read",
            resource="dataset",
            data_classification="confidential",
            impact="low",
            budget_cost=1,
            allowed_arguments=("resource_id",),
            required_arguments=("resource_id",),
            resource_argument="resource_id",
        ),
        ActionProfile(
            tool_endpoint="data.write",
            action_class="write",
            resource="dataset",
            data_classification="confidential",
            impact="medium",
            budget_cost=1,
            allowed_arguments=("resource_id", "operation"),
            required_arguments=("resource_id", "operation"),
            resource_argument="resource_id",
        ),
        ActionProfile(
            tool_endpoint="net.send",
            action_class="external_send",
            resource="external-network",
            data_classification="public",
            impact="high",
            budget_cost=2,
            allowed_arguments=("destination",),
            required_arguments=("destination",),
            resource_argument="destination",
        ),
        ActionProfile(
            tool_endpoint="data.delete",
            action_class="delete",
            resource="dataset",
            data_classification="restricted",
            impact="critical",
            budget_cost=3,
            allowed_arguments=("resource_id",),
            required_arguments=("resource_id",),
            resource_argument="resource_id",
        ),
    ]
    aliases = {"read": ("data.read",)} if with_aliases else None
    return TrustedActionRegistry(profiles, version="taxonomy/v1", aliases=aliases)


def _policy(*, pairs=(), sequences=(), budget_limit=20, scope="task"):
    return CompositionPolicy(
        version="policy/v1",
        prohibited_pairs=pairs,
        prohibited_sequences=sequences,
        budget_limit=budget_limit,
        scope=scope,
    )


def _engine(*, pairs=(), sequences=(), budget_limit=20, scope="task", aliases=False, key=AUTHORITY_KEY):
    return CompositionEngine(
        _registry(with_aliases=aliases),
        _policy(pairs=pairs, sequences=sequences, budget_limit=budget_limit, scope=scope),
        authority_key=key,
    )


def _setup(tmp_path, engine, *, lineage="task-1", session="session-1", workspace="ws-a"):
    conn = db.connect(str(tmp_path / "composition.db"))
    db.init_schema(conn)
    org_id = db.create_org(conn, "composition-org", tier="pro")["id"]
    authorization = engine.issue_lineage_authorization(
        org_id=org_id,
        task_lineage_id=lineage,
        session_id=session,
        workspace_scope=workspace,
        authority_action_id="aar-root-1",
        authority_ref="authority/v1",
        context_head_digest=CONTEXT_HASH,
    )
    state = engine.start_lineage(
        conn,
        org_id=org_id,
        task_lineage_id=lineage,
        session_id=session,
        workspace_scope=workspace,
        authority_action_id="aar-root-1",
        authority_ref="authority/v1",
        context_head_digest=CONTEXT_HASH,
        authorization=authorization,
    )
    return conn, org_id, state


def _admit(engine, conn, org_id, *, action_id, tool, args, session="session-1",
           lineage="task-1", workspace="ws-a", authority_action_id=None,
           override=None, **extra):
    return engine.admit(
        conn,
        org_id=org_id,
        task_lineage_id=lineage,
        session_id=session,
        workspace_scope=workspace,
        authority_action_id=authority_action_id or "aar-root-1",
        authority_ref="authority/v1",
        context_head_digest=CONTEXT_HASH,
        action_id=action_id,
        tool_endpoint=tool,
        arguments=args,
        override=override,
        **extra,
    )


def test_versioned_profiles_and_policy_are_canonical_and_hash_bound():
    registry = _registry()
    policy = _policy(pairs=(("external_send", "read"),))

    assert registry.schema == ACTION_TAXONOMY_SCHEMA
    assert policy.schema == COMPOSITION_POLICY_SCHEMA
    assert registry.taxonomy_hash == registry.taxonomy_hash
    assert policy.policy_hash == policy.policy_hash
    assert policy.prohibited_pairs == (("external_send", "read"),)
    assert registry.resolve("data.read", {"resource_id": "alpha"}).resource == "dataset:alpha"
    assert registry.resolve("data.read", {"resource_id": "alpha"}).profile_digest

    # Argument values are committed only as a digest; they never appear in the
    # profile projection or any policy serialization.
    resolved = registry.resolve("data.read", {"resource_id": "secret-record"})
    serialized = json.dumps(resolved.to_dict(), sort_keys=True)
    assert "secret-record" not in serialized
    assert resolved.arguments_hash


def test_read_external_send_pair_is_denied_and_permitted_action_is_admitted(tmp_path):
    engine = _engine(pairs=(("read", "external_send"),))
    conn, org_id, _ = _setup(tmp_path, engine)
    first = _admit(engine, conn, org_id, action_id="read-1", tool="data.read",
                   args={"resource_id": "alpha"})
    denied = _admit(engine, conn, org_id, action_id="send-1", tool="net.send",
                    args={"destination": "https://example.test"})

    assert first["outcome"] == "allow"
    assert denied["outcome"] == "deny"
    assert denied["reason_code"] == "prohibited_pair"
    assert denied["prior_action_classes"] == ["read"]

    permitted = _engine(pairs=(("read", "external_send"),))
    c2, org2, _ = _setup(tmp_path / "permitted", permitted, lineage="task-2")
    result = _admit(permitted, c2, org2, action_id="delete-1", tool="data.delete",
                    args={"resource_id": "alpha"}, lineage="task-2")
    assert result["outcome"] == "allow"
    conn.close()
    c2.close()


def test_ordered_tuple_denies_only_when_sequence_is_completed(tmp_path):
    engine = _engine(sequences=(("read", "write", "external_send"),))
    conn, org_id, _ = _setup(tmp_path, engine)
    read = _admit(engine, conn, org_id, action_id="read-1", tool="data.read",
                  args={"resource_id": "alpha"})
    write = _admit(engine, conn, org_id, action_id="write-1", tool="data.write",
                   args={"resource_id": "alpha", "operation": "append"})
    send = _admit(engine, conn, org_id, action_id="send-1", tool="net.send",
                  args={"destination": "https://example.test"})

    assert read["outcome"] == write["outcome"] == "allow"
    assert send["outcome"] == "deny"
    assert send["reason_code"] == "prohibited_sequence"
    assert send["matched_sequence"] == ["read", "write", "external_send"]
    conn.close()


def test_ordered_tuple_matches_as_an_ordered_subsequence_with_interleaved_noise(tmp_path):
    engine = _engine(sequences=(("read", "write", "external_send"),))
    conn, org_id, _ = _setup(tmp_path, engine, lineage="task-noise")
    assert _admit(engine, conn, org_id, action_id="read-1", tool="data.read",
                  args={"resource_id": "alpha"}, lineage="task-noise")["outcome"] == "allow"
    assert _admit(engine, conn, org_id, action_id="write-1", tool="data.write",
                  args={"resource_id": "alpha", "operation": "append"},
                  lineage="task-noise")["outcome"] == "allow"
    assert _admit(engine, conn, org_id, action_id="noise-1", tool="data.delete",
                  args={"resource_id": "alpha"}, lineage="task-noise")["outcome"] == "allow"
    denied = _admit(engine, conn, org_id, action_id="send-1", tool="net.send",
                    args={"destination": "https://example.test"}, lineage="task-noise")
    assert denied["outcome"] == "deny"
    assert denied["reason_code"] == "prohibited_sequence"
    conn.close()


def test_delegated_hop_uses_durable_lineage_history_not_a_fresh_engine_object(tmp_path):
    engine = _engine(pairs=(("read", "external_send"),), budget_limit=5)
    conn, org_id, _ = _setup(tmp_path, engine)
    assert _admit(engine, conn, org_id, action_id="read-1", tool="data.read",
                  args={"resource_id": "alpha"})["outcome"] == "allow"

    # A new caller-side engine object cannot reset the durable state. Task scope
    # permits a delegated session hop, but the same lineage still sees `read`.
    delegated = _engine(pairs=(("read", "external_send"),), budget_limit=5)
    denied = _admit(delegated, conn, org_id, action_id="send-1", tool="net.send",
                    args={"destination": "https://example.test"}, session="delegate-2")
    assert denied["outcome"] == "deny"
    assert denied["reason_code"] == "prohibited_pair"

    # Per-session policy is explicit and does not silently become task-scoped.
    session_engine = _engine(pairs=(("read", "external_send"),), scope="session")
    sconn, sorg, _ = _setup(tmp_path / "session", session_engine, lineage="session-task")
    assert _admit(session_engine, sconn, sorg, action_id="read-1", tool="data.read",
                  args={"resource_id": "alpha"}, lineage="session-task")["outcome"] == "allow"
    session_mismatch = _admit(session_engine, sconn, sorg, action_id="send-1", tool="net.send",
                              args={"destination": "https://example.test"},
                              session="delegate-2", lineage="session-task")
    assert session_mismatch["outcome"] == "hold"
    assert session_mismatch["reason_code"] == "session_binding_mismatch"
    conn.close()
    sconn.close()


def test_unknown_alias_ambiguous_and_malformed_inputs_fail_closed_or_review(tmp_path):
    engine = _engine(aliases=True)
    conn, org_id, _ = _setup(tmp_path, engine)
    for action_id, tool, args, code in [
        ("unknown", "data.unknown", {}, "unknown_tool"),
        ("alias", "read", {"resource_id": "alpha"}, "aliased_tool"),
        ("malformed", "data.read", {"unexpected": "x"}, "unknown_argument"),
        ("bad-resource", "data.read", {"resource_id": "../escape"}, "invalid_resource"),
    ]:
        verdict = _admit(engine, conn, org_id, action_id=action_id, tool=tool, args=args)
        assert verdict["outcome"] == "review"
        assert verdict["reason_code"] == code
        assert verdict["state_mutated"] is False
    conn.close()


def test_nonfinite_negative_and_caller_forged_profile_values_are_rejected():
    with pytest.raises(ValueError, match="budget_cost"):
        ActionProfile("data.read", "read", "dataset", "confidential", "low", -1)
    with pytest.raises(ValueError, match="finite"):
        ActionProfile("data.read", "read", "dataset", "confidential", "low", math.inf)
    with pytest.raises(ValueError, match="classification"):
        ActionProfile("data.read", "read", "dataset", "secret", "low", 1)

    engine = _engine()
    conn = db.connect(":memory:")
    db.init_schema(conn)
    org_id = db.create_org(conn, "forged-profile", tier="pro")["id"]
    auth = engine.issue_lineage_authorization(
        org_id=org_id, task_lineage_id="forged-task", session_id="s",
        workspace_scope="w", authority_action_id="root", authority_ref="authority/v1",
        context_head_digest=CONTEXT_HASH,
    )
    engine.start_lineage(
        conn, org_id=org_id, task_lineage_id="forged-task", session_id="s",
        workspace_scope="w", authority_action_id="root", authority_ref="authority/v1",
        context_head_digest=CONTEXT_HASH, authorization=auth,
    )
    verdict = engine.admit(
        conn, org_id=org_id, task_lineage_id="forged-task", session_id="s",
        workspace_scope="w", authority_action_id="aar-1", authority_ref="authority/v1",
        context_head_digest=CONTEXT_HASH, action_id="a1", tool_endpoint="data.read",
        arguments={"resource_id": "alpha"}, claimed_impact="critical",
    )
    assert verdict["outcome"] == "review"
    assert verdict["reason_code"] == "caller_profile_not_authoritative"
    conn.close()


def test_concurrent_admissions_serialize_one_time_budget_transition(tmp_path):
    engine = _engine(budget_limit=1)
    conn, org_id, _ = _setup(tmp_path, engine)
    conn.close()
    barrier = threading.Barrier(2)
    results = []
    lock = threading.Lock()

    def worker(action_id):
        local = db.connect(str(tmp_path / "composition.db"))
        try:
            barrier.wait(timeout=5)
            verdict = _admit(engine, local, org_id, action_id=action_id,
                             tool="data.read", args={"resource_id": action_id})
            with lock:
                results.append(verdict)
        finally:
            local.close()

    threads = [threading.Thread(target=worker, args=(f"a-{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert len(results) == 2
    assert [item["outcome"] for item in results].count("allow") == 1
    assert any(item["reason_code"] == "budget_exceeded" for item in results)


def test_retry_is_idempotent_and_conflicting_retry_cannot_change_state(tmp_path):
    engine = _engine()
    conn, org_id, _ = _setup(tmp_path, engine)
    first = _admit(engine, conn, org_id, action_id="read-1", tool="data.read",
                   args={"resource_id": "alpha"}, idempotency_key="idem-1")
    replay = _admit(engine, conn, org_id, action_id="read-1", tool="data.read",
                    args={"resource_id": "alpha"}, idempotency_key="idem-1")
    conflict = _admit(engine, conn, org_id, action_id="read-2", tool="data.read",
                      args={"resource_id": "alpha"}, idempotency_key="idem-1")

    assert replay["outcome"] == first["outcome"] == "allow"
    assert replay["action_digest"] == first["action_digest"]
    assert replay["idempotent_replay"] is True
    assert conflict["outcome"] == "deny"
    assert conflict["reason_code"] == "idempotency_conflict"
    state = engine.get_lineage(conn, org_id, "task-1")
    assert state["state_version"] == 1
    assert len(state["admitted_actions"]) == 1
    conn.close()


def test_reset_requires_authenticated_predeclared_authorization(tmp_path):
    engine = _engine()
    conn, org_id, _ = _setup(tmp_path, engine)
    with pytest.raises(CompositionError, match="authorization"):
        engine.reset_lineage(
            conn, org_id=org_id, task_lineage_id="task-1",
            successor_lineage_id="task-2", session_id="session-2",
            workspace_scope="ws-a", authority_action_id="reset-aar",
            authority_ref="authority/v1", context_head_digest=CONTEXT_HASH,
        )
    auth = engine.issue_reset_authorization(
        org_id=org_id, prior_lineage_id="task-1", successor_lineage_id="task-2",
        session_id="session-2", workspace_scope="ws-a", authority_action_id="reset-aar",
        authority_ref="authority/v1", context_head_digest=CONTEXT_HASH,
    )
    successor = engine.reset_lineage(
        conn, org_id=org_id, task_lineage_id="task-1", successor_lineage_id="task-2",
        session_id="session-2", workspace_scope="ws-a", authority_action_id="reset-aar",
        authority_ref="authority/v1", context_head_digest=CONTEXT_HASH,
        authorization=auth,
    )
    assert successor["parent_lineage_id"] == "task-1"
    assert successor["admitted_actions"] == []
    conn.close()


def test_authenticated_override_can_admit_policy_conflict_but_untrusted_text_cannot(tmp_path):
    engine = _engine(pairs=(("read", "external_send"),))
    conn, org_id, _ = _setup(tmp_path, engine)
    assert _admit(engine, conn, org_id, action_id="read-1", tool="data.read",
                  args={"resource_id": "alpha"})["outcome"] == "allow"
    resolved = engine.taxonomy.resolve("net.send", {"destination": "https://example.test"})
    override = engine.issue_override(
        org_id=org_id, task_lineage_id="task-1", action_id="send-1",
        action_digest=resolved.action_digest, authority_action_id="override-aar",
        authority_ref="authority/v1", workspace_scope="ws-a",
        context_head_digest=CONTEXT_HASH, approval_ref="approval-1",
    )
    allowed = _admit(engine, conn, org_id, action_id="send-1", tool="net.send",
                     args={"destination": "https://example.test"},
                     authority_action_id="override-aar", override=override)
    assert allowed["outcome"] == "allow"
    assert allowed["reason_code"] == "override_authorized"
    assert allowed["authority_action_id"] == "override-aar"

    forged = dict(override)
    forged["signature"] = "0" * 64
    denied = _admit(engine, conn, org_id, action_id="send-2", tool="net.send",
                    args={"destination": "https://example.test"}, override=forged)
    assert denied["outcome"] == "review"
    assert denied["reason_code"] == "invalid_override"
    conn.close()


def test_composition_verdict_binds_hash_only_receipt_and_rejects_non_allow_effect(tmp_path):
    engine = _engine()
    conn, org_id, _ = _setup(tmp_path, engine)
    verdict = _admit(engine, conn, org_id, action_id="read-1", tool="data.read",
                     args={"resource_id": "secret-record"})
    assert validate_verdict(verdict) == (True, [])
    assert verdict["schema"] == COMPOSITION_VERDICT_SCHEMA
    from ledger_agent.prebind import build_prebind_v2
    prebind = build_prebind_v2(
        attempted_action="action:read-1", actor_ref="agent:test",
        authority_ref="authority/v1", trusted_scope="workspace:ws-a",
        policy_version="policy/v1", evidence_hashes=["a" * 64],
        selected_context_digest=CONTEXT_HASH, resource_ref="dataset",
        boundary_outcome="allow", non_effective_result="not_executed",
        replay_id="replay:read-1", composition_binding=composition_binding(verdict),
    )

    result = metering.record_usage(
        conn, org_id, provider="ledger", model="fixture", task_type="read",
        workspace="ws-a", external_ref="task-1", input_tokens=1, output_tokens=0, cost_usd=0.01,
        composition_verdict=verdict, prebind=prebind,
    )
    receipt = audit_json(conn, org_id, external_ref="task-1")
    binding = receipt["events"][0]["composition"]
    assert result.recorded is True
    assert binding["policy_hash"] == verdict["policy_hash"]
    assert binding["profile_digest"] == verdict["profile_digest"]
    assert binding["state_hash"] == verdict["state_hash"]
    assert binding["verdict"] == "allow"
    assert "secret-record" not in json.dumps(binding)
    assert "composition_json" not in json.dumps(binding)
    stored_state = engine.get_lineage(conn, org_id, "task-1")
    admission_row = conn.execute(
        "SELECT verdict_json FROM composition_admissions WHERE org_id=? AND action_id=?",
        (org_id, "read-1"),
    ).fetchone()
    assert "secret-record" not in json.dumps(stored_state)
    assert "secret-record" not in admission_row["verdict_json"]

    forged = {**verdict, "state_mutated": False}
    assert validate_verdict(forged) == (True, [])
    with pytest.raises(ValueError, match="durable admission"):
        metering.record_usage(
            conn, org_id, provider="ledger", model="fixture", cost_usd=0,
            composition_verdict=forged,
        )

    with pytest.raises(ValueError, match="composition verdict"):
        metering.record_usage(
            conn, org_id, provider="ledger", model="fixture", cost_usd=0,
            composition_verdict={**verdict, "outcome": "deny"},
        )
    conn.execute("UPDATE usage_events SET composition_policy_hash=? WHERE id=?",
                 ("0" * 64, result.event_id))
    assert db.verify_chain(conn, org_id)["ok"] is False
    conn.close()


def test_invalid_identity_is_not_reflected_in_review_or_binding(tmp_path):
    engine = _engine()
    conn, org_id, _ = _setup(tmp_path, engine, lineage="privacy-task")
    secretish = '{"api_key":"SECRET-SENTINEL"}'
    verdict = _admit(
        engine, conn, org_id, action_id=secretish, tool="data.read",
        args={"resource_id": "alpha"}, lineage="privacy-task",
    )
    assert verdict["outcome"] == "review"
    assert verdict["action_id"] == "unbound"
    assert secretish not in json.dumps(verdict, sort_keys=True)

    valid = _admit(
        engine, conn, org_id, action_id="safe-1", tool="data.read",
        args={"resource_id": "alpha"}, lineage="privacy-task",
    )
    tampered = {**valid, "action_id": secretish}
    ok, errors = validate_verdict(tampered)
    assert not ok
    assert "action_id" in errors
    conn.close()


def test_admission_rejects_unbound_authority_action_id(tmp_path):
    engine = _engine()
    conn, org_id, _ = _setup(tmp_path, engine, lineage="authority-task")
    verdict = _admit(
        engine, conn, org_id, action_id="safe-1", tool="data.read",
        args={"resource_id": "alpha"}, lineage="authority-task",
        authority_action_id="unbound-aar",
    )
    assert verdict["outcome"] == "hold"
    assert verdict["reason_code"] == "lineage_binding_mismatch"
    conn.close()


def test_composition_binding_cannot_be_persisted_without_durable_allow(tmp_path):
    engine = _engine()
    conn, org_id, _ = _setup(tmp_path, engine, lineage="binding-task")
    verdict = _admit(
        engine, conn, org_id, action_id="read-1", tool="data.read",
        args={"resource_id": "alpha"}, lineage="binding-task",
    )
    from ledger_agent.prebind import build_prebind_v2
    prebind = build_prebind_v2(
        attempted_action="action:read-1", actor_ref="agent:test",
        authority_ref="authority/v1", trusted_scope="workspace:ws-a",
        policy_version="policy/v1", evidence_hashes=["a" * 64],
        selected_context_digest=CONTEXT_HASH, resource_ref="dataset",
        boundary_outcome="allow", non_effective_result="not_executed",
        replay_id="replay:read-1", composition_binding=composition_binding(verdict),
    )
    with pytest.raises(ValueError, match="durable composition verdict"):
        metering.record_usage(
            conn, org_id, provider="ledger", model="fixture", cost_usd=0,
            prebind=prebind,
        )
    assert conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 0
    conn.close()


def test_tampered_composition_projection_is_suppressed_from_receipt_and_export(tmp_path):
    engine = _engine()
    conn, org_id, _ = _setup(tmp_path, engine, lineage="export-task")
    verdict = _admit(
        engine, conn, org_id, action_id="read-1", tool="data.read",
        args={"resource_id": "alpha"}, lineage="export-task",
    )
    from ledger_agent.prebind import build_prebind_v2
    prebind = build_prebind_v2(
        attempted_action="action:read-1", actor_ref="agent:test",
        authority_ref="authority/v1", trusted_scope="workspace:ws-a",
        policy_version="policy/v1", evidence_hashes=["a" * 64],
        selected_context_digest=CONTEXT_HASH, resource_ref="dataset",
        boundary_outcome="allow", non_effective_result="not_executed",
        replay_id="replay:read-1", composition_binding=composition_binding(verdict),
    )
    result = metering.record_usage(
        conn, org_id, provider="ledger", model="fixture", task_type="read",
        workspace="ws-a", external_ref="export-task", cost_usd=0, prebind=prebind,
        composition_verdict=verdict,
    )
    conn.execute(
        "UPDATE usage_events SET composition_json=? WHERE id=?",
        (json.dumps({"composition_hash": "0" * 64, "raw": "SECRET-SENTINEL"}), result.event_id),
    )
    conn.commit()
    receipt = audit_json(conn, org_id, external_ref="export-task")
    exported = db.export_events(conn, org_id)
    assert receipt["events"][0]["composition"] is None
    assert exported[0]["composition"] is None
    assert "SECRET-SENTINEL" not in json.dumps(receipt)
    assert "SECRET-SENTINEL" not in json.dumps(exported)
    conn.close()
