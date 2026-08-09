# Roadmap — efficiency leakage & policy adherence

Ledger measures efficiency **achieved**. It should also measure efficiency
**leaked** — the turns where Claude Code / Hermes / Perseus did *not* use their
routing, tools, or memory the way they were configured to. Together those two
halves are the honest, complete efficiency picture, and they yield the metric
that turns a nice number into an actionable one: the **adherence rate**.

## The two halves

| | Definition | Answers |
|---|---|---|
| **Efficiency achieved** *(shipped)* | `flagship_value − actual_cost` | "What did routing / local models / subscriptions save me?" |
| **Efficiency leaked** *(this roadmap)* | `actual_cost − optimal_cost` | "What did I leave on the table because a turn ran off-policy?" |

- **`flagship`** = the counterfactual "without Perseus you'd have run the best API model" (already metered as `baseline_micros`).
- **`optimal`** = the cheapest option the configured policy *would* have picked and that still cleared the quality bar.
- **Adherence rate** = share of turns where `actual == optimal` (no leak). This is the headline quality signal — for a customer ("your stack followed policy 87% of the time, the other 13% cost you $Y"), and for a provider ("Claude Code used its tools correctly N% of the time").

Why it matters: "you saved $X" is a one-sided number. Pairing it with "and here's
$Y you missed, on these turns, because the agent ran the flagship when your policy
said route down" is what a customer can *act on*. And a neutral, **verifiable**
meter of tool/routing adherence is something a provider would want to be measured
by — which is on-thesis (see the efficiency-meter thesis).

## Tiers

### Tier 1 — routing leakage — **near-term, buildable**
The router already knows what its policy would have chosen. Meter each event with
an `optimal_model` (or `optimal_cost_usd`) alongside the existing `baseline_model`;
the server prices it from the published table (exactly like the baseline). Then:

- **leaked** = `Σ max(0, actual − optimal)` over events that carry an optimal.
- **adherence** = fraction of policy-covered events where `actual ≤ optimal` (on-policy).
- Surfaced in `ledger savings` / `ledger efficiency` next to achieved savings.

This reuses the `baseline_micros` machinery (nullable, hash-chained, optional
trailing chain field) — no new mechanism, just a second counterfactual. It
answers the question directly for the two products we control (Hermes routing,
Perseus). Wiring the router to *emit* `optimal_model` on deviation is the
follow-up (same staging as the baseline bridge).

### Tier 2 — Perseus context leakage — **medium**
Perseus's core is resolve-before-context. So "turns that re-fed full context
instead of resolving from memory" is a measurable Perseus inefficiency — Perseus
knows when it did *not* resolve. Emit a per-turn `resolved` / `context_tokens_saved`
signal; leak = the context tokens that should have been trimmed but weren't.

### Tier 3 — general tool-adherence — **research / roadmap**
"The model should have called a tool but hallucinated instead" (esp. Claude Code)
requires an evaluator to establish the *right* action for each turn — an
LLM-judge/eval layer, fallible and to be labelled as such. This is genuine R&D.

## The rule that keeps it honest

**Never fabricate the negative signal.** We only record a deviation the harness
can actually attest: the router *knows* it ran off-policy; Perseus *knows* it
skipped a resolve. A Tier-3 judge is a separate, fallible layer and its verdicts
are marked as estimates, not facts. The whole value of the meter is that its
numbers are reconstructable — the leakage number has to meet the same bar as the
savings number.

## Unifying design

Every integration emits, per turn, not just *what it did* (tokens, model) but
*whether it followed its own policy* — a `deviation` / `optimal` signal next to
the usage it already sends. Same ingest, one more optional field.
