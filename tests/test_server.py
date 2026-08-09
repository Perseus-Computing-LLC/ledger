#!/usr/bin/env python3
"""Integration smoke test: boot the HTTP server on an ephemeral port."""
import io
import json
import os
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ledger_agent import Meter, db, demo, metering
from ledger_agent.client import LedgerAuthError, LedgerError
from ledger_agent.config import DEFAULT_CONFIG
from ledger_agent.server import app


class TestServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls.dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = db.connect(cls.dbpath)
        cls.org_id = demo.seed(conn, events=120)
        _, cls.key = db.create_api_key(conn, cls.org_id, name="test")
        conn.close()

        ctx = app._Ctx(dict(DEFAULT_CONFIG), cls.dbpath, demo=True)
        cls.httpd = app._Server(("127.0.0.1", 0), app.Handler, ctx)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(cls.dbpath + ext)
            except OSError:
                pass

    def _get(self, path):
        url = f"http://127.0.0.1:{self.port}{path}"
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.read().decode()

    def _post(self, path, payload, token=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_healthz(self):
        status, body = self._get("/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_dashboard_renders(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("Perseus Ledger", body)
        self.assertIn("verifiable activity", body)
        self.assertNotIn("Hermes Cloud", body)
        self.assertNotIn("Hermes Agent", body)
        self.assertIn("Your AI spend", body)
        self.assertIn("Spend by workspace", body)
        self.assertIn("#0a1018", body)  # the monitor canvas color is present

    def test_api_evidence_receipt_by_external_ref(self):
        conn = db.connect(self.dbpath)
        metering.record_usage(
            conn, self.org_id, provider="openai", model="gpt-fixture",
            task_type="artifact", external_ref="demo-artifact-1",
            input_tokens=1, output_tokens=1, cost_usd=0.01,
        )
        conn.close()

        status, body = self._get(
            f"/api/audit?org={self.org_id}&external_ref=demo-artifact-1")
        receipt = json.loads(body)

        self.assertEqual(status, 200)
        self.assertEqual(receipt["receipt_version"], "perseus-evidence-receipt/v1")
        self.assertEqual(receipt["external_ref"], "demo-artifact-1")
        self.assertEqual(receipt["events"][0]["action"], "artifact")

    def test_usage_ingests_decision_evidence_for_receipts(self):
        source_hash = "c" * 64
        result_hash = "d" * 64
        status, _ = self._post("/v1/usage", {
            "provider": "openai", "model": "gpt-fixture", "task_type": "recommend",
            "external_ref": "api-decision-1", "input_tokens": 1, "output_tokens": 1,
            "cost_usd": 0.01, "evidence_hashes": [source_hash],
            "policy_version": "policy/v1", "result_hash": result_hash,
            "human_review": "approved",
        }, token=self.key)
        self.assertEqual(status, 200)

        status, body = self._get(
            f"/api/audit?org={self.org_id}&external_ref=api-decision-1")
        receipt = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(receipt["events"][0]["evidence"]["source_hashes"], [source_hash])
        self.assertEqual(receipt["events"][0]["decision_context"]["policy_version"], "policy/v1")

    def test_dashboard_uses_a_monitor_layout_with_clear_action_hierarchy(self):
        """The primary dashboard is a dense monitor, not a stack of generic cards."""
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn('<main class="dashboard wrap"', body)
        self.assertIn('class="hero-metric"', body)
        self.assertIn('class="section-title"', body)
        self.assertIn('class="btn btn-primary"', body)
        self.assertIn('font-variant-numeric:tabular-nums', body)
        self.assertIn('aria-live="polite"', body)
        self.assertNotIn("Credit balance", body)
        self.assertNotIn("Low credit balance", body)

    def test_pricing_page_public(self):
        status, body = self._get("/pricing")
        self.assertEqual(status, 200)
        for name in ("Free", "Pro", "Enterprise"):
            self.assertIn(name, body)
        self.assertIn("$20", body)
        self.assertIn("Contact sales", body)

    def test_api_summary(self):
        status, body = self._get("/api/summary")
        self.assertEqual(status, 200)
        d = json.loads(body)
        self.assertIn("balance", d)
        self.assertIn("by_provider", d)
        self.assertGreater(len(d["by_provider"]), 0)

    def test_api_orgs(self):
        status, body = self._get("/api/orgs")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(json.loads(body)), 1)

    def test_404(self):
        try:
            self._get("/nope")
            self.fail("expected 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    # ---- ingest API ----------------------------------------------------------
    def test_ingest_requires_key(self):
        status, body = self._post("/v1/usage", {"provider": "anthropic"})
        self.assertEqual(status, 401)

    def test_ingest_bad_key_rejected(self):
        status, _ = self._post("/v1/usage", {"provider": "anthropic"},
                               token="ledger_sk_bogus")
        self.assertEqual(status, 401)

    def test_ingest_records_event(self):
        status, body = self._post("/v1/usage", {
            "provider": "anthropic", "model": "claude-opus-4-8",
            "input_tokens": 1200, "output_tokens": 800, "workspace": "prod",
        }, token=self.key)
        self.assertEqual(status, 200)
        self.assertTrue(body["recorded"])
        self.assertTrue(body["event_id"].startswith("evt_"))
        self.assertGreater(body["cost_usd"], 0)
        self.assertEqual(body["org_id"], self.org_id)

    def test_ingest_missing_provider_400(self):
        status, _ = self._post("/v1/usage", {"input_tokens": 10}, token=self.key)
        self.assertEqual(status, 400)

    def test_ingest_batch(self):
        status, body = self._post("/v1/usage", [
            {"provider": "anthropic", "input_tokens": 100, "cost_usd": 0.01},
            {"provider": "google", "input_tokens": 200, "cost_usd": 0.02},
        ], token=self.key)
        self.assertEqual(status, 200)
        self.assertEqual(len(body["results"]), 2)
        self.assertEqual(body["recorded"], 2)

    # ---- SDK remote mode (Meter → /v1/usage) --------------------------------
    def _remote_meter(self, **kw):
        return Meter(remote=f"http://127.0.0.1:{self.port}", api_key=self.key, **kw)

    def test_remote_meter_records(self):
        m = self._remote_meter()
        self.assertTrue(m.is_remote)
        r = m.track(provider="anthropic", model="claude-opus-4-8",
                    input_tokens=1000, output_tokens=500, workspace="prod")
        self.assertTrue(r.recorded)
        self.assertTrue(r.event_id.startswith("evt_"))
        self.assertGreater(r.cost_usd, 0)
        m.close()

    def test_remote_meter_maps_savings(self):
        # #143: the live /v1/usage response has carried savings_usd/leaked_usd
        # since #7/#134, but _track_remote's response→result mapping predated
        # them, so a remote track() with a baseline reported savings_usd=0.0
        # while the ledger recorded the saving. Assert the caller-visible
        # result agrees with the live response shape.
        m = self._remote_meter()
        r = m.track(provider="anthropic", model="claude-opus-4-8",
                    input_tokens=25_497, output_tokens=500,
                    baseline_input_tokens=104_180, baseline_output_tokens=500)
        self.assertTrue(r.recorded)
        self.assertGreater(r.savings_usd, 0)
        self.assertIsNotNone(r.baseline_usd)
        self.assertAlmostEqual(r.savings_usd, r.baseline_usd - r.cost_usd,
                               places=6)
        self.assertEqual(r.leaked_usd, 0.0)
        m.close()

    def test_remote_meter_old_server_defaults(self):
        # #143: an older server that omits the savings fields must map to the
        # dataclass defaults (0.0 / None), not crash or invent a saving.
        import urllib.request as ur

        class _FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self):
                return b'{"recorded": true, "cost_usd": 0.01, "balance_after": 0.0}'

        orig = ur.urlopen
        ur.urlopen = lambda req, *a, **k: _FakeResp()
        try:
            r = Meter(remote="http://x", api_key="ledger_sk_x").track(
                provider="anthropic", input_tokens=1, baseline_input_tokens=100)
        finally:
            ur.urlopen = orig
        self.assertTrue(r.recorded)
        self.assertEqual(r.savings_usd, 0.0)
        self.assertIsNone(r.baseline_usd)
        self.assertEqual(r.leaked_usd, 0.0)
        self.assertFalse(r.over_balance)
        self.assertFalse(r.unpriced)

    def test_remote_meter_bad_key_raises(self):
        m = Meter(remote=f"http://127.0.0.1:{self.port}", api_key="ledger_sk_bogus")
        with self.assertRaises(LedgerAuthError):
            m.track(provider="anthropic", input_tokens=10)

    def test_remote_meter_no_key_errors(self):
        # LEDGER_API_KEY may be set in the environment
        old_key = os.environ.pop("LEDGER_API_KEY", None)
        try:
            with self.assertRaises(ValueError):
                Meter(remote=f"http://127.0.0.1:{self.port}")
        finally:
            if old_key is not None:
                os.environ["LEDGER_API_KEY"] = old_key

    def test_remote_balance_is_local_only(self):
        m = self._remote_meter()
        with self.assertRaises(LedgerError):
            m.balance()
        m.close()

    def test_remote_meter_sends_real_user_agent(self):
        # Cloudflare (error 1010) hard-blocks the default "Python-urllib" UA, so
        # the SDK must send its own or ingest breaks behind the proxy.
        import urllib.request as ur
        captured = {}

        class _FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self):
                return b'{"recorded": true, "cost_usd": 0.0, "balance_after": 0.0}'

        orig = ur.urlopen
        ur.urlopen = lambda req, *a, **k: (
            captured.update(ua=req.get_header("User-agent")) or _FakeResp())
        try:
            Meter(remote="http://x", api_key="ledger_sk_x").track(
                provider="anthropic", input_tokens=1)
        finally:
            ur.urlopen = orig
        self.assertTrue(captured["ua"])
        self.assertTrue(captured["ua"].startswith("ledger-agent"))
        self.assertNotIn("urllib", captured["ua"].lower())


