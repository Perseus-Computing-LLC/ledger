# Local Perseus + Vault + Ledger integration

This guide wires the three products together on one workstation with explicit,
throwaway paths:

- **Perseus** resolves the active workspace context and selects the recall
  posture.
- **Perseus Vault** is the encrypted local memory/control plane. It owns
  admission, retention, deletion, curation, visibility, and recall.
- **Perseus Ledger** is the evidence layer. It records supplied usage,
  provenance references, and hash-covered receipts; it does not own Vault's
  lifecycle.

The walkthrough uses no provider credentials, prompts, memory bodies, or
network transport. It creates one encrypted Vault database and one local
Ledger database under `$HOME/.perseus-ledger-local` by default. The smoke event
uses zero tokens and zero cost and exists only to verify the receipt and
integrity paths.

For the governance mapping behind the hash-only fields, see
[Memory governance and Ledger provenance](memory-governance-provenance.md).

## 1. Prerequisites and one environment

The commands below use `uv` so Perseus and the local Ledger package share one
Python environment. They use the public Perseus Vault installer for the Rust
binary.

```bash
set -eu
umask 077

ROOT="${ROOT:-$HOME/.perseus-ledger-local}"
LEDGER_SOURCE="${LEDGER_SOURCE:-$ROOT/src/ledger}"
mkdir -p "$ROOT/src"

# Install the current public Ledger checkout only when one is not supplied.
if [ ! -f "$LEDGER_SOURCE/pyproject.toml" ]; then
  git clone --depth 1 https://github.com/Perseus-Computing-LLC/ledger.git "$LEDGER_SOURCE"
fi

# Keep Perseus and plutus-agent in the same environment: Perseus's optional
# local metering imports plutus_agent lazily from this interpreter.
if [ ! -x "$ROOT/.venv/bin/python" ]; then
  uv venv "$ROOT/.venv"
fi
uv pip install --python "$ROOT/.venv/bin/python" perseus-ctx
uv pip install --python "$ROOT/.venv/bin/python" -e "$LEDGER_SOURCE"
export PATH="$ROOT/.venv/bin:$HOME/.local/bin:$PATH"

# Install the local Vault binary if it is not already available.
if ! command -v perseus-vault >/dev/null 2>&1; then
  curl -sSf https://raw.githubusercontent.com/Perseus-Computing-LLC/perseus-vault/main/scripts/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

command -v perseus
command -v perseus-vault
command -v plutus
```

If the binary is built from source instead, `cargo install --git
https://github.com/Perseus-Computing-LLC/perseus-vault` supplies the same
`perseus-vault` command. Keep the command name and the explicit paths below;
do not rely on an implicit database selected from a different installation.

## 2. Declare the paths

These are the paths used by every subsequent command. Explicit paths avoid
accidentally opening a second Vault database or a second Ledger database.

```bash
VAULT_BIN="$(command -v perseus-vault)"
WORKSPACE="$ROOT/workspace"
VAULT_DIR="$ROOT/vault"
VAULT_DB="$VAULT_DIR/perseus-vault.db"
VAULT_KEY="$VAULT_DIR/secret.key"
LEDGER_ROOT="$ROOT/ledger-state"
LEDGER_DB="$LEDGER_ROOT/plutus.db"
PERSEUS_HOME="$ROOT/perseus-home"

# Opaque smoke-test references. They are identifiers, not memory content.
ORG_REF="org_local_ledger_check"
PROVIDER_REF="provider_local"
MODEL_REF="model_local"
TASK_REF="task_receipt_check"
WORKSPACE_REF="workspace_local"
RECEIPT_REF="ref_local_ledger_check"

mkdir -p "$WORKSPACE/.perseus" "$VAULT_DIR" "$LEDGER_ROOT" "$PERSEUS_HOME"

# Current config path/environment contracts.
export PERSEUS_HOME
export PLUTUS_HOME="$LEDGER_ROOT"
export PLUTUS_CONFIG="$LEDGER_ROOT/config.yaml"
export PLUTUS_DB="$LEDGER_DB"
```

The resulting layout is:

| Component | Path |
|---|---|
| Perseus workspace config | `$WORKSPACE/.perseus/config.yaml` |
| Perseus context source | `$WORKSPACE/.perseus/context.md` |
| Perseus rendered output | `$WORKSPACE/AGENTS.md` |
| Perseus global home | `$PERSEUS_HOME` |
| Vault database | `$VAULT_DB` |
| Vault AES-256-GCM key file | `$VAULT_KEY` |
| Ledger config | `$PLUTUS_CONFIG` |
| Ledger SQLite database | `$LEDGER_DB` |
| Perseus metering health | `$ROOT/metering-status.json` |

