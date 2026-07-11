"""Smoke test for the before/after attribution exhibit tool (Task 4).

Guards the logic that backs the dogfooding chart: on a state.db with a
mid-session provider switch, the OLD aggregate puts all cost on the initial
provider while the #97 per-model logic splits it, with the total preserved.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tools"))

import attribution_exhibit as ex


class TestAttributionExhibit(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        c = sqlite3.connect(self.db)
        c.execute("""CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at REAL,
            actual_cost_usd REAL, estimated_cost_usd REAL, billing_provider TEXT,
            model TEXT, input_tokens INT, output_tokens INT, cache_read_tokens INT,
            reasoning_tokens INT)""")
        c.execute("""CREATE TABLE session_model_usage (session_id TEXT, model TEXT,
            billing_provider TEXT, input_tokens INT, output_tokens INT,
            cache_read_tokens INT, reasoning_tokens INT, estimated_cost_usd REAL,
            PRIMARY KEY (session_id, model, billing_provider))""")
        # started on anthropic, switched to openai; $1.00 actual cost
        c.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?)",
                  ("s1", 1000.0, 1.00, 0.90, "anthropic", "claude-opus-4-8",
                   1000, 500, 0, 0))
        c.execute("INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?)",
                  ("s1", "claude-opus-4-8", "anthropic", 700, 300, 0, 0, 0.60))
        c.execute("INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?)",
                  ("s1", "gpt-5", "openai", 300, 200, 0, 0, 0.30))
        c.commit()
        c.close()

    def tearDown(self):
        for e in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db + e)
            except OSError:
                pass

    def test_before_dumps_all_on_initial_provider(self):
        before = ex.before_spend(self.db)
        self.assertAlmostEqual(before["anthropic"], 1.00)
        self.assertNotIn("openai", before)      # switched-to provider invisible

    def test_after_splits_and_preserves_total(self):
        after = ex.after_spend(self.db)
        self.assertAlmostEqual(after["anthropic"], 1.00 * 0.60 / 0.90)
        self.assertAlmostEqual(after["openai"], 1.00 * 0.30 / 0.90)
        self.assertAlmostEqual(sum(after.values()), 1.00)  # total preserved

    def test_svg_is_wellformed(self):
        svg = ex._svg(ex.before_spend(self.db), ex.after_spend(self.db), False)
        self.assertTrue(svg.startswith("<svg"))
        self.assertTrue(svg.strip().endswith("</svg>"))


if __name__ == "__main__":
    unittest.main()
