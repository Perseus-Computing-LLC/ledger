# Launch assets — Plutus public launch

## Show HN post

**Title:** Show HN: Plutus — self-hosted billing for AI agents (MIT, Stripe-integrated)

**Body:**

Plutus is a billing layer for AI agents. It meters every LLM call, tracks spend per provider/model/workspace, and proves how much money your context engine saved — on a tamper-evident hash chain.

The pitch: if you're building an AI agent that costs real money to run, you need to bill your users. Plutus gives you that — self-hosted, MIT-licensed, Stripe-integrated.

What it does:
- Drop-in metering: `plutus meter --provider anthropic --model claude-opus --cost 0.14`
- REST API for agent frameworks: `POST /v1/usage` with API key auth
- Prepaid credit ledger: users top up via Stripe, Plutus debits as calls happen
- Savings-share billing: prove how much your routing saved, bill 10% of verified savings
- Hash-chained tamper evidence: every event is cryptographically linked; modifying history breaks `plutus verify`
- Dark-themed dashboard at :8420 with real-time spend, per-provider breakdowns, efficiency billboard
- Five tiers: Free (unlimited metering) → Pro ($20/mo) → Pro Team ($50/mo, 5 seats) → Team ($10/seat + savings-share) → Enterprise

Stack: Python, SQLite, Stripe SDK. Zero external dependencies beyond Stripe. Works fully offline without a Stripe key.

Open source (MIT): https://github.com/Perseus-Computing-LLC/plutus
Docs: https://perseus.observer/plutus
PyPI: `pip install plutus-agent`

---

## r/LocalLLaMA post

**Title:** I built a billing layer for local AI agents — self-hosted, MIT, no cloud dependency

**Body:**

Been running AI agents locally for a while, and the one thing nobody talks about is: how do you charge for them? If you build an agent that costs real compute to run (even local models burn electricity/GPU time), you need billing.

Built Plutus to solve this. It's MIT-licensed, runs on SQLite, and integrates with Stripe for payments. But the Stripe part is optional — the metering, dashboards, and spend tracking all work fully offline.

Key features for the local-first crowd:
- Zero cloud dependency for metering. All data stays in your SQLite DB.
- Hash-chained usage events — every metered call is cryptographically linked. You can prove to a customer exactly what they spent.
- Local dashboard at localhost:8420 — dark theme, real-time updates.
- Savings-share billing: if you use Perseus context engine to reduce token usage, Plutus proves the savings and lets you bill a share of it.

Stack: Python 3.9+, SQLite, optional Stripe. No Docker required (though there's a GHCR image). `pip install plutus-agent` and you're done.

Repo: https://github.com/Perseus-Computing-LLC/plutus
Would love feedback, especially from anyone who's wrestled with agent billing before.

---

## Social posts (X/Twitter, LinkedIn)

**X thread:**

1/ I built a billing layer for AI agents. It's called Plutus. MIT license, self-hosted, Stripe-integrated. `pip install plutus-agent`

2/ The problem: if your AI agent costs real money to run, how do you bill for it? Existing tools (Helicone, Langfuse) are great for observability but don't do billing — charging end users for the agent's compute.

3/ Plutus solves this with a prepaid credit ledger. Users top up via Stripe → Plutus meters every LLM call → debits from their balance. Hash-chained so every event is cryptographically verifiable.

4/ The differentiated play: savings-share billing. If you use Perseus context engine to reduce token costs by 90%, Plutus proves the savings and lets you bill 10% of what you saved the customer. The product pays for itself.

5/ Free tier: unlimited metering, 1 seat. Pro: $20/mo flat. Pro Team: $50/mo for 5. Team: $10/seat + 10% savings-share. Enterprise: custom.

6/ Everything is open source (MIT). Works offline without Stripe. Dashboard at :8420. REST API at /v1/usage.

7/ https://github.com/Perseus-Computing-LLC/plutus
   https://perseus.observer/plutus

**LinkedIn post:**

I shipped Plutus, an open-source billing layer for AI agents. MIT license, Stripe-integrated, self-hosted.

If you're building agentic products, you eventually need to charge for them. Plutus gives you metering, prepaid credits, and savings-based billing — out of the box. Hash-chained for auditability. Five pricing tiers from free to enterprise.

Stack: Python, SQLite, Stripe. `pip install plutus-agent`.

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
