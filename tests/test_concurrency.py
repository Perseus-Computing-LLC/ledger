"""P2: money correctness under real concurrency.

Boots the threaded HTTP server and hammers ONE org from many threads, then
checks the invariant that catches both lost writes and double-counts:

    final balance == grant + topups - refunds - (recorded debits x cost)  (exact)

Bounded so it's fast+deterministic in CI; the same driver
(`tools/concurrency_soak.py`) runs heavy as a standalone soak.
"""
import os
import sqlite3
import sys
import threading
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tools"))

import concurrency_soak as soak
from plutus_agent import metering
from plutus_agent.server import app


class TestConcurrency(unittest.TestCase):
    def test_concurrent_ingest_exact_balance(self):
        # Rate limit disabled so all 64 debits actually reach the DB writer path.
        r = soak.run_ingest_soak(threads=16, requests_per=4, cost=0.01,
                                 rate_per_min=0)
        self.assertEqual(r["recorded_debits"], 64)
        self.assertEqual(r["errors"], 0, r["sample_errors"])
        self.assertTrue(r["no_double_count"], r)          # events == recorded
        self.assertTrue(r["balance_exact_match"], r)      # no lost writes

    def test_concurrent_ingest_with_reversals_exact(self):
        # Debits race concurrent refund webhooks (each fired twice to prove
        # a replayed reversal converges and never double-reverses).
        r = soak.run_ingest_soak(threads=8, requests_per=4, cost=0.01,
                                 with_reversals=True, rate_per_min=0)
        self.assertEqual(r["errors"], 0, r["sample_errors"])
        self.assertGreater(r["refunds"], 0)
        self.assertTrue(r["no_double_count"], r)
        self.assertTrue(r["balance_exact_match"], r)

    def test_concurrent_duplicate_idempotency_charges_once(self):
        r = soak.run_idempotency_race(threads=24, cost=0.25)
        self.assertEqual(r["recorded_events"], 1, r)      # charged exactly once
        self.assertTrue(r["charged_once"], r)


class TestLockReturns503(unittest.TestCase):
    """A transient DB lock on ingest must be a retryable 503, not a 400.

    Surfaced by the soak under saturation: the blanket except returned 400
    ("don't retry"), which would make a client drop a chargeable event on a
    transient lock. Forced deterministically here by making record_usage raise
    OperationalError."""

    def test_operational_error_is_retryable_503(self):
        import http.client
        import tempfile
        fd, dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        httpd = None
        orig = metering.record_usage
        try:
            httpd, port, org_id, key = soak._boot(dbpath, grant_usd=100.0)

            def _boom(*a, **k):
                raise sqlite3.OperationalError("database is locked")

            metering.record_usage = _boom
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("POST", "/v1/usage",
                         body=b'{"provider":"anthropic","cost_usd":0.01}',
                         headers={"Content-Type": "application/json",
                                  "Authorization": f"Bearer {key}"})
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 503)
            self.assertEqual(resp.getheader("Retry-After"), "1")
            conn.close()
        finally:
            metering.record_usage = orig
            if httpd is not None:
                httpd.shutdown()
                httpd.server_close()
            for ext in ("", "-wal", "-shm"):
                try:
                    os.unlink(dbpath + ext)
                except OSError:
                    pass

    def test_lock_outside_ingest_txn_is_503(self):
        """A transient lock in the pre-transaction path (here the API-key auth
        lookup, `_bearer_org`) hits the OUTER request handler, which must also
        map OperationalError to a retryable 503 rather than a generic 500. The
        cloud soak at 500-way contention produced 5/30k such 500s before this."""
        import http.client
        import tempfile
        fd, dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        httpd = None
        orig = app.Handler._bearer_org
        try:
            httpd, port, org_id, key = soak._boot(dbpath, grant_usd=100.0)

            def _boom(self, conn):
                raise sqlite3.OperationalError("database is locked")

            app.Handler._bearer_org = _boom
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("POST", "/v1/usage",
                         body=b'{"provider":"anthropic","cost_usd":0.01}',
                         headers={"Content-Type": "application/json",
                                  "Authorization": f"Bearer {key}"})
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 503)
            self.assertEqual(resp.getheader("Retry-After"), "1")
            conn.close()
        finally:
            app.Handler._bearer_org = orig
            if httpd is not None:
                httpd.shutdown()
                httpd.server_close()
            for ext in ("", "-wal", "-shm"):
                try:
                    os.unlink(dbpath + ext)
                except OSError:
                    pass


if __name__ == "__main__":
    unittest.main()
