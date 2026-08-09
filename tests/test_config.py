"""Runtime configuration regression tests."""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ledger_agent import config


class TestPublicBillingUrls(unittest.TestCase):
    def test_base_url_supplies_public_checkout_return_urls_when_unconfigured(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                "LEDGER_CONFIG": os.path.join(tmp, "config.yaml"),
                "LEDGER_BASE_URL": "https://ledger.example.test/",
            },
        ):
            cfg = config.load()

        self.assertEqual(
            cfg["billing"]["success_url"],
            "https://ledger.example.test/billing/success",
        )
        self.assertEqual(
            cfg["billing"]["cancel_url"],
            "https://ledger.example.test/billing/cancel",
        )


if __name__ == "__main__":
    unittest.main()
