#!/usr/bin/env python3
"""Refresh helper for the checked-in fallback price table (ledger_agent/pricing.py).

The freshness gate (tools/check_price_freshness.py) enforces the table is
re-verified on a ~45-day cadence. This script makes that verification
mechanical: it fetches the vendor DOCS pricing pages, extracts the current
rates for every model currently tracked in PRICE_TABLE, and diffs them against
the checked-in values.

It NEVER edits the table: prices are hand-applied so the provenance comments
stay truthful. Output is a per-provider change report; apply any real diffs,
bump PRICE_TABLE_AS_OF, and record the sources in the provenance comment.

Usage:
  python tools/refresh_price_table.py            # fetch + diff, exit 1 on drift
  python tools/refresh_price_table.py --offline  # diff against cached fetches
  python tools/refresh_price_table.py --check    # same as default (read-only)

Exit codes: 0 = all tracked models match the live pages; 1 = drift found;
2 = a provider that should be parseable could not be parsed (fail-closed —
do NOT stamp as_of when this happens).

Notes:
- anthropic/openai: the docs sites serve markdown at <url>.md — parsed from
  that. google: the page HTML embeds <table class="pricing-table"> — parsed
  from HTML (<=200k-token tier values; single-rate table model).
- deepseek: the pricing table is client-rendered (no static content) —
  NOT machine-verifiable; the script prints a reminder to check it by hand.
- openai: Standard, short-context row is extracted (the table's single-rate
  model); Batch/Flex/Fast tables are ignored.
- Legacy/dated model aliases (gpt-5/o4 rows, dated claude ids) are carried
  forward by policy and excluded from the diff.
"""
import argparse
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ledger_agent.pricing import PRICE_TABLE  # noqa: E402

PAGES = {
    "anthropic": "https://docs.anthropic.com/en/docs/about-claude/pricing.md",
    "openai": "https://developers.openai.com/api/docs/pricing.md",
    "google": "https://ai.google.dev/gemini-api/docs/pricing",
    "deepseek": "https://api-docs.deepseek.com/quick_start/pricing",
}

CACHE_DIR = Path("/tmp/ledger-price-cache")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) refresh-price-table/1.0"}


