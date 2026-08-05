#!/usr/bin/env python3
"""#67: schema-version stamping, the reader, and the forward-incompat guard."""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plutus_agent import db


_DOCUMENTED_MIGRATIONS = {
    "4": {"cost_micros", "delta_micros", "balance_after_micros", "monthly_budget_micros"},
    "5": {"ingest_idempotency"},
    "6": {"prev_hash", "row_hash"},
    "7": {"baseline_micros", "savings_invoices"},
    "8": {"optimal_micros"},
    "9": {"chain_checkpoints"},
    "10": {"external_ref", "ix_usage_extref"},
    "11": {"cache_write_tokens"},
    "12": {"user_id", "active", "ix_usage_user"},
    "13": {"stripe_subscription_id"},
    "14": {"scope", "event_count", "rotation_of", "ingest_health"},
    "15": {"evidence_hashes", "policy_version", "result_hash", "human_review", "correction_ref"},
    "16": {"agent_id", "authority_manifest_ref", "scope_anchor", "action_intent_hash", "action_status", "approval_ref"},
    "17": {"context_render_schema", "context_render_hash", "served_memory_provenance_hash", "action_receipt_hash", "resource_constraints_version", "resource_constraints_hash", "prebind_json", "prebind_hash", "reconciliation_note"},
}


class TestSchemaVersion(unittest.TestCase):
    def setUp(self):
        fd, self.dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(self.dbpath + ext)
            except OSError:
                pass

    def test_fresh_db_stamped_with_current_version(self):
        conn = db.connect(self.dbpath)
        db.init_schema(conn)
        self.assertEqual(db.get_schema_version(conn), db.SCHEMA_VERSION)
        conn.close()

    def test_reader_none_for_uninitialized_db(self):
        conn = db.connect(self.dbpath)
        # No meta table yet.
        self.assertIsNone(db.get_schema_version(conn))
        conn.close()

    def test_reinit_is_idempotent(self):
        conn = db.connect(self.dbpath)
        db.init_schema(conn)
        db.init_schema(conn)  # must not raise
        self.assertEqual(db.get_schema_version(conn), db.SCHEMA_VERSION)
        conn.close()

    def test_refuses_db_from_newer_plutus(self):
        conn = db.connect(self.dbpath)
        db.init_schema(conn)
        # Simulate a database written by a future Plutus.
        conn.execute("UPDATE meta SET value=? WHERE key='schema_version'",
                     (str(db.SCHEMA_VERSION + 1),))
        conn.commit()
        with self.assertRaises(RuntimeError):
            db.init_schema(conn)
        conn.close()

    def test_schema_docs_match_runtime_v17_migration_contract(self):
        docs_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "docs", "schema.md")
        with open(docs_path, encoding="utf-8") as handle:
            docs = handle.read()
        self.assertEqual(db.SCHEMA_VERSION, 17)
        self.assertIn("SCHEMA_VERSION=17", docs)

        table_rows = {}
        for line in docs.splitlines():
            cells = [cell.strip() for cell in line.split("|")]
            if len(cells) >= 3 and cells[1].isdigit():
                table_rows[cells[1]] = cells[2]
        self.assertEqual(set(_DOCUMENTED_MIGRATIONS), set(table_rows))
        for version, markers in _DOCUMENTED_MIGRATIONS.items():
            for marker in markers:
                self.assertIn(marker, table_rows[version],
                              f"schema v{version} docs omit runtime marker {marker}")

        conn = db.connect(self.dbpath)
        db.init_schema(conn)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(usage_events)")}
        self.assertTrue({
            "context_render_schema", "context_render_hash",
            "served_memory_provenance_hash", "action_receipt_hash",
            "resource_constraints_version", "resource_constraints_hash",
            "prebind_json", "prebind_hash", "reconciliation_note",
        } <= columns)
        conn.close()


if __name__ == "__main__":
    unittest.main()
