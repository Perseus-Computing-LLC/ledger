# Partner first moves — 2026-07

Concrete first move for the top targets in `partner-targets-2026-07.csv`. Each is
scoped to a spike or a single PR/listing. Surfaces verified against live docs
(2026-07-11); see the CSV for source URLs.

## 0. The enabler — Ledger MCP server (do this first)

Six of the marketplace listings (official registry, awesome-mcp-servers,
PulseMCP, Glama, Smithery, mcp.so) require a working MCP server. Build it once and
all six unlock. It also *is* the deep integration for the whole MCP ecosystem:
agents get spend/billing as callable tools.

Proposed tools (thin wrappers over `ledger_agent.Meter` + `ledger.py --json`):

- `meter_usage(provider, model, task_type, input_tokens, output_tokens, cost_usd?)`
  → records a usage event, returns the new balance. (`Meter.track`)
- `get_balance(org?)` → remaining prepaid credit. (`Meter.balance`)
- `get_spend(window)` → per-provider/workspace spend for today/7d/30d/all.
- `get_runway()` → per-provider days-left + burn (reads `ledger.py --json`).
- `topup(amount)` → add prepaid credit (guarded; Stripe in prod). Mark
  `destructiveHint`/write in the tool annotations.

Safety annotations matter for the Anthropic directory: reads are
`readOnlyHint: true`; `topup` is a write. Ship a privacy policy (its absence is an
instant reject in the Anthropic portal).

`server.json` sketch (for the official registry; README must contain a matching
`mcp-name`):

```json
{
  "name": "io.github.perseus-computing-llc/ledger",
  "description": "Self-hosted billing, prepaid credit, and runway for AI agents",
  "version": "1.0.0",
  "packages": [
    { "registry": "pypi", "name": "ledger-agent" }
  ]
}
```

First move: a `mcp/` spike in this repo exposing the read tools (`get_balance`,
`get_spend`, `get_runway`) over stdio first, then Streamable-HTTP for the hosted
directories. Estimate: M.

## 1. Official MCP Registry (publish)

Once the server exists:

1. Add the `mcp-name: io.github.perseus-computing-llc/ledger` string to the
   README.
2. `mcp-publisher init` → `mcp-publisher login github` → `mcp-publisher publish`.

Namespace ownership is validated via GitHub OIDC, so publish from the org repo.
Estimate: S. This is the single highest-ROI listing because downstream
directories ingest from it.

## 2. LiteLLM custom callback (deep)

Ship a small package with a `CustomLogger` that forwards each call's cost to
Ledger:

```python
from litellm.integrations.custom_logger import CustomLogger
import litellm

class LedgerLogger(CustomLogger):
    def __init__(self, meter):           # a ledger_agent.Meter (local or remote)
        self.meter = meter

    async def async_log_success_event(self, kwargs, response_obj, start, end):
        usage = getattr(response_obj, "usage", None) or {}
        self.meter.track(
            provider=kwargs.get("custom_llm_provider") or "unknown",
            model=kwargs.get("model"),
            task_type="litellm",
            input_tokens=getattr(usage, "prompt_tokens", 0),
            output_tokens=getattr(usage, "completion_tokens", 0),
            cost_usd=kwargs.get("response_cost"),   # LiteLLM already priced it
        )

litellm.callbacks = [LedgerLogger(meter)]
```

First move: publish `litellm-ledger` (or fold into `ledger_agent.integrations`),
then a docs PR to `BerriAI/litellm` observability page. Positioning note below.
Estimate: M.

## 3. Helicone webhook receiver (deep)

Helicone posts per-request objects with `cost`, `promptTokens`, `completionTokens`.
A tiny receiver maps that onto `POST /v1/usage`:

```python
# maps a Helicone webhook payload -> a Ledger usage event
def on_helicone_webhook(payload):
    body = payload["request_response_body"]  # or top-level fields per Helicone schema
    return {
      "provider": payload.get("provider", "unknown"),
      "model": payload.get("model"),
      "input_tokens": payload["promptTokens"],
      "output_tokens": payload["completionTokens"],
      "cost_usd": payload["cost"],
      "source": "helicone",
    }
```

First move: add `examples/helicone_webhook.py` + a docs section; no partner PR
needed (integration is webhook config). Estimate: S.

## 4. LangChain callback + docs PR (deep)

```python
from langchain_core.callbacks import BaseCallbackHandler

class LedgerCallback(BaseCallbackHandler):
    def __init__(self, meter): self.meter = meter
    def on_llm_end(self, response, **kw):
        for gen in response.generations:
            msg = getattr(gen[0], "message", None)
            um = getattr(msg, "usage_metadata", None) or {}
            self.meter.track(provider="unknown", model=response.llm_output.get("model_name"),
                             task_type="langchain",
                             input_tokens=um.get("input_tokens", 0),
                             output_tokens=um.get("output_tokens", 0))
```

First move: publish `langchain-ledger` to PyPI, then a docs-only PR to
`langchain-ai/docs` adding an integration page (bootstrap with
`langchain-cli integration create-doc`). Code PRs into langchain core are not
accepted. Estimate: M.

## 5. n8n community node (marketplace, unverified first)

Publish `n8n-nodes-ledger` (npm) with keyword `n8n-community-node-package`,
exposing nodes for "meter usage", "get balance", and a "low balance" trigger.

Gotcha: chasing *verified* status after 2026-05-01 requires GitHub-Actions publish
with npm provenance and **no runtime dependencies** — hard if the node calls the
Ledger HTTP API via a client lib. Ship unverified first (stdlib `fetch` only, no
deps), pursue verification later. Estimate: M.

---

## Runway router: overlap vs. complement for LLM gateways/routers

The pitch against gateways (LiteLLM, OpenRouter, Portkey, Helicone) must be
precise, because they already do parts of this.

- **They route on latency/price/health per request.** Ledger's router ranks
  providers by **projected days-left of credit (runway)** and rebalances your
  flagship model onto the provider you can most afford to keep using. That is a
  budget-horizon decision, not a per-request one.
- **They track cost; few enforce prepaid credit.** Ledger adds an append-only
  prepaid-credit ledger with a hard-stop, plus Stripe top-ups. Gateways generally
  assume you pay the provider directly.
- **Positioning line:** "Billing + credit runway they don't have." Ledger sits
  *beside* the gateway: the gateway executes calls, Ledger meters them, holds the
  prepaid balance, and tells the gateway (or your router) which provider has the
  most runway. LiteLLM/Helicone are the cleanest to co-sell with because they
  already emit per-call cost — Ledger is the natural sink for it.
- **Where NOT to overclaim:** Ledger does not proxy calls, do failover, or manage
  API keys. If a prospect wants request-level routing, that's the gateway's job;
  Ledger makes the budget-level decision on top.
