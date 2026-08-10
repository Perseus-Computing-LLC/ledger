#!/usr/bin/env python3
"""Gate the MCP registry metadata before publish.

Checks, against the single source of truth (``ledger_agent.mcp_server``):

- the published tool surface in ``server.json`` exactly matches the runtime
  tool list (names + schemas), so the registry listing can never drift from
  what ``ledger mcp`` actually serves;
- the ``server.json`` description stays under the official registry's
  100-character limit (a longer description is rejected with 422 on publish —
  seen live on perseus-vault v2.22.0);
- ``server.json`` version matches the package version (the registry is
  immutable per version, so a wrong version would publish a duplicate).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))

# 1. description length (registry hard limit)
desc = server.get("description", "")
if len(desc) > 100:
    raise SystemExit(f"server.json: description is {len(desc)} chars "
                     f"(registry limit 100): {desc!r}")

# 2. version sync with the package
sys.path.insert(0, str(ROOT))
from ledger_agent import __version__  # noqa: E402

if server.get("version") != __version__:
    raise SystemExit(f"server.json: version {server.get('version')} != "
                     f"package {__version__}")

# 3. tool surface parity with the running server
from ledger_agent import mcp_server  # noqa: E402

runtime = {t["name"]: t["inputSchema"] for t in mcp_server.TOOLS}
published = {t["name"]: t.get("inputSchema") for t in server.get("tools", [])}
if runtime != published:
    missing = sorted(set(runtime) - set(published))
    extra = sorted(set(published) - set(runtime))
    changed = sorted(
        n for n in set(runtime) & set(published)
        if runtime[n] != published[n])
    raise SystemExit(
        f"server.json tool surface drifted from ledger_agent.mcp_server: "
        f"missing={missing} extra={extra} changed={changed}")

print(json.dumps({
    "version": __version__,
    "description_len": len(desc),
    "tools": len(runtime),
    "packages": [p["registryType"] + ":" + p["identifier"]
                 for p in server.get("packages", [])],
}, sort_keys=True))
