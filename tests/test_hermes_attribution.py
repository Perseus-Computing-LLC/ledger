"""Per-model spend attribution from a Hermes ``state.db`` (issue #51607).

Covers ``ledger_agent.hermes`` — the canonical reader that prefers the v17
``session_model_usage`` table (so a mid-session model switch splits across the
providers that actually served each call) and falls back to the aggregate
``sessions`` row for pre-v17 / un-backfilled data so totals never regress.
"""
import sqlite3

import pytest

from ledger_agent.hermes import allocate_cost, read_spend_events


# --------------------------------------------------------------- allocate ---
def test_allocate_proportional():
    assert allocate_cost(10.0, [3, 1]) == pytest.approx([7.5, 2.5])


def test_allocate_sum_is_preserved():
    out = allocate_cost(1.0, [1, 1, 1])
    assert sum(out) == pytest.approx(1.0)


def test_allocate_even_split_when_all_weights_zero():
    assert allocate_cost(9.0, [0, 0, 0]) == pytest.approx([3.0, 3.0, 3.0])


def test_allocate_ignores_negative_weights():
    # A negative weight can't claim cost; it's floored to zero.
    assert allocate_cost(4.0, [-5, 2, 2]) == pytest.approx([0.0, 2.0, 2.0])


def test_allocate_zero_cost_is_all_zero():
    assert allocate_cost(0.0, [1, 2, 3]) == [0.0, 0.0, 0.0]


def test_allocate_empty():
    assert allocate_cost(5.0, []) == []