class TestIngestQuota(unittest.TestCase):
    """Free org past its cap with hard-blocking on → 402."""
    @classmethod
    def setUpClass(cls):
        fd, cls.dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        # Free ships unlimited now; pin a capped variant to exercise the 402 path.
        import dataclasses
        from ledger_agent import pricing
        cls._orig_free = pricing.TIERS["free"]
        pricing.TIERS["free"] = dataclasses.replace(
            cls._orig_free, tracked_tokens_month=10_000, workspaces=1)
        conn = db.connect(cls.dbpath)
        db.init_schema(conn)
        cls.org_id = db.create_org(conn, "Free Co", tier="free")["id"]
        _, cls.key = db.create_api_key(conn, cls.org_id)
        # blow past the 10K free cap
        from ledger_agent import metering
        metering.record_usage(conn, cls.org_id, provider="anthropic",
                              input_tokens=11_000, cost_usd=0.0)
        conn.close()

        cfg = dict(DEFAULT_CONFIG)
        cfg["pricing"] = dict(cfg["pricing"], block_over_free_limit=True)
        ctx = app._Ctx(cfg, cls.dbpath, demo=False)
        cls.httpd = app._Server(("127.0.0.1", 0), app.Handler, ctx)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        from ledger_agent import pricing
        pricing.TIERS["free"] = cls._orig_free
        cls.httpd.shutdown()
        cls.httpd.server_close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(cls.dbpath + ext)
            except OSError:
                pass

    def test_over_quota_returns_402(self):
        url = f"http://127.0.0.1:{self.port}/v1/usage"
        req = urllib.request.Request(
            url, data=json.dumps({"provider": "anthropic", "input_tokens": 500}).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.key}"}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("expected 402")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 402)
            body = json.loads(e.read().decode())
            self.assertFalse(body["recorded"])
            self.assertIn("upgrade_url", body)

    def test_remote_meter_402_does_not_raise(self):
        # An over-quota event should report recorded=False, not crash the agent.
        m = Meter(remote=f"http://127.0.0.1:{self.port}", api_key=self.key)
        r = m.track(provider="anthropic", input_tokens=500)
        self.assertFalse(r.recorded)
        self.assertTrue(r.over_free_limit)
        m.close()


