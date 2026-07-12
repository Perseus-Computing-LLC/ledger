# The three-tier model — one meter, three ways to pay

Plutus has one primitive: a **verifiable efficiency meter** that records what your
AI stack *would* have cost on a flagship model versus what it actually cost, on a
tamper-evident hash chain. Everything below is a way to monetize that one number
for a different segment. The key design idea: **savings-share is a single lever,
set to a different position per tier.**

| Tier | Who | Price | Reporting | Savings-share |
|---|---|---|---|---|
| **Free** | Individual devs | $0, unlimited metering | Headline savings number only | **Suggested** — optional tip jar |
| **Pro** | Power users / prosumers | $20/mo flat | Full depth + verifiable receipts | **Waived** — the flat fee replaces it |
| **Team** | Companies | $10/seat/mo | Full depth + team attribution | **Mandatory** — 18% of provable savings |
| **Enterprise** | Large orgs | Custom | Everything + SSO/SLA/self-host | Negotiated |

## Why three tiers and not one price

They map to willingness-to-pay **and** to how *provable* the value is:

1. **Free is the growth engine, not the revenue.** Metering is unlimited so the
   "Plutus has saved you $X" billboard keeps running for real workloads — every
   free user is a walking advertisement for their own savings number. The tip jar
   ("chip in what we saved you") earns little directly; its job is virality +
   goodwill + a funnel into Pro/Team. The number is honest because it's
   tamper-evident and reconstructable — that's the moat.

2. **Don't double-dip on individuals.** Charging a solo user $20/mo *and* a % of
   their savings feels predatory and kills conversion. On Pro the flat $20 **is**
   the deal — predictable, no variable bill. Savings-share is *waived*.

3. **Savings-share is a business instrument.** Companies expect outcome-based
   pricing and their savings numbers are large enough to justify a %. Team pays a
   per-seat floor (covers attribution + admin) plus a *mandatory* 18% of the
   provable savings. This is the only tier where the share is billed by default.

## Where the lever lives in the code

* `plutus_agent/pricing.py` — `Tier.savings_share ∈ {suggested, waived,
  mandatory, custom, none}`, `Tier.per_seat_usd_month`, `Tier.full_reporting`,
  and `pricing.savings_mode(tier_key)`.
* **Dashboard billboard** — `server/app._dashboard` injects the current-month
  `efficiency` + `savings_share` into the summary; `server/views.render_dashboard`
  renders the "saved you $X" hero on every tier and the Free tip jar
  (`POST /billing/checkout/donate`).
* **Reporting gate** — `Tier.full_reporting` hides the per-task / leakage / export
  panels on Free behind a "Pro feature" card.
* **Billing guard** — `plutus bill-savings --apply` refuses to invoice a `pro`
  (waived) or `free` (suggested) org unless `--force`; mandatory billing is the
  Team path.

## Donations (the Free tip jar)

`donate_checkout` opens a one-time Stripe Checkout (`kind=donation`). On
`checkout.session.completed` the webhook records a distinct **`donation`** ledger
entry — hash-chained like everything else, never confused with prepaid credit or a
savings-share invoice. On the hosted instance the payment is real revenue; the
matching account credit is a thank-you.

## Not yet automated (follow-ups)

* **Per-seat Team subscriptions** — Team checkout is currently "talk to us"; the
  per-seat Stripe subscription + seat roster/roles are a follow-up. Savings-share
  for Team is billed today via `plutus bill-savings --apply` (mandatory tier, no
  `--force` needed).
* **Aggregate + individual attribution UI** for Team.
