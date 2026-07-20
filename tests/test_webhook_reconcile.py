from __future__ import annotations

import unittest

from plutus_agent import db
from plutus_agent import reconcile_webhooks as rw


def _mem():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    return conn


class TestMissingEvents(unittest.TestCase):
    def test_filters_already_processed(self):
        conn = _mem()
        db.mark_stripe_event(conn, "evt_done", "checkout.session.completed")
        events = [{"id": "evt_done", "type": "x", "created": 1},
                  {"id": "evt_gap", "type": "x", "created": 2}]
        missing = rw.missing_events(conn, events)
        self.assertEqual([e["id"] for e in missing], ["evt_gap"])
        conn.close()


class TestReconcile(unittest.TestCase):
    def setUp(self):
        self.conn = _mem()
        self.org_id = db.create_org(self.conn, "Acme", tier="free")["id"]
        self._orig_fetch = rw.fetch_events
        rw.fetch_events = lambda secret, types, since: self._events()

    def tearDown(self):
        rw.fetch_events = self._orig_fetch
        self.conn.close()

    def _events(self):
        return [{"id": "evt_1", "type": "checkout.session.completed", "created": 100,
                 "data": {"object": {"metadata": {"plutus_org_id": self.org_id,
                                                  "kind": "credit"},
                                     "amount_total": 500, "mode": "payment",
                                     "payment_intent": "pi_fake_1"}}}]

    def test_dry_run_reports_without_applying(self):
        report = rw.reconcile(self.conn, "sk_fake", days=1, apply=False)
        self.assertEqual(len(report["missing"]), 1)
        self.assertEqual(report["missing"][0]["id"], "evt_1")
        self.assertEqual(report["applied"], [])
        self.assertNotIn("evt_1", rw.processed_ids(self.conn))

    def test_apply_replays_marks_and_is_idempotent(self):
        report = rw.reconcile(self.conn, "sk_fake", days=1, apply=True)
        self.assertEqual(len(report["applied"]), 1)
        self.assertEqual(report["applied"][0]["status"], "credited")
        self.assertIn("evt_1", rw.processed_ids(self.conn))

        again = rw.reconcile(self.conn, "sk_fake", days=1, apply=True)
        self.assertEqual(again["missing"], [])
        self.assertEqual(again["applied"], [])

        # credited exactly once ($5.00)
        self.assertAlmostEqual(float(db.get_balance(self.conn, self.org_id)), 5.0)


if __name__ == "__main__":
    unittest.main()