class TestBatchAtomicity(unittest.TestCase):
    """Fix #27: batch POST /v1/usage must be all-or-nothing."""
    @classmethod
    def setUpClass(cls):
        fd, cls.dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = db.connect(cls.dbpath)
        db.init_schema(conn)
        cls.org_id = db.create_org(conn, "Batch Co", tier="pro")["id"]
        _, cls.key = db.create_api_key(conn, cls.org_id)
        conn.close()

        ctx = app._Ctx(dict(DEFAULT_CONFIG), cls.dbpath, demo=False)
        cls.httpd = app._Server(("127.0.0.1", 0), app.Handler, ctx)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(cls.dbpath + ext)
            except OSError:
                pass
    
    def _post(self, payload):
        url = f"http://127.0.0.1:{self.port}/v1/usage"
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.key}"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())
    
    def test_malformed_second_event_records_nothing(self):
        """Fix #27: if event 2 is invalid, event 1 must not commit."""
        conn = db.connect(self.dbpath)
        from ledger_agent import metering
        before = metering.tracked_tokens_mtd(conn, self.org_id)
        conn.close()
        
        status, body = self._post([
            {"provider": "anthropic", "input_tokens": 1000},
            {"provider": "", "input_tokens": 500},  # invalid: empty provider
        ])
        self.assertEqual(status, 400)
        
        conn = db.connect(self.dbpath)
        after = metering.tracked_tokens_mtd(conn, self.org_id)
        conn.close()
        self.assertEqual(before, after, "No tokens should have been recorded")

    def test_baseline_tokens_recorded_with_savings(self):
        # #134: token-reduction counterfactual through the API. The reduced call
        # sent 25,497 input tokens; without the optimization it would have sent
        # 104,180. Priced at the event's own model, the saving must be positive
        # and the response must carry it.
        status, body = self._post({
            "provider": "anthropic", "model": "claude-opus-4-8",
            "input_tokens": 25497, "output_tokens": 500,
            "baseline_input_tokens": 104180, "baseline_output_tokens": 500,
        })
        self.assertEqual(status, 200)
        self.assertTrue(body["recorded"])
        self.assertGreater(body["savings_usd"], 0)

    def test_negative_baseline_tokens_rejected(self):
        # #134: same non-negative rule as the actual token fields; a negative
        # counterfactual could only inflate billable savings.
        status, body = self._post({
            "provider": "anthropic", "input_tokens": 100,
            "baseline_input_tokens": -1,
        })
        self.assertEqual(status, 400)
        self.assertIn("baseline token", body["error"])


