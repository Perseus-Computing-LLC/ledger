#!/usr/bin/env python3
"""Deploy hardening (2026-07-05 security review): the server fails closed rather
than expose an unauthenticated dashboard on a non-loopback interface."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ledger_agent.server import app


class TestInsecureBindGuard(unittest.TestCase):
    def test_refuses_public_bind_with_auth_off(self):
        with self.assertRaises(SystemExit):
            app._guard_insecure_bind("0.0.0.0", auth_on=False, cfg={})

    def test_allows_loopback_even_with_auth_off(self):
        for h in ("127.0.0.1", "localhost", "::1", ""):
            app._guard_insecure_bind(h, False, {})  # must not raise

    def test_allows_public_bind_when_auth_on(self):
        app._guard_insecure_bind("0.0.0.0", True, {})  # must not raise

    def test_allows_public_bind_with_config_optin(self):
        app._guard_insecure_bind("0.0.0.0", False, {"server": {"allow_insecure": True}})

    def test_allows_public_bind_with_env_optin(self):
        os.environ["LEDGER_ALLOW_INSECURE"] = "1"
        try:
            app._guard_insecure_bind("0.0.0.0", False, {})
        finally:
            del os.environ["LEDGER_ALLOW_INSECURE"]


if __name__ == "__main__":
    unittest.main()
