#!/usr/bin/env python3
"""HTTP hardening (2026-07-05 security review): security headers on every
response, and CSV export formula-injection neutralization."""
import os
import sys
import tempfile
import threading
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ledger_agent import db
from ledger_agent.config import DEFAULT_CONFIG
from ledger_agent.server import api, app


class TestCsvSafe(unittest.TestCase):
    def test_neutralizes_formula_leads(self):
        for bad in ("=HYPERLINK(1)", "+1", "-1", "@SUM(1)", "\tx", "\rx"):
            self.assertEqual(api._csv_safe(bad), "'" + bad,
                             f"{bad!r} should be prefixed with a quote")

    def test_leaves_safe_values_untouched(self):
        for ok in ("openai", "gpt-5", "general", "workspace-1", "", "1.5"):
            self.assertEqual(api._csv_safe(ok), ok)

    def test_non_strings_passthrough(self):
        for v in (0, 1, 3.14, None, True):
            self.assertEqual(api._csv_safe(v), v)


class TestSecurityHeaders(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls.dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = db.connect(cls.dbpath); db.init_schema(conn); conn.close()
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

    def test_security_headers_present(self):
        r = urllib.request.urlopen(f"http://127.0.0.1:{self.port}/healthz", timeout=5)
        self.assertEqual(r.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(r.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(r.headers.get("Referrer-Policy"), "same-origin")
        csp = r.headers.get("Content-Security-Policy", "")
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("default-src 'self'", csp)


if __name__ == "__main__":
    unittest.main()
