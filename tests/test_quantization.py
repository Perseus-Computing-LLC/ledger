#!/usr/bin/env python3
"""Tests for the quantization/precision dimension of the cost model (#128).

Design contract: the precision multiplier defaults to identity (1.0) for every
recognized tier and for unrecognized input, so an uncalibrated deployment can
never over-report savings. Measured multipliers (perseus-vault#630) arrive via
config/overrides — never vendor-published claims — and only then change cost.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plutus_agent import pricing, config as cfgmod


class TestPrecisionMultiplier(unittest.TestCase):
    def test_defaults_are_identity(self):
        # Uncalibrated tiers are 1.0 — no assumed savings.
        UNCALIBRATED = {"fp16", "fp8", "nvfp4", "int4"}
        for tier in UNCALIBRATED:
            mult, known = pricing.resolve_precision_multiplier(tier)
            self.assertTrue(known, tier)
            self.assertEqual(mult, 1.0, tier)

    def test_1bit_is_measured(self):
        # 1bit is now populated from perseus-vault#630 benchmark data.
        mult, known = pricing.resolve_precision_multiplier("1bit")
        self.assertTrue(known)
        self.assertEqual(mult, 0.05)

    def test_int8_is_baseline(self):
        # INT8 is the Vault's shipped default — multiplier is 1.0 (the baseline).
        mult, known = pricing.resolve_precision_multiplier("int8")
        self.assertTrue(known)
        self.assertEqual(mult, 1.0)

    def test_none_is_safe_noop(self):
        self.assertEqual(pricing.resolve_precision_multiplier(None), (1.0, False))
        self.assertEqual(pricing.resolve_precision_multiplier(""), (1.0, False))

    def test_unknown_tier_is_safe_noop(self):
        mult, known = pricing.resolve_precision_multiplier("fp3.5-imaginary")
        self.assertEqual(mult, 1.0)
        self.assertFalse(known)

    def test_case_insensitive(self):
        self.assertEqual(pricing.resolve_precision_multiplier("NVFP4")[0], 1.0)
        self.assertEqual(
            pricing.resolve_precision_multiplier("NVFP4", {"nvfp4": 0.5})[0], 0.5
        )

    def test_override_takes_precedence(self):
        mult, known = pricing.resolve_precision_multiplier("nvfp4", {"nvfp4": 0.55})
        self.assertAlmostEqual(mult, 0.55)
        self.assertTrue(known)

    def test_bad_override_value_falls_back_safely(self):
        # A malformed multiplier must never become a silent discount.
        mult, known = pricing.resolve_precision_multiplier("int4", {"int4": "cheap"})
        self.assertEqual(mult, 1.0)
        self.assertFalse(known)


class TestEstimateCostWithQuantization(unittest.TestCase):
    def test_default_matches_unquantized(self):
        base = pricing.estimate_cost("anthropic", "claude-opus-4-8", 1_000_000, 0)
        quoted = pricing.estimate_cost(
            "anthropic", "claude-opus-4-8", 1_000_000, 0, quantization="nvfp4"
        )
        self.assertEqual(base, quoted)  # identity default, no free lunch

    def test_measured_override_scales_cost(self):
        base = pricing.estimate_cost("anthropic", "claude-opus-4-8", 1_000_000, 0)
        quoted = pricing.estimate_cost(
            "anthropic", "claude-opus-4-8", 1_000_000, 0,
            quantization="nvfp4", quantization_overrides={"nvfp4": 0.5},
        )
        self.assertAlmostEqual(quoted, base * 0.5, places=6)

    def test_unknown_tier_does_not_discount(self):
        base = pricing.estimate_cost("anthropic", "claude-opus-4-8", 1_000_000, 0)
        quoted = pricing.estimate_cost(
            "anthropic", "claude-opus-4-8", 1_000_000, 0, quantization="bogus"
        )
        self.assertEqual(base, quoted)


class TestConfigDefault(unittest.TestCase):
    def test_quantization_block_present_and_empty(self):
        # Ships empty so a fresh install assumes zero quantization savings.
        q = cfgmod.DEFAULT_CONFIG["pricing"]["quantization"]
        self.assertEqual(q, {})


if __name__ == "__main__":
    unittest.main()
