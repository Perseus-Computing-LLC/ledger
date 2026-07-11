#!/usr/bin/env python3
"""Before/after per-provider spend exhibit for the #97 attribution fix.

The dogfooding pitch is: Hermes attributed a whole session's cost to the model
active when the session *started*, so a mid-session ``/model`` switch mis-billed
the provider. #97 fixed it (consume the v17 ``session_model_usage`` table). This
tool turns that claim into a picture, computed from a REAL Hermes ``state.db``:

  BEFORE  = the old aggregate: every session's cost on its initial provider
            (``sessions`` row, grouped by ``billing_provider``).
  AFTER   = #97's per-model attribution (``plutus_agent.hermes.read_spend_events``:
            cost allocated across the providers that actually served each call).

Emits a Markdown table + a self-contained SVG bar chart to ``docs/exhibits/``.

    # real asset: point at a genuine Hermes state.db (e.g. from a Lambda
    # multi-model workload, or greg's Hermes once it is on schema v17):
    python tools/attribution_exhibit.py --state-db /path/to/state.db --label "hermes-prod"

    # illustrative format demo (writes SYNTHETIC data, clearly labelled):
    python tools/attribution_exhibit.py --demo

Never fabricates: with ``--demo`` the output is stamped ILLUSTRATIVE/SYNTHETIC in
both the table and the chart so it can't be mistaken for a real workload.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plutus_agent.hermes import read_spend_events  # noqa: E402

_EXHIBITS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "docs", "exhibits")


def before_spend(state_db: str) -> dict:
    """Old logic: aggregate the ``sessions`` row by billing_provider, so every
    session's whole cost lands on the provider it *started* on."""
    out: dict = defaultdict(float)
    conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    try:
        for prov, cost in conn.execute(
            """SELECT coalesce(nullif(billing_provider,''),'unknown'),
                      coalesce(nullif(actual_cost_usd,0), estimated_cost_usd, 0)
               FROM sessions"""
        ):
            out[prov] += float(cost or 0)
    finally:
        conn.close()
    return dict(out)


def after_spend(state_db: str) -> dict:
    """#97 logic: per-(session, model, provider) events with allocated cost."""
    out: dict = defaultdict(float)
    for ev in read_spend_events(state_db):
        out[ev["billing_provider"]] += float(ev["cost_usd"] or 0)
    return dict(out)


def _fmt_table(before: dict, after: dict, synthetic: bool) -> str:
    provs = sorted(set(before) | set(after))
    lines = []
    if synthetic:
        lines.append("> ILLUSTRATIVE / SYNTHETIC data, format demo only. "
                     "Run with --state-db on a real Hermes state.db for the real exhibit.\n")
    lines.append("| provider | before ($) | after ($) | delta ($) |")
    lines.append("|---|---|---|---|")
    for p in provs:
        b, a = before.get(p, 0.0), after.get(p, 0.0)
        lines.append(f"| {p} | {b:.4f} | {a:.4f} | {a - b:+.4f} |")
    lines.append(f"| **total** | **{sum(before.values()):.4f}** | "
                 f"**{sum(after.values()):.4f}** | (preserved) |")
    return "\n".join(lines)


def _svg(before: dict, after: dict, synthetic: bool) -> str:
    """A dependency-free grouped bar chart (before vs after per provider)."""
    provs = sorted(set(before) | set(after))
    W, H, pad, top = 720, 360, 60, 40
    plot_h = H - pad - top
    mx = max([*before.values(), *after.values(), 0.0001])
    n = len(provs)
    group_w = (W - 2 * pad) / max(n, 1)
    bw = group_w * 0.32

    def bar(x, val, cls):
        h = plot_h * (val / mx)
        y = top + plot_h - h
        return (f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{h:.1f}" '
                f'class="{cls}"><title>{val:.4f}</title></rect>')

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="system-ui,sans-serif">',
        f'<rect width="{W}" height="{H}" fill="#0c0814"/>',
        '<style>.b{fill:#6b7280}.a{fill:#a78bfa}text{fill:#e5e7eb}'
        '.t{font-size:16px;font-weight:600}.l{font-size:12px}.w{fill:#fca5a5;font-size:12px}</style>',
        f'<text x="{pad}" y="26" class="t">Per-provider spend: before vs after #97 attribution fix</text>',
    ]
    if synthetic:
        parts.append(f'<text x="{W-pad}" y="26" text-anchor="end" class="w">ILLUSTRATIVE / SYNTHETIC</text>')
    parts.append(f'<line x1="{pad}" y1="{top+plot_h}" x2="{W-pad}" y2="{top+plot_h}" stroke="#374151"/>')
    for i, p in enumerate(provs):
        gx = pad + i * group_w + (group_w - 2 * bw) / 2
        parts.append(bar(gx, before.get(p, 0.0), "b"))
        parts.append(bar(gx + bw, after.get(p, 0.0), "a"))
        parts.append(f'<text x="{gx + bw:.1f}" y="{top+plot_h+18}" text-anchor="middle" class="l">{p}</text>')
    # legend
    parts.append(f'<rect x="{pad}" y="{H-22}" width="12" height="12" class="b"/>'
                 f'<text x="{pad+18}" y="{H-12}" class="l">before (initial-provider)</text>')
    parts.append(f'<rect x="{pad+200}" y="{H-22}" width="12" height="12" class="a"/>'
                 f'<text x="{pad+218}" y="{H-12}" class="l">after (per-model)</text>')
    parts.append("</svg>")
    return "".join(parts)


