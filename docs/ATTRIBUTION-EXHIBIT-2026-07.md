# Attribution before/after exhibit (Task 4)

The #97 dogfooding pitch is "mid-session model switches were mis-attributed, we
fixed it upstream and consumed the fix." `tools/attribution_exhibit.py` turns that
claim into a chart + table computed directly from a Hermes `state.db`:

- **before** = the old aggregate (`sessions` row grouped by `billing_provider`),
  which puts every session's whole cost on the provider it *started* on;
- **after** = the #97 per-model attribution
  (`plutus_agent.hermes.read_spend_events`), which allocates each session's
  authoritative cost across the providers that actually served each call.

The total is identical before and after (attribution moves cost between
providers, it never invents or drops it) — the visual is the redistribution.

## The real (hero) asset — human-gated

Point the tool at a **real** Hermes `state.db` that contains schema-v17
`session_model_usage` rows from genuine mid-session model switches:

```
python tools/attribution_exhibit.py --state-db /path/to/state.db --label hermes-prod
# writes docs/exhibits/attribution-before-after-hermes-prod.{md,svg}
```

Two ways to get that real DB:

1. **Lambda multi-model workload** (the follow-up's Task 4 intent): run an agent
   on Lambda that switches models mid-session (Ollama-served models on GPU),
   producing a real `state.db`, then run the tool on it. Left to the human: it
   needs a GPU instance, and the Lambda credits are earmarked for the Perseus
   Vault campaign (keep any Plutus spend ≤ ~$1,000 and terminate on completion).
2. **greg's Hermes**, once its deployment is on schema v17 (the upstream
   `session_model_usage` feature). Today greg's DB was not reachable for a
   read-only probe from this environment and its schema version is unconfirmed,
   so this is a verify-then-run step, not something to assume.

Not run here on purpose: producing the real asset needs infrastructure the human
owns, and fabricating a "real" workload would violate the never-fabricate rule.

## Illustrative format demo (synthetic, committed)

To show the exhibit format, `--demo` runs on a small synthetic `state.db` (two
sessions that switch anthropic to openai mid-flight, plus a clean deepseek
session). The output is stamped ILLUSTRATIVE/SYNTHETIC so it can't be mistaken for
real data. Committed at
[`docs/exhibits/attribution-before-after-demo.svg`](exhibits/attribution-before-after-demo.svg)
and `.md`:

| provider | before ($) | after ($) | delta ($) |
|---|---|---|---|
| anthropic | 1.8000 | 1.2000 | -0.6000 |
| deepseek | 0.2000 | 0.2000 | +0.0000 |
| openai | 0.0000 | 0.6000 | +0.6000 |
| total | 2.0000 | 2.0000 | (preserved) |

Before, the provider a session switched *to* (openai) is invisible and its cost
is wrongly piled on anthropic; after, it lands where it belongs, and the clean
single-provider deepseek session is unchanged. That is the same shape the real
asset will show, on real numbers.