class TestPrepaidHardStop(unittest.TestCase):
    """Fix #28: block_over_balance prevents debits past zero."""
    @classmethod
    def setUpClass(cls):
        fd, cls.dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = db.connect(cls.dbpath)
        db.init_schema(conn)
        cls.org_id = db.create_org(conn, "Prepaid Co", tier="pro")["id"]
        _, cls.key = db.create_api_key(conn, cls.org_id)
        db.add_ledger(conn, cls.org_id, 1.0, "topup")  # $1 credit
        conn.close()

        cfg = dict(DEFAULT_CONFIG)
        cfg["pricing"] = dict(cfg["pricing"], block_over_balance=True)
        ctx = app._Ctx(cfg, cls.dbpath, demo=False)
        cls.httpd = app._Server(("127.0.0.1", 0), app.Handler, ctx)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(cls.dbpath + ext)
            except OSError:
                pass
    
    def test_over_balance_returns_402(self):
        """Fix #28: POST /v1/usage with cost > balance returns 402."""
        url = f"http://127.0.0.1:{self.port}/v1/usage"
        req = urllib.request.Request(
            url, data=json.dumps({"provider": "anthropic", "cost_usd": 10.0}).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.key}"}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("expected 402")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 402)
            body = json.loads(e.read().decode())
            self.assertFalse(body["recorded"])
            self.assertTrue(body["over_balance"])
            self.assertIn("credit exhausted", body["error"])


