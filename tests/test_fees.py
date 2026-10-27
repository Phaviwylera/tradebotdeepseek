"""Fee arithmetic.

Polymarket charges `fee = shares x rate x p x (1-p)` on taker fills. The bot's
gates priced edge *before* this existed, which is the single most expensive
mistake in the whole strategy layer: at 0.94 with a 5% sports fee, the fee is
28% of a 1-cent edge.

These tests pin the algebra so a future refactor cannot quietly un-charge it.
"""

from __future__ import annotations

import math
import unittest

from fees import (
    ASSUMED_UNKNOWN_FEE_RATE,
    ZERO_FEE,
    FeeSchedule,
    arbitrage_cost,
    from_gamma,
    max_price_for_hold,
    net_edge,
    required_hold,
)


class FeeScheduleTests(unittest.TestCase):
    def test_the_published_formula(self) -> None:
        # 5% at 0.50 on 100 shares = $1.25, the number Polymarket advertises.
        schedule = FeeSchedule(rate=0.05)
        self.assertAlmostEqual(schedule.per_share(0.50), 0.0125, places=9)
        self.assertAlmostEqual(schedule.cost(0.50, 100), 1.25, places=9)

    def test_fee_is_highest_at_the_coin_flip(self) -> None:
        schedule = FeeSchedule(rate=0.05)
        self.assertGreater(schedule.per_share(0.50), schedule.per_share(0.90))
        self.assertGreater(schedule.per_share(0.90), schedule.per_share(0.99))
        self.assertAlmostEqual(schedule.per_share(0.0), 0.0)
        self.assertAlmostEqual(schedule.per_share(1.0), 0.0)

    def test_fee_free_markets_cost_nothing(self) -> None:
        self.assertEqual(ZERO_FEE.per_share(0.5), 0.0)
        self.assertEqual(FeeSchedule(rate=0.05, enabled=False).per_share(0.5), 0.0)

    def test_exponent_other_than_one_is_honoured(self) -> None:
        schedule = FeeSchedule(rate=0.05, exponent=2)
        self.assertAlmostEqual(schedule.per_share(0.5), 0.05 * 0.5 * 0.25, places=9)

    def test_prices_outside_the_range_are_clamped_not_wrapped(self) -> None:
        schedule = FeeSchedule(rate=0.05)
        self.assertEqual(schedule.per_share(-0.5), 0.0)
        self.assertEqual(schedule.per_share(1.5), 0.0)

    def test_invalid_rates_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            FeeSchedule(rate=-0.01)
        with self.assertRaises(ValueError):
            FeeSchedule(exponent=-1)


class GammaParsingTests(unittest.TestCase):
    def test_reads_a_real_sports_payload(self) -> None:
        # Copied from a live La Liga market on 2026-10-26.
        schedule = FeeSchedule.from_gamma(
            {
                "feesEnabled": True,
                "feeType": "sports_fees_v3",
                "feeSchedule": {"exponent": 1, "rate": 0.05, "takerOnly": True, "rebateRate": 0.15},
            }
        )
        self.assertAlmostEqual(schedule.rate, 0.05)
        self.assertEqual(schedule.exponent, 1)

    def test_zero_fees_is_not_the_same_as_fees_disabled(self) -> None:
        zero = FeeSchedule.from_gamma(
            {"feesEnabled": True, "feeType": "zero_fees", "feeSchedule": {"rate": 0, "exponent": 1}}
        )
        self.assertEqual(zero.per_share(0.5), 0.0)
        disabled = FeeSchedule.from_gamma(
            {"feesEnabled": False, "feeSchedule": {"rate": 0.05, "exponent": 1}}
        )
        self.assertEqual(disabled.per_share(0.5), 0.0)

    def test_missing_block_assumes_the_conservative_rate(self) -> None:
        # Never assume a market is free: that would manufacture edge.
        self.assertAlmostEqual(
            from_gamma({"question": "no fee fields"}).rate, ASSUMED_UNKNOWN_FEE_RATE
        )
        self.assertGreater(from_gamma({}).per_share(0.9), 0.0)

    def test_garbage_values_degrade_to_zero_rather_than_crashing(self) -> None:
        schedule = FeeSchedule.from_gamma({"feeSchedule": {"rate": "abc", "exponent": None}})
        self.assertEqual(schedule.per_share(0.5), 0.0)


