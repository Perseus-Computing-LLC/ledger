"""Savings-share bridge wiring (#7): the Hermes sync tags events with a baseline
model so hosted Ledger can price the counterfactual and record the saving.

Covers examples/hermes_sync.py — the env-driven baseline resolver, the
"only when routing happened" rule, and that collect_sessions attaches
baseline_model to real session events. Stdlib-only, like the script itself.
"""
import importlib.util
import os
import sqlite3
import tempfile
import unittest

# Load the install-free script by path (it lives under examples/, not the package).
_HERE = os.path.dirname(os.path.abspath(__file__))
_SYNC = os.path.join(_HERE, "..", "examples", "hermes_sync.py")
_spec = importlib.util.spec_from_file_location("hermes_sync", _SYNC)
hs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hs)


class TestResolveBaselineModels(unittest.TestCase):
    def test_off_by_default(self):
        self.assertEqual(hs.resolve_baseline_models({}), {})

    def test_flagship_map(self):
        m = hs.resolve_baseline_models({"LEDGER_BASELINE": "flagship"})
        self.assertEqual(m["anthropic"], "claude-opus-4-8")
        self.assertEqual(m["openai"], "gpt-5")

    def test_single_global_model(self):
        m = hs.resolve_baseline_models({"LEDGER_BASELINE_MODEL": "gpt-5"})
        self.assertEqual(m, {"*": "gpt-5"})

    def test_explicit_json_wins(self):
        m = hs.resolve_baseline_models({
            "LEDGER_BASELINE": "flagship",  # ignored when JSON is present
            "LEDGER_BASELINE_MODELS": '{"openai": "gpt-5"}',
        })
        self.assertEqual(m, {"openai": "gpt-5"})

    def test_bad_json_exits(self):
        with self.assertRaises(SystemExit):
            hs.resolve_baseline_models({"LEDGER_BASELINE_MODELS": "not json"})


class TestBaselineFor(unittest.TestCase):
    def test_none_when_off(self):
        self.assertIsNone(hs.baseline_for("anthropic", "claude-haiku-4-5", {}))

    def test_returns_provider_baseline(self):
        m = {"anthropic": "claude-opus-4-8"}
        self.assertEqual(hs.baseline_for("anthropic", "claude-haiku-4-5", m),
                         "claude-opus-4-8")

    def test_none_when_actual_is_baseline(self):
        # No routing happened → no saving to record.
        m = {"anthropic": "claude-opus-4-8"}
        self.assertIsNone(hs.baseline_for("anthropic", "claude-opus-4-8", m))

    def test_global_star_applies_to_any_provider(self):
        m = {"*": "gpt-5"}
        self.assertEqual(hs.baseline_for("openai", "gpt-5-mini", m), "gpt-5")

    def test_family_inferred_from_model_not_mistagged_provider(self):
        # An Opus call mis-tagged with billing_provider='deepseek' must resolve to
        # the ANTHROPIC flagship (opus) via the model name → no spurious baseline,
        # not a nonsensical opus→deepseek-v4-pro pairing.
        m = dict(hs.FLAGSHIP_BASELINE)
        self.assertIsNone(hs.baseline_for("deepseek", "claude-opus-4-8", m))
        # A cheaper anthropic model mis-tagged 'deepseek' still bills vs opus.
        self.assertEqual(hs.baseline_for("deepseek", "claude-haiku-4-5", m),
                         "claude-opus-4-8")

    def test_vendor_prefix_normalized(self):
        m = dict(hs.FLAGSHIP_BASELINE)
        # 'deepseek/deepseek-v4-pro' is the deepseek flagship → no saving.
        self.assertIsNone(hs.baseline_for("", "deepseek/deepseek-v4-pro", m))
        # 'models/gemini-2.5-pro' → google family → billed vs gemini-3.1-pro.
        self.assertEqual(hs.baseline_for("", "models/gemini-2.5-pro", m),
                         "gemini-3.1-pro")


class TestCollectSessionsAttachesBaseline(unittest.TestCase):
    def _mk_db(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(path)
        conn.execute("""CREATE TABLE sessions (
            id TEXT, started_at REAL, billing_provider TEXT, model TEXT,
            task_type TEXT, actual_cost_usd REAL, estimated_cost_usd REAL,
            input_tokens INT, output_tokens INT, cache_read_tokens INT,
            reasoning_tokens INT)""")
        conn.execute("INSERT INTO sessions VALUES "
                     "('s1', 1, 'anthropic', 'claude-haiku-4-5', 'agent', "
                     "1.0, 1.0, 1000, 500, 0, 0)")
        conn.execute("INSERT INTO sessions VALUES "
                     "('s2', 2, 'anthropic', 'claude-opus-4-8', 'agent', "
                     "5.0, 5.0, 1000, 500, 0, 0)")
        conn.commit()
        conn.close()
        return path

    def test_baseline_attached_only_to_routed_session(self):
        path = self._mk_db()
        try:
            pairs = hs.collect_sessions(
                path, 0, "hermes", {"anthropic": "claude-opus-4-8"})
            events = {e["model"]: e for _, e in pairs}
            # routed-away-from-opus session gets a baseline
            self.assertEqual(events["claude-haiku-4-5"]["baseline_model"],
                             "claude-opus-4-8")
            # session already on opus: no baseline (no routing)
            self.assertNotIn("baseline_model", events["claude-opus-4-8"])
        finally:
            for ext in ("", "-wal", "-shm"):
                try:
                    os.unlink(path + ext)
                except OSError:
                    pass

    def test_no_baseline_when_savings_off(self):
        path = self._mk_db()
        try:
            pairs = hs.collect_sessions(path, 0, "hermes", {})
            for _, e in pairs:
                self.assertNotIn("baseline_model", e)
        finally:
            for ext in ("", "-wal", "-shm"):
                try:
                    os.unlink(path + ext)
                except OSError:
                    pass


if __name__ == "__main__":
    unittest.main()