`perseus-vault` currently prefers the explicit `--db` path. Its fresh-install
default is under `~/.perseus-vault`, and older installations may be discovered
under compatibility paths; this guide deliberately bypasses that discovery.

## 3. Initialize and check encrypted Vault

`keygen` writes a base64-encoded 32-byte AES-256-GCM key file. `init` creates the
database, enables encryption, and writes the encryption canary. The key is never
put in SQLite or in this document.

```bash
# Never generate a replacement key for an existing database.
if [ -e "$VAULT_DB" ] && [ ! -f "$VAULT_KEY" ]; then
  printf '%s\n' 'Vault DB exists but its key file is missing; stop rather than rotating blindly.' >&2
  exit 1
fi

if [ ! -f "$VAULT_KEY" ]; then
  "$VAULT_BIN" keygen --key-file "$VAULT_KEY"
fi
if [ ! -f "$VAULT_DB" ]; then
  "$VAULT_BIN" init --db "$VAULT_DB" --key-file "$VAULT_KEY"
fi
chmod 600 "$VAULT_KEY" 2>/dev/null || true

# doctor reports the on-disk encryption state without printing key material.
"$VAULT_BIN" doctor --db "$VAULT_DB"

# A report-only maintenance pass: no curation, archive, purge, or VACUUM is applied.
"$VAULT_BIN" maintain --db "$VAULT_DB" \
  --encryption-key "$VAULT_KEY" --dry-run
```

Treat these states differently:

- `Encrypted`/canary-present is the expected result for this walkthrough.
- `Plaintext` means this database was not initialized by the block above; do
  not treat it as an encrypted deployment.
- A wrong or missing key for an encrypted database is a stop condition. Pass
  the same explicit key to `serve` and every write-capable maintenance command.
- A mixed state requires the Vault migration/rekey procedure owned by Vault;
  do not repair it by deleting or replacing the key file.

## 4. Initialize Ledger and write the Perseus config

Ledger's current local configuration contracts are `PLUTUS_HOME`,
`PLUTUS_CONFIG`, and `PLUTUS_DB`. `plutus init` creates the config/database;
the optional hash-chain HMAC is `ledger.hmac_key` or the
`PLUTUS_CHAIN_HMAC_KEY` environment variable. This local smoke test leaves the
optional HMAC secret unset and uses the default SHA-256 chain.

```bash
if [ ! -f "$LEDGER_DB" ]; then
  plutus init --org "$ORG_REF"
else
  plutus init
fi
```

Write the workspace-local Perseus config. Every key below is from the current
`perseus_vault` connector and `plutus` metering contracts; the Vault command
contains both the database path and the key path so the MCP child cannot select
an unintended store.

```bash
cat >"$WORKSPACE/.perseus/config.yaml" <<YAML
profiles:
  default:
    context_target: 200000
    memory: on_demand

perseus_vault:
  enabled: true
  transport: stdio
  command: ["$VAULT_BIN", "serve", "--db", "$VAULT_DB", "--encryption-key", "$VAULT_KEY"]
  timeout_s: 10.0
  init_timeout_s: 30.0
  workspace_scope: true
  merge_strategy: local_first
  decay_priority_weight: 0.4
  fallback_to_local: true
  circuit_breaker:
    threshold: 3
    cooldown: 120
  retry_policy:
    max_attempts: 3
    backoff_base: 1.5

plutus:
  enabled: true
  db_path: "$LEDGER_DB"
  org: "$ORG_REF"
  status_path: "$ROOT/metering-status.json"
YAML

cat >"$WORKSPACE/.perseus/context.md" <<'EOF'
@perseus
@vault query="ref_local_context" k=5
EOF
```

`profiles.default.memory: on_demand` is the recall-first posture: Perseus
renders a retrieval pointer rather than a pre-materialized memory dump. Use
`relevant` only when trigger-matched injection is explicitly desired. An
unconditional `always` posture is an explicit compatibility choice, not the
recommended default for a consequential action.

The `plutus` block enables only provider-usage metering. It does not infer Vault
lifecycle decisions or context provenance. The host that performs a
consequential action must explicitly send the hash-only context bindings
shown in [Memory governance and Ledger provenance](memory-governance-provenance.md).

## 5. Run Perseus, Vault, and MCP checks