class TestBatchPartialBlock(unittest.TestCase):
    """#62: a batch with some over-balance rejections must surface them — a 200
    is not "all recorded", and the prepaid hard-stop count was missing entirely."""
    @classmethod
    def setUpClass(cls):
        fd, cls.dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = db.connect(cls.dbpath)
        db.init_schema(conn)
        cls.org_id = db.create_org(conn, "Prepaid Co", tier="pro")["id"]
        _, cls.key = db.create_api_key(conn, cls.org_id)
        db.add_ledger(conn, cls.org_id, 1.0, "topup")  # $1 credit
        conn.close()
        cfg = dict(DEFAULT_CONFIG)
        cfg["pricing"] = dict(cfg["pricing"], block_over_balance=True)
        ctx = app._Ctx(cfg, cls.dbpath, demo=False)
        cls.httpd = app._Server(("127.0.0.1", 0), app.Handler, ctx)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(cls.dbpath + ext)
            except OSError:
                pass

    def _post(self, payload):
        url = f"http://127.0.0.1:{self.port}/v1/usage"
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.key}"}, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=5)
            return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_partial_block_is_200_but_surfaces_over_balance(self):
        # First event lands ($0.50 of $1), second exceeds remaining credit.
        status, body = self._post([
            {"provider": "anthropic", "cost_usd": 0.50},
            {"provider": "anthropic", "cost_usd": 10.0},
        ])
        self.assertEqual(status, 200)
        self.assertEqual(body["recorded"], 1)
        self.assertEqual(body["over_balance_blocked"], 1)
        self.assertEqual(body["free_limit_blocked"], 0)
        self.assertEqual(body["blocked"], 1)  # total includes the hard-stop

    def test_whole_batch_over_balance_returns_402(self):
        status, body = self._post([
            {"provider": "anthropic", "cost_usd": 10.0},
            {"provider": "anthropic", "cost_usd": 10.0},
        ])
        self.assertEqual(status, 402)
        self.assertEqual(body["recorded"], 0)
        self.assertEqual(body["over_balance_blocked"], 2)
        self.assertIn("credit exhausted", body["error"])


if __name__ == "__main__":
    unittest.main()


# ---- Security hardening tests (issues #31-#36) --------------------------------
class TestSecurityHardening(unittest.TestCase):
    """Tests for security fixes #31-#36."""
    
    @classmethod
    def setUpClass(cls):
        fd, cls.dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = db.connect(cls.dbpath)
        cls.org_id = demo.seed(conn, events=10)
        _, cls.key = db.create_api_key(conn, cls.org_id, name="test")
        conn.close()

        ctx = app._Ctx(dict(DEFAULT_CONFIG), cls.dbpath, demo=True)
        cls.httpd = app._Server(("127.0.0.1", 0), app.Handler, ctx)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(cls.dbpath + ext)
            except OSError:
                pass

    def _post(self, path, payload, token=None):
        """Helper to POST JSON."""
        url = f"http://127.0.0.1:{self.port}{path}"
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    # Fix #31: Body size limit
    def test_oversized_body_returns_413(self):
        """Oversized request body should return 413."""
        # Manually send a request with a huge Content-Length header
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.connect(("127.0.0.1", self.port))
            huge_size = 2 * 1024 * 1024  # 2 MiB
            request = (
                f"POST /v1/usage HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.port}\r\n"
                f"Authorization: Bearer {self.key}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {huge_size}\r\n"
                f"\r\n"
                # Send at least 1 byte to trigger the read, but server should reject before reading it all
                f"{{"
            )
            sock.sendall(request.encode())
            response = sock.recv(8192).decode()
            sock.close()
            
            self.assertIn("413", response)
            self.assertIn("too large", response.lower())
        finally:
            try:
                sock.close()
            except:
                pass

    def test_normal_body_size_works(self):
        """Normal-sized body should still work."""
        status, body = self._post("/v1/usage", {
            "provider": "anthropic",
            "input_tokens": 100
        }, token=self.key)
        self.assertEqual(status, 200)
        self.assertTrue(body["recorded"])

    # Fix #34: HTML escaping in reports
    def test_report_escapes_xss_in_workspace_name(self):
        """Reports should escape workspace names containing HTML/script tags."""
        from ledger_agent import reports, db as db_mod
        conn = db_mod.connect(self.dbpath)
        
        # Create workspace with XSS payload
        xss_name = "<script>alert('xss')</script>"
        ws_id = db_mod.create_workspace(conn, self.org_id, xss_name)["id"]
        
        # Record usage to that workspace
        from ledger_agent import metering
        metering.record_usage(conn, self.org_id, provider="test", workspace=xss_name,
                            input_tokens=100, cost_usd=0.01)
        
        # Build and render report
        import datetime as dt
        now = dt.datetime.now()
        report = reports.build_report(conn, self.org_id, now.year, now.month)
        html = reports.render_html(report)
        
        conn.close()
        
        # The literal script tag should NOT appear in HTML
        self.assertNotIn("<script>", html)
        self.assertNotIn("</script>", html)
        # But the escaped version should
        self.assertIn("&lt;script&gt;", html)


