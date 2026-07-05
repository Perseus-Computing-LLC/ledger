#!/usr/bin/env python3
"""Transaction-atomicity guarantees (2026-07-05 security review): every money
side effect commits in the SAME transaction as the marker that guards it, so a
crash or exception between them rolls BOTH back."""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plutus_agent import db, metering
from plutus_agent.billing import handle_webhook_event
from plutus_agent.config import DEFAULT_CONFIG
from plutus_agent.server import app


class TestWebhookAtomicity(unittest.TestCase):
    """F2/F5/F6: the Stripe event claim and its ledger effect commit together, so
    a failure between them rolls BOTH back — no claimed-but-unapplied event
    (silent credit loss) and no half-applied refund."""

    def setUp(self):
        fd, self.dbpath = tempfile.mkstemp(suffix=".db"); os.close(fd)
        self.conn = db.connect(self.dbpath); db.init_schema(self.conn)
        self.org = db.create_org(self.conn, "Acme")["id"]
        db.set_stripe_customer(self.conn, self.org, "cus_1")
        db.add_ledger(self.conn, self.org, 50.0, "topup", stripe_ref="pi_seed")

    def tearDown(self):
        self.conn.close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(self.dbpath + ext)
            except OSError:
                pass

    def _checkout(self, event_id):
        return handle_webhook_event(self.conn, {
            "id": event_id, "type": "checkout.session.completed",
            "data": {"object": {"id": "cs_1", "mode": "payment", "customer": "cus_1",
                                "amount_total": 1000, "metadata": {"kind": "credit"},
                                "payment_intent": "pi_new"}}})

    def _claim_exists(self, event_id):
        return self.conn.execute(
            "SELECT 1 FROM stripe_events WHERE event_id=?", (event_id,)).fetchone() is not None

    def test_apply_failure_rolls_back_claim_and_balance(self):
        def boom(*a, **k):
            raise RuntimeError("simulated crash mid-apply")
        orig = db.add_ledger
        db.add_ledger = boom
        try:
            with self.assertRaises(RuntimeError):
                self._checkout("evt_fail")
        finally:
            db.add_ledger = orig
        # The claim was rolled back (so Stripe's retry is honored) and no credit
        # was applied — the pre-fix bug left the claim committed + credit lost.
        self.assertFalse(self._claim_exists("evt_fail"))
        self.assertAlmostEqual(db.get_balance(self.conn, self.org), 50.0, places=6)
        # Retry now applies exactly once.
        res = self._checkout("evt_fail")
        self.assertEqual(res["status"], "credited")
        self.assertAlmostEqual(db.get_balance(self.conn, self.org), 60.0, places=6)

    def test_duplicate_event_credits_once(self):
        self.assertEqual(self._checkout("evt_dup")["status"], "credited")
        self.assertEqual(self._checkout("evt_dup")["status"], "duplicate")
        self.assertAlmostEqual(db.get_balance(self.conn, self.org), 60.0, places=6)


class TestHardStopMicros(unittest.TestCase):
    """#14: the prepaid hard-stop decides in integer micro-dollars, not float USD."""

    def setUp(self):
        fd, self.dbpath = tempfile.mkstemp(suffix=".db"); os.close(fd)
        self.conn = db.connect(self.dbpath); db.init_schema(self.conn)
        self.org = db.create_org(self.conn, "Acme", tier="pro")["id"]

    def tearDown(self):
        self.conn.close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(self.dbpath + ext)
            except OSError:
                pass

    def test_boundary_allows_to_exact_zero_then_blocks(self):
        db.add_ledger(self.conn, self.org, 0.000002, "topup")  # 2 micro-dollars
        rs = [metering.record_usage(self.conn, self.org, "openai",
                                    cost_usd=0.000001, block_over_balance=True)
              for _ in range(3)]
        self.assertTrue(rs[0].recorded)
        self.assertTrue(rs[1].recorded)           # drains to exactly 0 (0 is not < 0)
        self.assertFalse(rs[2].recorded)          # 0 - 1 micro < 0 -> blocked
        self.assertTrue(rs[2].over_balance)
        self.assertEqual(db.get_balance_micros(self.conn, self.org), 0)


class TestIdempotencyAtomicStore(unittest.TestCase):
    """#4: the idempotent response is stored in the SAME txn as the debits, so a
    successful keyed ingest never leaves a reclaimable NULL-status claim that a
    later retry could re-record (double-debit)."""

    @classmethod
    def setUpClass(cls):
        fd, cls.dbpath = tempfile.mkstemp(suffix=".db"); os.close(fd)
        conn = db.connect(cls.dbpath); db.init_schema(conn)
        cls.org = db.create_org(conn, "Acme", tier="pro")["id"]
        _, cls.key = db.create_api_key(conn, cls.org)
        db.add_ledger(conn, cls.org, 100.0, "topup"); conn.close()
        ctx = app._Ctx(dict(DEFAULT_CONFIG), cls.dbpath, demo=False)
        cls.httpd = app._Server(("127.0.0.1", 0), app.Handler, ctx)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown(); cls.httpd.server_close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.remove(cls.dbpath + ext)
            except OSError:
                pass

    def _post(self, idem):
        data = json.dumps({"provider": "openai", "input_tokens": 10}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/usage", data=data, method="POST",
            headers={"Authorization": f"Bearer {self.key}",
                     "Content-Type": "application/json", "Idempotency-Key": idem})
        r = urllib.request.urlopen(req, timeout=5)
        return r.status, json.loads(r.read().decode())

    def test_response_stored_with_non_null_status(self):
        st, _ = self._post("key-atomic-1")
        self.assertEqual(st, 200)
        conn = db.connect(self.dbpath)
        try:
            row = db.idempotency_response(conn, self.org, "key-atomic-1")
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertIsNotNone(row[0])  # status non-NULL => never reclaimed => no double-debit

    def test_retry_replays_without_double_debit(self):
        self._post("key-atomic-2")
        bal1 = db.get_balance(db.connect(self.dbpath), self.org)
        _, body = self._post("key-atomic-2")
        self.assertTrue(body.get("idempotent_replay"))
        bal2 = db.get_balance(db.connect(self.dbpath), self.org)
        self.assertAlmostEqual(bal1, bal2, places=6)  # retry did not debit again


if __name__ == "__main__":
    unittest.main()