First run Perseus's readiness check. It attempts the Vault MCP handshake and a
health call when `perseus_vault.enabled` is true. The filter below prints no
paths, bodies, or configuration values.

```bash
perseus doctor --workspace "$WORKSPACE" --json >"$ROOT/perseus-doctor.json"

"$ROOT/.venv/bin/python" - "$ROOT/perseus-doctor.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    report = json.load(handle)
checks = {item["id"]: item for item in report.get("checks", [])}
for required in ("vault_connectivity", "mcp_server"):
    if checks.get(required, {}).get("status") != "ok":
        raise SystemExit(f"Perseus doctor did not pass {required}")
print("Perseus doctor: Vault bridge and MCP checks passed")
PY
```

Then exercise the Vault binary directly over MCP stdio. This sends only
protocol metadata and a health request; it does not write a memory entity.

```bash
set -euo pipefail
cat >"$ROOT/vault-mcp-request.jsonl" <<'JSONL'
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"local-ledger-check","version":"1"}}}
{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}
{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"perseus_vault_health","arguments":{}}}
JSONL

"$VAULT_BIN" serve --db "$VAULT_DB" --encryption-key "$VAULT_KEY" \
  <"$ROOT/vault-mcp-request.jsonl" >"$ROOT/vault-mcp-response.jsonl"

"$ROOT/.venv/bin/python" - "$ROOT/vault-mcp-response.jsonl" <<'PY'
import json
import sys

responses = {}
with open(sys.argv[1], encoding="utf-8") as stream:
    for line in stream:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "id" in item:
            responses[item["id"]] = item

if "error" in responses.get(1, {}):
    raise SystemExit("Vault initialize failed")
tools = responses.get(2, {}).get("result", {}).get("tools", [])
names = {tool.get("name") for tool in tools}
if "perseus_vault_recall" not in names:
    raise SystemExit("canonical Vault recall tool was not advertised")
health = responses.get(3, {}).get("result")
if not isinstance(health, dict) or health.get("isError"):
    raise SystemExit("Vault health call failed")
print(f"Vault MCP: {len(names)} tools advertised; health call returned")
PY
```

Finally render the context from the workspace. An empty result from a healthy
new Vault is a valid no-match state; it is not the same as an unreachable Vault.

```bash
(
  cd "$WORKSPACE"
  perseus render .perseus/context.md --output AGENTS.md --strict
)
```

## 6. Harmless Ledger receipt and dry-run verification

Create one synthetic, zero-cost event in the scratch Ledger. `--ref` becomes
`external_ref`, so the event can be selected by the task-scoped receipt without
embedding a prompt or memory body.

```bash
plutus meter \
  --org "$ORG_REF" \
  --provider "$PROVIDER_REF" \
  --model "$MODEL_REF" \
  --task "$TASK_REF" \
  --workspace "$WORKSPACE_REF" \
  --input 0 --output 0 --cost 0 \
  --ref "$RECEIPT_REF" --json

# Read-only chain verification. Exit 0 is required.
plutus verify --org "$ORG_REF" --json

# Reconciliation is dry-run unless --apply is supplied. This writes nothing.
plutus reconcile --org "$ORG_REF" \
  --provider "$PROVIDER_REF" --amount 0 --json
```

Run the local receipt endpoint on loopback and check only its contract fields.
The default Ledger config has dashboard auth disabled for localhost; if a local
operator enables auth, use the normal org-scoped API key out of band rather than
putting it in a workspace file.

```bash
set -euo pipefail
LEDGER_PORT="${LEDGER_PORT:-18420}"
plutus serve --host 127.0.0.1 --port "$LEDGER_PORT" \
  >"$LEDGER_ROOT/server.log" 2>&1 &
LEDGER_PID=$!
cleanup_ledger() {
  kill "$LEDGER_PID" 2>/dev/null || true
  wait "$LEDGER_PID" 2>/dev/null || true
}
trap cleanup_ledger EXIT

ready=0
i=0
while [ "$i" -lt 50 ]; do
  if curl -fsS "http://127.0.0.1:${LEDGER_PORT}/healthz" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 0.1
  i=$((i + 1))
done
test "$ready" -eq 1

RECEIPT_PATH="$ROOT/ledger-receipt.json"
curl -fsS "http://127.0.0.1:${LEDGER_PORT}/api/audit?external_ref=${RECEIPT_REF}" \
  >"$RECEIPT_PATH"
RECEIPT_REF="$RECEIPT_REF" "$ROOT/.venv/bin/python" - "$RECEIPT_PATH" <<'PY'
import json
import os
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    receipt = json.load(stream)
if receipt.get("receipt_version") != "perseus-evidence-receipt/v1":
    raise SystemExit("unexpected receipt version")
if receipt.get("external_ref") != os.environ["RECEIPT_REF"]:
    raise SystemExit("receipt correlation mismatch")
if receipt.get("verification", {}).get("chain_ok") is not True:
    raise SystemExit("Ledger chain did not verify")
for event in receipt.get("events", []):
    encoded = json.dumps(event, sort_keys=True).lower()
    if any(forbidden in encoded for forbidden in ("prompt", "body", "credentials", "tool_arguments")):
        raise SystemExit("receipt contains a forbidden raw-material field")
print(f"Ledger receipt: {len(receipt.get('events', []))} event(s), chain verified, raw-material check passed")
PY
```

