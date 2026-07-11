"""Read per-model spend from a Hermes ``state.db``.

Hermes attributes every token in a session to the ``(model, billing_provider)``
that was active when the session *started*. A mid-session ``/model`` switch
therefore dumps every token onto the initial model, corrupting per-provider
spend on the ``sessions`` row (hermes-agent issue #51607, authored+fixed
upstream by Perseus). Schema v17 adds a ``session_model_usage`` table that
accumulates each per-API-call delta under the model live at that call, keyed
``(session_id, model, billing_provider)``.

Plutus reads that table when present so spend lands on the provider that
actually served each call, and falls back to the aggregate ``sessions`` row for
pre-v17 databases (or sessions not covered by the v17 backfill) so totals never
regress.

Cost note: ``session_model_usage`` carries only ``estimated_cost_usd``. The
authoritative per-session figure is ``sessions.actual_cost_usd`` (what the
provider actually billed). We keep that authority by allocating each session's
actual cost across its per-model rows in proportion to their estimated cost
(falling back to token weight, then an even split). The token counts come
straight from the per-model rows — those are exact — so a split never invents
or drops tokens, and the per-session cost total is preserved exactly.

Stdlib only (``sqlite3``) so this can run on a Hermes box with no extra install.
The same logic is mirrored inline in the standalone, install-free entrypoints
``plutus.py`` (the monitor) and ``examples/hermes_sync.py`` (the hosted sync);
this module is the canonical, unit-tested copy.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Dict, List, Sequence


def has_table(conn: sqlite3.Connection, name: str) -> bool:
    """True if ``name`` is a table in the connected SQLite database."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def allocate_cost(total_cost: float, weights: Sequence[float]) -> List[float]:
    """Split ``total_cost`` across buckets in proportion to ``weights``.

    - Returns a list the same length as ``weights`` that sums to ``total_cost``
      (modulo float error), so the per-session total is never inflated or lost.
    - When every weight is zero (or negative), splits evenly — the caller has a
      real cost but no signal for how to attribute it.
    - A zero/falsy ``total_cost`` yields all zeros.
    """
    n = len(weights)
    if n == 0:
        return []
    if not total_cost:
        return [0.0] * n
    positive = [w if w and w > 0 else 0.0 for w in weights]
    s = sum(positive)
    if s <= 0:
        share = float(total_cost) / n
        return [share] * n
    return [float(total_cost) * (w / s) for w in positive]


# Columns pulled from ``sessions`` for the authoritative per-session figures.
# ``cost`` prefers the provider-billed ``actual_cost_usd`` and only falls back
# to the estimate, matching what the legacy monitor already trusts.
_SESSIONS_SQL = """
    SELECT id,
           started_at,
           coalesce(nullif(actual_cost_usd, 0), estimated_cost_usd, 0) AS cost,
           coalesce(nullif(billing_provider, ''), 'unknown')          AS provider,
           model,
           coalesce(input_tokens, 0)      AS input_tokens,
           coalesce(output_tokens, 0)     AS output_tokens,
           coalesce(cache_read_tokens, 0) AS cache_read_tokens,
           coalesce(reasoning_tokens, 0)  AS reasoning_tokens
    FROM sessions
"""

_MODEL_USAGE_SQL = """
    SELECT session_id,
           model,
           coalesce(nullif(billing_provider, ''), 'unknown') AS provider,
           coalesce(input_tokens, 0)      AS input_tokens,
           coalesce(output_tokens, 0)     AS output_tokens,
           coalesce(cache_read_tokens, 0) AS cache_read_tokens,
           coalesce(reasoning_tokens, 0)  AS reasoning_tokens,
           coalesce(estimated_cost_usd, 0) AS estimated_cost_usd
    FROM session_model_usage
"""


def _event(session_id, started_at, provider, model,
           itok, otok, ctok, rtok, cost) -> Dict:
    return {
        "session_id": session_id,
        "started_at": started_at or 0,
        "billing_provider": provider,
        "model": model,
        "input_tokens": int(itok or 0),
        "output_tokens": int(otok or 0),
        "cache_read_tokens": int(ctok or 0),
        "reasoning_tokens": int(rtok or 0),
        "cost_usd": float(cost or 0.0),
    }


def read_spend_events(state_db: str) -> List[Dict]:
    """Return one spend event per ``(session, model, billing_provider)``.

    Prefers ``session_model_usage`` (schema v17+), allocating each session's
    authoritative cost across its per-model rows. Sessions with token totals but
    no per-model row (pre-v17, or not covered by the v17 backfill) fall back to
    their aggregate ``sessions`` row. A session is never counted from both
    sources.

    Each event carries: ``session_id``, ``started_at`` (epoch seconds),
    ``billing_provider``, ``model``, the four token counts, and an allocated
    ``cost_usd``.
    """
    conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        sess = {r["id"]: r for r in conn.execute(_SESSIONS_SQL)}
        events: List[Dict] = []
        covered: set = set()

        if has_table(conn, "session_model_usage"):
            by_session: Dict[str, List[sqlite3.Row]] = defaultdict(list)
            for u in conn.execute(_MODEL_USAGE_SQL):
                by_session[u["session_id"]].append(u)
            for sid, urows in by_session.items():
                s = sess.get(sid)
                if s is None:
                    # Usage row with no anchoring session (shouldn't happen given
                    # the FK) — skip: no started_at to window on, no authoritative
                    # cost to allocate.
                    continue
                covered.add(sid)
                total = float(s["cost"] or 0.0)
                weights = [float(u["estimated_cost_usd"]) for u in urows]
                if sum(w for w in weights if w > 0) <= 0:
                    # No per-model cost signal: weight by tokens instead.
                    weights = [
                        u["input_tokens"] + u["output_tokens"]
                        + u["cache_read_tokens"] + u["reasoning_tokens"]
                        for u in urows
                    ]
                for u, cost in zip(urows, allocate_cost(total, weights)):
                    events.append(_event(
                        sid, s["started_at"], u["provider"], u["model"],
                        u["input_tokens"], u["output_tokens"],
                        u["cache_read_tokens"], u["reasoning_tokens"], cost,
                    ))

        for sid, s in sess.items():
            if sid in covered:
                continue
            if not (s["input_tokens"] or s["output_tokens"]
                    or s["cache_read_tokens"] or s["reasoning_tokens"]):
                continue
            events.append(_event(
                sid, s["started_at"], s["provider"], s["model"],
                s["input_tokens"], s["output_tokens"],
                s["cache_read_tokens"], s["reasoning_tokens"], s["cost"],
            ))
        return events
    finally:
        conn.close()