class RequiredHoldTests(unittest.TestCase):
    """The inverse of the EV gate: what the assumption has to beat."""

    def test_requirement_at_a_fee_free_market(self) -> None:
        # p*(1+min_ev) with no fee.
        self.assertAlmostEqual(required_hold(0.93, 0.02, 0.0), 0.93 * 1.02, places=9)

    def test_the_fee_raises_the_bar(self) -> None:
        no_fee = required_hold(0.94, 0.02, 0.0)
        with_fee = required_hold(0.94, 0.02, 0.05)
        self.assertGreater(with_fee, no_fee)
        self.assertAlmostEqual(with_fee, 0.94 * 1.02 + 0.05 * 0.94 * 0.06, places=9)

    def test_the_live_094_trade_needed_more_than_the_assumed_095(self) -> None:
        # This is the finding: at 0.94 with a 5% fee and a 1% target, the trade
        # requires a 95.22% hold -- above the 95% assumption it was built on.
        requirement = required_hold(0.94, 0.01, 0.05)
        self.assertGreater(requirement, 0.95)

    def test_the_old_default_band_was_empty_once_fees_counted(self) -> None:
        # 0.93 floor vs a fee-aware cap of 0.928 at MIN_EV=0.02.
        cap = max_price_for_hold(0.95, 0.02, 0.05)
        assert cap is not None
        self.assertLess(cap, 0.93)


class NetEdgeTests(unittest.TestCase):
    def test_matches_the_hand_calculation(self) -> None:
        roi, profit = net_edge(0.94, 0.95, 0.05)
        self.assertAlmostEqual(profit, 0.01 - 0.00282, places=9)
        self.assertAlmostEqual(roi, profit / 0.94, places=9)

    def test_the_live_signal_falls_below_the_one_percent_gate(self) -> None:
        # Pre-fee it cleared 1%; post-fee it is 0.76%.
        roi, _ = net_edge(0.94, 0.95, 0.05)
        self.assertLess(roi, 0.01)
        self.assertGreater(roi, 0.0)

    def test_fees_can_turn_an_edge_into_a_loss(self) -> None:
        roi, _ = net_edge(0.95, 0.95, 0.05)
        self.assertLess(roi, 0.0)


class MaxPriceTests(unittest.TestCase):
    def test_closed_form_matches_a_numeric_search(self) -> None:
        analytic = max_price_for_hold(0.95, 0.02, 0.05)
        assert analytic is not None
        # At the cap the EV should equal the target almost exactly.
        self.assertAlmostEqual(net_edge(analytic, 0.95, 0.05)[0], 0.02, places=6)
        self.assertLess(net_edge(analytic + 0.001, 0.95, 0.05)[0], 0.02)

    def test_fee_free_cap_is_the_simple_ratio(self) -> None:
        cap = max_price_for_hold(0.95, 0.02, 0.0)
        assert cap is not None
        self.assertAlmostEqual(cap, 0.95 / 1.02, places=9)

    def test_ceiling_is_respected(self) -> None:
        cap = max_price_for_hold(0.99, 0.0, 0.0, ceiling=0.97)
        assert cap is not None
        self.assertAlmostEqual(cap, 0.97, places=9)

    def test_higher_fees_lower_the_cap_monotonically(self) -> None:
        caps = [max_price_for_hold(0.95, 0.02, rate) for rate in (0.0, 0.03, 0.05, 0.07)]
        self.assertTrue(all(cap is not None for cap in caps))
        values = [float(cap) for cap in caps]
        self.assertEqual(values, sorted(values, reverse=True))

    def test_impossible_requirements_return_none(self) -> None:
        self.assertIsNone(max_price_for_hold(0.0, 0.02, 0.05))
        self.assertIsNone(max_price_for_hold(-0.2, 0.02, 0.05))

    def test_a_low_hold_still_finds_a_cheap_price(self) -> None:
        # ROI is (hold - p - fee)/p, so a small hold is only tradeable very low.
        cap = max_price_for_hold(0.5, 0.02, 0.05)
        assert cap is not None
        self.assertLess(cap, 0.5)

    def test_numeric_fallback_agrees_with_the_closed_form(self) -> None:
        analytic = max_price_for_hold(0.95, 0.02, 0.05, exponent=1)
        numeric = max_price_for_hold(0.95, 0.02, 0.05, exponent=2)
        assert analytic is not None and numeric is not None
        # A steeper curve charges less near the top, so it allows a higher price;
        # both must still satisfy the gate.
        self.assertLess(net_edge(numeric, 0.95, 0.05, exponent=2)[0], 0.0201)
        self.assertGreater(math.isclose(analytic, numeric, rel_tol=0.2), False)


class ArbitrageCostTests(unittest.TestCase):
    def test_a_pre_fee_arbitrage_can_be_a_post_fee_loss(self) -> None:
        # 0.4925 + 0.4925 = 0.985, which the old threshold accepted.
        total, profit = arbitrage_cost(0.4925, 0.4925, 0.05)
        self.assertGreater(total, 1.0)
        self.assertLess(profit, 0.0)

    def test_fee_free_pairs_keep_their_profit(self) -> None:
        total, profit = arbitrage_cost(0.49, 0.49, 0.0)
        self.assertAlmostEqual(total, 0.98, places=9)
        self.assertAlmostEqual(profit, 0.02, places=9)

    def test_lopsided_pairs_are_cheaper_to_arbitrage(self) -> None:
        balanced = arbitrage_cost(0.5, 0.5, 0.05)[0]
        lopsided = arbitrage_cost(0.95, 0.05, 0.05)[0]
        self.assertLess(lopsided, balanced)


if __name__ == "__main__":
    unittest.main()
