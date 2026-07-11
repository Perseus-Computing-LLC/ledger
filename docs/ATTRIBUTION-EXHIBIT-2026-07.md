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

## The real (hero) asset — run on Lambda (2026-07-11)

Produced from a genuine multi-model workload on a Lambda A10 GPU. Two locally
served models (`qwen2.5:1.5b` and `llama3.2:1b` via Ollama) ran 20 agent sessions,
each doing real mid-session model switches, recorded through Hermes' OWN v17
accounting code (`SessionDB.update_token_counts` -> `_record_model_usage`). Token
counts are real inference (qwen: 2,340 in / 12,795 out; llama: 2,180 in / 4,331
out); the two model backends are labelled as distinct `billing_provider`s to
mirror a multi-provider route, and per-token costs are representative small-model
rates. Committed at
[`docs/exhibits/attribution-before-after-lambda-real.svg`](exhibits/attribution-before-after-lambda-real.svg)
and `.md`:

| provider | before ($) | after ($) | delta ($) |
|---|---|---|---|
| ollama:llama | 0.0000 | 0.0033 | +0.0033 |
| ollama:qwen | 0.0063 | 0.0030 | -0.0033 |
| total | 0.0063 | 0.0063 | (preserved) |

This is the bug in real numbers: `llama` served 4,331 real output tokens of work,
but **before** the fix it shows **$0** — every session's cost is piled on `qwen`,
the model each session *started* on. **After**, the $0.0063 splits ~50/50 onto the
providers that actually did the work, and the total is preserved exactly. (Dollar
magnitudes are small because these are tiny local models; the redistribution is
what the fix is about.)

The tool runs on any real Hermes `state.db`:

```
python tools/attribution_exhibit.py --state-db /path/to/state.db --label hermes-prod
```

so it can also be pointed at greg's Hermes once that deployment is on schema v17
(its DB was not reachable for a read-only probe from this environment, so that is
a verify-then-run step).

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
