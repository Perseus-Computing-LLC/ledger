# Ledger MCP server

`ledger mcp` exposes a curated, agent-callable tool surface over the Ledger
record/query/verify stack, speaking the Model Context Protocol over stdio. An
agent can meter itself, inspect its own usage trail, verify the integrity of
the hash chain, and mint hash-only evidence receipts — no browser, no manual
CLI, no admin surface.

The server is **stdlib-only** (newline-delimited JSON-RPC 2.0 over stdin/
stdout) and honors the package's zero-extra-dependencies rule.

## Install

```bash
pip install perseus-ledger
```

or run the container image:

```bash
docker run -i --rm ghcr.io/perseus-computing-llc/ledger:latest mcp
```

## Configure

Local mode (default) reads the same configuration as the CLI:

| Env var | Meaning |
| --- | --- |
| `LEDGER_DB` | Database path (default `~/ledger.db`) |
| `LEDGER_ORG` | Org id/slug (default: first org, or `default`) |

Remote mode: set `LEDGER_REMOTE_URL` + `LEDGER_API_KEY` to meter against a
hosted Ledger (`/v1/usage`). In remote mode only `ledger_record` is available;
the local-chain tools return an explicit error telling the caller to use the
SDK or the `/v1` API.

## Connect

Claude Code / Claude Desktop:

```bash
pip install perseus-ledger
claude mcp add ledger -- ledger mcp
```

or with the local checkout:

```bash
claude mcp add ledger -- ledger mcp
```

## Tools

| Tool | Kind | Description |
| --- | --- | --- |
| `ledger_health` | read | Org, balance, workspace MTD spend, provider health |
| `ledger_record` | write | Meter one call into the hash-chained ledger |
| `ledger_query` | read | Recent usage events, newest first, cursor pagination |
| `ledger_verify` | read | Walk the usage_events tamper-evidence hash chain |
| `ledger_receipt` | read | Build a hash-only served-claim evidence receipt |

### Action provenance (all-or-nothing)

`ledger_record` carries the same integrity contract as the SDK: if any of
`agent_id`, `authority_manifest_ref`, `scope_anchor`, `action_intent_hash`,
`action_status`, `approval_ref` is supplied, **all** of them must be. This
keeps the ledger's evidence model intact — an action with partial provenance
is refused, exactly as the CLI and SDK refuse it. Valid `action_status`
values: `intent`, `approval_requested`, `approved`, `denied`, `expired`,
`executed`, `failed`, `cancelled`.

## Registry

The server is published on the official Model Context Protocol registry as
`io.github.Perseus-Computing-LLC/ledger`, installable as the `perseus-ledger`
PyPI package or the GHCR image. The registry listing is regenerated on every
release by `.github/workflows/mcp-registry.yml` (OIDC-authenticated), gated by
`scripts/registry_metadata_check.py` so the published tool surface can never
drift from what `ledger mcp` actually serves.

## Verification

`tests/test_mcp.py` covers the full surface: initialize handshake,
`tools/list` schema shape, record → query → verify → receipt round trip,
all-or-nothing provenance enforcement, remote-mode guards, and a real
stdio subprocess protocol exchange.

```bash
python -m pytest tests/test_mcp.py -q
```
