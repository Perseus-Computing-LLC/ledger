# Optional Stripe settlement setup for Perseus Ledger

Perseus Ledger runs fully offline without Stripe. This guide configures the
optional settlement adapter for test-mode payments and prepaid-credit workflows.
It is not required for event recording, chain verification, evidence receipts,
or local operation.

The stable package, CLI, config path, and lookup keys below retain `ledger*`
names as compatibility contracts during the product transition.

## 0. Prerequisites
```bash
pip install "ledger-agent[stripe]"
ledger init --org "Your Co" --tier pro
```
Grab your **test-mode** keys from the Stripe dashboard (Developers → API keys).
Always start in test mode (`sk_test_…`).

## 1. Point Ledger at Stripe
```bash
export STRIPE_SECRET_KEY=sk_test_xxx
export STRIPE_PUBLISHABLE_KEY=pk_test_xxx
export STRIPE_WEBHOOK_SECRET=whsec_xxx        # from step 3
```
Or put them in `~/.ledger/config.yaml` under `billing:` (env wins).

## 2. Create the Pro plan
```bash
ledger stripe-setup
```
This creates the configured recurring product and price (idempotent via the
legacy-compatible `ledger_pro_monthly` lookup key) and writes the price ID into
your config. Credit top-ups are priced dynamically per checkout, so nothing else
to create.

## 3. Run the server + forward webhooks
```bash
ledger serve            # dashboard at http://localhost:8420
# in another terminal (Stripe CLI):
stripe listen --forward-to localhost:8420/webhook/stripe
```
`stripe listen` prints a `whsec_…` — that's your `STRIPE_WEBHOOK_SECRET` for
step 1. Restart `ledger serve` after setting it.

## 4. Take a payment
On the dashboard's **Billing** panel: enter an amount → **Top up →** → pay with
Stripe's test card `4242 4242 4242 4242` (any future expiry / CVC). Or fire a
synthetic event:
```bash
stripe trigger checkout.session.completed
```
On `checkout.session.completed`, Ledger credits the organization's ledger and the
**Credit balance** card updates on the next 5-second refresh. The existing
upgrade flow moves the organization to the configured tier; the Customer Portal
button manages the subscription.

## 5. Go live
Swap `sk_test_…`/`pk_test_…`/`whsec_…` for live keys, register a production
webhook endpoint in the Stripe dashboard pointing at
`https://your-host/webhook/stripe`, and re-run `ledger stripe-setup` once
against the live key to create the live price.

## How it's safe
- Webhooks are **signature-verified** and recorded by event id — a replay never
  double-credits (`stripe_events` table).
- The credit balance is the **sum of an append-only ledger**, so it's auditable
  and can't silently drift.
- No Stripe key → Checkout is simply disabled and everything else runs offline.

---

## 6. Savings-share billing (the Perseus value path)

The Pro subscription and prepaid credit above are the *subscription* half of the
model. The other half bills a share of the **money Perseus actually saved** a
customer — the differentiated revenue path.

**How savings are recorded.** Every metered event can carry a counterfactual:
what the *same* call would have cost without Perseus (same token counts, priced
at the customer's designated baseline model). Pass it at meter time:

```bash
# actual cost $1.00; would have been $4.00 without Perseus routing → $3.00 saved
ledger meter --provider anthropic --model claude-haiku-4-5 \
    --cost 1.00 --baseline 4.00
```

or over HTTP:

```json
POST /v1/usage
{ "provider": "anthropic", "cost_usd": 1.00, "baseline_cost_usd": 4.00 }
```

The baseline is folded into the usage-event **hash chain**, so a billed saving
is as tamper-evident as the actual cost, and it's exported alongside cost
(`baseline_usd` column) so a customer can reconstruct every figure.

**See what's owed (read-only):**
```bash
ledger savings --period 2026-07
#   verified savings   : $3.5000
#   share rate         : 18.0%
#   ── billable share  : $0.6300
```

**Raise the invoice** (dry-run without `--apply`):
```bash
ledger bill-savings --period 2026-07 --apply
```
With `--apply` and a live Stripe key this creates a Stripe invoice for the share
and records it in `savings_invoices` (idempotent per org+period — a re-run is a
no-op). Without Stripe connected it records the period as `pending` so nothing is
lost.

**The rules that keep the number honest** (see `docs/savings-share.md`):
- Only events with a baseline count — **never a blanket "you saved 40%"**.
- A per-event saving is `max(0, baseline − cost)`; it can never go negative.
- Coverage (how many events carried a baseline) is always shown, so a
  thin-coverage period is visible, not hidden.
- The rate is `billing.savings_share_pct` (default `0.10`); override per-run with
  `--rate`.
