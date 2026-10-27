"""Growth feasibility: the arithmetic behind "$20 -> $6,000 in 72 hours"."""

from __future__ import annotations

import unittest

from research.growth import (
    GrowthRequirement,
    annualised_return,
    feasibility,
    required_win_rate,
    survival_probability,
    trades_needed,
)


class GrowthRequirementTests(unittest.TestCase):
    def test_multiple_and_total_return(self) -> None:
        requirement = GrowthRequirement(start=20.0, target=6000.0, days=3.0)

        self.assertAlmostEqual(requirement.multiple, 300.0)
        self.assertAlmostEqual(requirement.total_return, 299.0)

    def test_daily_rate_matches_the_document(self) -> None:
        requirement = GrowthRequirement(start=20.0, target=6000.0, days=3.0)

        self.assertAlmostEqual(requirement.daily_rate * 100.0, 569.43, places=2)

    def test_hourly_rate_is_the_compounded_twelfth_root(self) -> None:
        requirement = GrowthRequirement(start=20.0, target=6000.0, days=3.0)

        self.assertAlmostEqual(requirement.hourly_rate * 100.0, 8.2442, places=3)

    def test_a_flat_target_has_no_growth(self) -> None:
        requirement = GrowthRequirement(start=50.0, target=50.0, days=3.0)

        self.assertEqual(requirement.daily_rate, 0.0)
        self.assertEqual(requirement.hourly_rate, 0.0)

    def test_zero_start_is_infinite_multiple_but_no_rate(self) -> None:
        requirement = GrowthRequirement(start=0.0, target=6000.0, days=3.0)

        self.assertEqual(requirement.multiple, float("inf"))
        self.assertEqual(requirement.daily_rate, 0.0)


class TradesNeededTests(unittest.TestCase):
    def test_document_example_needs_three_and_a_half_wins(self) -> None:
        self.assertAlmostEqual(trades_needed(20.0, 6000.0, 0.20), 3.54, places=2)

    def test_cheaper_entry_needs_fewer_wins(self) -> None:
        self.assertLess(trades_needed(20.0, 6000.0, 0.10), trades_needed(20.0, 6000.0, 0.20))

    def test_no_target_means_no_trades(self) -> None:
        self.assertEqual(trades_needed(100.0, 100.0, 0.20), 0.0)
        self.assertEqual(trades_needed(100.0, 50.0, 0.20), 0.0)

    def test_impossible_entry_prices_are_rejected(self) -> None:
        self.assertEqual(trades_needed(20.0, 6000.0, 0.0), 0.0)
        self.assertEqual(trades_needed(20.0, 6000.0, 1.0), 0.0)


class SurvivalTests(unittest.TestCase):
    def test_fair_priced_survival_is_one_in_three_hundred(self) -> None:
        survival = survival_probability(20.0, 6000.0, 0.20)

        self.assertAlmostEqual(survival, 1.0 / 300.0, places=4)
        self.assertAlmostEqual(1.0 - survival, 0.996667, places=6)

    def test_fair_priced_survival_is_the_inverse_of_the_multiple(self) -> None:
        # p^n where n = log(1/p)(multiple) collapses to exactly 1/multiple.
        for multiple in (10.0, 300.0, 1234.0):
            survival = survival_probability(20.0, 20.0 * multiple, 0.20)
            self.assertAlmostEqual(survival, 1.0 / multiple, places=10)

    def test_an_edge_improves_survival_but_does_not_make_it_certain(self) -> None:
        fair = survival_probability(20.0, 6000.0, 0.20)
        edged = survival_probability(20.0, 6000.0, 0.20, win_rate=0.30)

        self.assertGreater(edged, fair)
        self.assertLess(edged, 1.0)

    def test_a_reachable_target_is_almost_certain(self) -> None:
        self.assertGreater(survival_probability(100.0, 101.0, 0.50), 0.9)

    def test_no_target_is_certain_survival(self) -> None:
        self.assertEqual(survival_probability(100.0, 100.0, 0.20), 1.0)

    def test_impossible_win_rate_is_zero(self) -> None:
        self.assertEqual(survival_probability(20.0, 6000.0, 0.20, win_rate=0.0), 0.0)
        self.assertEqual(survival_probability(20.0, 6000.0, 0.20, win_rate=1.0), 0.0)


