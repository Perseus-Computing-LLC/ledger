# Invarium × Plutus: accuracy-gated, per-task savings metering

**Status:** design + working prototype (this branch). Prototype verified end-to-end
against Plutus `1.0.1` and Invarium `0.3.1`.

## Why

Plutus's differentiated revenue path is the **savings share**: bill a percentage of
the money Perseus provably saved (`baseline_cost_usd − cost_usd`, hash-chained). The
headline claim from the certification work is "fewer dollars **at higher accuracy**."
The second half of that claim is the fragile one — Plutus meters *cost* precisely,
but it has no notion of whether the cheaper run was actually *correct*. A regression
that makes an agent skip a required tool usually makes it **cheaper**, so a naive
meter books a *larger* saving on a worse answer.

[Invarium](https://github.com/invarium-ai/invarium) ("pytest for AI agents") is the
missing correctness signal. It runs one `AgentResult` per task/question — carrying
`cost`, `latency`, and a free-form `metadata` dict — and returns a pass/fail verdict
from behavioral assertions (`used_tools_in_order`, step budgets,
`did_not_claim_confirmation_without_tool`, …), with a `bless`/`compare` baseline flow
for regression detection. Its per-run unit is exactly Plutus's per-event unit.

This doc specifies the convergence as one arc, plus the pieces already prototyped.

## Where the two systems meet (verified against source)

| Invarium `AgentResult` | Plutus `usage_events` (`db.py`) / `record_usage` |
|---|---|
| `cost: float` | `cost_usd` → `cost_micros` |
| `latency: float` | *(no column today — see shape C)* |
| `metadata["baseline_cost_usd"]` | `baseline_cost_usd` → `baseline_micros` (hash-chained, #7) |
| `metadata["task_id"]` (per question) | `external_ref` (schema v10 — shape A, done) |
| pass/fail verdict | *(caller-enforced gate — see shape B)* |
| test name / category | `task_type` (a bucket, e.g. `code_review`) |

Two facts from reading the code that shape the design:

1. **A withheld baseline already books $0.** `record_usage` records `baseline_micros
   = NULL` when no baseline is passed, and `savings.period_savings` only sums
   `max(0, baseline − cost)` over events *with* a baseline (`test_savings.py::
   test_no_baseline_means_zero_saving`). So *gating is just declining to pass a
   baseline* — no schema change required.
2. **The chain is already tamper-evident.** `usage_events` carries `prev_hash` /
   `row_hash` (schema v6, #108) with an optional HMAC key, and the baseline is folded
   in. Invarium does **not** add tamper-evidence (it isn't hash-chained either) — that
   remains Plutus's own track (`feat/chain-checkpoints-independent-verify`). This
   integration is about *correctness gating and attribution*, not tamper-evidence.

## The arc

### B — Accuracy-gated savings (highest leverage; prototyped, zero schema change)

Forward the counterfactual baseline to Plutus **only when the task passed its
Invarium contract**. Implemented in `plutus_agent/integrations/invarium.py`:

```python
from invarium import expect
from plutus_agent.integrations.invarium import meter_agent_result

check = expect(result, collect=True)          # collect: don't raise on first failure
check.used_tools_in_order(["retrieve", "answer"])
check.did_not_claim_confirmation_without_tool("retrieve")
try:
    check.verify()
    verified = True
except AssertionError:
    verified = False

meter_agent_result(conn, org_id, result, verified=verified,
                   provider="anthropic", model="claude-haiku-4-5",
                   task_type="q3_summary")
# verified → baseline flows through; not verified → baseline withheld → $0 saving
```

(A small upstream `expect(...).passed` / `verify(raise_on_fail=False)` would drop the
`try`/`except`; folded into the shape-C contribution.)

Prototype output (`examples/invarium_gated_savings.py`) — same two tasks, one
correct at $1.00 and one *cheaper-but-wrong* at $0.60, both against a $4.00 baseline:

```
NAIVE (meter everything)
  events booking savings: 2
  gross billable savings: $6.40      # $3.40 booked on a wrong answer

GATED (Invarium verdict)
  events booking savings: 1
  gross billable savings: $3.00      # only the verified task
```

**Optional first-class step (later):** add a `verified INTEGER` column so reports can
distinguish *verified savings coverage* from raw baseline coverage, and so
`bill-savings` can require verification. Not needed for the gate to work — the
conservative default already protects the number — but it makes the guarantee
auditable rather than caller-enforced.

### C — Cost/latency assertions upstream + a meter-accuracy regression suite

**Upstream primitives (in review — invarium PR #28):** Invarium declared
`cost_exceeded` and `latency_exceeded` in `FAILURE_CATEGORIES` but shipped no
assertion that emits them. PR #28 adds them:

```python
expect(result).cost_less_than(2.00)                 # -> cost_exceeded
expect(result).latency_less_than(4.0)               # -> latency_exceeded
# a two-sided cost_within(expected, tol) is the natural follow-on for regression pins
```

These let a *cost* regression fail a test the same way a behavioral one does, and
are the natural precondition folded into shape B's `verified`.

**Meter-accuracy regression suite ✅ implemented** (`examples/invarium_meter_regression.py`,
`tests/test_meter_regression.py`). Plutus's cost attribution is deterministic given
fixed token counts and a pinned price table, so a golden catalog of workloads is
`bless`ed and re-`compare`d on every change: a drift in the pricing table or the
baseline-derivation math surfaces as a **cost regression** on exactly the affected
model — Invarium testing the meter, not just the agent.

The mechanism is worth stating because it's non-obvious: `compare_reports` only
flags a regression when a test's **success rate drops**. So cost drift must fail an
assertion to be caught. Each golden workload therefore carries a two-sided cost pin
(metered cost must equal its frozen value); a price change trips the pin →
success 100%→0% → `compare` reports the regression *and* surfaces the `cost_delta`
under the `cost_exceeded` category. That interlock is exactly why the assertion (C's
upstream half) and the suite belong together. (The suite pins costs with a plain
two-sided check so it runs on released invarium; it does not depend on PR #28.)

```
Re-meter, no change        -> No regression detected across 4 matched test(s).
Re-meter after price bump  -> Regression detected in 1 of 4 matched test(s).
  [REGRESSION] meter[anthropic/claude-haiku-4-5]  success 100% -> 0%  cost delta $+1.0000  {cost_exceeded: 1}
```

### A — Per-task / per-question attribution ✅ implemented (schema v10)

Previously Plutus attributed by `task_type` (a category) and `workspace` (an org
grouping), with no per-question identifier. `usage_events` now carries a nullable
`external_ref` column (schema **v10**) — an opaque caller-supplied id such as an
Invarium `task_id`:

```sql
-- added additively in _migrate_add_columns; index created there too so it applies
-- to upgraded DBs (a fresh DB gets the column from SCHEMA).
ALTER TABLE usage_events ADD COLUMN external_ref TEXT;
CREATE INDEX IF NOT EXISTS ix_usage_extref ON usage_events(org_id, external_ref);
```

`external_ref` is an **optional trailing chain field** (`_CHAIN_FIELDS_OPTIONAL`),
so a NULL value reproduces the pre-v10 canonical form byte-for-byte — existing
chains still verify — while a set value is folded into the hash, so a billed
saving can't be silently re-pointed to a different task (see
`test_external_ref.py::test_tampering_with_external_ref_breaks_the_chain`).

It's threaded through `record_usage(..., external_ref=...)`, the `/v1/usage` body,
`plutus meter --ref`, and the CSV/JSON export, and the bridge sets it from
`metadata["task_id"]` for **every** event (attribution isn't gated — only the
savings baseline is). `db.events_by_ref(org, task_id)` is the join that closes the
loop from *billed saving* → the exact task that justified it.

## Recommended sequence

1. ✅ **B (the bridge)** — merged (#124): accuracy-gated savings, zero schema change.
2. **C (assertions + suite)** — upstream `cost_less_than`/`latency_less_than` in
   Invarium (PR #28), then a Plutus-side golden `bless`/`compare` suite that guards
   attribution.
3. ✅ **A (`external_ref`)** — schema v10 for native per-task queries and the
   billed-saving → verified-trace join (this change).

## Honest limits

- Invarium runs offline / in CI. This does **not** meter live production traffic; it
  makes the *tested* path's savings claim trustworthy and regression-guarded. Extending
  gating to production needs a runtime verification signal, which is a separate design.
- The gate is only as good as the contract. A weak assertion set passes weak agents;
  the savings figure inherits the test suite's rigor.
- No tamper-evidence is added here (see above).

## Prototype artifacts on this branch

- `plutus_agent/integrations/invarium.py` — the dependency-free bridge (shape B).
- `examples/invarium_gated_savings.py` — runnable naive-vs-gated demo.
- `tests/test_invarium_gated_savings.py` — 5 tests pinning the gate.
