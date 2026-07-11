# Cost reconciliation

Plutus meters what your agents spend, but the wired providers return token
counts, not dollars. `record_usage` turns tokens into dollars using the static
table in `plutus_agent/pricing.py` and marks the row `estimated = True`. That is
correct for a live estimate and the prepaid hard-stop, but it can drift from
what a provider actually charged (price changes, committed-use discounts,
enterprise rates, batch or cache pricing). You should not send an invoice built
on the table.

Reconciliation closes the gap: at the end of a billing period you feed Plutus
the provider's own authoritative total, and it writes one `adjust` ledger entry
per provider so the ledger, and therefore the prepaid balance, matches the real
bill.

Two things worth knowing:

- The savings percentage is rate-invariant. In an A/B comparison both arms use
  the same model at the same rate, so a stale table still yields the correct
  savings percent. Reconciliation is about the absolute dollars: the customer's
  spend, their prepaid balance, and any amount you bill a percentage of.
- Reconciliation never assumes missing data is zero. A provider that has metered
  usage but no authoritative total supplied is reported as unreconciled and left
  untouched, not refunded.

## Workflow

1. Get the provider's authoritative total for the period from its own export or
   console (see per-provider notes below).
2. Normalize it to a small file of `provider -> USD`. JSON or CSV:

   ```json
   { "period": "2026-07", "totals": { "openai": 812.44, "anthropic": 203.10 } }
   ```

   ```csv
   provider,cost_usd
   openai,812.44
   anthropic,203.10
   ```

3. Dry-run to see the deltas (nothing is written):

   ```
   plutus reconcile --org acme --period 2026-07 --totals totals.json
   ```

4. Apply when the numbers look right:

   ```
   plutus reconcile --org acme --period 2026-07 --totals totals.json --apply
   ```

   For a single provider you can skip the file:
   `plutus reconcile --period 2026-07 --provider openai --amount 812.44 --apply`.

## How the adjustment is computed

Usage debits are negative ledger deltas, so the ledger's contribution for a
provider over a period is `-recorded`. To make it equal `-authoritative` the
reconciler adds `recorded - authoritative`:

- recorded greater than authoritative (over-charged): a positive adjust credits
  the balance back.
- recorded less than authoritative (under-charged): a negative adjust debits the
  shortfall.

Each provider+period adjust is keyed by a deterministic `stripe_ref`
(`reconcile:<period>:<provider>`) and is written net of any prior adjust for
that key. So:

- Re-running with the same authoritative total is a no-op.
- If the provider later restates its invoice, re-running applies only the
  incremental correction.

The `--period YYYY-MM` flag also windows which `usage_events` are counted (by
timestamp, UTC month bounds). Omit it to reconcile all usage under one label.

## Getting the authoritative total per provider

Plutus does not call provider billing APIs directly; you produce the normalized
totals file from the provider's own export. The authoritative sources:

- OpenAI: the Usage and Costs API (organization costs endpoint) or the billing
  export in the dashboard.
- Anthropic: the usage and cost reporting in the console.
- Bedrock and other cloud-hosted models: the AWS Cost and Usage Report, filtered
  to the relevant service, or the provider line items on the cloud invoice.

Wherever your gateway already knows the real per-call cost (for example a
LiteLLM or Hermes proxy holding the customer's actual rate card), prefer passing
that exact cost into `record_usage(..., cost_usd=...)` at ingest. Then the row is
stored authoritative from the start and reconciliation only needs to catch the
rows that were estimated.

## Billing implication

For a savings-share or usage bill, charge on the reconciled number, not the raw
table. The table figure is for the live dashboard, the estimate, and the prepaid
hard-stop. The meter of record for a charge is the reconciled ledger, which now
matches the provider's own invoice.
