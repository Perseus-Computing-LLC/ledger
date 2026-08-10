"""MCP stdio server for Perseus Ledger.

Exposes a curated tool surface over the Ledger record/query/verify stack so an
agent can meter itself, inspect its own usage trail, verify chain integrity,
and mint hash-only evidence receipts — without a browser or manual CLI.

Transport is MCP stdio: newline-delimited JSON-RPC 2.0 on stdin/stdout, no
framing, no dependencies beyond the Python standard library (the package's
zero-extra-deps rule, cf. ``hermes.py`` and the /v1 server).

Tools (5, curated — read + scoped writes only, no admin surface):

- ``ledger_health``   — org, balance, workspace MTD, provider health (local).
- ``ledger_record``   — meter one call (wraps ``client.Meter.track``).
- ``ledger_query``    — recent usage events, newest first (cursor pagination).
- ``ledger_verify``   — walk the usage_events hash chain (tamper evidence).
- ``ledger_receipt``  — build a hash-only served-claim evidence receipt.

Local-first: in local mode (default) every tool reads/writes the SQLite chain
directly. In remote mode (``LEDGER_REMOTE_URL`` + ``LEDGER_API_KEY``) only
``ledger_record`` is supported; the rest return an explicit isError telling the
caller to use the SDK or /v1 API instead.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import asdict
from typing import Any, Callable, Optional

from . import client, config as cfgmod, db, demo, metering, receipts

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "perseus-ledger-mcp"
SERVER_VERSION = "1.2.0"


def _version() -> str:
    try:
        from . import __version__  # noqa: PLC0415 (lazy to avoid import cycle)

        return __version__
    except Exception:  # pragma: no cover - import cycle fallback
        return SERVER_VERSION


def _tool(name: str, description: str, properties: dict,
          required: Optional[list[str]] = None) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        },
    }


def _tools() -> list[dict[str, Any]]:
    return [
        _tool(
            "ledger_health",
            "Report the Ledger org, balance, workspace month-to-date spend, and "
            "provider health. Read-only.",
            {},
        ),
        _tool(
            "ledger_record",
            "Meter one AI call: record provider/model/tokens/cost plus optional "
            "task, workspace, external ref, and action provenance into the "
            "hash-chained evidence ledger. Scoped write. Action-provenance "
            "fields (agent_id, authority_manifest_ref, scope_anchor, "
            "action_intent_hash, action_status, approval_ref) are all-or-"
            "nothing: supply all of them or none.",
            {
                "provider": {"type": "string",
                             "description": "Provider name (required), e.g. "
                                            "anthropic, openai, bedrock."},
                "model": {"type": "string"},
                "task_type": {"type": "string", "default": "general"},
                "workspace": {"type": "string"},
                "input_tokens": {"type": "integer", "default": 0},
                "output_tokens": {"type": "integer", "default": 0},
                "cache_read_tokens": {"type": "integer", "default": 0},
                "reasoning_tokens": {"type": "integer", "default": 0},
                "cost_usd": {"type": "number",
                             "description": "Exact cost in USD if known; "
                                            "estimated from pricing otherwise."},
                "external_ref": {"type": "string"},
                "user_id": {"type": "string"},
                "agent_id": {"type": "string",
                             "description": "Agent identity; required with "
                                            "the action-provenance set."},
                "authority_manifest_ref": {"type": "string"},
                "scope_anchor": {"type": "string"},
                "action_intent_hash": {"type": "string"},
                "action_status": {"type": "string",
                                  "enum": ["intent", "approval_requested",
                                           "approved", "denied", "expired",
                                           "executed", "failed", "cancelled"]},
                "approval_ref": {"type": "string"},
            },
            required=["provider"],
        ),
        _tool(
            "ledger_query",
            "List recent usage events for the org, newest first. Pass `before` "
            "(the `_rowid` of the last event from the previous page) to "
            "paginate. Read-only.",
            {
                "limit": {"type": "integer", "default": 25,
                          "description": "Max events (1-500)."},
                "before": {"type": "integer",
                           "description": "Cursor: rowid of the last event "
                                          "seen, for pagination."},
            },
        ),
        _tool(
            "ledger_verify",
            "Walk the usage_events tamper-evidence hash chain and report "
            "divergences per org. Read-only.",
            {
                "org": {"type": "string",
                        "description": "Org id or slug; all orgs if omitted."},
            },
        ),
        _tool(
            "ledger_receipt",
            "Build a hash-only served-claim evidence receipt binding a source "
            "reference to a ledger event, with optional authority/provenance/"
            "scope/state context. Contains no prompts, memory bodies, or "
            "secrets. Read-only.",
            {
                "source_ref": {"type": "string",
                               "description": "Reference to the source being "
                                              "served (e.g. vault entity id)."},
                "event_ref": {"type": "string",
                              "description": "Ledger event hash or external "
                                             "reference."},
                "authority_ref": {"type": "string"},
                "provenance_class": {"type": "string"},
                "scope_anchor": {"type": "string"},
                "state": {"type": "string"},
            },
            required=["source_ref", "event_ref"],
        ),
    ]


TOOLS = _tools()
HANDLERS: dict[str, Callable[[client.Meter, dict], dict]] = {}


def _local(meter: client.Meter) -> Any:
    """Return the local connection or raise a clear remote-mode error."""
    if meter.conn is None:
        raise ValueError(
            "this tool requires local mode (no LEDGER_REMOTE_URL); use the "
            "ledger_agent SDK or the /v1 API for remote operation")
    return meter.conn


def _org_id(meter: client.Meter) -> str:
    """The resolved org id; always set in local mode after Meter init."""
    if meter.org_id is None:
        raise ValueError("org not resolved")
    return meter.org_id


def _handle_health(meter: client.Meter, args: dict) -> dict:
    conn = _local(meter)
    org_id = _org_id(meter)
    out: dict[str, Any] = {
        "mode": "local",
        "org": org_id,
        "summary": metering.org_summary(conn, org_id),
        "provider_health": metering.provider_health(conn, org_id),
    }
    return out


def _handle_record(meter: client.Meter, args: dict) -> dict:
    res = meter.track(
        provider=args["provider"],
        model=args.get("model"),
        task_type=args.get("task_type", "general"),
        workspace=args.get("workspace"),
        input_tokens=int(args.get("input_tokens", 0) or 0),
        output_tokens=int(args.get("output_tokens", 0) or 0),
        cache_read_tokens=int(args.get("cache_read_tokens", 0) or 0),
        reasoning_tokens=int(args.get("reasoning_tokens", 0) or 0),
        cost_usd=args.get("cost_usd"),
        external_ref=args.get("external_ref"),
        user_id=args.get("user_id"),
        agent_id=args.get("agent_id"),
        authority_manifest_ref=args.get("authority_manifest_ref"),
        scope_anchor=args.get("scope_anchor"),
        action_intent_hash=args.get("action_intent_hash"),
        action_status=args.get("action_status"),
        approval_ref=args.get("approval_ref"),
        source="mcp",
    )
    return asdict(res)


def _handle_query(meter: client.Meter, args: dict) -> dict:
    conn = _local(meter)
    org_id = _org_id(meter)
    limit = max(1, min(int(args.get("limit", 25)), 500))
    events = metering.recent_events(
        conn, org_id, limit=limit, before=args.get("before"))
    return {"events": events, "count": len(events)}


def _handle_verify(meter: client.Meter, args: dict) -> dict:
    conn = _local(meter)
    org_id: Optional[str] = None
    if args.get("org"):
        org_id = meter._resolve_org(args["org"], tier="free", create=False)
    report = db.verify_chain(
        conn, org_id=org_id, hmac_key=cfgmod.chain_hmac_key(cfgmod.load()))
    return report


def _handle_receipt(meter: client.Meter, args: dict) -> dict:
    # Receipts are hash-only constructs; no db access required (works in
    # remote mode too, since nothing touches the local chain).
    claim = receipts.build_served_claim(
        source_ref=args["source_ref"],
        event_ref=args["event_ref"],
        authority_ref=args.get("authority_ref"),
        provenance_class=args.get("provenance_class"),
        scope_anchor=args.get("scope_anchor"),
        state=args.get("state"),
    )
    valid, errors = receipts.validate_served_claim(claim)
    return {"claim": claim, "valid": valid, "errors": errors}


HANDLERS = {
    "ledger_health": _handle_health,
    "ledger_record": _handle_record,
    "ledger_query": _handle_query,
    "ledger_verify": _handle_verify,
    "ledger_receipt": _handle_receipt,
}


class McpError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _handle(method: str, params: dict, meter: client.Meter) -> dict:
    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": _version()},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name = (params or {}).get("name")
        if not isinstance(name, str):
            raise McpError(-32602, "missing tool name")
        handler = HANDLERS.get(name)
        if handler is None:
            raise McpError(-32602, f"unknown tool: {name}")
        try:
            result = handler(meter, (params or {}).get("arguments") or {})
        except Exception as exc:  # tool-level failure -> isError result
            return {"content": [{"type": "text",
                                 "text": json.dumps({"error": str(exc)},
                                                    default=str)}],
                    "isError": True}
        return {"content": [{"type": "text",
                             "text": json.dumps(result, default=str)}]}
    raise McpError(-32601, f"method not found: {method}")


def _send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def serve_stdio(meter: client.Meter) -> None:
    """Line-delimited JSON-RPC 2.0 loop over stdin/stdout."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            _send({"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32700, "message": "parse error"}})
            continue
        if not isinstance(msg, dict) or "method" not in msg:
            continue
        if msg["method"] == "notifications/initialized" or "id" not in msg:
            continue  # notifications get no response
        try:
            result = _handle(msg["method"], msg.get("params") or {}, meter)
            _send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
        except McpError as exc:
            _send({"jsonrpc": "2.0", "id": msg["id"],
                   "error": {"code": exc.code, "message": exc.message}})
        except Exception as exc:  # pragma: no cover - defensive
            _send({"jsonrpc": "2.0", "id": msg["id"],
                   "error": {"code": -32603, "message": str(exc)}})


