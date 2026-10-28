"""Resolution risk: the N/A settlement that quietly eats near-certain trades."""

from __future__ import annotations

import unittest

from resolution import (
    AMBIGUOUS_FALLBACKS,
    NA_PAYOUT,
    assess_resolution,
    fallback_is_ambiguous,
    n_a_adjusted_hold,
    n_a_budget,
)


class FallbackTests(unittest.TestCase):
    def test_equal_split_is_ambiguous(self) -> None:
        self.assertTrue(fallback_is_ambiguous("equal_split"))

    def test_none_is_safe(self) -> None:
        self.assertFalse(fallback_is_ambiguous("none"))

    def test_missing_fallback_is_treated_as_safe(self) -> None:
        # Older Gamma payloads omit the field; refusing them all would disable
        # the strategy on a parsing gap rather than on evidence.
        self.assertFalse(fallback_is_ambiguous(None))
        self.assertFalse(fallback_is_ambiguous(""))

    def test_casing_and_whitespace_are_tolerated(self) -> None:
        self.assertTrue(fallback_is_ambiguous("  Equal_Split "))

    def test_every_declared_ambiguous_value_is_recognised(self) -> None:
        for value in AMBIGUOUS_FALLBACKS:
            self.assertTrue(fallback_is_ambiguous(value), value)

    def test_an_unknown_fallback_value_is_not_guessed_ambiguous(self) -> None:
        self.assertFalse(fallback_is_ambiguous("some_future_type"))


class AdjustedHoldTests(unittest.TestCase):
    def test_zero_na_probability_leaves_the_hold_alone(self) -> None:
        self.assertAlmostEqual(n_a_adjusted_hold(0.95, 0.0), 0.95)

    def test_certain_na_gives_the_na_payout(self) -> None:
        self.assertAlmostEqual(n_a_adjusted_hold(0.95, 1.0), NA_PAYOUT)

    def test_blend_is_linear(self) -> None:
        self.assertAlmostEqual(n_a_adjusted_hold(0.95, 0.10), 0.90 * 0.95 + 0.10 * 0.50)

    def test_one_percent_na_costs_four_tenths_of_a_cent(self) -> None:
        # 0.95 -> 0.9455: a 0.45-cent charge, which is 42% of a 0.94 trade's edge.
        self.assertAlmostEqual(0.95 - n_a_adjusted_hold(0.95, 0.01), 0.0045, places=9)

    def test_out_of_range_probabilities_are_rejected(self) -> None:
        for bad in (-0.01, 1.01):
            with self.assertRaises(ValueError):
                n_a_adjusted_hold(0.95, bad)
        with self.assertRaises(ValueError):
            n_a_adjusted_hold(1.2, 0.1)


class NaBudgetTests(unittest.TestCase):
    def test_the_margin_at_094_is_almost_no_ambiguity_at_all(self) -> None:
        # The headline finding: a 0.94 entry with a 0.95 hold and a 1% target
        # survives only 0.13% of N/A probability.
        self.assertAlmostEqual(n_a_budget(0.95, 0.94, min_ev=0.01), 0.001333, places=5)

    def test_a_cheaper_entry_survives_more_ambiguity(self) -> None:
        cheap = n_a_budget(0.95, 0.90, min_ev=0.01)
        dear = n_a_budget(0.95, 0.94, min_ev=0.01)

        self.assertGreater(cheap, dear)

    def test_five_percent_na_turns_the_trade_negative(self) -> None:
        hold = n_a_adjusted_hold(0.95, 0.05)

        # 0.9275 expected payout against a 0.94 ask, with a 1% gate requiring
        # 0.9494: a guaranteed loss, not a thin edge.
        self.assertLess(hold, 0.94 * 1.01)
        self.assertAlmostEqual((hold - 0.94) / 0.94, -0.0133, places=4)

    def test_a_hold_at_the_payout_is_immune_to_na(self) -> None:
        # If the hold is already 0.50, an N/A settlement pays exactly what a
        # normal loss would, so ambiguity costs nothing at all.
        self.assertEqual(n_a_budget(0.50, 0.45, min_ev=0.01), 1.0)
        self.assertEqual(n_a_budget(0.40, 0.30, min_ev=0.01), 1.0)

    def test_a_hold_that_cannot_clear_the_gate_has_no_budget(self) -> None:
        self.assertEqual(n_a_budget(0.60, 0.62, min_ev=0.01), 0.0)

    def test_an_unprofitable_ask_has_no_budget(self) -> None:
        self.assertEqual(n_a_budget(0.90, 0.95, min_ev=0.0), 0.0)

    def test_invalid_ask_returns_zero_rather_than_raising(self) -> None:
        self.assertEqual(n_a_budget(0.95, 0.0, min_ev=0.01), 0.0)
        self.assertEqual(n_a_budget(0.95, 1.0, min_ev=0.01), 0.0)

    def test_budget_is_capped_at_one(self) -> None:
        self.assertLessEqual(n_a_budget(1.0, 0.10, min_ev=0.0), 1.0)


class AssessResolutionTests(unittest.TestCase):
    def test_a_safe_market_is_charged_nothing(self) -> None:
        risk = assess_resolution("none", hold_probability=0.95, n_a_probability=0.10)

        self.assertFalse(risk.ambiguous)
        self.assertTrue(risk.safe)
        self.assertEqual(risk.n_a_probability, 0.0)
        self.assertAlmostEqual(risk.adjusted_hold, 0.95)

    def test_an_ambiguous_market_is_charged_the_global_rate(self) -> None:
        risk = assess_resolution("equal_split", hold_probability=0.95, n_a_probability=0.02)

        self.assertTrue(risk.ambiguous)
        self.assertFalse(risk.safe)
        self.assertAlmostEqual(risk.n_a_probability, 0.02)
        self.assertAlmostEqual(risk.adjusted_hold, 0.98 * 0.95 + 0.02 * 0.5)

    def test_as_dict_is_publishable(self) -> None:
        payload = assess_resolution("equal_split", hold_probability=0.95).as_dict()

        self.assertEqual(payload["fallback_type"], "equal_split")
        self.assertIs(payload["ambiguous"], True)
        self.assertIn("adjusted_hold", payload)

    def test_a_missing_fallback_serialises_as_null(self) -> None:
        self.assertIsNone(assess_resolution(None, hold_probability=0.95).as_dict()["fallback_type"])


if __name__ == "__main__":
    unittest.main()
