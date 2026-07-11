# Plutus positioning — 2026-07

## One line

**Plutus is the self-hosted billing layer for AI agents** — usage metering,
prepaid-credit ledger, and Stripe billing you drop into your own stack, plus a
runway-based model router that shifts traffic to the provider you can most afford
to keep using.

## The problem

Teams running agents burn money across several LLM providers at once and can't
answer three questions in one place: *what did each call cost, how much credit is
left, and which provider will run dry first?* The market is split — observability
tools (Langfuse, Helicone) *watch* spend, gateways (LiteLLM, OpenRouter, Portkey)
*route* calls, and Stripe *charges* customers — but nothing self-hosted ties
metering, prepaid credit, and runway-aware routing together behind your own
firewall.

## What makes Plutus different — the four-in-one

No open-source tool does all four of these. Plutus does:

1. **Live balance monitoring** — real per-provider balances (DeepSeek/OpenAI
   live APIs) fused with a local cost ledger, on a dashboard at `:8420`.
2. **Ledger spend** — an append-only, integer-micro-dollar credit ledger; the
   balance is the sum of deltas, robust to out-of-order and concurrent inserts.
3. **Self-calibrating budgets** — `--calibrate` back-solves a provider's budget
   from real balance vs. recorded spend, so projections track reality instead of
   a guess you set once.
4. **Runway routing** — ranks providers by projected days-left and rewrites the
   router so the highest-runway provider runs its flagship, with the others as
   fallbacks. Billing *and* the credit runway gateways don't have.

Everything except Stripe runs fully offline. Single-file SQLite state, one-import
SDK, MIT-licensed.

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