class TestCheckoutHandoffPage(unittest.TestCase):
    def test_renders_a_visible_new_tab_checkout_link(self):
        from ledger_agent.server import views

        url = "https://checkout.stripe.com/c/pay/cs_test_example"
        page = views.checkout_handoff_page(url)

        self.assertIn("Checkout ready", page)
        self.assertIn("Open secure Stripe checkout", page)
        self.assertIn(f'href="{url}"', page)
        self.assertIn('target="_blank"', page)
        self.assertIn('rel="noopener noreferrer"', page)
        self.assertIn('referrerpolicy="no-referrer"', page)


    def test_donation_checkout_returns_handoff_page_not_redirect(self):
        class Stripe:
            def donate_checkout(self, conn, org_id, amount):
                return {"url": "https://checkout.stripe.com/c/pay/cs_test_example"}

        sent = {}
        fake = types.SimpleNamespace(
            _form=lambda: {"org": "org_test", "amount": "1"},
            _authz_org=lambda conn, org, strict: "org_test",
            _redirect=lambda url: sent.update(redirect=url),
            _send=lambda code, body: sent.update(code=code, body=body),
            ctx=types.SimpleNamespace(stripe=Stripe()),
        )

        app.Handler._checkout_donate(fake, None)

        self.assertNotIn("redirect", sent)
        self.assertEqual(sent["code"], 200)
        self.assertIn("Open secure Stripe checkout", sent["body"])



class TestSubscriptionGate(unittest.TestCase):
    """#175: Pro/Team subscription checkout gated off until launch gates pass."""

    def _fake(self, cfg):
        sent = {}

        class Stripe:
            def pro_checkout(self, conn, org_id):
                sent["pro_called"] = True
                return {"url": "https://checkout.stripe.com/c/pay/cs_test_example"}

            def team_checkout(self, conn, org_id):
                sent["team_called"] = True
                return {"url": "https://checkout.stripe.com/c/pay/cs_test_example"}

        fake = types.SimpleNamespace(
            _form=lambda: {"org": "org_test"},
            _authz_org=lambda conn, org, strict: "org_test",
            _redirect=lambda url: sent.update(redirect=url),
            _send=lambda code, body: sent.update(code=code, body=body),
            ctx=types.SimpleNamespace(stripe=Stripe(), cfg=cfg),
        )
        fake._subscriptions_enabled = lambda: app.Handler._subscriptions_enabled(fake)
        fake._subscriptions_gated = lambda: app.Handler._subscriptions_gated(fake)
        return fake, sent

    def test_pro_checkout_blocked_when_gate_off(self):
        fake, sent = self._fake({"billing": {}})
        app.Handler._checkout_pro(fake, None)
        self.assertEqual(sent["code"], 403)
        self.assertIn("Subscriptions open at launch", sent["body"])
        self.assertNotIn("redirect", sent)
        self.assertNotIn("pro_called", sent)

    def test_team_checkout_blocked_when_gate_off(self):
        fake, sent = self._fake({"billing": {}})
        app.Handler._checkout_team(fake, None)
        self.assertEqual(sent["code"], 403)
        self.assertIn("Subscriptions open at launch", sent["body"])
        self.assertNotIn("redirect", sent)
        self.assertNotIn("team_called", sent)

    def test_pro_checkout_allowed_when_gate_on(self):
        fake, sent = self._fake({"billing": {"subscriptions_enabled": True}})
        app.Handler._checkout_pro(fake, None)
        self.assertEqual(sent.get("redirect"),
                         "https://checkout.stripe.com/c/pay/cs_test_example")
        self.assertTrue(sent.get("pro_called"))

    def test_team_checkout_allowed_when_gate_on(self):
        fake, sent = self._fake({"billing": {"subscriptions_enabled": True}})
        app.Handler._checkout_team(fake, None)
        self.assertEqual(sent.get("redirect"),
                         "https://checkout.stripe.com/c/pay/cs_test_example")
        self.assertTrue(sent.get("team_called"))


