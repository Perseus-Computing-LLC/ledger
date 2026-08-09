# Cost reconciliation

Ledger meters what your agents spend, but the wired providers return token
counts, not dollars. `record_usage` turns tokens into dollars using the static
table in `ledger_agent/pricing.py` and marks the row `estimated = True`. That is
correct for a live estimate and the prepaid hard-stop, but it can drift from
what a provider actually charged (price changes, committed-use discounts,
enterprise rates, batch or cache pricing). You should not send an invoice built
on the table.

Reconciliation closes the gap: at the end of a billing period you feed Ledger
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
   ledger reconcile --org acme --period 2026-07 --totals totals.json
   ```

4. Apply when the numbers look right:

   ```
   ledger reconcile --org acme --period 2026-07 --totals totals.json --apply
   ```

   For a single provider you can skip the file:
   `ledger reconcile --period 2026-07 --provider openai --amount 812.44 --apply`.

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

## Automated close: fetch → reconcile (`ledger close`)

`ledger reconcile` needs the authoritative totals handed to it. `ledger close`
(#109) fetches them for you and reconciles in one step — the piece that lets a
cron job true up billable dollars after month end without manual export:

```
# dry run for the previous month (the default period), all providers with usage
ledger close --org acme

# apply, for a specific month and provider set
ledger close --org acme --period 2026-07 --providers openai,anthropic --apply
```

- **Period** defaults to the *previous* month (UTC), so a job that runs on the
  1st trues up the month that just ended. Override with `--period YYYY-MM`.
- **Providers** default to those with recorded usage in the period. Pass
  `--providers` to also true up a provider that billed but wasn't metered.
- **Never zeros on a failed fetch.** If a provider's cost API can't be reached
  (missing key, API error, unrecognized response), that provider is reported
  under "could not fetch" and left untouched — same rule as a missing manual
  total. `ledger close` exits non-zero when any requested provider failed, so a
  cron job surfaces the gap instead of silently under-reconciling.
- The written `adjust` entries land in the ledger and show up on the dashboard
  balance like any other reconciliation.

### Cron (the greg Hermes deployment target)

```cron
# 06:00 UTC on the 2nd of each month: true up the month that just closed.
0 6 2 * *  OPENAI_ADMIN_KEY=... ANTHROPIC_ADMIN_KEY=... \
           ledger close --org acme --apply --json >> /var/log/ledger-close.log 2>&1
```

## Getting the authoritative total per provider

Either let `ledger close` fetch it (above) or produce the normalized totals file
yourself from the provider's own export. The authoritative sources — and the
credentials `ledger close` uses for each:

- **OpenAI** — the organization Costs API (`/v1/organization/costs`). Needs an
  **admin/organization** key in `OPENAI_ADMIN_KEY` (falls back to
  `OPENAI_API_KEY`). Install: no extra (stdlib only).
- **Anthropic** — the organization cost report (`/v1/organizations/cost_report`).
  Needs an **admin** key in `ANTHROPIC_ADMIN_KEY` (falls back to
  `ANTHROPIC_API_KEY`). Install: no extra (stdlib only).
- **Bedrock** (`bedrock`/`aws`) — the AWS Cost Explorer API, filtered to the
  Amazon Bedrock service. Uses the standard AWS credential chain. Install:
  `pip install 'ledger-agent[fetchers]'` (boto3).

The fetchers reconcile only in the **billed currency** (USD) and fail loudly on a
non-USD figure rather than converting. They follow each provider's documented
cost-report schema; validate the first run against your provider console, since
the endpoints evolve. A fetcher that doesn't recognize the response raises rather
than guessing a number.

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
