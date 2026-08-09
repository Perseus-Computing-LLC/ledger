#!/usr/bin/env python3
"""Claude Code -> Ledger metering hook (standalone, stdlib only).

Registered as a Claude Code ``Stop`` hook. On each turn end, Claude Code pipes a
JSON payload on stdin that includes ``transcript_path``. This hook reads the
transcript, sums the token usage of assistant messages it has NOT metered yet
(watermarked per transcript by line count), and POSTs the delta to a remote
Ledger ``/v1/usage`` so Claude Code spend lands in the ``claude-code`` org.

Design notes / safety:
- A hook must NEVER break the host tool: every path catches and exits 0.
- Watermarked per transcript so each turn only meters NEW assistant lines (the
  Stop hook fires every turn; summing the whole transcript each time would
  massively over-count).
- Idempotency-Key = transcript + line range, so a retry after a lost response
  can't double-count.
- Config from env, falling back to ~/.claude/ledger_cc_config.json:
  {"remote": "https://ledger.perseus.observer", "api_key": "ledger_sk_..."}.
- No baseline_model is sent: Claude Code runs the flagship (no routing), so
  there is no saving to claim — this is pure cost tracking.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path

CONFIG_PATH = Path.home() / ".claude" / "ledger_cc_config.json"
STATE_PATH = Path.home() / ".claude" / "ledger_cc_watermarks.json"
TIMEOUT = 6.0


def _load_config() -> dict:
    cfg = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    remote = os.environ.get("LEDGER_REMOTE_URL") or cfg.get("remote") \
        or "https://ledger.perseus.observer"
    api_key = os.environ.get("LEDGER_CC_API_KEY") or cfg.get("api_key") or ""
    return {"remote": remote.rstrip("/"), "api_key": api_key}


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        pass


def _sum_usage(lines):
    """Sum assistant-message usage over ``lines``, grouped by model.

    Returns {model: {input, output, cache_read, cache_write}}. Cache creation is
    kept separate so provider cache-write premiums are not hidden as input."""
    by_model = {}
    for ln in lines:
        try:
            o = json.loads(ln)
        except Exception:
            continue
        if o.get("type") != "assistant":
            continue
        msg = o.get("message") or {}
        u = msg.get("usage") or {}
        if not u:
            continue
        model = msg.get("model") or "claude"
        agg = by_model.setdefault(model, {"input": 0, "output": 0,
                                           "cache_read": 0, "cache_write": 0})
        agg["input"] += int(u.get("input_tokens") or 0)
        agg["cache_write"] += int(u.get("cache_creation_input_tokens") or 0)
        agg["output"] += int(u.get("output_tokens") or 0)
        agg["cache_read"] += int(u.get("cache_read_input_tokens") or 0)
    return by_model


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    transcript = payload.get("transcript_path")
    if not transcript or not os.path.exists(transcript):
        return 0

    cfg = _load_config()
    if not cfg["api_key"]:
        sys.stderr.write("[ledger] no claude-code api key configured; skipping\n")
        return 0

    cwd = payload.get("cwd") or os.getcwd() or "claude-code"
    workspace = str(cwd).rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1] \
        or "claude-code"

    try:
        lines = Path(transcript).read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return 0

    state = _load_state()
    start = int(state.get(transcript, 0))
    if start > len(lines):
        start = 0  # transcript was rotated/truncated; re-baseline
    new_lines = lines[start:]
    if not new_lines:
        return 0

    by_model = _sum_usage(new_lines)
    events = []
    for model, u in by_model.items():
        if u["input"] or u["output"] or u["cache_read"] or u["cache_write"]:
            events.append({
                "provider": "anthropic",
                "model": model,
                "task_type": "coding",
                "workspace": workspace,
                "source": "claude-code",
                "input_tokens": u["input"],
                "output_tokens": u["output"],
                "cache_read_tokens": u["cache_read"],
                "cache_write_tokens": u["cache_write"],
            })

    if not events:
        # No billable usage in the new lines, but still advance the watermark so
        # we don't re-scan them forever.
        state[transcript] = len(lines)
        _save_state(state)
        return 0

    idem = hashlib.sha256(
        f"{transcript}:{start}:{len(lines)}".encode("utf-8")).hexdigest()[:32]
    req = urllib.request.Request(
        cfg["remote"] + "/v1/usage",
        data=json.dumps(events).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg['api_key']}",
            "Idempotency-Key": idem,
            # Real UA: Cloudflare (error 1010) blocks the default Python-urllib UA
            # on the public host.
            "User-Agent": "ledger-claude-code-hook",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            resp.read()
        # advance watermark only on a successful POST
        state[transcript] = len(lines)
        _save_state(state)
    except Exception as e:
        # leave the watermark so the next turn retries (idempotent); never break.
        sys.stderr.write(f"[ledger] claude-code meter deferred (non-fatal): {e}\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # absolute backstop — a hook must never fail loudly
        sys.stderr.write(f"[ledger] hook error (non-fatal): {e}\n")
        sys.exit(0)
