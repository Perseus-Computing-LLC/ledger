# Ledger revamp — independent verification report (Hermes)

**Reviewer:** Hermes Agent (independent second pass)
**Date:** 2026-07-11
**Object under review:** Claude Code's 2026-07 revamp of `Perseus-Computing-LLC/ledger`
(kickoff prompt `prompts/ledger-revamp-2026-07.md`), landed as merged PR **#97** +
open docs PR **#98**.
**Method:** cloned the repo fresh, checked out both `main` and the #98 head, read the
source, and re-ran the full suite on Linux. Every claim below is backed by tool output
I produced myself, not by trusting the revamp's self-report.

---

## 1. Bottom line

The revamp is **substantively real and high quality.** This is not a docs-varnish pass —
the headline engineering claim (per-model spend attribution) is correctly implemented,
the security-hardening release it builds on is genuine, and the review doc's findings
reproduce against source. I found **no fabricated results.** Two things are worth your
attention before merge and are covered in §5.

Independent verdict on the four phases:

| Phase | Claim | My verification | Status |
|-------|-------|-----------------|--------|
| P1 Deep review | `docs/REVIEW-2026-07.md`, 284 passing, carryover closed | Read in full; reran suite → **300 passed** on Linux (284 was pre-#97 Windows tree); spot-checked money + security findings against source | **Confirmed** |
| P2 Attribution | #97 consumes v17 `session_model_usage` w/ fallback | Read `ledger.py:201-320`; prefers `session_model_usage`, COALESCE-style fallback to aggregate `sessions` for pre-v17; attribution tests present and green | **Confirmed** |
| P3 Partner hunt | ranked CSV + first-moves doc | 18-row CSV with the required columns; MCP-server-as-enabler thesis is sound; assumption corrections (Langfuse is a data source, MCP servers README closed to PRs) are accurate | **Confirmed** |
| P4 Awareness | positioning + launch assets | Positioning one-pager is tight; four-in-one framing and dogfooding narrative are defensible | **Confirmed** |

---

## 2. What actually shipped (verified state)

- **Merged (#97, on `main`):** per-model spend attribution. `_provider_spend` /
  breakdown logic in `ledger.py` now prefers the Hermes schema-v17
  `session_model_usage` table and falls back to the aggregate `sessions` row when the
  table is absent (pre-v17 / un-backfilled), so totals never regress. This is the exact
  contract the kickoff prompt asked for, and it mirrors the upstream COALESCE fallback.
- **Open (#98):** docs-only, 6 files, +634/-0 — `REVIEW-2026-07.md`,
  `POSITIONING-2026-07.md`, `launch-assets-2026-07.md`, `partner-first-moves-2026-07.md`,
  `partner-targets-2026-07.csv`, README polish. No code, so it's safe to merge on docs
  quality alone.
- **Release lineage:** v1.0.1 security-hardening release (#92-#96) is real and merged:
  login-CSRF guard, HTTP hardening, money-atomicity, deploy fail-closed. The v0.7.1
  security carryover (#28/#30/#32/#33/#37) is closed **in code**, which I confirmed by
  reading the guards, not just the tracker.
- **Test suite:** I ran `pytest tests/ test_ledger.py -q` on `main` → **300 passed in
  21.77s**, zero failures. The review's "284" was the pre-attribution Windows tree; the
  delta is the new `test_hermes_attribution.py` cluster.

---

## 3. Independent confirmation of the version discrepancy

The kickoff prompt flagged a v1.0.0-vs-v0.1.1 version conflict. I reproduced the exact
state:

- `ledger_agent.__version__` → **1.0.1** (single-sourced into the wheel; correct)
- `ledger.py` line 785 → `VERSION = "0.1.1"` (**stale**, standalone monitor)
- `openapi.yaml` line 4 → `version: "1.0.0"` (**drifted** from package 1.0.1)

So three version literals coexist. The review correctly catches all three (findings
P3/P4/P6) and reasonably argues `ledger.py` *may* be an independently-versioned line —
but leaving it at `0.1.1` reads as rot to any outside reviewer or acquirer doing
diligence. **Recommend closing this before any external security review or awareness
push** (see §5, my only substantive add).

---

## 4. Where I agree with the review's punch list

All seven punch-list items (P1-P7) reproduce and are correctly severity-rated. The two
that actually matter:

- **P1 (Medium) — embedded Meter SDK doesn't enforce the prepaid hard-stop.**
  `Meter.track()` never threads `block_over_balance`, so it defaults `False`; a process
  using the local SDK debits past zero. The hard-stop is hosted-HTTP-only. This is a
  **product-truth gap**: "prepaid credit enforcement" is a headline feature, and it does
  not hold on the embedded path. Either thread the config through or explicitly document
  local mode as unmetered-by-design — but pick one before the awareness push, because a
  sharp reader will test exactly this.
- **P2 (Medium) — no concurrency/load test.** The server is threaded and money
  correctness rests on `BEGIN IMMEDIATE` serialization, yet every atomicity test is
  single-threaded (crash simulated by monkeypatch + serial replay). The roadmap's own
  exit criteria for #28/#30 asked for a real concurrency test. This is the single most
  valuable test to add, and **it's also the natural home for the Lambda credits** (§6).

---

## 5. My one substantive addition beyond the revamp

The revamp is thorough on correctness and distribution but treats the **version-literal
cleanup and the awareness-push gating as separate low-priority items.** They're coupled.
The dogfooding narrative ("we authored the upstream fix, it made our product more
accurate") is the strongest awareness asset you have — but it invites people to *look at
the repo*. The moment they do, `ledger.py --version → 0.1.1` on a "v1.0.1" product
undercuts the credibility the narrative just built.

**Recommendation:** before Phase 4 goes outward, land a tiny follow-up PR that
single-sources `ledger.py` and `openapi.yaml` from `ledger_agent.__version__` (with a
stdlib fallback for the standalone tools) and extends `test_version_single_source.py`
(P6) to guard all four literals. It's a 20-minute change that removes the one thing a
diligence reader would screenshot. I can open that PR on request.

---

## 6. Lambda credits — where the untouched ~$7,000 earns its keep

You have roughly **$7,000 of unused Lambda Cloud credit** (CTAN grant, acct
`perseus@perseus.observer`; the kit at `lambda-kit/` already has provisioning,
persistent-FS, and teardown wired). Two Ledger-specific uses, ranked by leverage:

### 6a. Fund P2 — the concurrency/load proof (highest fit, low cost)
Ledger's money-correctness story is currently **asserted, not stress-proven.** A
multi-GPU-adjacent load box isn't needed for the DB contention test itself, but the
*driver* — thousands of concurrent `/v1/usage` + webhook requests hammering one org —
benefits from a real multi-core cloud instance to expose lost writes / double-counts
that a laptop can't surface. Spin a cheap CPU-heavy or single-A100 instance, run an
N-thread soak against the threaded server, assert exact final balance. Cost: a few
dollars of the credit. Payoff: converts "we believe the ledger is atomic" into "we
proved it under contention," which is exactly what an acquirer or enterprise buyer
diligences. **This is the highest-ROI use of the credit for Ledger specifically.**

### 6b. Generate a *real* cost-attribution corpus for the dogfooding story
The attribution fix's whole pitch is "mid-session model switches were mis-attributed."
Right now that's demonstrated on synthetic/local `state.db` data. Lambda credit can host
a genuine multi-model agent workload (Ollama-served models on GPU, mid-run model
switches) that produces a real Hermes `state.db`, then show Ledger's per-provider spend
**before vs after** #97 on that real data. That before/after delta chart is a far
stronger awareness asset than a prose claim — it's the visual proof the positioning doc
is currently missing. The `lambda-kit/` Ollama-on-GPU harness already does most of this;
it needs a Ledger consumer bolted on.

### 6c. Note on scope — keep Ledger and Perseus Vault distinct
The credit has been feeding the **Perseus Vault** benchmark campaign (Gauntlet, fleet
throughput, 10k-entity recall). Ledger is a *different* product (billing layer, not
memory). Don't let the two campaigns blur: the Vault benchmarks answer "is retrieval
good and cheap"; the Ledger Lambda spend above answers "is the money ledger correct under
load." Both are legitimate, but they're separate line items against the same $7k.

**Suggested split:** cap Ledger Lambda spend at ~$200-300 (6a is cheap, 6b is a
few-hour GPU run), leave the bulk of the $7k for the Vault dynamic-range campaign that's
already producing publishable numbers.

---

## 7. Recommended next actions (in order)

1. **Merge #98** — docs are accurate and well-written; no code risk. (Your call; I
   verified content, you handle the merge button per your workflow.)
2. **Land the version-single-source follow-up** (§5) before awareness goes outward. I
   can open this PR now.
3. **Write the P2 concurrency test and run the soak on a cheap Lambda instance** (§6a) —
   highest-leverage correctness proof, trivial cost.
4. **Fix or document P1** (embedded SDK hard-stop) — product-truth gap that a reader
   will test.
5. **Optional / higher-effort:** the real before/after attribution corpus on Lambda
   (§6b) as the hero awareness asset.

---

## Appendix — verification commands I ran

- `gh repo clone Perseus-Computing-LLC/ledger` → 92 tracked files, HEAD `b568f17`
- `git log --oneline` → confirmed #97 merged, v1.0.1 lineage (#92-#96)
- `gh pr view 98` → docs-only, 6 files, +634/-0
- `python3 -c "import ledger_agent; print(ledger_agent.__version__)"` → `1.0.1`
- `grep -n VERSION ledger.py` → `0.1.1` (line 785); `openapi.yaml` → `1.0.0` (line 4)
- `grep -n session_model_usage ledger.py` → prefer-v17-with-fallback logic at 201-320
- `pytest tests/ test_ledger.py -q` → **300 passed in 21.77s**
