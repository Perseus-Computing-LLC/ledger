# Perseus Ledger

> **Perseus resolves. Vault remembers. Ledger proves.**

Perseus Ledger is the verifiable event and provenance layer for autonomous systems. It records **what happened, under what authority and evidence, and whether the history can be independently verified**.

It is deliberately **not** an AI-spend dashboard. Ledger provides an append-only, hash-chained record that ties activity to its actor, boundary, evidence, configuration, action, result, and optional resource allocation. It works independently with any agent runtime, application, internal tool, or offline deployment.

## What Ledger establishes

For each recorded event, the stable ledger captures the operational facts already available to the system:

- **Actor and boundary** — organization, workspace, user/agent, and task type
- **Execution configuration** — provider, model, and event metadata
- **Action and result** — the event itself plus its immutable record hash
- **Resource allocation** — optional token and cost attribution
- **Evidence linkage** — external references and retained checkpoints where supplied
- **Integrity** — an append-only cryptographic hash chain that can be verified independently

The current ingestion contract is deliberately stable during the product transition: `plutus_agent`, the `plutus` CLI, `/v1/usage`, existing database paths, and deployed integrations remain supported compatibility surfaces. Stripe is an **optional settlement adapter**, not the product boundary.

## Why it matters

AI systems need more than observability. They need a defensible answer to:

> What did the system know, what did it do, under which model and policy, what did it consume, and can we prove it later?

Perseus Ledger provides the evidentiary layer for that answer. It can work beside any agent framework, application, internal tool, offline environment, or federated deployment.

### DoD and regulated-data relevance

- **AI assurance:** reconstruct a recommendation from the configuration, sources, actions, and evidence available when it was made.
- **Program and cost-data curation:** preserve source-to-output lineage, validation flags, analyst adjudications, and reproducible audit trails.
- **Autonomous / distributed operations:** retain a verifiable record of agent state, tool activity, and resource allocation for post-operation review.
- **Governance:** keep the human approval, correction, and policy context associated with consequential automated activity.

This is a product and architecture position, not a claim of handling CUI or satisfying a particular compliance regime.

## Perseus Computing product family

| Product | Question it answers |
|---|---|
| **Perseus** | What verified workspace state should be available before an agent acts? |
| **Perseus Vault** | What durable, time-valid knowledge did the system have? |
| **Perseus Ledger** | What happened, under what authority and evidence, and can we prove it? |

Each product is useful on its own and integrates through documented, runtime-neutral contracts. Ledger does not require Perseus, Vault, or any specific agent runtime.

## Quick start: record a verifiable event

```bash
pip install plutus-agent  # compatibility package name during transition
plutus demo
# → opens the local Ledger console on http://localhost:8420
```

### Container image

The canonical GHCR image is `ghcr.io/perseus-computing-llc/ledger`:

```bash
docker pull ghcr.io/perseus-computing-llc/ledger:latest
docker run --rm -p 8420:8420 ghcr.io/perseus-computing-llc/ledger:latest
```

The `plutus-agent` package, `plutus` CLI, and `PLUTUS_*` environment variables
remain compatibility interfaces for existing integrations.

```python
from plutus_agent import Meter

ledger = Meter(org="Acme Autonomous Systems")
ledger.track(
    provider="anthropic",
    model="claude-opus-4-8",
    task_type="evidence_review",
    workspace="mission-analysis",
    input_tokens=8200,
    output_tokens=2400,
)
```

This writes an immutable event into the local SQLite-backed hash chain. Existing hosted ingestion continues to use `POST /v1/usage`; refer to [the API reference](docs/api.md) for the compatibility contract.

## Integrity verification

Ledger integrity is not a marketing assertion. It is checked from the recorded chain and can be exposed through the existing admin verification endpoint in a controlled deployment.

- [Ledger integrity](docs/ledger-integrity.md)
- [Continuous attestation](docs/continuous-attestation.md) — admission vs. runtime evidence; attestation blocks; mechanical vs. reasoning provenance
- [Evidence receipts](docs/evidence-receipts.md) — task-scoped, machine-readable views of hash-chained events
- [API reference](docs/api.md)
- [Schema](docs/schema.md)
- [Reconciliation](docs/reconciliation.md) — optional provider-cost and Stripe settlement reconciliation

## Transition principles

1. **Runtime-neutral by design.** Ledger integrates with any agent runtime or application through its SDK and HTTP contracts; no Perseus product is required.
2. **No broken integrations.** Legacy package names, CLI commands, state paths, `/v1` routes, deployed domains, and keys remain supported until a separately announced migration.
3. **No billing-first story.** Resource allocation, billing, and Stripe reconciliation remain optional adapters beneath the ledger.
4. **Evidence before claims.** The product must only claim provenance fields it actually records and can verify.

## License

MIT — see [LICENSE](LICENSE). © Perseus Computing LLC.
