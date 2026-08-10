"""MCP server tests: in-process tool surface + real stdio protocol round-trip.

Covers the curated 5-tool surface end to end: initialize handshake,
tools/list schema shape, record -> query -> verify -> receipt round trip,
unknown-tool errors, and the remote-mode guard.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from typing import Any

from ledger_agent import mcp_server


def _meter(tmp_path, monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.delenv("LEDGER_REMOTE_URL", raising=False)
        monkeypatch.delenv("LEDGER_API_KEY", raising=False)
    return mcp_server.build_meter(db_path=str(tmp_path / "ledger.db"))


def _call(tool: str, args: dict, meter) -> tuple[Any, Any]:
    result = mcp_server._handle("tools/call", {"name": tool, "arguments": args},
                                meter)
    assert "content" in result and len(result["content"]) == 1
    text = json.loads(result["content"][0]["text"])
    return text, result


# --------------------------------------------------------------------------- #
# initialize / tools/list
# --------------------------------------------------------------------------- #

def test_initialize_handshake(tmp_path, monkeypatch):
    meter = _meter(tmp_path, monkeypatch)
    init = mcp_server._handle("initialize", {}, meter)
    assert init["protocolVersion"] == mcp_server.PROTOCOL_VERSION
    assert init["capabilities"] == {"tools": {}}
    assert init["serverInfo"]["name"] == "perseus-ledger-mcp"


def test_tools_list_surface(tmp_path, monkeypatch):
    meter = _meter(tmp_path, monkeypatch)
    resp = mcp_server._handle("tools/list", {}, meter)
    tools = resp["tools"]
    names = [t["name"] for t in tools]
    assert names == ["ledger_health", "ledger_record", "ledger_query",
                     "ledger_verify", "ledger_receipt"]
    for t in tools:
        schema = t["inputSchema"]
        assert schema["type"] == "object"
        assert schema.get("additionalProperties") is False
        assert set(schema.keys()) >= {"type", "properties", "required"}


def test_unknown_method_and_tool(tmp_path, monkeypatch):
    meter = _meter(tmp_path, monkeypatch)
    with pytest.raises(mcp_server.McpError) as e:
        mcp_server._handle("bogus/method", {}, meter)
    assert e.value.code == -32601

    with pytest.raises(mcp_server.McpError) as e:
        mcp_server._handle("tools/call", {"name": "nope", "arguments": {}},
                           meter)
    assert e.value.code == -32602


# --------------------------------------------------------------------------- #
# record -> query -> verify -> receipt
# --------------------------------------------------------------------------- #

def test_record_query_verify_receipt_roundtrip(tmp_path, monkeypatch):
    meter = _meter(tmp_path, monkeypatch)

    # health on a fresh ledger
    text, _ = _call("ledger_health", {}, meter)
    assert text["mode"] == "local"
    assert "summary" in text and "provider_health" in text

    # record (with full action-provenance set — all-or-nothing contract)
    text, _ = _call("ledger_record", {
        "provider": "anthropic", "model": "claude-sonnet-4-5",
        "task_type": "research", "workspace": "analysis",
        "input_tokens": 1200, "output_tokens": 400,
        "cost_usd": 0.012, "external_ref": "task-42",
        "agent_id": "agent-1",
        "authority_manifest_ref": "manifest:auth-aeb2cecc",
        "scope_anchor": "workspace:analysis",
        "action_intent_hash": "1b3faf1e70628d8f54c3ade4a5987d285797f3258e37bd385fff38a432d434f8",
        "action_status": "executed",
        "approval_ref": "approval:5242924409",
    }, meter)
    assert text["recorded"] is True
    assert text["cost_usd"] == 0.012
    assert text["balance_after"] is not None

    # query shows the event with a hash
    text, _ = _call("ledger_query", {"limit": 10}, meter)
    assert text["count"] == 1
    ev = text["events"][0]
    assert ev["provider"] == "anthropic"
    assert ev["model"] == "claude-sonnet-4-5"
    assert ev["cost_usd"] == 0.012
    assert ev.get("external_ref") == "task-42"
    assert ev.get("row_hash")  # hash-chained

    # verify: chain intact
    text, _ = _call("ledger_verify", {}, meter)
    assert text["ok"] is True
    org_rows = [o for o in text["orgs"] if o["events"] > 0]
    assert org_rows and org_rows[0]["status"] == "ok"

    # receipt: hash-only served claim validates
    text, _ = _call("ledger_receipt", {
        "source_ref": "vault:entity:mem-abc123",
        "event_ref": ev["row_hash"],
        "authority_ref": "manifest:auth-aeb2cecc",
        "provenance_class": "served_memory",
        "scope_anchor": "workspace:analysis",
        "state": "recalled",
    }, meter)
    assert text["valid"] is True and text["errors"] == []
    claim = text["claim"]
    assert claim["source_ref"] == "vault:entity:mem-abc123"
    assert claim["event_ref"] == ev["row_hash"]
    assert claim.get("claim_digest")


def test_record_requires_provider(tmp_path, monkeypatch):
    meter = _meter(tmp_path, monkeypatch)
    text, result = _call("ledger_record", {"model": "x"}, meter)
    assert result.get("isError") is True
    assert "provider" in text["error"]


def test_record_partial_action_provenance_rejected(tmp_path, monkeypatch):
    meter = _meter(tmp_path, monkeypatch)
    text, result = _call("ledger_record", {
        "provider": "anthropic", "agent_id": "agent-1",
    }, meter)
    assert result.get("isError") is True
    assert "required together" in text["error"]


def test_remote_mode_guards_local_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_REMOTE_URL", "https://ledger.example")
    monkeypatch.setenv("LEDGER_API_KEY", "k")
    meter = mcp_server.build_meter(db_path=str(tmp_path / "ledger.db"))
    assert meter.is_remote

    for tool in ("ledger_query", "ledger_verify", "ledger_health"):
        text, result = _call(tool, {}, meter)
        assert result.get("isError") is True
        assert "local mode" in text["error"]


# --------------------------------------------------------------------------- #
# stdio protocol round-trip (real subprocess)
# --------------------------------------------------------------------------- #

def _spawn_server(tmp_path):
    env = dict(os.environ)
    env.pop("LEDGER_REMOTE_URL", None)
    env.pop("LEDGER_API_KEY", None)
    env["PYTHONUNBUFFERED"] = "1"
    return subprocess.Popen(
        [sys.executable, "-m", "ledger_agent.mcp_server",
         "--db", str(tmp_path / "stdio.db")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, env=env,
        cwd=str(tmp_path.parent))


def _exchange(proc, msgs):
    """Send one JSON-RPC message per line; return parsed responses by id."""
    for m in msgs:
        proc.stdin.write(json.dumps(m) + "\n")
    proc.stdin.flush()
    expected = sum(1 for m in msgs if "id" in m)
    out = {}
    import time
    deadline = time.time() + 15
    while len(out) < expected and time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        resp = json.loads(line)
        if "id" in resp:
            out[resp["id"]] = resp
    return out


def test_stdio_protocol_roundtrip(tmp_path):
    proc = _spawn_server(tmp_path)
    try:
        responses = _exchange(proc, [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18",
                        "capabilities": {}, "clientInfo": {"name": "t"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "ledger_record",
                        "arguments": {"provider": "openai",
                                      "model": "gpt-5",
                                      "output_tokens": 77}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "ledger_query", "arguments": {"limit": 5}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
             "params": {"name": "ledger_verify", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
             "params": {"name": "does_not_exist", "arguments": {}}},
        ])

        assert responses[1]["result"]["serverInfo"]["name"] == \
            "perseus-ledger-mcp"
        assert len(responses[2]["result"]["tools"]) == 5

        rec = json.loads(responses[3]["result"]["content"][0]["text"])
        assert rec["recorded"] is True

        q = json.loads(responses[4]["result"]["content"][0]["text"])
        assert q["count"] == 1 and q["events"][0]["provider"] == "openai"

        v = json.loads(responses[5]["result"]["content"][0]["text"])
        assert v["ok"] is True

        assert responses[6]["error"]["code"] == -32602
    finally:
        if proc.stdin:
            proc.stdin.close()
        proc.terminate()
        proc.wait(timeout=10)
