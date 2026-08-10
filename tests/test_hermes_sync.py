#!/usr/bin/env python3
"""Tests for the Hermes → Ledger sync bridge (examples/hermes_sync.py)."""
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "examples"))

import hermes_sync  # noqa: E402


def _make_db(rows, *, with_model=True):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    cols = ["billing_provider TEXT", "started_at REAL",
            "actual_cost_usd REAL", "estimated_cost_usd REAL",
            "input_tokens INT", "output_tokens INT",
            "cache_read_tokens INT", "reasoning_tokens INT"]
    if with_model:
        cols += ["model TEXT", "task_type TEXT"]
    conn.execute(f"CREATE TABLE sessions ({', '.join(cols)})")
    for r in rows:
        keys = ", ".join(r.keys())
        ph = ", ".join("?" * len(r))
        conn.execute(f"INSERT INTO sessions ({keys}) VALUES ({ph})", tuple(r.values()))
    conn.commit()
    conn.close()
    return path


class TestCollectSessions(unittest.TestCase):
    def tearDown(self):
        for p in getattr(self, "_paths", []):
            for ext in ("", "-wal", "-shm"):
                try:
                    os.unlink(p + ext)
                except OSError:
                    pass

    def _db(self, *a, **k):
        p = _make_db(*a, **k)
        self._paths = getattr(self, "_paths", []) + [p]
        return p

    def test_maps_rows_to_events(self):
        db = self._db([
            {"billing_provider": "anthropic", "model": "claude-opus-4-8",
             "task_type": "code_review", "actual_cost_usd": 0.14,
             "estimated_cost_usd": 0.20, "input_tokens": 1200, "output_tokens": 800,
             "cache_read_tokens": 0, "reasoning_tokens": 0},
        ])
        pairs = hermes_sync.collect_sessions(db, 0, workspace="hermes")
        self.assertEqual(len(pairs), 1)
        rowid, ev = pairs[0]
        self.assertEqual(ev["provider"], "anthropic")
        self.assertEqual(ev["model"], "claude-opus-4-8")
        self.assertEqual(ev["task_type"], "code_review")
        self.assertEqual(ev["cost_usd"], 0.14)      # actual preferred over estimated
        self.assertEqual(ev["input_tokens"], 1200)
        self.assertEqual(ev["workspace"], "hermes")
        self.assertEqual(ev["source"], "hermes")

    def test_estimated_cost_fallback(self):
        db = self._db([
            {"billing_provider": "google", "actual_cost_usd": 0,
             "estimated_cost_usd": 0.5, "input_tokens": 10, "output_tokens": 5,
             "cache_read_tokens": 0, "reasoning_tokens": 0,
             "model": "gemini", "task_type": "chat"},
        ])
        _, ev = hermes_sync.collect_sessions(db, 0)[0]
        self.assertEqual(ev["cost_usd"], 0.5)

    def test_watermark_filters(self):
        db = self._db([
            {"billing_provider": "a", "actual_cost_usd": 1, "estimated_cost_usd": 0,
             "input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0,
             "reasoning_tokens": 0, "model": "m", "task_type": "t"},
            {"billing_provider": "b", "actual_cost_usd": 2, "estimated_cost_usd": 0,
             "input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0,
             "reasoning_tokens": 0, "model": "m", "task_type": "t"},
        ])
        first_rowid = hermes_sync.collect_sessions(db, 0)[0][0]
        after = hermes_sync.collect_sessions(db, first_rowid)
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0][1]["provider"], "b")

    def test_tolerates_missing_model_and_task(self):
        db = self._db([
            {"billing_provider": "deepseek", "actual_cost_usd": 0.01,
             "estimated_cost_usd": 0, "input_tokens": 5, "output_tokens": 5,
             "cache_read_tokens": 0, "reasoning_tokens": 0},
        ], with_model=False)
        _, ev = hermes_sync.collect_sessions(db, 0)[0]
        self.assertNotIn("model", ev)         # column absent → omitted
        self.assertEqual(ev["task_type"], "agent")  # default

    def test_derives_hash_only_evidence_context_from_session_messages(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._paths = getattr(self, "_paths", []) + [path]
        conn = sqlite3.connect(path)
        conn.execute("""CREATE TABLE sessions (
            id TEXT PRIMARY KEY, billing_provider TEXT, model TEXT, task_type TEXT,
            started_at REAL, actual_cost_usd REAL, estimated_cost_usd REAL,
            input_tokens INT, output_tokens INT, cache_read_tokens INT,
            reasoning_tokens INT, system_prompt TEXT, model_config TEXT)""")
        conn.execute("""CREATE TABLE messages (
            id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT)""")
        conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("s-ledger", "openai", "gpt-fixture", "analysis", 1000.0,
                      0.10, 0.10, 10, 5, 0, 0, "policy text", '{"temperature":0}'))
        conn.executemany("INSERT INTO messages(id,session_id,role,content) VALUES(?,?,?,?)", [
            (1, "s-ledger", "user", "public source"),
            (2, "s-ledger", "tool", "retrieved evidence"),
            (3, "s-ledger", "assistant", "final conclusion"),
        ])
        conn.commit(); conn.close()

        _, ev = hermes_sync.collect_sessions(path, 0)[0]
        source_hashes = sorted([
            hashlib.sha256(b"public source").hexdigest(),
            hashlib.sha256(b"retrieved evidence").hexdigest(),
        ])
        policy_material = json.dumps(
            {"model_config": '{"temperature":0}', "system_prompt": "policy text"},
            sort_keys=True, separators=(",", ":"),
        ).encode()
        self.assertEqual(ev["external_ref"], "s-ledger")
        self.assertEqual(ev["evidence_hashes"], source_hashes)
        self.assertEqual(ev["result_hash"], hashlib.sha256(b"final conclusion").hexdigest())
        self.assertEqual(ev["policy_version"],
                         "hermes-policy/" + hashlib.sha256(policy_material).hexdigest()[:16])