class TestSameOrigin(unittest.TestCase):
    """Unit coverage for the same-origin check behind CSRF protection (Fix #32)."""

    def _check(self, base_url, headers):
        fake = types.SimpleNamespace()
        fake.ctx = types.SimpleNamespace(cfg={"auth": {"base_url": base_url}})
        fake.headers = headers
        return app.Handler._same_origin(fake)

    def test_matching_origin_allowed(self):
        self.assertTrue(self._check("https://app.example.com",
                                    {"Origin": "https://app.example.com"}))

    def test_mismatched_origin_blocked(self):
        self.assertFalse(self._check("https://app.example.com",
                                     {"Origin": "https://evil.example.com"}))

    def test_referer_fallback_allowed(self):
        self.assertTrue(self._check("https://app.example.com",
                                    {"Referer": "https://app.example.com/dashboard"}))

    def test_referer_fallback_blocked(self):
        self.assertFalse(self._check("https://app.example.com",
                                     {"Referer": "https://evil.example.com/x"}))

    def test_no_headers_blocked(self):
        # Absent Origin AND Referer is rejected for safety.
        self.assertFalse(self._check("https://app.example.com", {}))

    def test_unconfigured_base_url_falls_back_to_host(self):
        # Fix #32: with no base_url, judge origin against the request's own Host
        # header (fail closed) — not "allow anything", as the old code did.
        self.assertTrue(self._check(
            "", {"Host": "app.example.com", "Origin": "https://app.example.com"}))
        self.assertFalse(self._check(
            "", {"Host": "app.example.com", "Origin": "https://evil.example.com"}))

    def test_unconfigured_base_url_and_no_host_blocked(self):
        # Nothing to compare against → reject (previously this allowed through).
        self.assertFalse(self._check("", {"Origin": "https://anywhere.com"}))

    def test_origin_takes_precedence_over_referer(self):
        # A mismatched Origin blocks even when Referer would have matched.
        self.assertFalse(self._check(
            "https://app.example.com",
            {"Origin": "https://evil.example.com",
             "Referer": "https://app.example.com/x"}))

    def test_base_url_trailing_slash_normalized(self):
        self.assertTrue(self._check("https://app.example.com/",
                                    {"Origin": "https://app.example.com"}))