# --------------------------------------------------------------- fixtures ---
_SESSIONS_DDL = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    started_at REAL,
    model TEXT,
    billing_provider TEXT,
    actual_cost_usd REAL,
    estimated_cost_usd REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    reasoning_tokens INTEGER
);
"""

_USAGE_DDL = """
CREATE TABLE session_model_usage (
    session_id TEXT NOT NULL,
    model TEXT NOT NULL,
    billing_provider TEXT NOT NULL DEFAULT '',
    api_call_count INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0,
    first_seen REAL,
    last_seen REAL,
    PRIMARY KEY (session_id, model, billing_provider)
);
"""


def _db(tmp_path, with_usage=True):
    p = tmp_path / "state.db"
    conn = sqlite3.connect(p)
    conn.executescript(_SESSIONS_DDL)
    if with_usage:
        conn.executescript(_USAGE_DDL)
    conn.commit()
    conn.close()
    return str(p)


def _insert_session(db, **kw):
    cols = ", ".join(kw)
    ph = ", ".join("?" for _ in kw)
    conn = sqlite3.connect(db)
    conn.execute(f"INSERT INTO sessions ({cols}) VALUES ({ph})", tuple(kw.values()))
    conn.commit()
    conn.close()


def _insert_usage(db, **kw):
    cols = ", ".join(kw)
    ph = ", ".join("?" for _ in kw)
    conn = sqlite3.connect(db)
    conn.execute(f"INSERT INTO session_model_usage ({cols}) VALUES ({ph})",
                 tuple(kw.values()))
    conn.commit()
    conn.close()


# ---------------------------------------------------------- read events ----
def test_midsession_switch_splits_across_providers(tmp_path):
    """The scenario that motivates the whole feature.

    A session starts on Anthropic then switches to OpenAI. The ``sessions`` row
    records the *initial* provider (anthropic) and the whole $1.00 actual cost.
    Reading per-model rows must split that cost across both providers instead of
    dumping it all on anthropic.
    """
    db = _db(tmp_path)
    _insert_session(db, id="s1", started_at=1000.0, model="claude-opus-4-8",
                    billing_provider="anthropic", actual_cost_usd=1.00,
                    estimated_cost_usd=0.90, input_tokens=1000, output_tokens=500)
    # per-model rows: estimates 0.60 (anthropic) + 0.30 (openai) = 0.90
    _insert_usage(db, session_id="s1", model="claude-opus-4-8",
                  billing_provider="anthropic", input_tokens=700, output_tokens=300,
                  estimated_cost_usd=0.60)
    _insert_usage(db, session_id="s1", model="gpt-5",
                  billing_provider="openai", input_tokens=300, output_tokens=200,
                  estimated_cost_usd=0.30)

    events = read_spend_events(db)
    by_prov = {e["billing_provider"]: e for e in events}
    assert set(by_prov) == {"anthropic", "openai"}
    # authoritative $1.00 allocated 2:1 by estimate weight
    assert by_prov["anthropic"]["cost_usd"] == pytest.approx(1.00 * 0.60 / 0.90)
    assert by_prov["openai"]["cost_usd"] == pytest.approx(1.00 * 0.30 / 0.90)
    # total is preserved exactly — no regression against the sessions row
    assert sum(e["cost_usd"] for e in events) == pytest.approx(1.00)
    # tokens come straight from the per-model rows (exact, not split)
    assert by_prov["openai"]["input_tokens"] == 300
    assert by_prov["anthropic"]["input_tokens"] == 700


def test_totals_preserved_when_actual_differs_from_estimate(tmp_path):
    db = _db(tmp_path)
    _insert_session(db, id="s1", started_at=1000.0, model="m", billing_provider="p",
                    actual_cost_usd=2.00, estimated_cost_usd=0.50,
                    input_tokens=100, output_tokens=100)
    _insert_usage(db, session_id="s1", model="m", billing_provider="p",
                  input_tokens=100, output_tokens=100, estimated_cost_usd=0.50)
    events = read_spend_events(db)
    # single provider: allocation gives it the full authoritative $2.00,
    # NOT the $0.50 estimate.
    assert len(events) == 1
    assert events[0]["cost_usd"] == pytest.approx(2.00)


def test_token_weight_when_no_cost_estimate(tmp_path):
    db = _db(tmp_path)
    _insert_session(db, id="s1", started_at=1000.0, model="a", billing_provider="anthropic",
                    actual_cost_usd=1.00, input_tokens=0, output_tokens=0)
    _insert_usage(db, session_id="s1", model="a", billing_provider="anthropic",
                  input_tokens=300, output_tokens=0, estimated_cost_usd=0.0)
    _insert_usage(db, session_id="s1", model="b", billing_provider="openai",
                  input_tokens=100, output_tokens=0, estimated_cost_usd=0.0)
    events = read_spend_events(db)
    by_prov = {e["billing_provider"]: e["cost_usd"] for e in events}
    assert by_prov["anthropic"] == pytest.approx(0.75)  # 300/400
    assert by_prov["openai"] == pytest.approx(0.25)     # 100/400


def test_fallback_to_sessions_when_no_usage_table(tmp_path):
    db = _db(tmp_path, with_usage=False)
    _insert_session(db, id="s1", started_at=1000.0, model="m", billing_provider="anthropic",
                    actual_cost_usd=0.42, input_tokens=100, output_tokens=50)
    events = read_spend_events(db)
    assert len(events) == 1
    assert events[0]["billing_provider"] == "anthropic"
    assert events[0]["cost_usd"] == pytest.approx(0.42)


def test_uncovered_session_falls_back(tmp_path):
    """A token-bearing session with no per-model row (pre-v17 / un-backfilled)
    still contributes via the aggregate row, and is not double-counted."""
    db = _db(tmp_path)
    _insert_session(db, id="covered", started_at=1000.0, model="m", billing_provider="anthropic",
                    actual_cost_usd=1.00, input_tokens=10, output_tokens=10)
    _insert_usage(db, session_id="covered", model="m", billing_provider="anthropic",
                  input_tokens=10, output_tokens=10, estimated_cost_usd=1.00)
    _insert_session(db, id="legacy", started_at=1000.0, model="m2", billing_provider="openai",
                    actual_cost_usd=0.30, input_tokens=5, output_tokens=5)
    events = read_spend_events(db)
    ids = sorted(e["session_id"] for e in events)
    assert ids == ["covered", "legacy"]  # each counted exactly once
    assert sum(e["cost_usd"] for e in events) == pytest.approx(1.30)


def test_zero_token_session_skipped(tmp_path):
    db = _db(tmp_path, with_usage=False)
    _insert_session(db, id="empty", started_at=1000.0, model="m", billing_provider="anthropic",
                    actual_cost_usd=0.0, input_tokens=0, output_tokens=0)
    assert read_spend_events(db) == []


def test_estimated_cost_used_when_no_actual(tmp_path):
    db = _db(tmp_path)
    _insert_session(db, id="s1", started_at=1000.0, model="m", billing_provider="p",
                    actual_cost_usd=0.0, estimated_cost_usd=0.75,
                    input_tokens=100, output_tokens=100)
    _insert_usage(db, session_id="s1", model="m", billing_provider="p",
                  input_tokens=100, output_tokens=100, estimated_cost_usd=0.75)
    events = read_spend_events(db)
    assert events[0]["cost_usd"] == pytest.approx(0.75)
