# Perseus Computing Brand Architecture

Status: approved product-direction baseline
Date: 2026-07-30

## Canonical product family

Perseus Computing builds runtime-neutral infrastructure for trustworthy
autonomous systems. The product family has three primary products:

| Product | Purpose |
|---|---|
| **Perseus** | Resolves verified workspace state before an agent or application acts. |
| **Perseus Vault** | Preserves durable, encrypted, time-valid knowledge. |
| **Perseus Ledger** | Records verifiable events, authority references, evidence links, and optional resource allocation. |

The short form **Ledger** is acceptable after the first full reference. Use
**Perseus Ledger** in headings, package metadata, registry listings, product
pages, and first references. Do not use bare `Ledger` as the legal or primary
product name.

## Independence and interoperability

Each product stands alone. None requires a particular agent runtime, hosted
service, provider, or framework. Perseus, Vault, and Ledger may integrate
together through documented contracts; those integrations are optional.

External runtimes, including Hermes Agent, are integrations rather than members
of the Perseus Computing product family. Do not describe external runtimes as
Perseus Computing products or make them a prerequisite in product positioning.

## Ledger positioning

**Perseus Ledger is the verifiable event and provenance layer for autonomous
systems.** It answers: what happened, under what authority and evidence, and can
the resulting history be independently verified?

Ledger's core is an append-only, hash-chained evidence record. Resource
allocation, usage metering, billing, provider reconciliation, and Stripe are
optional adapters—not the product boundary and not the primary story.

## Ledger transition

`Ledger` is a legacy compatibility identity during migration. Keep existing
contracts stable, including `ledger-agent`, `ledger_agent`, the `ledger` CLI,
`LEDGER_*` configuration, deployed endpoints, and `/v1` routes, until a
separately announced migration supplies tested Ledger aliases and a deprecation
window.

New public-facing prose, metadata, dashboard titles, and documentation use
**Perseus Ledger**. Legacy identifiers appear only in explicit compatibility or
migration contexts.

## Guardrails

- Do not claim a compliance certification or data-handling regime not supported
  by implementation and evidence.
- Do not describe Ledger as dependent on, owned by, or built for a specific
  external agent runtime.
- Do not remove compatibility identifiers in a marketing sweep.
- Do not claim authority enforcement belongs to Ledger when another control
  plane validates it; Ledger records opaque, hash-covered provenance.