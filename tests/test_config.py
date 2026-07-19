"""Runtime configuration regression tests."""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plutus_agent import config


class TestPublicBillingUrls(unittest.TestCase):
    def test_base_url_supplies_public_checkout_return_urls_when_unconfigured(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                "PLUTUS_CONFIG": os.path.join(tmp, "config.yaml"),
                "PLUTUS_BASE_URL": "https://plutus.example.test/",
            },
        ):
            cfg = config.load()

        self.assertEqual(
            cfg["billing"]["success_url"],
            "https://plutus.example.test/billing/success",
        )
        self.assertEqual(
            cfg["billing"]["cancel_url"],
            "https://plutus.example.test/billing/cancel",
        )


if __name__ == "__main__":
    unittest.main()