def _demo_db() -> str:
    """A small SYNTHETIC state.db: two sessions that switch providers mid-flight
    (anthropic->openai) plus single-provider sessions. Illustrative only."""
    import tempfile
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    c = sqlite3.connect(p)
    c.execute("""CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at REAL,
        actual_cost_usd REAL, estimated_cost_usd REAL, billing_provider TEXT,
        model TEXT, input_tokens INT, output_tokens INT, cache_read_tokens INT,
        reasoning_tokens INT)""")
    c.execute("""CREATE TABLE session_model_usage (session_id TEXT, model TEXT,
        billing_provider TEXT, input_tokens INT, output_tokens INT,
        cache_read_tokens INT, reasoning_tokens INT, estimated_cost_usd REAL,
        PRIMARY KEY (session_id, model, billing_provider))""")
    # two sessions that started on anthropic and switched to openai mid-flight
    for sid, cost in (("s1", 1.00), ("s2", 0.80)):
        c.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (sid, 1000.0, cost, cost * 0.9, "anthropic", "claude-opus-4-8",
                   1000, 500, 0, 0))
        c.execute("INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?)",
                  (sid, "claude-opus-4-8", "anthropic", 700, 300, 0, 0, cost * 0.6))
        c.execute("INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?)",
                  (sid, "gpt-5", "openai", 300, 200, 0, 0, cost * 0.3))
    # a clean single-provider deepseek session (unchanged before vs after)
    c.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?)",
              ("s3", 1000.0, 0.20, 0.20, "deepseek", "deepseek-chat", 4000, 1000, 0, 0))
    c.execute("INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?)",
              ("s3", "deepseek-chat", "deepseek", 4000, 1000, 0, 0, 0.20))
    c.commit()
    c.close()
    return p


def main(argv=None):
    ap = argparse.ArgumentParser(description="Before/after attribution exhibit")
    ap.add_argument("--state-db", help="path to a real Hermes state.db")
    ap.add_argument("--demo", action="store_true", help="use synthetic illustrative data")
    ap.add_argument("--label", default="exhibit", help="artifact name suffix")
    args = ap.parse_args(argv)

    if not args.state_db and not args.demo:
        ap.error("pass --state-db <path> for the real exhibit, or --demo")

    synthetic = bool(args.demo)
    db = _demo_db() if synthetic else args.state_db
    try:
        before, after = before_spend(db), after_spend(db)
    finally:
        if synthetic:
            for ext in ("", "-wal", "-shm"):
                try:
                    os.unlink(db + ext)
                except OSError:
                    pass

    os.makedirs(_EXHIBITS, exist_ok=True)
    stem = f"attribution-before-after-{args.label}"
    md = _fmt_table(before, after, synthetic)
    svg = _svg(before, after, synthetic)
    with open(os.path.join(_EXHIBITS, stem + ".md"), "w", encoding="utf-8") as f:
        f.write("# Attribution before/after\n\n" + md + "\n")
    with open(os.path.join(_EXHIBITS, stem + ".svg"), "w", encoding="utf-8") as f:
        f.write(svg)
    print(md)
    print(f"\nwrote docs/exhibits/{stem}.md and .svg"
          + ("  (SYNTHETIC)" if synthetic else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
