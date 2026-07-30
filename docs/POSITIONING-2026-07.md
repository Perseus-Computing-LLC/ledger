# Perseus Ledger positioning — 2026-07

## One line

**Perseus Ledger is the self-hosted, verifiable event and provenance layer for
autonomous systems.** It records what happened, under what authority and
evidence, and whether the resulting history can be independently verified.

Usage metering, resource allocation, reconciliation, prepaid credit, and Stripe
settlement are optional adapters—not the product boundary.

## The problem

Teams operating autonomous systems need more than a dashboard aggregate. They
need a defensible record of the actor, boundary, configuration, action, result,
authority reference, evidence linkage, and allocation behind consequential work.
Existing observability, gateway, billing, and workflow tools each solve part of
that problem; Ledger supplies a portable, independently verifiable event record.

## What makes Ledger different

Ledger combines these runtime-neutral capabilities:

1. **Hash-chained event history** — append-only, independently verifiable
   records with optional external checkpoints.
2. **Evidence and authority references** — opaque, hash-covered links to the
   action context, scope, approval, and result without storing raw secrets.
3. **Resource allocation** — optional provider, model, token, and cost metadata
   that can be reconciled against a system of record.
4. **Portable operation** — self-hosted SQLite state, HTTP ingestion, and an SDK
   usable with any runtime or application.

Everything except optional settlement adapters runs fully offline. Single-file
SQLite state, one-import SDK, MIT-licensed.

## The dogfooding narrative (lead with this)

**We found a money-attribution bug at the source, fixed it upstream, and it made
our own product more accurate.**

Plutus reads Hermes Agent's `state.db` to know what each session spent. Hermes
recorded a session's tokens against the model that was active when the session
*started* — so any mid-session model switch dumped the whole session's cost onto
the wrong provider. That corrupts per-provider spend, the runway projections
built on it, and the router's decisions (a provider quietly draining looks like
it has infinite runway, so the router sends it *more* traffic).

We authored the fix in **hermes-agent itself** (issue #51607, merged upstream): a
`session_model_usage` table that attributes each API call to the model live at
that call. Then we taught Plutus to consume it, allocating each session's
authoritative cost across the providers that actually served it — so totals never
regress and the split is finally correct.

Why it lands: it's proof of *integrity*, not a demo. A billing tool's only job is
to be right about money. We chase wrong numbers to their source in someone else's
codebase, fix them in the open, and hold ourselves to the same standard. That's
the whole pitch for trusting a billing layer.

Concrete before/after (real `plutus.py --json`, one session that switched
Anthropic → OpenAI mid-flight, $1.00 actual cost):

| provider | before | after |
|---|---|---|
| anthropic | $1.00 / 1000 tok | $0.667 / 700 tok |
| openai | **$0.00 / not shown** | $0.333 / 300 tok |

Before, the provider being drained was invisible. After, spend lands where it
belongs and the router stops over-routing to it.

## Proof points

- 300 passing tests; integer-micro-dollar money path; append-only auditable
  ledger; idempotent Stripe reversals (refunds/disputes/failed payments).
- Hardened self-serve surface (CSRF fail-closed, DB-backed signup cap, security
  headers, CSV-injection defense, fail-closed public bind) — see
  `docs/REVIEW-2026-07.md`.
- Shipped: v1.0.1 on PyPI (`plutus-agent`) and GHCR; MIT.

## Who it's for

Teams self-hosting agents on multiple LLM providers who need FinOps behind their
own firewall — and builders who want to resell agent capacity on prepaid credit
without wiring Stripe, a ledger, and quota enforcement themselves.

## What we don't claim

Plutus is not a hosted SaaS, not an observability suite, and not a gateway that
proxies your calls. It meters what you report and bills against it. It complements
LiteLLM/Langfuse/Helicone rather than replacing them.
