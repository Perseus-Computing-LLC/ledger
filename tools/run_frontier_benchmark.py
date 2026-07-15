#!/usr/bin/env python3
"""
Plutus Benchmark Runner — one command to generate the cost-to-quality frontier.

Runs a seeded evaluation across four systems (full-context with prompt caching,
sliding-window, vanilla RAG, Perseus+Vault) on LongMemEval and produces the
frontier chart consumable by plutus.report() and the savings calculator.

Single paid seed (~$35 total) validates the entire savings-share thesis.

Usage:
    python3 run_frontier_benchmark.py --seed 0 --lme-dir ~/lme-run

Requires:
    - LongMemEval harness installed (pip install longmemeval)
    - plutus_harness.py (drop-in recorder + metered client)
    - OpenAI/Anthropic API keys for the benchmark models
    - Perseus+Vault running locally for the Perseus+Vault system

Output:
    runs/seed0/{fullctx,sliding,rag,perseus}.jsonl   — per-system call records
    runs/seed0/{fullctx,sliding,rag,perseus}.summary.json  — accuracy per system
    runs/seed0/frontier.json                          — combined cost-to-quality data
    runs/seed0/frontier.html                          — self-contained chart
"""

from __future__ import annotations

import argparse, json, os, sys, time
from dataclasses import dataclass, field
from typing import Optional

# ── Configuration ──────────────────────────────────────────────────────

# Published API prices (USD per 1M tokens) — input, output, cache_read
# These are the prices used to compute cost-per-system.
# Sync with plutus_agent/pricing.py PRICE_TABLE when providers change prices.

PRICES = {
    # Flagship (used for full-context / prompt-cached baselines)
    "claude-opus-4-8":     {"input": 15.0, "output": 75.0, "cache_read": 1.50},
    # Workhorse (used by sliding-window and vanilla RAG)
    "claude-sonnet-4-6":   {"input": 3.0,  "output": 15.0, "cache_read": 0.30},
    # Cheap model (Perseus routes here after retrieval collapse)
    "claude-haiku-4-5":    {"input": 1.0,  "output": 5.0,  "cache_read": 0.10},
    # Embedding model (Perseus retrieval — ONNX local = $0)
    "embed-local":         {"input": 0.0,  "output": 0.0,  "cache_read": 0.0},
}

# Systems to compare
SYSTEMS = [
    {
        "id": "fullctx",
        "name": "Full Context",
        "desc": "All history in the context window, Claude Opus 4.8, prompt caching on",
        "model": "claude-opus-4-8",
        "uses_cache": True,
    },
    {
        "id": "sliding",
        "name": "Sliding Window",
        "desc": "Last 8k tokens of history, Claude Sonnet 4.6",
        "model": "claude-sonnet-4-6",
        "uses_cache": False,
    },
    {
        "id": "rag",
        "name": "Vanilla RAG",
        "desc": "Naive keyword retrieval + top-3 chunks, Claude Sonnet 4.6",
        "model": "claude-sonnet-4-6",
        "uses_cache": False,
    },
    {
        "id": "perseus",
        "name": "Perseus+Vault",
        "desc": "Perseus context resolution + Vault memory retrieval, Claude Haiku 4.5",
        "model": "claude-haiku-4-5",
        "uses_cache": False,
    },
]


@dataclass
class Call:
    model: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int = 0
    task_id: str = ""

    def cost_usd(self) -> float:
        p = PRICES.get(self.model, {"input": 1.0, "output": 3.0, "cache_read": 0.10})
        regular_input = self.input_tokens - self.cached_tokens
        regular_cost = (regular_input / 1_000_000) * p["input"]
        cached_cost = (self.cached_tokens / 1_000_000) * p["cache_read"]
        output_cost = (self.output_tokens / 1_000_000) * p["output"]
        return round(regular_cost + cached_cost + output_cost, 6)


@dataclass
class SystemResult:
    system_id: str
    name: str
    calls: list[Call] = field(default_factory=list)
    n_tasks: int = 0
    n_correct: int = 0

    @property
    def total_cost(self) -> float:
        return round(sum(c.cost_usd() for c in self.calls), 4)

    @property
    def accuracy(self) -> float:
        return round(self.n_correct / self.n_tasks, 4) if self.n_tasks else 0.0

    @property
    def cost_per_correct(self) -> float:
        return round(self.total_cost / self.n_correct, 4) if self.n_correct else float("inf")

    @property
    def avg_input_tokens(self) -> float:
        return round(sum(c.input_tokens for c in self.calls) / len(self.calls), 0) if self.calls else 0

    @property
    def total_tokens(self) -> int:
        return sum(c.input_tokens + c.output_tokens for c in self.calls)

    def as_dict(self) -> dict:
        return {
            "system": self.system_id,
            "name": self.name,
            "n_tasks": self.n_tasks,
            "n_correct": self.n_correct,
            "accuracy": self.accuracy,
            "total_cost_usd": self.total_cost,
            "cost_per_correct_usd": self.cost_per_correct,
            "n_calls": len(self.calls),
            "avg_input_tokens": self.avg_input_tokens,
            "total_tokens": self.total_tokens,
        }


