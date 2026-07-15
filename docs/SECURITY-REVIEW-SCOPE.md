# Perseus Cloud — Security Review Scope

> Prepared for external security review of the Plutus public surface.
> Target: v1.0.1+ · Date: 2026-07-12

## Scope

The review covers the **public-facing attack surface** of a hosted Plutus instance
configured for open signup with live Stripe billing. Internal-only paths (Hermes
monitor, local CLI usage) are out of scope.

### In Scope

| Surface | Path | Risk |
|---|---|---|
| Ingest API | `POST /v1/usage` | API-key auth, body limits, DoS, injection in event fields |
| Signup flow | `POST /auth/signup`, `GET /auth/login` | Rate limiting, org creation caps, OIDC token validation |
| Stripe webhooks | `POST /webhook/stripe` | Signature verification, idempotency, replay protection |
| Billing checkout | `POST /billing/checkout/*` | CSRF, amount validation, org binding |
| Dashboard | `GET /` (authenticated) | XSS in report rendering, HTML injection in org/workspace names |
| Static assets | `GET /assets/*`, `GET /pricing` | Path traversal |
| OIDC callback | `GET /auth/oidc/callback` | RS256 JWT verification, redirect validation, state parameter |

### Key Areas for Penetration Testing

1. **Money path (`/v1/usage` → `record_usage` → `add_ledger`)**
   - Can a negative `cost_usd` mint credit? (Guard: `cost_usd < 0` → ValueError before debit)
   - Can a forged `baseline_cost_usd` inflate billable savings? (Guard: hash-chained, recomputable from published prices)
   - Does the `BEGIN IMMEDIATE` serialization hold under concurrent events + webhooks?
   - Can an attacker exhaust prepaid credit past zero via race? (Guard: micro-dollar integer comparison inside the transaction)

2. **Webhook idempotency (`POST /webhook/stripe`)**
   - Can a replayed webhook double-credit? (Guard: `stripe_events` unique + `BEGIN IMMEDIATE` atomic claim-and-apply)
   - Can a forged webhook bypass signature verification? (Guard: `stripe.Webhook.construct_event` with `webhook_secret`)
   - Refund/dispute convergence: does repeated partial refund correctly converge without double-reversing?

3. **OIDC authentication (`/auth/oidc/*`)**
   - Is the RS256 signature verified on EVERY token (not just at login)?
   - Is the `state` parameter validated to prevent CSRF on the callback?
   - Can an attacker forge a JWT with `alg: none`? (Guard: explicit RS256 requirement)
   - Session fixation: does login rotate the session token?

4. **CSRF (`/billing/checkout/*`, state-changing POSTs)**
   - Is the per-session CSRF token validated on all state-changing endpoints?
   - Does the token rotate on login?
   - SameSite cookie attribute?

5. **Injection / XSS**
   - Org names, workspace names, and provider/model strings rendered in dashboard HTML
   - 404 path reflection (already escaped per #37)
   - Error message leakage: do 500s expose stack traces or DB details?

6. **Rate limiting / abuse**
   - Signup rate: hourly + daily caps enforced at DB level?
   - Ingest rate: per-org, per-API-key?
   - Body size: `POST /v1/usage` capped? (Guard: `MAX_CONTENT_LENGTH`)
   - Webhook: Stripe signatures verified before body read?

### Out of Scope

- Internal Hermes monitor (`plutus.py`, `plutus_route.py`) — not exposed publicly
- Perseus Vault MCP server — separate binary, separate review
- Physical/infrastructure security of the hosting environment
- Stripe's own API security (their responsibility)

## Pre-Review Checklist (self-audit before handing off)

- [ ] `billing.stripe_webhook_secret` is set and non-empty before `plutus serve` starts
- [ ] `auth.base_url` is set to the production domain (CSRF origin check)
- [ ] `PLUTUS_ALLOW_SIGNUP=1` is set (otherwise signup is disabled, reducing attack surface)
- [ ] `MAX_CONTENT_LENGTH` (default 1MB) is appropriate for `/v1/usage`
- [ ] Stripe is in **test mode** during the review (`sk_test_...`)
- [ ] All secrets are injected via environment variables, not committed to config files
- [ ] Server runs behind HTTPS (TLS termination at reverse proxy)
- [ ] `STRIPE_WEBHOOK_SECRET` is rotated after the review

## Review Artifacts Requested

1. Written report with findings ranked by severity (Critical / High / Medium / Low)
2. Proof-of-concept for any Critical or High findings
3. Recommended remediation per finding
4. Re-test confirmation after fixes applied

## Post-Review

After the review and remediation:
1. Rotate all API keys and webhook secrets
2. Switch Stripe from test mode to live mode
3. Enable `PLUTUS_ALLOW_SIGNUP=1` if not already
4. Deploy behind production HTTPS
5. Schedule follow-up review in 6 months or after major feature release
