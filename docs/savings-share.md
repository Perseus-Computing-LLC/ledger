# Savings-share billing (#7)

Ledger's differentiated revenue path: bill a share of the money Perseus actually
saved a customer, not a flat subscription. This document is the methodology (why
the number is defensible), the design (how it plugs into the ledger), and the
operator go-live checklist.

## The model

Two-part pricing — the *hybrid*:

| Part | What | Where it lives |
|------|------|----------------|
| Subscription floor | Free up to 5 users, then **$20/mo** Pro | `pricing.py` tiers, existing Stripe sub |
| Savings-share | **10%** of independently-verified monthly savings (Pro & Enterprise, opt-in) | `savings.py`, `savings_invoices` table |

The floor covers infrastructure even in a thin month; the share captures value
where Perseus is actually reducing spend. `10%` is `billing.savings_share_pct`
(override per-run with `--rate`).

## Why the number is defensible

An outcome-based bill only works if the customer can't reasonably dispute it.
Three properties make it stick:

1. **Per-event, provable baseline.** Savings are never a blanket percentage. Each
   metered event may carry `baseline_cost_usd` — what that *same* call (same
   token counts) would have cost at the customer's designated baseline model. The
   saving is `max(0, baseline − cost)` for that one event. The period figure is
   the sum. An event with no baseline contributes **zero**.

2. **Recomputable + tamper-evident.** `baseline_micros` is folded into the
   `usage_events` SHA-256 hash chain (see [`ledger-integrity.md`](ledger-integrity.md)),
   so altering a recorded baseline to inflate savings breaks `ledger verify`. It
   is also a deterministic function of the chained token counts and the published
   price table, so the customer can independently reconstruct every dollar. The
   export includes a `baseline_usd` column for exactly this audit.

3. **Coverage is disclosed.** `ledger savings` always reports how many events in
   the period carried a baseline (`2/3 (67% coverage)`). A period where only some
   traffic was instrumented is visible, never hidden — you bill on what you can
   prove, and the customer sees the denominator.

Backward compatibility: the baseline is an *optional trailing* chain field —
omitted from the canonical hash when absent — so chains written before this
feature (and any no-baseline event) verify byte-identically to before.

## Data flow

```
meter (--baseline / baseline_cost_usd)
   └─ usage_events.baseline_micros   (nullable, hash-chained)
        └─ savings.period_savings()   Σ max(0, baseline−cost) over covered events
             └─ savings.savings_share_report()   × rate  → billable share
                  └─ bill_savings_share(--apply)
                       ├─ stripe_client.create_savings_invoice()  (InvoiceItem + Invoice)
                       └─ savings_invoices row  (idempotent per org+period)
```

`bill_savings_share` mirrors `reconcile.close_period`: **dry-run by default**,
one serialized transaction on apply, idempotent by the
`savings_invoices UNIQUE(org_id, period_label)` row. A second apply for an
already-invoiced period is a no-op; Stripe is never called twice (the
`create_savings_invoice` idempotency key is `savings:{org}:{period}`).

Amounts below `billing.savings_min_charge_usd` (default $0.50) record the period
as `pending` without raising a sub-dollar Stripe invoice.

## Producing baselines in production

### The Hermes bridge (automatic)

The honest baseline comes from the component that already knows both the routed
model and the counterfactual — the Hermes sync (`examples/hermes_sync.py`). It
tags each session event with a **`baseline_model`** — the flagship the customer
would have run without Perseus routing — and hosted Ledger prices the *same*
token counts at that model. The sync never sends a dollar figure, only the model
name, so the amount is derived from the published price table and stays
reconstructable.

Turn it on with one env var on the Hermes box:

```bash
LEDGER_BASELINE=flagship            # built-in provider→flagship map
# or pin one baseline for every provider:
LEDGER_BASELINE_MODEL=claude-opus-4-8
# or per-provider:
LEDGER_BASELINE_MODELS='{"anthropic":"claude-opus-4-8","openai":"gpt-5"}'
```

Rules that keep it honest:

- A baseline is attached **only when the session's actual model differs from the
  baseline** — i.e. routing actually happened. Un-routed traffic records no
  saving.
- The flagship map mirrors `config.py` `savings.baseline_models` (the operator
  owns both). The counterfactual assumption — "without Perseus you'd run the
  flagship" — is stated on the invoice/contract, not inferred silently.
- `hermes_sync.py --dry-run` prints how many events were tagged
  (`savings-share ON — 2/2 event(s) tagged`) so coverage is visible before
  anything is sent.

Server-side, `record_usage(baseline_model=…)` (and the `/v1/usage`
`baseline_model` field) resolve the price from `pricing.resolve_price`, honoring
`pricing.overrides`.

### Manual / other integrations

`ledger meter --baseline <usd>` and the `/v1/usage` `baseline_cost_usd` field let
any integration record an explicit baseline directly. When both a model and a
cost are supplied, the explicit cost wins. **No baseline → no billable savings**
(the safe default), so partial rollout under-bills rather than over-bills.

## Operator go-live checklist

Steps only the account owner can do (Ledger can't hold your Stripe credentials):

- [ ] `pip install "ledger-agent[stripe]"`
- [ ] Create or connect a **live** Stripe account; grab live keys.
- [ ] `export STRIPE_SECRET_KEY=sk_live_… STRIPE_PUBLISHABLE_KEY=pk_live_…`
- [ ] `ledger stripe-setup` once against the live key (creates the $20/mo price).
- [ ] Deploy `ledger serve` behind HTTPS; register the production webhook at
      `https://your-host/webhook/stripe`; set `STRIPE_WEBHOOK_SECRET`.
- [ ] Set `billing.savings_share_pct` if not 0.10, and `auth.base_url` for the
      pricing/upgrade links.
- [ ] Turn on baselines in the Hermes sync: set `LEDGER_BASELINE=flagship` (or a
      `LEDGER_BASELINE_MODEL[S]` override) on the Hermes box, then
      `hermes_sync.py --dry-run` and confirm events are tagged. `ledger savings`
      coverage should climb above 0% after the next sync.
- [ ] Dry-run `ledger bill-savings --period <last-month>`, eyeball the figure and
      coverage, then re-run with `--apply`. Schedule it monthly (cron, after
      month end) alongside `ledger close`.

Everything up to the last two steps also works in Stripe **test mode**
(`sk_test_…`) end-to-end, so you can rehearse the whole flow before going live.
