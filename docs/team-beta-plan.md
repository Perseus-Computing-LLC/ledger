# Evidence-Gated Team Beta Plan (>10 seats)

**Status:** Planning only — do **not** activate paid self-service checkout.
**Tracking issue:** plutus#164 · **Sequence:** after plutus#163 (donation path) and the Cloud API activation instrumentation, before any self-serve subscription.

## Goal

Define an invite-only, manually provisioned Team beta for organizations with
11+ seats, gated on production evidence from the Free tier and the voluntary
donation path. This document is the definition of record; nothing in it
authorizes enabling checkout.

## Activation gates (all five must pass before implementation)

| # | Gate | Evidence required |
|---|------|-------------------|
| 1 | One successful **live voluntary donation** | Signature-verified `checkout.session.completed`, exactly one `donation` record in the hash-chained ledger, clean `/v1/admin/verify` (per #163 acceptance) |
| 2 | Activation data from **5–10 Free design partners** | Per-partner funnel: signup → verified email → API key → first metered usage event → second-week active (instrumented by Cloud API activation issue) |
| 3 | At least **three explicit requests** for >10 seats or advanced reporting | Dated request log (source, org, ask) attached to this issue |
| 4 | **Refund / cancellation / support policy** approved by the operator | Policy text committed to this repo under `docs/` |
| 5 | **Stripe test-mode coverage** | Seat quantity changes, failed payment, refund, cancellation, and webhook replay — all green in CI against test mode |

## Initial beta constraints

- **Teams with 11+ seats only.** Seats 1–10 remain Free; seat 11+ triggers the
  Team conversation (the dashboard already surfaces "Add seat 11 to start a
  Team upgrade").
- **Invite-only and manually provisioned.** Operator creates the org, sets the
  seat count, and applies the Team tier by hand. No self-serve upgrade form.
- **Pricing:** $20/seat/month for teams of 11+, per the canonical pricing
  contract on `/pricing` (merged in #157, deployed in #159).
- **No broad marketing and no self-serve subscription** until every gate above
  passes. Outreach language must not advertise paid checkout while it is
  disabled.
- **Savings-share posture:** Team is flat per-seat; the 10%
  independently-verified-savings share applies to Enterprise only. No automatic
  charges of any kind during the beta beyond the agreed seat invoice.

## Provisioning runbook (per invited team)

1. Confirm the invitation and seat count in writing (email thread is enough).
2. Operator creates/upgrades the org to Team with the agreed seats.
3. Configure Stripe in **test mode first**: create the subscription with the
   seat quantity, run the failed-payment / refund / cancellation / replay
   matrix, and attach the evidence to the gate-5 checklist.
4. Only after gate 5 evidence is attached for this provisioning flow, switch
   the subscription to live mode and send the invoice.
5. Record the deployment/version identifier for the provisioning change.

## Cancellation, refund, and support policy (to be approved — gate 4)

Draft for operator approval:

- Cancel anytime; service continues to the end of the paid period, then the org
  returns to Free (10 seats). No prorated charges after cancellation.
- Full refund within 14 days of the first invoice, no questions asked.
- Support via email with a 2-business-day first-response target during beta.
- Every refund or cancellation reverses the ledger via the existing
  `charge.refunded` / `customer.subscription.deleted` webhook path — the same
  idempotent, hash-chained path exercised in `tests/test_webhook_reversals.py`.

## Explicit non-goals for the beta

- No credit top-ups, no metered/usage-based billing, no automatic savings-share
  collection.
- No Enterprise self-serve; Enterprise remains contact-sales with negotiated
  verified-savings terms.
- No changes to the Free donation path (owned by #163).

## Done definition for this planning issue

This issue closes when this plan is merged. Implementation begins only after
all five gates pass, tracked as separate implementation issues referencing
this document.