def build_meter(*, demo_mode: bool = False, db_path: Optional[str] = None,
                org: Optional[str] = None) -> client.Meter:
    """Construct a Meter exactly like the CLI does (LEDGER_DB honored)."""
    if demo_mode:
        path = db_path or (tempfile.mkdtemp(prefix="ledger-mcp-demo-")
                           + "/ledger.db")
        conn = db.connect(path)
        db.init_schema(conn)
        demo.seed(conn)
        conn.close()
        db_path = path
    return client.Meter(org=org, db_path=db_path, create=True)


def serve(*, demo_mode: bool = False, db_path: Optional[str] = None,
          org: Optional[str] = None) -> None:
    serve_stdio(build_meter(demo_mode=demo_mode, db_path=db_path, org=org))


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        prog="ledger mcp",
        description="Run the Perseus Ledger MCP stdio server.")
    parser.add_argument("--demo", action="store_true",
                        help="seed sample data into a throwaway database first")
    parser.add_argument("--db", default=None,
                        help="database path (default: LEDGER_DB or ~/ledger.db)")
    parser.add_argument("--org", default=None,
                        help="organization id or slug")
    args = parser.parse_args(argv)
    serve(demo_mode=args.demo, db_path=args.db, org=args.org)


if __name__ == "__main__":
    main()
