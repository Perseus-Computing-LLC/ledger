# Launch assets — Perseus Ledger public launch

> **Status:** draft copy only. These assets must be fact-checked against the
> release and reviewed before any outward post. The stable package, CLI, and API
> retain their legacy `plutus*` names during the transition.

## Show HN post

**Title:** Show HN: Perseus Ledger — self-hosted, verifiable event provenance for autonomous systems

**Body:**

Autonomous systems need more than aggregate observability: they need to answer
what happened, under what authority and evidence, and whether that history can
be verified later.

Perseus Ledger is a self-hosted, MIT-licensed event and provenance layer. It
records activity in an append-only, hash-chained ledger with optional links to
actor, workspace, provider/model configuration, external evidence, authority
references, and resource allocation. It runs offline with SQLite and works with
any runtime or application.

What it does today:
- HTTP ingestion at `POST /v1/usage` and a Python SDK
- Per-organization hash chains with local verification and optional retained checkpoints
- Opaque, hash-covered evidence and action-authority references
- Optional provider/model/token/cost allocation and reconciliation
- Optional Stripe settlement adapters; Stripe is not required or the product boundary
- Self-hosted dashboard and API at `:8420`

The installed compatibility package remains `plutus-agent` and the CLI remains
`plutus` during transition; the product is Perseus Ledger.

Open source (MIT): https://github.com/Perseus-Computing-LLC/plutus
Docs: https://perseus.observer/ledger/
PyPI compatibility package: `pip install plutus-agent`

---

## r/LocalLLaMA post

**Title:** Perseus Ledger: a self-hosted, verifiable event record for local and autonomous systems

**Body:**

I wanted a local-first way to preserve an auditable record of autonomous-system
activity without making a hosted platform or a specific agent runtime part of
the trust boundary.

Perseus Ledger is MIT-licensed, runs with SQLite, and records activity in an
append-only hash chain. It can carry resource allocation where useful, but it is
not a billing product first: the core question is what happened, under what
evidence and authority, and can the record be verified later?

- No cloud dependency for local operation
- Hash-chained records with local verification and optional external checkpoints
- HTTP ingestion and Python SDK
- Optional metering, reconciliation, and Stripe settlement adapters
- Dashboard at `localhost:8420`

Install remains `pip install plutus-agent` during the compatibility transition.

Repo: https://github.com/Perseus-Computing-LLC/plutus

---

## Social posts (X/Twitter, LinkedIn)

**X thread:**

1/ Introducing Perseus Ledger: a self-hosted, verifiable event and provenance layer for autonomous systems. MIT licensed. Runtime-neutral.

2/ The question is not only “what did the system cost?” It is: what happened, under what authority and evidence, and can we verify the history later?

3/ Ledger records append-only, hash-chained events with optional actor, workspace, provider/model, evidence, authority, and allocation metadata.

4/ It works offline with SQLite, speaks HTTP, and does not require a particular agent framework or hosted service.

5/ Metering, reconciliation, prepaid credit, and Stripe remain optional adapters—not the product boundary.

6/ Existing users keep the stable compatibility package and CLI: `pip install plutus-agent`; `plutus` remains supported during transition.

7/ https://github.com/Perseus-Computing-LLC/plutus
   https://perseus.observer/ledger/

**LinkedIn post:**

Perseus Ledger is an open-source, self-hosted event and provenance layer for
autonomous systems. It preserves a hash-chained record of activity, evidence,
authority references, and optional resource allocation so teams can reconstruct
what happened and independently verify the resulting history.

It is runtime-neutral, works offline with SQLite, and exposes HTTP and Python
interfaces. Metering, reconciliation, and settlement remain optional adapters.

The current compatibility install remains `pip install plutus-agent` while the
product transition is completed.

GitHub: https://github.com/Perseus-Computing-LLC/plutus

---

## Landing page checklist

- [ ] perseus.observer/plutus deployed (see plutus/index.html)
- [ ] Pricing page shows all five tiers with feature comparison
- [ ] Code snippet: `pip install plutus-agent && plutus init && plutus serve`
- [ ] "View on GitHub" button links to Perseus-Computing-LLC/plutus
- [ ] Savings calculator embeds the frontier chart (once benchmark data exists)
- [ ] Footer links to main perseus.observer, GitHub, docs

## Community engagement checklist

- [ ] Show HN post drafted (above)
- [ ] r/LocalLLaMA post drafted (above)
- [ ] X/Twitter thread drafted (above)
- [ ] LinkedIn post drafted (above)
- [ ] Reply to every Show HN comment within 24 hours
- [ ] List in LangChain / CrewAI integration directories once callbacks ship
- [ ] Submit to AI-agent newsletter roundups (TheBatch, TLDR AI, etc.)
