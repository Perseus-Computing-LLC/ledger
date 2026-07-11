# Plutus launch assets — 2026-07

Draft copy and a launch checklist. All postable copy below is written to be
pasted as-is (no em-dashes, no filler). The human (tcconnally) does every
outward post; nothing here sends itself.

---

## Demo recording plan

Goal: a 45-60 second loop that shows the four-in-one and the dogfooding fix.

1. `plutus demo` then open `http://localhost:8420`. Let the dashboard load with a
   month of sample data (balance, burn, per-provider breakdown, live feed).
2. Show the SDK: three lines in a REPL, `track(...)`, then `balance()` drops.
3. Show the monitor + router: `python plutus.py` table, then the before/after of
   the attribution fix (a session that switched providers mid-flight; OpenAI goes
   from invisible to correctly attributed).
4. End on the dashboard's per-provider runway.

Capture: asciinema for the terminal steps (`asciinema rec`), a short screen GIF
for the dashboard (the repo already ships `plutus-dashboard.png` for a static
hero). Keep it silent with on-screen captions so it embeds anywhere.

Assets to produce and where they live: `docs/media/plutus-demo.cast` (asciinema),
`docs/media/plutus-dashboard.gif`. Link both from the README hero.

---

## README polish (small, high-signal)

- Add a one-line "why Plutus vs. a gateway/observability tool" note near the top:
  it bills, it doesn't proxy; it complements LiteLLM/Langfuse/Helicone.
- Add the four-in-one framing (live balance, ledger spend, self-calibrating
  budgets, runway routing) as a short bullet list above the capability table.
- Link `docs/POSITIONING-2026-07.md` and the demo media once recorded.
- Add a "Meter Hermes Agent" subsection pointing at `examples/hermes_sync.py` and
  noting the per-model attribution (schema v17) so the dogfooding story is
  discoverable from the README.

---

## Show HN draft

Title:
```
Show HN: Plutus – self-hosted billing and prepaid credit for AI agents
```

Body:
```
I run agents across several LLM providers and could never answer three things in
one place: what each call cost, how much credit was left, and which provider
would run dry first. Observability tools watch spend, gateways route calls, and
Stripe charges customers, but nothing self-hosted tied metering, prepaid credit,
and runway-aware routing together.

Plutus is my attempt. It is a Python package (pip install plutus-agent) plus an
optional HTTP API and dashboard. One import meters usage per provider, model,
task, and workspace, writes an append-only credit ledger in integer
micro-dollars, and depletes prepaid credit as calls land. A runway router ranks
providers by projected days-left and shifts your flagship model to whichever
provider you can most afford to keep using. Everything except Stripe runs offline,
state is a single SQLite file, and it is MIT licensed.

One thing I want to be honest about, because it is the reason I trust it with
money: Plutus reads Hermes Agent's state.db for spend, and I found that a
mid-session model switch attributed the whole session's cost to the wrong
provider. I fixed that upstream in Hermes itself (a per-model usage table), then
taught Plutus to consume it, allocating each session's real cost across the
providers that actually served it. Before the fix, a provider quietly draining
showed zero spend and the router sent it more traffic. After, spend lands where it
belongs.

Repo, docs, and a 60-second demo in the README. Happy to answer anything about the
ledger design, the Stripe reversal handling (refunds/disputes/failed payments
reverse idempotently), or the router.
```

---

## X / thread draft

```
1/ Plutus: the self-hosted billing layer for AI agents.

Meter every call, bill against prepaid credit, and route to the provider you can
most afford to keep using. pip install plutus-agent. MIT.

2/ Four things in one, which no OSS tool does together:
- live per-provider balance
- append-only credit ledger (integer micro-dollars)
- self-calibrating budgets
- runway routing (shift your flagship to the provider with the most days left)

3/ The part I care about most: it is correct about money.

Plutus reads Hermes Agent for spend. A mid-session model switch was attributing a
whole session's cost to the wrong provider.

4/ So we fixed it at the source, in Hermes itself, and then consumed the fix in
Plutus.

Before: a draining provider showed $0 and the router sent it MORE traffic.
After: cost lands on the provider that actually served each call.

5/ Everything except Stripe runs offline. Single SQLite file. One import in your
agent hot path.

Repo + 60s demo: <link>
```

---

## dev.to / blog draft (outline + intro)

Working title: **"A billing bug is a trust bug: how we fixed our spend numbers in
someone else's codebase"**

Intro:
```
A billing tool has one job: be right about money. So when our numbers looked
slightly off, we did not paper over it in our own code. We followed it upstream,
found the root cause in the tool we read spend from, fixed it there in the open,
and then consumed the fix. Here is the whole trail, because the trail is the
point.
```

Sections:
1. What Plutus is and why per-provider accuracy drives real decisions (the runway
   router).
2. The symptom: a provider draining but showing zero spend.
3. The root cause in Hermes: session cost attributed to the initial model, not the
   model live at each call.
4. The upstream fix (session_model_usage, issue #51607) and why we sent it there
   instead of patching around it.
5. Consuming it in Plutus: allocate the authoritative session cost across
   providers so totals never regress. Show the before/after table.
6. The takeaway: trust a billing layer that chases wrong numbers to their source.

---

## Submission-ready blurb (directories, newsletters, one-liners)

```
Plutus is the self-hosted billing layer for AI agents: usage metering, an
append-only prepaid-credit ledger, Stripe billing, and a runway-based model
router, all behind your own firewall. Drop the one-import Python SDK into your
agent to see every call's cost live and bill against prepaid credit, or run the
HTTP API and dashboard for a full multi-tenant setup. Everything except Stripe
runs offline. MIT licensed. pip install plutus-agent.
```

---

## Release checklist (v1.0.x)

Status verified 2026-07-11. Most is already done; the outward gates remain.

- [x] Package version single-sourced (`plutus_agent.__version__` = 1.0.1) into
      wheel metadata.
- [x] Tag pushed (`v1.0.0`, `v1.0.1`) and GitHub Releases published.
- [x] PyPI: `plutus-agent` 1.0.1 live.
- [x] GHCR image published.
- [ ] **External security review** (the standing pre-public-launch gate; scope
      includes the hand-rolled OIDC RS256 verifier — see `docs/REVIEW-2026-07.md`
      P5). **Human/outward gate.**
- [ ] Close review punch-list P1-P7 (`docs/REVIEW-2026-07.md`) before the
      Perseus / Perseus-Vault convergence.
- [ ] Record demo media and wire into the README (see plan above).
- [ ] **Outward posts** (Show HN, X, dev.to, directory submissions) — drafts
      above; **human posts.**

### Left explicitly to the human (outward-facing)

- Posting anything public (Show HN, X, dev.to, marketplace/directory listings).
- Commissioning/accepting the external security review.
- Any Stripe App Marketplace submission that requires the Perseus Stripe account.

---

## Recurring cadence (propose as a Hermes cron, not host cron)

Two things want a heartbeat once the push starts. Per house policy these should
be **Hermes Agent cron jobs**, alongside the existing Plutus credit-refresh /
balance check-in jobs, not host crontab entries:

1. **Partner-pipeline monitor** (weekly) — re-check the top listing surfaces in
   `partner-targets-2026-07.csv` for status changes (new submission forms, dead
   links, review outcomes) and open a short digest.
2. **Awareness cadence** (weekly during launch) — remind to advance one drafted
   asset (Show HN / X / dev.to / one directory) so the push doesn't stall on a
   single big post.

Proposed, not created — wiring a cron is a Hermes-side change and an outward
commitment for the human to approve.