def _fetch(name: str, offline: bool) -> str:
    cache = CACHE_DIR / f"{name}.md"
    if offline:
        if not cache.exists():
            raise SystemExit(f"--offline but no cache for {name}; run once online first")
        return cache.read_text(encoding="utf-8", errors="replace")
    CACHE_DIR.mkdir(exist_ok=True)
    req = urllib.request.Request(PAGES[name], headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read().decode("utf-8", errors="replace")
    cache.write_text(body, encoding="utf-8")
    return body


def _money(s: str) -> "float | None":
    """First '$X' in a cell ('$0.50 / MTok', '$2.00, prompts <= 200k tokens<br>...')."""
    m = re.search(r"\$([0-9]+(?:\.[0-9]+)?)", s.replace(",", ""))
    return float(m.group(1)) if m else None


# ── per-provider extractors: page text -> {model_id: (input, output, cache_read)} ──

def extract_anthropic(text: str):
    """Markdown rows: '| Claude Opus 4.8 | $5 / MTok | $6.25 / MTok | $10 / MTok | $0.50 / MTok | $25 / MTok |'"""
    out = {}
    for row in re.finditer(
        r"\|\s*Claude\s+([^|]+?)\s*\|\s*(\$[\d.]+[^|]*?)\s*\|\s*(\$[\d.]+[^|]*?)\s*\|\s*(\$[\d.]+[^|]*?)\s*\|\s*(\$[\d.]+[^|]*?)\s*\|\s*(\$[\d.]+[^|]*?)\s*\|",
        text,
    ):
        name, inp, cw5, cw1h, cache_hit, outp = row.groups()
        key = "claude-" + name.strip().lower().replace(" ", "-").replace(".", "-")
        out[key] = (_money(inp), _money(outp), _money(cache_hit))
    return out


def extract_openai(text: str):
    """'### Standard pricing data' section, short-context columns:
    '| gpt-5.6-sol | $5.00 | $0.50 | $6.25 | $30.00 | $10.00 | ...'"""
    sec = re.search(r"Standard pricing data\n(.*?)(?:\n##|\n# |\Z)", text, re.S)
    if not sec:
        return {}
    out = {}
    for row in re.finditer(
        r"\|\s*(gpt-5[\w.-]+?)\s*\|\s*(\$[\d.]+)\s*\|\s*(\$[\d.]+)\s*\|\s*(?:\$[\d.]+|-)\s*\|\s*(\$[\d.]+)\s*\|",
        sec.group(1),
    ):
        key, inp, cached, outp = row.groups()
        out[key] = (_money(inp), _money(outp), _money(cached))
    return out


def extract_google(text: str):
    """HTML '<h2 id="gemini-...">' sections with '<table class="pricing-table">':
    Input price / Output price / Context caching price rows, paid-tier cell."""
    out = {}
    for m in re.finditer(r'<h2 id="(gemini-[\w.-]+)"(?:[^>]*)>(.*?)(?=<h2 id="gemini-|\Z)', text, re.S):
        mid, sec = m.group(1), m.group(2)
        tab = re.search(r'<table class="pricing-table">(.*?)</table>', sec, re.S)
        if not tab:
            continue
        body = tab.group(1)
        def row_price(label):
            rm = re.search(r"<td>%s</td>\s*<td>.*?</td>\s*<td>(.*?)</td>" % label, body, re.S)
            return _money(rm.group(1)) if rm else None
        inp, outp, cache = row_price("Input price"), row_price("Output price[^<]*"), row_price("Context caching price")
        if inp is not None and outp is not None:
            out[mid] = (inp, outp, cache)
    return out


def extract_deepseek(_text: str):
    """Client-rendered page — no static content to parse. Handled by the caller."""
    return {}


EXTRACTORS = {
    "anthropic": extract_anthropic,
    "openai": extract_openai,
    "google": extract_google,
    "deepseek": extract_deepseek,
}

# model_id -> (provider, input, output, cache_read) for every tracked row
TRACKED = {}
for prov, models in PRICE_TABLE.items():
    if prov == "_default":
        continue
    for mid, p in models.items():
        if mid == "_default":
            continue
        TRACKED[mid] = (prov, p.input, p.output, p.cache_read)

# Carried-forward policy rows — never expected on current vendor pages.
LEGACY = {
    "gpt-5", "gpt-5-mini", "gpt-5-nano", "o4", "o4-mini",
    "claude-sonnet-4-5-20250929", "claude-haiku-4-5-20251001",
    # Carried-forward alias: the page only lists the -preview id.
    "gemini-3.1-pro",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--check", action="store_true", help="CI-safe alias of the default mode")
    args = ap.parse_args()

    print("deepseek: pricing table is client-rendered — verify deepseek rates by hand.")
    drift, unparsed = [], []
    for prov in sorted(EXTRACTORS):
        if prov == "deepseek":
            continue
        try:
            live = EXTRACTORS[prov](_fetch(prov, args.offline))
        except Exception as e:  # noqa: BLE001 — report and fail closed
            print(f"[{prov}] UNPARSEABLE: {e}")
            unparsed.append(prov)
            continue
        tracked = {m: v for m, v in TRACKED.items() if v[0] == prov}
        missing = [m for m in tracked if m not in live and m not in LEGACY]
        if missing:
            print(f"[{prov}] models not found on page: {', '.join(missing)}")
            unparsed.append(prov)
        for mid, (_, cur_in, cur_out, cur_cache) in tracked.items():
            if mid not in live or mid in LEGACY:
                continue
            li, lo, lc = live[mid]
            changed = []
            if li is not None and abs(li - cur_in) > 1e-9:
                changed.append(f"input {cur_in}->{li}")
            if lo is not None and abs(lo - cur_out) > 1e-9:
                changed.append(f"output {cur_out}->{lo}")
            if lc is not None and cur_cache and abs(lc - cur_cache) > 1e-9:
                changed.append(f"cache_read {cur_cache}->{lc}")
            if changed:
                print(f"[{prov}] {mid}: " + "; ".join(changed))
                drift.append(mid)

    if drift or unparsed:
        if drift:
            print(f"\nDRIFT: {len(drift)} model(s) differ from the live pages.")
        if unparsed:
            print(f"UNPARSED PROVIDERS: {', '.join(unparsed)} — do NOT stamp as_of.")
        sys.exit(1 if drift else 2)
    print("All tracked models match the live pricing pages.")


if __name__ == "__main__":
    main()
