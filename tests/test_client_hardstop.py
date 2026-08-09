"""P1: the embedded local Meter SDK enforces the prepaid hard-stop, same as the
hosted /v1/usage API. Before the fix, Meter.track() never threaded
block_over_balance, so a local process debited straight past zero while the
docs advertised prepaid-credit enforcement.
"""
import os
import tempfile
import unittest

from ledger_agent import Meter


class TestEmbeddedHardStop(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db + ext)
            except OSError:
                pass

    def _meter(self, config=None):
        return Meter(org="acme", tier="pro", db_path=self.db, config=config or {})

    def test_default_on_blocks_over_balance(self):
        """An org holding prepaid credit cannot debit past zero on the local SDK."""
        m = self._meter()
        m.topup(1.00)
        res = m.track(provider="anthropic", model="claude-opus-4-8",
                      cost_usd=2.00)
        self.assertFalse(res.recorded)         # rejected, not recorded
        self.assertTrue(res.over_balance)
        self.assertAlmostEqual(m.balance(), 1.00)   # balance untouched
        m.close()

    def test_within_balance_still_records(self):
        m = self._meter()
        m.topup(5.00)
        res = m.track(provider="anthropic", cost_usd=2.00)
        self.assertTrue(res.recorded)
        self.assertAlmostEqual(m.balance(), 3.00)
        m.close()

    def test_no_credit_org_not_blocked(self):
        """A track-only org that never topped up keeps full tracking (the
        hard-stop only bites orgs that actually hold prepaid credit)."""
        m = self._meter()
        res = m.track(provider="anthropic", cost_usd=2.00)   # no topup first
        self.assertTrue(res.recorded)
        m.close()

    def test_opt_out_allows_negative(self):
        """Setting pricing.block_over_balance=False restores unmetered debit."""
        m = self._meter(config={"pricing": {"block_over_balance": False}})
        m.topup(1.00)
        res = m.track(provider="anthropic", cost_usd=2.00)
        self.assertTrue(res.recorded)
        self.assertAlmostEqual(m.balance(), -1.00)
        m.close()


if __name__ == "__main__":
    unittest.main()