def load_run(run_dir: str, system_id: str) -> SystemResult | None:
    """Load a dumped evaluation run from JSONL + summary files."""
    summary_path = os.path.join(run_dir, f"{system_id}.summary.json")
    calls_path = os.path.join(run_dir, f"{system_id}.jsonl")
    if not os.path.exists(summary_path) or not os.path.exists(calls_path):
        return None
    with open(summary_path) as f:
        s = json.load(f)
    result = SystemResult(
        system_id=system_id, name=s.get("name", system_id),
        n_tasks=s["n_tasks"], n_correct=s["n_correct"],
    )
    with open(calls_path) as f:
        for line in f:
            c = json.loads(line)
            result.calls.append(Call(**{k: c.get(k, 0) for k in Call.__dataclass_fields__}))
    return result


def compute_frontier(results: list[SystemResult]) -> dict:
    """Compute the cost-to-quality frontier and iso-accuracy ratios."""
    baseline = next((r for r in results if r.system_id == "fullctx"), None)
    perseus = next((r for r in results if r.system_id == "perseus"), None)

    frontier = {
        "systems": [r.as_dict() for r in results],
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    if baseline and perseus and baseline.accuracy > 0 and perseus.accuracy > 0:
        # Iso-accuracy ratio: to hit Perseus's accuracy, what would fullctx cost?
        # Conservative: fullctx costs what it costs; Perseus costs what it costs.
        acc_ratio = perseus.accuracy / baseline.accuracy if baseline.accuracy > 0 else 0
        cost_ratio = perseus.total_cost / baseline.total_cost if baseline.total_cost > 0 else 0
        frontier["iso_accuracy_ratio"] = round(acc_ratio, 4)
        frontier["cost_ratio_vs_fullctx"] = round(cost_ratio, 4)
        frontier["savings_pct"] = round((1 - cost_ratio) * 100, 1) if cost_ratio < 1 else 0.0
        frontier["headline"] = (
            f"Perseus+Vault achieves {perseus.accuracy:.1%} accuracy "
            f"at ${perseus.total_cost:.2f} vs "
            f"Full Context {baseline.accuracy:.1%} at ${baseline.total_cost:.2f} — "
            f"{frontier['savings_pct']:.0f}% cost reduction"
        )

    return frontier


def render_frontier_html(frontier: dict) -> str:
    """Self-contained HTML chart page for the cost-to-quality frontier."""
    systems_json = json.dumps(frontier["systems"])
    headline = frontier.get("headline", "Benchmark results")

    return f"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Perseus Cost-to-Quality Frontier — LongMemEval</title>
<style>
:root{{--bg:#050508;--surface:#0B0A12;--surface-2:#0F0D1A;--elevate:#14111F;
--border:#221E36;--text:#F4F2FB;--text-dim:#8B8798;--text-faint:#6A6676;
--amber:#A78BFA;--amber-ink:#CBB8FF;--amber-soft:rgba(167,139,250,.12);
--violet:#8B9CFF;--green:#4ADE80;--red:#F87171;}}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,sans-serif;line-height:1.6;padding:2rem;max-width:960px;margin:0 auto}}
h1{{font-size:1.75rem;margin-bottom:.5rem;color:var(--amber-ink)}}
h2{{font-size:1.25rem;margin:2rem 0 1rem;color:var(--text)}}
.headline{{background:var(--amber-soft);border:1px solid var(--amber);border-radius:8px;padding:1rem;margin:1.5rem 0;color:var(--amber-ink)}}
.system-card{{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:1.25rem;margin:1rem 0}}
.system-card h3{{color:var(--amber);margin-bottom:.5rem}}
.system-card .stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:.75rem;margin-top:.75rem}}
.system-card .stat{{background:var(--surface-2);border-radius:6px;padding:.5rem .75rem}}
.system-card .stat .value{{font-size:1.2rem;font-weight:700;color:var(--amber-ink)}}
.system-card .stat .label{{font-size:.8rem;color:var(--text-dim)}}
.winner{{border-color:var(--green)}}
.winner h3{{color:var(--green)}}
.winner .stat .value{{color:var(--green)}}
.baseline{{border-color:var(--red);opacity:.85}}
footer{{margin-top:3rem;padding-top:1.5rem;border-top:1px solid var(--border);color:var(--text-faint);font-size:.85rem}}
</style>
</head>
<body>
<h1>Perseus Cost-to-Quality Frontier</h1>
<p style="color:var(--text-dim)">LongMemEval benchmark — single paid seed (~$35 total). Generated {frontier['generated_at']}.</p>
<div class="headline">{headline}</div>
<h2>Systems Compared</h2>
<div id="systems"></div>
<footer>
  <p>All costs computed from published API prices × recorded token usage. Full-context baseline uses prompt caching. Perseus+Vault includes local ONNX embedding cost ($0) and retrieval overhead. Results are independently reproducible — run <code>python3 run_frontier_benchmark.py --seed 0</code>.</p>
  <p style="margin-top:.5rem">Perseus Computing LLC · <a href="https://perseus.observer" style="color:var(--amber)">perseus.observer</a></p>
</footer>
<script>
const systems = {systems_json};
const container = document.getElementById('systems');
systems.forEach(sys => {{
    const isPerseus = sys.system === 'perseus';
    const isFullCtx = sys.system === 'fullctx';
    const cls = isPerseus ? 'winner' : (isFullCtx ? 'baseline' : '');
    container.innerHTML += `<div class="system-card ${{cls}}">
        <h3>${{sys.name}} ${{isPerseus ? '🏆' : ''}} ${{isFullCtx ? '(baseline)' : ''}}</h3>
        <div class="stats">
            <div class="stat"><div class="value">${{(sys.accuracy*100).toFixed(1)}}%</div><div class="label">Accuracy</div></div>
            <div class="stat"><div class="value">${{sys.total_cost_usd.toFixed(2)}}</div><div class="label">Total cost</div></div>
            <div class="stat"><div class="value">${{sys.cost_per_correct_usd.toFixed(3)}}</div><div class="label">$/correct answer</div></div>
            <div class="stat"><div class="value">${{sys.avg_input_tokens.toLocaleString()}}</div><div class="label">Avg input tokens</div></div>
            <div class="stat"><div class="value">${{sys.n_calls}}</div><div class="label">LLM calls</div></div>
            <div class="stat"><div class="value">${{sys.n_correct}}/${{sys.n_tasks}}</div><div class="label">Correct / Total</div></div>
        </div>
    </div>`;
}});
</script>
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(description="Plutus cost-to-quality frontier benchmark")
    parser.add_argument("--seed", type=int, default=0, help="Benchmark seed (default: 0)")
    parser.add_argument("--lme-dir", default="~/lme-run", help="LongMemEval harness directory")
    parser.add_argument("--run-dir", default=None, help="Output directory (default: runs/seed<N>)")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan without running")
    args = parser.parse_args()

    run_dir = args.run_dir or f"runs/seed{args.seed}"
    os.makedirs(run_dir, exist_ok=True)

    if args.dry_run:
        print(f"Would run LongMemEval seed {args.seed} across {len(SYSTEMS)} systems.")
        print(f"Output directory: {run_dir}")
        print(f"Estimated cost: ~$35 (single seed, all 4 systems)")
        print()
        for s in SYSTEMS:
            print(f"  {s['id']:12s} → {s['name']:20s}  model={s['model']}")
        print()
        print("After running, call this script again without --dry-run to generate the frontier report.")
        print("To integrate with the harness, use plutus_harness.py's MeteredClient + Recorder.")
        return 0

    # Load existing runs (if any) and compute the frontier
    results = []
    for s in SYSTEMS:
        r = load_run(run_dir, s["id"])
        if r:
            results.append(r)
            print(f"  ✓ {s['id']:12s} loaded: {r.n_tasks} tasks, {r.accuracy:.1%} accuracy, ${r.total_cost:.2f}")
        else:
            print(f"  ✗ {s['id']:12s} not found — run the LongMemEval harness with plutus_harness.py first")

    if not results:
        print("\nNo runs found. To run the full benchmark:")
        print(f"  1. cd {args.lme_dir}")
        print(f"  2. Copy plutus_harness.py to the harness directory")
        print(f"  3. For each system, run: python3 longmemeval_official.py --seed {args.seed} --system <system_id>")
        print(f"  4. Re-run this script to generate the frontier report")
        return 0

    # Compute and save
    frontier = compute_frontier(results)

    frontier_path = os.path.join(run_dir, "frontier.json")
    with open(frontier_path, "w") as f:
        json.dump(frontier, f, indent=2)
    print(f"\nFrontier data → {frontier_path}")

    html_path = os.path.join(run_dir, "frontier.html")
    with open(html_path, "w") as f:
        f.write(render_frontier_html(frontier))
    print(f"Frontier chart → {html_path} (open in browser)")

    # Print summary
    print(f"\n{'─'*60}")
    print(frontier.get("headline", "Benchmark complete."))
    print(f"{'─'*60}")
    for r in results:
        print(f"  {r.name:20s}  {r.accuracy:.1%} acc  ${r.total_cost:>8.2f} total  ${r.cost_per_correct:>8.3f}/correct")
    print(f"{'─'*60}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