def _make_pm_db():
    """A Hermes-shaped DB with an ``id`` PK and a v17 session_model_usage table
    holding one mid-session model switch (anthropic → openai)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE sessions (
        id TEXT PRIMARY KEY, billing_provider TEXT, model TEXT, task_type TEXT,
        started_at REAL, actual_cost_usd REAL, estimated_cost_usd REAL,
        input_tokens INT, output_tokens INT, cache_read_tokens INT,
        reasoning_tokens INT)""")
    conn.execute("""CREATE TABLE session_model_usage (
        session_id TEXT, model TEXT, billing_provider TEXT,
        input_tokens INT, output_tokens INT, cache_read_tokens INT,
        reasoning_tokens INT, estimated_cost_usd REAL,
        PRIMARY KEY (session_id, model, billing_provider))""")
    conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 ("s1", "anthropic", "claude-opus-4-8", "code_review",
                  1000.0, 1.00, 0.90, 1000, 500, 0, 0))
    conn.execute("INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?)",
                 ("s1", "claude-opus-4-8", "anthropic", 700, 300, 0, 0, 0.60))
    conn.execute("INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?)",
                 ("s1", "gpt-5", "openai", 300, 200, 0, 0, 0.30))
    conn.commit()
    conn.close()
    return path


class TestDefaultStateDb(unittest.TestCase):
    """#171: state.db default resolution — LEDGER_STATE_DB > $HERMES_HOME/
    state.db (when it exists) > the legacy hardcoded path."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.hh_db = os.path.join(self.tmp, "state.db")
        open(self.hh_db, "w").close()

    def test_explicit_env_wins(self):
        env = {"LEDGER_STATE_DB": "/explicit/x.db", "HERMES_HOME": self.tmp}
        self.assertEqual(hermes_sync.default_state_db(env), "/explicit/x.db")

    def test_hermes_home_state_db_when_present(self):
        self.assertEqual(hermes_sync.default_state_db({"HERMES_HOME": self.tmp}),
                         self.hh_db)

    def test_hermes_home_without_file_falls_back(self):
        os.unlink(self.hh_db)
        self.assertEqual(hermes_sync.default_state_db({"HERMES_HOME": self.tmp}),
                         hermes_sync.DEFAULT_STATE_DB)

    def test_no_env_falls_back(self):
        self.assertEqual(hermes_sync.default_state_db({}),
                         hermes_sync.DEFAULT_STATE_DB)


class TestPerModelSync(unittest.TestCase):
    def tearDown(self):
        for p in getattr(self, "_paths", []):
            for ext in ("", "-wal", "-shm"):
                try:
                    os.unlink(p + ext)
                except OSError:
                    pass

    def test_switch_emits_one_event_per_provider(self):
        db = _make_pm_db()
        self._paths = [db]
        pairs = hermes_sync.collect_sessions(db, 0, workspace="hermes")
        self.assertEqual(len(pairs), 2)               # one per model
        rowids = {rid for rid, _ in pairs}
        self.assertEqual(len(rowids), 1)              # sharing the session rowid
        by_prov = {ev["provider"]: ev for _, ev in pairs}
        self.assertEqual(set(by_prov), {"anthropic", "openai"})
        self.assertAlmostEqual(by_prov["anthropic"]["cost_usd"], 1.00 * 0.60 / 0.90)
        self.assertAlmostEqual(by_prov["openai"]["cost_usd"], 1.00 * 0.30 / 0.90)
        # authoritative total preserved
        self.assertAlmostEqual(sum(ev["cost_usd"] for _, ev in pairs), 1.00)
        self.assertEqual(by_prov["openai"]["input_tokens"], 300)
        self.assertEqual(by_prov["openai"]["model"], "gpt-5")

    def test_batches_never_split_a_session(self):
        # Two sessions, three events sharing rowids; a size-1 batch must still
        # keep each session's events together (cut only at rowid boundaries).
        pairs = [(1, {}), (1, {}), (2, {})]
        chunks = list(hermes_sync._batches(pairs, 1))
        self.assertEqual([[r for r, _ in c] for c in chunks], [[1, 1], [2]])


class TestFoldedWarning(unittest.TestCase):
    """#170: surface a server-side workspace fold from an ingest response."""

    def test_single_event_folded(self):
        resp = {"recorded": True, "workspace_folded": True,
                "workspace_note": "tier workspace cap reached: ..."}
        self.assertEqual(hermes_sync.folded_warning(resp),
                         "tier workspace cap reached: ...")

    def test_batch_one_folded(self):
        resp = {"recorded": 2, "results": [
            {"recorded": True, "workspace_folded": False},
            {"recorded": True, "workspace_folded": True},
        ]}
        warn = hermes_sync.folded_warning(resp)
        self.assertIsNotNone(warn)
        self.assertIn("workspace", warn)

    def test_no_fold_is_silent(self):
        self.assertIsNone(hermes_sync.folded_warning(
            {"recorded": True, "workspace_folded": False}))
        self.assertIsNone(hermes_sync.folded_warning(
            {"recorded": 1, "results": [{"recorded": True}]}))


