#!/usr/bin/env python3
"""Incrementally meter Hermes ``state.db`` sessions into a Plutus instance.

This is the cron-safe, hosted-instance counterpart to ``hermes_integration.py``
(which is a local-Meter demo). It reads *new* rows from the Hermes ``sessions``
table — the same table the credit monitor ``plutus.py`` reads — and POSTs them
to ``POST /v1/usage`` with an API key, so Hermes spend shows up live on a hosted
Plutus dashboard.

Stdlib only (sqlite3 + urllib) — no ``plutus_agent`` install needed on the
Hermes box, just python3. Progress is tracked by a ``sessions.rowid`` watermark
in a small JSON state file and advanced per successful batch, so re-runs never
double-count and a mid-run failure resumes cleanly.

    export PLUTUS_REMOTE_URL=https://plutus.perseus.observer
    export PLUTUS_API_KEY=plutus_sk_…
    python3 hermes_sync.py --dry-run     # show what would be sent
    python3 hermes_sync.py               # sync new sessions (cron this)
    python3 hermes_sync.py --reset       # forget the watermark, re-sync all

Env: PLUTUS_REMOTE_URL, PLUTUS_API_KEY (required); PLUTUS_STATE_DB (default the
Hermes path below); PLUTUS_SYNC_STATE (watermark file); PLUTUS_WORKSPACE
(default "hermes").
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request

DEFAULT_STATE_DB = "/opt/data/webui/minions-hermes-config/state.db"
BATCH = 500


def _session_columns(conn) -> set:
    return {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}


def _has_table(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _allocate_cost(total: float, weights: list) -> list:
    """Split ``total`` across buckets proportional to ``weights`` (>=0), summing
    back to ``total``; even split when all weights are zero. Mirrors
    ``plutus_agent.hermes.allocate_cost`` (inline so this stays install-free)."""
    n = len(weights)
    if n == 0:
        return []
    if not total:
        return [0.0] * n
    pos = [w if w and w > 0 else 0.0 for w in weights]
    s = sum(pos)
    if s <= 0:
        return [float(total) / n] * n
    return [float(total) * (w / s) for w in pos]


def _event(provider, model, task, workspace, itok, otok, ctok, rtok, cost) -> dict:
    ev = {
        "provider": provider,
        "task_type": task or "agent",
        "workspace": workspace,
        "source": "hermes",
        "input_tokens": int(itok), "output_tokens": int(otok),
        "cache_read_tokens": int(ctok), "reasoning_tokens": int(rtok),
    }
    if model:
        ev["model"] = model
    if cost:
        ev["cost_usd"] = float(cost)   # Hermes' own cost beats a re-estimate
    return ev


def collect_sessions(state_db: str, last_rowid: int = 0,
                     workspace: str = "hermes") -> list[tuple[int, dict]]:
    """Return ``[(rowid, event_dict), …]`` for sessions newer than ``last_rowid``.

    When the Hermes DB has the v17 ``session_model_usage`` table (and an ``id``
    column to join on), each new session is emitted as one event *per model*,
    so a mid-session ``/model`` switch is attributed to the provider that
    actually served each call. The session's authoritative cost (actual over
    estimated) is allocated across those per-model rows in proportion to their
    estimated cost (or tokens), so the per-session total is preserved exactly.
    All events for a session share the session's ``rowid`` — the watermark
    advances per session, not per model. Falls back to the aggregate
    ``sessions`` row on older databases so nothing is lost. Mirrors
    ``plutus_agent.hermes.read_spend_events`` (kept inline; no install needed).
    """
    conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    try:
        cols = _session_columns(conn)
        model_sel = "model" if "model" in cols else "NULL"
        task_sel = "task_type" if "task_type" in cols else "NULL"
        use_pm = "id" in cols and _has_table(conn, "session_model_usage")

        rows = conn.execute(
            f"""SELECT rowid,
                   {'id' if 'id' in cols else 'NULL'} AS id,
                   coalesce(nullif(billing_provider,''),'unknown') AS provider,
                   {model_sel} AS model,
                   {task_sel} AS task_type,
                   coalesce(nullif(actual_cost_usd,0), estimated_cost_usd, 0) AS cost,
                   coalesce(input_tokens,0), coalesce(output_tokens,0),
                   coalesce(cache_read_tokens,0), coalesce(reasoning_tokens,0)
                FROM sessions WHERE rowid > ? ORDER BY rowid""",
            (last_rowid,),
        ).fetchall()

        by_session = {}
        if use_pm:
            for u in conn.execute(
                """SELECT session_id, model,
                       coalesce(nullif(billing_provider,''),'unknown') AS provider,
                       coalesce(input_tokens,0), coalesce(output_tokens,0),
                       coalesce(cache_read_tokens,0), coalesce(reasoning_tokens,0),
                       coalesce(estimated_cost_usd,0)
                    FROM session_model_usage"""
            ):
                by_session.setdefault(u[0], []).append(u)
    finally:
        conn.close()

    out = []
    for rowid, sid, provider, model, task, cost, itok, otok, ctok, rtok in rows:
        urows = by_session.get(sid) if use_pm else None
        if urows:
            total = float(cost or 0)
            weights = [float(u[7]) for u in urows]
            if sum(w for w in weights if w > 0) <= 0:
                weights = [u[3] + u[4] + u[5] + u[6] for u in urows]
            for u, c in zip(urows, _allocate_cost(total, weights)):
                out.append((rowid, _event(u[2], u[1], task, workspace,
                                          u[3], u[4], u[5], u[6], c)))
        else:
            # No per-model rows for this session (pre-v17 / un-backfilled) — emit
            # the aggregate, exactly as before.
            out.append((rowid, _event(provider, model, task, workspace,
                                      itok, otok, ctok, rtok, cost)))
    return out


def post_events(remote: str, api_key: str, events: list[dict], timeout: float = 30.0) -> dict:
    """POST a batch of events to ``/v1/usage``; raise on a non-2xx/again-later."""
    req = urllib.request.Request(
        remote.rstrip("/") + "/v1/usage",
        data=json.dumps(events).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}",
                 # Real UA — Cloudflare (error 1010) blocks the default
                 # "Python-urllib" signature when posting through the public URL.
                 "User-Agent": "plutus-agent-hermes-sync"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _batches(pairs, size):
    """Yield chunks of ~``size`` pairs, cutting only at a session (rowid)
    boundary so all of a session's per-model events land in the same batch — the
    watermark then advances one whole session at a time and never re-sends or
    drops a partially-sent session on a mid-run failure."""
    chunk = []
    for i, pair in enumerate(pairs):
        chunk.append(pair)
        at_boundary = (i + 1 == len(pairs)) or (pairs[i + 1][0] != pair[0])
        if len(chunk) >= size and at_boundary:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def _load_watermark(path: str) -> int:
    try:
        return int(json.load(open(path)).get("last_rowid", 0))
    except Exception:
        return 0


def _save_watermark(path: str, rowid: int, count: int) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    json.dump({"last_rowid": rowid, "synced_at": time.time(), "count": count},
              open(path, "w"))


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    dry = "--dry-run" in argv
    reset = "--reset" in argv

    remote = (os.environ.get("PLUTUS_REMOTE_URL") or "").rstrip("/")
    api_key = os.environ.get("PLUTUS_API_KEY")
    state_db = os.environ.get("PLUTUS_STATE_DB", DEFAULT_STATE_DB)
    wm_path = os.environ.get("PLUTUS_SYNC_STATE",
                             os.path.expanduser("~/.plutus/hermes_sync.json"))
    workspace = os.environ.get("PLUTUS_WORKSPACE", "hermes")

    if not remote or not api_key:
        sys.exit("plutus: set PLUTUS_REMOTE_URL and PLUTUS_API_KEY")
    if not os.path.exists(state_db):
        sys.exit(f"plutus: state.db not found: {state_db}")

    last = 0 if reset else _load_watermark(wm_path)
    pairs = collect_sessions(state_db, last, workspace)
    if not pairs:
        print(f"plutus: nothing new (watermark rowid={last})")
        return 0
    print(f"plutus: {len(pairs)} new session(s), rowid {pairs[0][0]}..{pairs[-1][0]}")

    if dry:
        print(json.dumps([e for _, e in pairs[:3]], indent=2))
        print("(dry-run — nothing sent, watermark unchanged)")
        return 0

    sent = 0
    for chunk in _batches(pairs, BATCH):
        try:
            post_events(remote, api_key, [e for _, e in chunk])
        except urllib.error.HTTPError as e:
            sys.exit(f"plutus: ingest failed HTTP {e.code}: "
                     f"{e.read().decode()[:200]} (watermark at {last}, not advanced)")
        except urllib.error.URLError as e:
            sys.exit(f"plutus: could not reach {remote}: {e.reason} "
                     f"(watermark at {last}, not advanced)")
        sent += len(chunk)
        last = chunk[-1][0]
        _save_watermark(wm_path, last, sent)   # advance per batch → resumable

    print(f"plutus: metered {sent} session(s) → {remote} (watermark rowid={last})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
