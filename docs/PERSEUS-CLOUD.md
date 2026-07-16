# Perseus Cloud — Unified Offering

Perseus Cloud bundles the full stack — context engine, persistent memory, and
billing — into a single hosted product. One signup, one bill, one dashboard.

## Why bundle?

The three products are tightly coupled in the value chain:

```
Perseus (context engine) → resolves what the agent needs before the LLM call
     ↓
Perseus Vault (memory)   → retrieves relevant past context, no re-sending
     ↓
Plutus (billing)         → meters the spend, proves the savings, collects payment
```

A customer using only one piece gets partial value. A customer using all three
gets the full savings proposition — and Plutus proves it. Bundling aligns the
product with the monetization: you pay for what Perseus+Vault saves you, and
Plutus is the meter that proves it.

## Tiers

| Tier | Price | Includes |
|---|---|---|
| **Free** | $0 | Perseus context engine (local), Perseus Vault (local, up to 10K entities), Plutus metering (unlimited), 1 seat |
| **Pro** | $20/mo | Everything in Free + Vault (unlimited entities), full Plutus reporting, no savings-share |
| **Pro Team** | $50/mo | Pro for up to 5 seats + attribution by workspace, 25 workspaces (per-user attribution tracked in #136) |
| **Team** | $10/seat/mo + 10% savings-share | Pro Team at scale + mandatory savings-share on verified Perseus savings |
| **Enterprise** | Custom | Self-hosted or dedicated, SSO, SLA, negotiated terms |

## How it works

1. **Sign up** at plutus.perseus.observer — one account covers all three products
2. **Install** the Perseus CLI (`pip install perseus-ctx`) and wire it to your agent
3. **Point your agent** at the hosted Vault endpoint for persistent memory
4. **Plutus meters automatically** — every LLM call is tracked, every saving is proven
5. **Pay monthly** — subscription floor + savings-share (Team tier only)

## Self-hosted vs Cloud

| | Perseus Cloud | Self-Hosted |
|---|---|---|
| Perseus (context) | ✓ Included | `pip install perseus-ctx` (free, MIT) |
| Perseus Vault | ✓ Hosted, managed | Build from source (free, MIT) |
| Plutus (metering) | ✓ Included | `pip install plutus-agent` (free, MIT) |
| Stripe billing | ✓ Managed | Bring your own Stripe keys |
| Dashboard | ✓ Hosted at plutus.perseus.observer | Self-host `plutus serve` |
| Support | ✓ Email + community | Community (GitHub Issues) |

Everything is open-source (MIT). Perseus Cloud is the managed version — you're
paying for hosting, not for the software.

## The savings proposition

The core pitch: **Perseus Cloud pays for itself.**

A typical agent session without Perseus+Vault sends 50-200K tokens of repeated
context per turn. With Perseus+Vault, that collapses to 1-3K of relevant
retrieval. At Claude Opus prices ($15/$75 per 1M tokens), the savings are:

| Scenario | Without Perseus | With Perseus | Monthly savings |
|---|---|---|---|
| Solo dev, 100 turns/day | ~$450/mo | ~$45/mo | **$405/mo** |
| 5-person team, 500 turns/day | ~$2,250/mo | ~$225/mo | **$2,025/mo** |
| 20-person team, 2000 turns/day | ~$9,000/mo | ~$900/mo | **$8,100/mo** |

At Team tier ($10/seat + 10% savings-share), a 5-person team pays:
- $50/mo base (5 × $10) + $202.50/mo savings-share = **$252.50/mo**
- Net savings: $2,025 − $252.50 = **$1,772.50/mo** (88% net savings)

The product is self-funding from day one. This is the pitch.

## Go-to-market

1. **Free tier** — unlimited metering, local Vault (10K entities). Developers try it, see the efficiency number, upgrade.
2. **Pro ($20/mo)** — solo power users who want full reporting depth.
3. **Pro Team ($50/mo)** — small teams that need attribution but aren't ready for savings-share.
4. **Team ($10/seat + 10%)** — the revenue tier. Companies that see real savings and are happy to share 10% of what Perseus saved them, because the net is still 90% in their pocket.
5. **Enterprise** — large orgs with custom needs, negotiated directly.

## Implementation status

- [x] Perseus context engine (v1.0.22, PyPI)
- [x] Perseus Vault (v2.20.2, binary + source)
- [x] Plutus billing engine (v1.0.1, PyPI + GHCR)
- [x] Stripe integration (prepaid credits, Pro subscriptions, savings-share invoicing)
- [x] Hermes bridge (auto-baseline tagging for savings-share)
- [x] Five-tier pricing (Free / Pro / Pro Team / Team / Enterprise)
- [ ] Hosted signup flow (PLUTUS_ALLOW_SIGNUP + production deploy)
- [ ] External security review (see docs/SECURITY-REVIEW-SCOPE.md)
- [ ] Landing page (see plutus/index.html)
- [ ] Public launch (Show HN, r/LocalLLaMA, etc.)
