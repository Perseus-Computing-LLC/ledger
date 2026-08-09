# The three-tier model — one meter, three ways to pay

> **Attribution first.** Ledger *measures*; it does **not** save money. **Perseus**
> (routing / resolve-before-context) and **Perseus Vault** (memory) are what reduce
> spend. Ledger meters the spend and *proves* the savings. So the savings-share is
> an **ecosystem** monetization — it only applies when Perseus is in the loop
> producing (and Ledger proving) real savings. Standalone, Ledger is a "track my
> own spend, verify I'm getting my tokens" meter, and there's nothing to share.

Ledger has one primitive: a **verifiable efficiency meter** that records what your
AI stack *would* have cost on a flagship model versus what it actually cost, on a
tamper-evident hash chain. Everything below is a way to monetize that one number
for a different segment. The key design idea: **savings-share is a single lever,
set to a different position per tier.**

| Tier | Who | Price | Reporting | Savings-share |
|---|---|---|---|---|
| **Free** | Individual devs | $0, unlimited metering | Headline savings number only | **Suggested** — optional tip jar |
| **Pro** | Power users / prosumers | $20/mo flat | Full depth + chain-verifiable ledger | **Waived** — the flat fee replaces it |
| **Team** | Companies | $10/seat/mo | Full depth + team attribution | **Mandatory** — 10% of provable savings |
| **Enterprise** | Large orgs | Custom | Everything + SSO/SLA/self-host | Negotiated |

## Why three tiers and not one price

They map to willingness-to-pay **and** to how *provable* the value is:

1. **Free is the growth engine, not the revenue.** Metering is unlimited so the
   efficiency billboard keeps running for real workloads. Standalone it reads
   *"Your AI spend: $X · flagship-equivalent $Y · N× efficient"* (a tracking /
   verification stat); with Perseus in the loop it becomes *"Perseus saved you $X
   — verified by Ledger"* — every free user a walking advertisement for their own
   number. The tip jar ("chip in a share of what Perseus saved you") earns little
   directly; its job is virality + goodwill + a funnel into Pro/Team. The number
   is honest because it's tamper-evident and reconstructable — that's the moat.

2. **Don't double-dip on individuals.** Charging a solo user $20/mo *and* a % of
   their savings feels predatory and kills conversion. On Pro the flat $20 **is**
   the deal — predictable, no variable bill. Savings-share is *waived*.

3. **Savings-share is a business instrument.** Companies expect outcome-based
   pricing and their savings numbers are large enough to justify a %. Team pays a
   per-seat floor (covers attribution + admin) plus a *mandatory* 10% of the
   provable savings. This is the only tier where the share is billed by default.

## Where the lever lives in the code

* `ledger_agent/pricing.py` — `Tier.savings_share ∈ {suggested, waived,
  mandatory, custom, none}`, `Tier.per_seat_usd_month`, `Tier.full_reporting`,
  and `pricing.savings_mode(tier_key)`.
* **Dashboard billboard** — `server/app._dashboard` injects the current-month
  `efficiency` + `savings_share` into the summary; `server/views.render_dashboard`
  branches on whether a Perseus baseline is present (`savings_share.covered_events
  > 0`): ecosystem → "Perseus saved you $X — verified by Ledger"; standalone →
  "Your AI spend … flagship-equivalent … N× efficient" (no "saved" claim). The
  Free tip jar (`POST /billing/checkout/donate`) shows only when there are provable
  savings to share.
* **Reporting gate** — `Tier.full_reporting` hides the per-task / leakage / export
  panels on Free behind a "Pro feature" card.
* **Billing guard** — `ledger bill-savings --apply` refuses to invoice a `pro`
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
  for Team is billed today via `ledger bill-savings --apply` (mandatory tier, no
  `--force` needed).
* **Aggregate + individual attribution UI** for Team.
