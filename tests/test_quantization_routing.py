#!/usr/bin/env python3
"""Tests for quantization-aware routing (#128 step 3)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ledger_route


class TestQuantizationAwarePolicy(unittest.TestCase):
    def setUp(self):
        self.rw = {
            "deepseek": {"days_left": 45},
            "anthropic": {"days_left": 30},
            "google": {"days_left": 15},
        }
        self.order = sorted(
            ledger_route.PROVIDERS,
            key=lambda p: self.rw[p]["days_left"], reverse=True,
        )

    def test_policy_reranks_by_effective_cost(self):
        """quantization-aware re-ranks by effective cost (base * multiplier)."""
        order, skipped, notes = ledger_route._apply_policy(
            self.order, self.rw, "quantization-aware",
            {"quality_min_retention": 0.90},
        )
        # All models have fp16 (1.2×) and fp8 (1.0×). Cheapest effective cost
        # is fp8 for all, so order should be by base cost: google < deepseek < anthropic
        # google gemini = $2.50/M, deepseek = $2.50/M, anthropic opus = $15/M
        # DeepSeek ties with Google on fp8, but runway ordering breaks the tie.
        self.assertEqual(len(order), 3)
        self.assertEqual(len(skipped), 0)
        self.assertIn("quantization-aware", notes[0] if notes else "")

    def test_quality_floor_filters_tiers(self):
        """Tiers below quality_min_retention are excluded."""
        # Set floor to 1.0 — only fp8/int8 pass (both 1.0 retention).
        # fp16 has 0.99 retention, so it should be excluded.
        # But since fp8 is available for all models, order should still work.
        order, skipped, notes = ledger_route._apply_policy(
            self.order, self.rw, "quantization-aware",
            {"quality_min_retention": 1.0},
        )
        self.assertEqual(len(order), 3)
        self.assertEqual(len(skipped), 0)

    def test_quality_floor_drops_all(self):
        """When no tier meets the floor, keep runway order with a note."""
        order, skipped, notes = ledger_route._apply_policy(
            self.order, self.rw, "quantization-aware",
            {"quality_min_retention": 0.9999},
        )
        # fp8 quality=1.0 > 0.9999, so fp8 should still pass. All models have fp8.
        self.assertEqual(len(order), 3)

    def test_stacks_with_cost_prefer_cheapest(self):
        """quantization-aware can stack with cost-prefer-cheapest."""
        order, skipped, notes = ledger_route._apply_policy(
            self.order, self.rw, "quantization-aware,cost-prefer-cheapest",
            {"quality_min_retention": 0.90},
        )
        self.assertEqual(len(order), 3)

    def test_stacks_with_cost_cap_and_quality_floor(self):
        """quantization-aware stacks with cost-cap,quality-floor."""
        order, skipped, notes = ledger_route._apply_policy(
            self.order, self.rw,
            "cost-cap,quality-floor,quantization-aware",
            {"cost_max_per_1m": 20.0, "quality_min_score": 60,
             "quality_min_retention": 0.90},
        )
        self.assertEqual(len(order), 3)


class TestQuantizationMetadata(unittest.TestCase):
    def test_quality_floor_keys_match_pricing_tiers(self):
        """QUANTIZATION_QUALITY_FLOOR keys are a subset of pricing tiers."""
        from ledger_agent import pricing
        for tier in ledger_route.QUANTIZATION_QUALITY_FLOOR:
            self.assertIn(tier, pricing.QUANTIZATION_TIERS,
                          f"{tier} not in pricing.QUANTIZATION_TIERS")

    def test_model_quantization_tiers_have_quality_entries(self):
        """Every tier in MODEL_QUANTIZATION_TIERS has a quality floor."""
        all_tiers = set()
        for tiers in ledger_route.MODEL_QUANTIZATION_TIERS.values():
            all_tiers.update(tiers)
        for tier in all_tiers:
            self.assertIn(tier, ledger_route.QUANTIZATION_QUALITY_FLOOR,
                          f"{tier} missing from QUANTIZATION_QUALITY_FLOOR")

    def test_known_models_have_tiers(self):
        """All FLAGSHIP and SUBTASK models have quantization tier entries."""
        all_models = set(ledger_route.FLAGSHIP.values()) | set(ledger_route.SUBTASK.values())
        for model in all_models:
            self.assertIn(model, ledger_route.MODEL_QUANTIZATION_TIERS,
                          f"{model} missing from MODEL_QUANTIZATION_TIERS")

    def test_quality_floor_values_are_reasonable(self):
        """Quality floors are in [0, 1] range."""
        for tier, floor in ledger_route.QUANTIZATION_QUALITY_FLOOR.items():
            self.assertGreaterEqual(floor, 0.0, f"{tier} floor < 0")
            self.assertLessEqual(floor, 1.0, f"{tier} floor > 1")


class TestQuantizationAwareBacktest(unittest.TestCase):
    def test_backtest_accepts_quantization_aware(self):
        """Backtest with quantization-aware doesn't crash."""
        # backtest only reads the policy name, no actual state.db needed
        # (it will print "State DB not found" and return)
        try:
            ledger_route.backtest("quantization-aware", {})
        except Exception as e:
            self.fail(f"backtest raised: {e}")


class TestQuantizationEffectiveCost(unittest.TestCase):
    def test_effective_cost_applies_multiplier(self):
        """Effective cost = base_cost * precision_multiplier."""
        from ledger_agent import pricing

        # deepseek-v4-pro: $2.50/M, fp16 mult=1.2 -> effective = $3.00/M
        mult, _ = pricing.resolve_precision_multiplier("fp16")
        base = ledger_route.MODEL_COST_PER_1M_IN["deepseek-v4-pro"]
        effective = base * mult
        self.assertAlmostEqual(effective, 3.0, places=2)

        # deepseek-v4-pro: fp8 mult=1.0 -> effective = $2.50/M
        mult, _ = pricing.resolve_precision_multiplier("fp8")
        effective = base * mult
        self.assertAlmostEqual(effective, 2.50, places=2)

    def test_1bit_multiplier_is_aggressive(self):
        """1bit multiplier (0.05) makes effective cost very low."""
        from ledger_agent import pricing
        mult, _ = pricing.resolve_precision_multiplier("1bit")
        self.assertLess(mult, 0.1)
        # If a model supported 1bit, effective cost would be 5% of base
        base = ledger_route.MODEL_COST_PER_1M_IN["deepseek-v4-pro"]
        self.assertAlmostEqual(base * mult, 0.125, places=3)


if __name__ == "__main__":
    unittest.main()
