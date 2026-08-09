# Perseus Computing products and hosted deployments

Status: product architecture and deployment guidance

Perseus Computing builds three independent, interoperable products for
trustworthy autonomous systems. They can be used together, but none is a
prerequisite for another and none requires a particular agent runtime.

| Product | Core question | Standalone use |
|---|---|---|
| **Perseus** | What verified workspace state should be available before action? | Resolve local project state into a working context. |
| **Perseus Vault** | What durable, time-valid knowledge did the system have? | Store and retrieve encrypted, persistent knowledge. |
| **Perseus Ledger** | What happened, under what authority and evidence, and can the history be verified? | Record hash-chained events, evidence, and optional allocation. |

## Optional composition

A deployment may connect the products through documented contracts:

```text
Perseus       → verified workspace state
Perseus Vault → durable knowledge
Perseus Ledger → verifiable events, evidence, authority references, and allocation
```

This is a composition pattern, not a bundle requirement. Ledger accepts events
from any application or agent runtime. Vault and Perseus work independently of
Ledger. External tools and runtimes are integrations, not Perseus Computing
products.

## Ledger deployment

Perseus Ledger is self-hostable and local-first. Its stable compatibility
installation surface remains:

```bash
pip install ledger-agent
ledger serve
```

The product name is **Perseus Ledger**. The package name, `ledger` CLI,
`ledger_agent` import, `LEDGER_*` configuration, `/v1` routes, database paths,
and `ledger.perseus.observer` endpoint remain compatibility contracts during the
transition.

Ledger's core is an append-only, hash-chained record. It can record actor,
organization/workspace boundary, provider/model configuration, action result,
external evidence, and opaque authority references. Resource allocation,
metering, reconciliation, prepaid credit, and Stripe settlement are optional
adapters rather than product prerequisites.

## Hosted endpoints

- Product site: `https://perseus.observer/ledger/`
- Hosted Ledger: `https://ledger.perseus.observer/` (legacy host name retained)
- Stable ingestion: `POST /v1/usage`

Deployments must treat the hosted endpoint as an integration choice, not a
requirement. Local SQLite-backed use remains supported.

## Commercial and settlement adapters

Stripe support is optional. When configured, it can support prepaid credit,
subscriptions, and reconciliation. It is not required for event recording,
chain verification, evidence receipts, or local operation. See
[`../BILLING.md`](../BILLING.md) for the operator guide.

## Status and guardrails

- Do not claim a certification, compliance boundary, or data-handling regime not
  supported by implemented controls and evidence.
- Do not claim Ledger enforces authority: it records hash-covered provenance
  supplied by an enforcement/control plane.
- Do not represent external agent runtimes as members of the Perseus Computing
  product family.
- Preserve compatibility identifiers until canonical aliases have shipped with a
  migration guide, tests, and an announced deprecation window.
