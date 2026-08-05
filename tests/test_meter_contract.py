from __future__ import annotations

import hashlib
import inspect
import json
import urllib.request

import yaml

from plutus_agent import Meter, db
from plutus_agent.prebind import build_prebind


AAR_FIELDS = {
    "agent_id",
    "authority_manifest_ref",
    "scope_anchor",
    "action_intent_hash",
    "action_status",
    "approval_ref",
}
CONTEXT_RENDER_FIELDS = {
    "context_render_schema",
    "context_render_hash",
    "served_memory_provenance_hash",
    "action_receipt_hash",
}
RESOURCE_FIELDS = {
    "resource_constraints_version",
    "resource_constraints_hash",
}
PREBIND_FIELDS = {
    "prebind",
}
PREBIND_PROPERTIES = {
    "schema_version",
    "attempted_action",
    "actor_ref",
    "authority_ref",
    "trusted_scope",
    "policy_version",
    "evidence_hashes",
    "selected_context_digest",
    "resource_ref",
    "boundary_outcome",
    "non_effective_result",
    "replay_id",
    "approval_ref",
    "stage_refs",
    "prebind_hash",
}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _prebind() -> dict:
    return build_prebind(
        attempted_action="action:deploy-42",
        actor_ref="agent:hermes-prod",
        authority_ref="authority:manifest-3",
        trusted_scope="github:Perseus-Computing-LLC/ledger",
        policy_version="policy/v3",
        evidence_hashes=[_digest("source")],
        selected_context_digest=_digest("context-selection"),
        resource_ref="resource:ledger-event-42",
        boundary_outcome="allow",
        non_effective_result="not_executed",
        replay_id="replay:deploy-42",
        approval_ref="approval:deploy-42",
        stage_refs=["stage:prebind"],
    )


def _all_fields():
    return {
        "agent_id": "hermes-prod",
        "authority_manifest_ref": "authority:manifest-3",
        "scope_anchor": "github:Perseus-Computing-LLC/ledger",
        "action_intent_hash": "a" * 64,
        "action_status": "executed",
        "approval_ref": "approval:deploy-42",
        "context_render_schema": "perseus-context-render-trace/v1",
        "context_render_hash": "b" * 64,
        "served_memory_provenance_hash": "c" * 64,
        "action_receipt_hash": "d" * 64,
        "resource_constraints_version": "perseus-authorized-action/resource-constraints/v1",
        "resource_constraints_hash": "e" * 64,
        "prebind": _prebind(),
    }


def test_meter_track_local_persists_all_cross_product_fields(tmp_path):
    meter = Meter(
        org="contract-local",
        tier="pro",
        db_path=str(tmp_path / "ledger.db"),
        config={"pricing": {"block_over_balance": False}},
    )
    fields = _all_fields()
    result = meter.track(
        provider="openai",
        model="gpt-fixture",
        task_type="deploy",
        external_ref="deploy-42",
        input_tokens=1,
        output_tokens=1,
        cost_usd=0.01,
        **fields,
    )

    assert result.recorded is True
    row = meter.conn.execute(
        "SELECT agent_id, authority_manifest_ref, scope_anchor, action_intent_hash, "
        "action_status, approval_ref, context_render_schema, context_render_hash, "
        "served_memory_provenance_hash, action_receipt_hash, "
        "resource_constraints_version, resource_constraints_hash, prebind_json "
        "FROM usage_events WHERE id=?",
        (result.event_id,),
    ).fetchone()
    for name in AAR_FIELDS | CONTEXT_RENDER_FIELDS | RESOURCE_FIELDS:
        expected = fields[name]
        assert row[name] == expected
    assert json.loads(row["prebind_json"]) == fields["prebind"]
    meter.close()


def test_meter_track_remote_serializes_all_cross_product_fields(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({
                "event_id": "evt-remote",
                "org_id": "org-remote",
                "cost_usd": 0.01,
                "estimated": False,
                "balance_after": 1.0,
                "recorded": True,
            }).encode()

    def fake_urlopen(request, timeout):
        captured["timeout"] = timeout
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode())
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    meter = Meter(remote="https://ledger.example.test", api_key="plutus_sk_test")
    fields = _all_fields()
    result = meter.track(
        provider="openai",
        model="gpt-fixture",
        task_type="deploy",
        external_ref="deploy-42",
        input_tokens=1,
        output_tokens=1,
        cost_usd=0.01,
        **fields,
    )

    assert result.event_id == "evt-remote"
    assert captured["url"] == "https://ledger.example.test/v1/usage"
    assert captured["body"]["external_ref"] == "deploy-42"
    for name in AAR_FIELDS | CONTEXT_RENDER_FIELDS | RESOURCE_FIELDS | PREBIND_FIELDS:
        assert captured["body"][name] == fields[name]
    meter.close()


def test_meter_and_openapi_have_cross_product_field_parity():
    supported = AAR_FIELDS | CONTEXT_RENDER_FIELDS | RESOURCE_FIELDS | PREBIND_FIELDS
    track_parameters = set(inspect.signature(Meter.track).parameters)
    assert supported <= track_parameters

    with open("openapi.yaml", encoding="utf-8") as handle:
        spec = yaml.safe_load(handle)
    usage_properties = set(spec["components"]["schemas"]["UsageEvent"]["properties"])
    assert supported <= usage_properties

    prebind_schema = spec["components"]["schemas"]["Prebind"]
    assert PREBIND_PROPERTIES <= set(prebind_schema["properties"])
    assert spec["components"]["schemas"]["UsageEvent"]["properties"]["prebind"]["$ref"] == "#/components/schemas/Prebind"