class TestBodyCap(unittest.TestCase):
    """Unit coverage for the request-body size limit (Fix #31)."""

    def _read(self, declared_len, payload, max_bytes):
        fake = types.SimpleNamespace()
        fake.headers = ({"Content-Length": str(declared_len)}
                        if declared_len is not None else {})
        fake.rfile = io.BytesIO(payload)
        return app.Handler._body(fake, max_bytes=max_bytes)

    def test_at_limit_allowed(self):
        self.assertEqual(self._read(10, b"x" * 10, max_bytes=10), b"x" * 10)

    def test_over_limit_raises(self):
        with self.assertRaises(app._BodyTooLarge):
            self._read(11, b"x" * 11, max_bytes=10)

    def test_missing_content_length_is_empty(self):
        self.assertEqual(self._read(None, b"", max_bytes=10), b"")

    def test_default_limit_is_one_mib(self):
        self.assertEqual(app.MAX_BODY_BYTES, 1 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()


class TestCheckpointEndpoints(unittest.TestCase):
    """#121: record/list tamper-evidence anchors over the API."""

    @classmethod
    def setUpClass(cls):
        fd, cls.dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = db.connect(cls.dbpath)
        cls.org_id = demo.seed(conn, events=30)
        _, cls.key = db.create_api_key(conn, cls.org_id, name="ckpt-test")
        conn.close()
        ctx = app._Ctx(dict(DEFAULT_CONFIG), cls.dbpath, demo=True)
        cls.httpd = app._Server(("127.0.0.1", 0), app.Handler, ctx)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(cls.dbpath + ext)
            except OSError:
                pass

    def _req(self, method, path, token=None, payload=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_post_records_then_get_lists(self):
        status, body = self._req("POST", "/v1/checkpoints", token=self.key, payload={})
        self.assertEqual(status, 200)
        self.assertTrue(body["recorded"])
        cp = body["checkpoint"]
        self.assertEqual(cp["org_id"], self.org_id)
        self.assertTrue(cp["head_hash"])
        self.assertGreater(cp["event_count"], 0)

        status, body = self._req("GET", "/v1/checkpoints", token=self.key)
        self.assertEqual(status, 200)
        self.assertEqual(body["org_id"], self.org_id)
        self.assertEqual(len(body["checkpoints"]), 1)
        self.assertEqual(body["checkpoints"][0]["head_hash"], cp["head_hash"])
        self.assertIn("out of band", body["note"])

    def test_post_requires_bearer(self):
        status, body = self._req("POST", "/v1/checkpoints", payload={})
        self.assertEqual(status, 401)

    def test_post_is_idempotent_per_head(self):
        s1, b1 = self._req("POST", "/v1/checkpoints", token=self.key, payload={})
        s2, b2 = self._req("POST", "/v1/checkpoints", token=self.key, payload={})
        self.assertEqual((s1, s2), (200, 200))
        self.assertEqual(b1["checkpoint"]["through_rowid"],
                         b2["checkpoint"]["through_rowid"])


class TestWorkspaceFoldSignal(unittest.TestCase):
    """#170: client-sent attribution is honored verbatim, and a tier-capped
    workspace fold is FLAGGED (workspace_folded + workspace_note) instead of
    silently collapsing per-source breakdowns."""
    @classmethod
    def setUpClass(cls):
        import dataclasses
        from ledger_agent import pricing
        fd, cls.dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        # Pin a workspace-capped Free variant (cap = 1) to force the fold.
        cls._orig_free = pricing.TIERS["free"]
        pricing.TIERS["free"] = dataclasses.replace(cls._orig_free, workspaces=1)
        conn = db.connect(cls.dbpath)
        db.init_schema(conn)
        cls.org_id = db.create_org(conn, "Fold Co", tier="free")["id"]
        _, cls.key = db.create_api_key(conn, cls.org_id)
        cls.first_ws_id = db.create_workspace(conn, cls.org_id, "first")["id"]
        conn.close()

        ctx = app._Ctx(dict(DEFAULT_CONFIG), cls.dbpath, demo=False)
        cls.httpd = app._Server(("127.0.0.1", 0), app.Handler, ctx)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        from ledger_agent import pricing
        pricing.TIERS["free"] = cls._orig_free
        cls.httpd.shutdown()
        cls.httpd.server_close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(cls.dbpath + ext)
            except OSError:
                pass

    def _post(self, payload):
        url = f"http://127.0.0.1:{self.port}/v1/usage"
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.key}"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_new_workspace_beyond_cap_folds_and_flags(self):
        status, body = self._post({"provider": "acme-sent", "input_tokens": 10,
                                   "workspace": "second"})
        self.assertEqual(status, 200)
        self.assertTrue(body["recorded"])
        self.assertTrue(body["workspace_folded"])
        self.assertEqual(body["workspace_id"], self.first_ws_id)
        self.assertIn("workspace_note", body)
        # …and provider lands verbatim — the server never rewrites it.
        conn = db.connect(self.dbpath)
        try:
            row = conn.execute(
                "SELECT provider, workspace_id FROM usage_events WHERE id=?",
                (body["event_id"],)).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["provider"], "acme-sent")
        self.assertEqual(row["workspace_id"], self.first_ws_id)

    def test_existing_workspace_does_not_fold(self):
        status, body = self._post({"provider": "acme", "input_tokens": 10,
                                   "workspace": "first"})
        self.assertEqual(status, 200)
        self.assertFalse(body["workspace_folded"])
        self.assertEqual(body["workspace_id"], self.first_ws_id)
        self.assertNotIn("workspace_note", body)

    def test_batch_mixed_fold_flags_per_result(self):
        status, body = self._post([
            {"provider": "acme", "input_tokens": 10, "workspace": "third"},
            {"provider": "acme", "input_tokens": 10, "workspace": "first"},
        ])
        self.assertEqual(status, 200)
        self.assertEqual(body["recorded"], 2)
        self.assertTrue(body["results"][0]["workspace_folded"])
        self.assertFalse(body["results"][1]["workspace_folded"])
        self.assertEqual(body["results"][0]["workspace_id"], self.first_ws_id)
        self.assertIn("workspace_note", body)