class TestBatchesByBytes(unittest.TestCase):
    """#413 fix: byte-budgeted batching that still never splits a session."""

    def test_never_splits_a_session(self):
        pairs = [(1, {"m": "x" * 1000}), (1, {"m": "y" * 1000}),
                 (2, {"m": "z" * 1000})]
        chunks = list(hermes_sync._batches_by_bytes(pairs, 100, 2))
        self.assertEqual([c[0] for c in chunks[0]], [1, 1])
        self.assertEqual([c[0] for c in chunks[1]], [2])

    def test_respects_byte_budget(self):
        pairs = [(i, {"payload": "x" * 100}) for i in range(10)]
        chunks = list(hermes_sync._batches_by_bytes(pairs, 250, 100))
        for chunk in chunks:
            total = sum(
                len(json.dumps(p[1], separators=(",", ":"))) + 2
                for p in chunk)
            self.assertLessEqual(total, 250)

    def test_respects_count_cap(self):
        pairs = [(i, {"m": "x"}) for i in range(10)]
        chunks = list(hermes_sync._batches_by_bytes(pairs, 10 ** 6, 4))
        self.assertTrue(all(len(c) <= 4 for c in chunks))
        self.assertEqual(sum(len(c) for c in chunks), 10)

    def test_oversize_session_sent_whole(self):
        pairs = [(1, {"m": "x" * 5000}), (2, {"m": "y"})]
        chunks = list(hermes_sync._batches_by_bytes(pairs, 100, 2))
        self.assertEqual(len(chunks), 2)
        self.assertEqual([c[0] for c in chunks[0]], [1])

    def test_single_pair_at_or_over_budget_still_sent(self):
        pairs = [(1, {"m": "x" * 5000})]
        chunks = list(hermes_sync._batches_by_bytes(pairs, 100, 2))
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0][0][0], 1)


if __name__ == "__main__":
    unittest.main()