For a production or consequential action, the usage event can add the
existing hash-only fields below. The API key, if the Ledger instance requires
one, comes from an environment or secret manager and is not part of this
configuration.

```json
{
  "provider": "provider_id",
  "model": "model_id",
  "task_type": "task_id",
  "workspace": "workspace_id",
  "input_tokens": 0,
  "output_tokens": 0,
  "external_ref": "ref_7c91d2a4",
  "evidence_hashes": [
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  ],
  "policy_version": "policy_4f18c0e2",
  "result_hash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "context_render_schema": "perseus-context-render-trace/v1",
  "context_render_hash": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "served_memory_provenance_hash": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
  "action_receipt_hash": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
}
```

When any context-render digest is supplied, include
`context_render_schema`. Ledger rejects malformed digest values and
hash-covers every supplied optional field. It does not verify the external
artifact itself; the caller must recompute that digest in the owning system.

## 7. Degraded states and safe decisions

| State | Check | Meaning and safe response |
|---|---|---|
| Vault binary missing or MCP health fails | `perseus doctor --workspace "$WORKSPACE" --json`; inspect `vault_connectivity` | `fallback_to_local: true` may keep a render alive, but local fallback is not proof of durable Vault recall. Hold/abstain when the action requires Vault. |
| Vault health is successful but recall is empty | `perseus_vault_health` plus the render result | This can be a healthy no-match. Do not call it an outage or invent evidence. |
| Encrypted DB with a missing/wrong key | `perseus-vault doctor --db "$VAULT_DB"`; explicit `--encryption-key` on serve/maintenance | Stop. Do not generate a replacement key or allow plaintext writes beside ciphertext. |
| Ledger metering is disabled or degraded | `perseus doctor --workspace "$WORKSPACE" --json`; inspect `plutus_metering`; inspect `$ROOT/metering-status.json` when present | `fail_open` behavior keeps the caller running but dropped events make evidence incomplete. Reconcile before claiming coverage. |
| Receipt has `chain_ok: false` | `plutus verify --json` and the receipt's `verification` object | Stop evidence claims and investigate the first divergence. A later receipt cannot repair a broken chain. |
| Ledger endpoint unavailable | local process health and the metering status file | Do not silently label an action evidenced. Retry or record an explicit held/degraded outcome in the owning control plane. |

Ledger's `verify` command and a receipt's `chain_ok` are necessary checks, not a
claim that Vault's lifecycle decision was correct. Keep the owning Vault and
control-plane references available for any later verification.

## 8. Migration and related contracts

- [Memory governance and Ledger provenance](memory-governance-provenance.md)
  (#199) explains retention, admission, deletion, curation, and recall-posture
  linkage.
- [Vault migration guide](https://github.com/Perseus-Computing-LLC/perseus-vault/blob/main/docs/migration/legacy-tool-prefixes.md)
  covers the canonical MCP tool-prefix transition. New config and integrations
  in this guide use `perseus_vault_*` names directly.
- [Vault encryption specification](https://github.com/Perseus-Computing-LLC/perseus-vault/blob/main/docs/ENCRYPTION.md)
  documents AES-256-GCM scope, key custody, and the plaintext FTS caveat.
- [Perseus setup and configuration](https://github.com/Perseus-Computing-LLC/perseus/blob/main/SETUP-GUIDE.md)
  documents the `perseus_vault` connector and recall postures.
- [Evidence Receipts](evidence-receipts.md) and
  [Authorized Action Receipts](authorized-action-receipts.md) document the
  Ledger-side receipt contracts.

Do not place API keys, encryption key material, raw prompts, raw tool output, or
memory bodies in `.perseus/config.yaml`, `AGENTS.md`, Ledger receipts, or
tracked documentation.