class RequiredWinRateTests(unittest.TestCase):
    def test_half_chance_demands_a_huge_win_rate(self) -> None:
        needed = required_win_rate(20.0, 6000.0, 0.20, confidence=0.5)

        self.assertAlmostEqual(needed, 0.5 ** (1.0 / 3.5410), places=3)
        self.assertGreater(needed, 0.80)

    def test_fair_pricing_is_exactly_not_enough_at_full_confidence(self) -> None:
        self.assertAlmostEqual(required_win_rate(20.0, 6000.0, 0.20, confidence=1.0), 1.0)

    def test_reachable_target_needs_less_than_a_coin_flip(self) -> None:
        self.assertLess(required_win_rate(100.0, 101.0, 0.50, confidence=0.5), 0.5)

    def test_invalid_confidence_is_zero(self) -> None:
        self.assertEqual(required_win_rate(20.0, 6000.0, 0.20, confidence=0.0), 0.0)


class AnnualisedReturnTests(unittest.TestCase):
    def test_settlement_arb_annualises_enormously(self) -> None:
        # 5% in 2.4 hours compounded for a year is astronomical, which is the
        # point: the figure is a decoration, not a forecast.
        value = annualised_return(0.95, hours=2.4)

        self.assertGreater(value, 1.0)

    def test_overflowing_compounding_returns_infinity(self) -> None:
        self.assertEqual(annualised_return(0.50, hours=1.0), float("inf"))

    def test_a_full_year_holding_returns_the_plain_edge(self) -> None:
        value = annualised_return(0.90, hours=365 * 24.0)

        self.assertAlmostEqual(value, 1.0 / 0.90 - 1.0, places=6)

    def test_impossible_inputs_return_zero(self) -> None:
        self.assertEqual(annualised_return(0.0, hours=1.0), 0.0)
        self.assertEqual(annualised_return(1.0, hours=1.0), 0.0)
        self.assertEqual(annualised_return(0.5, hours=0.0), 0.0)


class FeasibilityTests(unittest.TestCase):
    def test_the_document_target_is_ruinous(self) -> None:
        result = feasibility(20, 6000, 3, entry_price=0.20)

        self.assertEqual(result.verdict, "ruinous")
        self.assertAlmostEqual(result.trades, 3.54396, places=4)
        self.assertAlmostEqual(result.survival, 1.0 / 300.0, places=4)

    def test_a_modest_target_is_plausible(self) -> None:
        self.assertEqual(feasibility(100, 101, 3, entry_price=0.90).verdict, "plausible")

    def test_verdict_thresholds_are_inclusive_at_the_boundary(self) -> None:
        result = feasibility(100, 101, 3, entry_price=0.50)

        self.assertIn(result.verdict, {"plausible", "marginal", "ruinous"})
        self.assertGreaterEqual(result.survival, 0.0)
        self.assertLessEqual(result.survival, 1.0)

    def test_as_dict_is_rounded_and_complete(self) -> None:
        payload = feasibility(20, 6000, 3).as_dict()

        self.assertEqual(payload["multiple"], 300.0)
        self.assertEqual(payload["daily_rate"], 5.69433)
        self.assertEqual(payload["trades_needed"], 3.544)
        self.assertEqual(payload["verdict"], "ruinous")
        self.assertAlmostEqual(payload["survival"] + payload["ruin"], 1.0, places=6)

    def test_describe_returns_one_readable_paragraph_per_fact(self) -> None:
        text = feasibility(20, 6000, 3).describe()

        self.assertIn("569.43%/day", text)
        self.assertIn("3.54 consecutive all-in wins", text)
        self.assertIn("0.3333%", text)
        self.assertIn("RUINOUS", text)


if __name__ == "__main__":
    unittest.main()
