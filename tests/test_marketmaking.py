"""Avellaneda-Stoikov quoting: units, inventory skew, and hard price bounds."""

from __future__ import annotations

import unittest

from marketmaking import (
    DEFAULT_K,
    MAX_QUOTE_WIDTH,
    MIN_TICK,
    optimal_spread,
    quote,
    rebate_value,
    reservation_price,
)


class ReservationTests(unittest.TestCase):
    def test_zero_inventory_quotes_the_mid(self) -> None:
        self.assertAlmostEqual(reservation_price(0.62, 0.0, 0.1, 0.02, 1.0), 0.62)

    def test_long_inventory_pulls_the_reservation_price_down(self) -> None:
        long_yes = reservation_price(0.62, inventory=100.0, gamma=0.1, sigma=0.02, horizon=1.0)
        short_yes = reservation_price(0.62, inventory=-100.0, gamma=0.1, sigma=0.02, horizon=1.0)

        self.assertLess(long_yes, 0.62)
        self.assertGreater(short_yes, 0.62)

    def test_no_risk_aversion_means_no_skew(self) -> None:
        self.assertAlmostEqual(reservation_price(0.62, 100.0, 0.0, 0.02, 1.0), 0.62)

    def test_expired_horizon_means_no_skew(self) -> None:
        self.assertAlmostEqual(reservation_price(0.62, 100.0, 0.1, 0.02, 0.0), 0.62)

    def test_invalid_mid_returns_zero(self) -> None:
        self.assertEqual(reservation_price(0.0, 0.0, 0.1, 0.02, 1.0), 0.0)


class SpreadTests(unittest.TestCase):
    def test_spread_has_the_right_order_of_magnitude_for_a_0_60_market(self) -> None:
        # Default k is scaled for prices in (0, 1); a 2c spread, not the whole book.
        width = optimal_spread(0.1, 0.02, 1.0, DEFAULT_K)

        self.assertGreater(width, 0.005)
        self.assertLess(width, 0.05)

    def test_more_volatility_widens_the_spread(self) -> None:
        calm = optimal_spread(0.1, 0.01, 1.0, DEFAULT_K)
        wild = optimal_spread(0.1, 0.05, 1.0, DEFAULT_K)

        self.assertGreater(wild, calm)

    def test_faster_arrival_narrows_the_spread(self) -> None:
        slow = optimal_spread(0.1, 0.02, 1.0, 10.0)
        fast = optimal_spread(0.1, 0.02, 1.0, 1000.0)

        self.assertLess(fast, slow)

    def test_zero_volatility_or_horizon_means_no_spread(self) -> None:
        self.assertEqual(optimal_spread(0.1, 0.0, 1.0, DEFAULT_K), 0.0)
        self.assertEqual(optimal_spread(0.1, 0.02, 0.0, DEFAULT_K), 0.0)

    def test_negative_k_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            optimal_spread(0.1, 0.02, 1.0, 0.0)


class QuoteTests(unittest.TestCase):
    def test_quote_brackets_the_mid(self) -> None:
        result = quote(0.62)

        self.assertLess(result.bid, result.mid)
        self.assertGreater(result.ask, result.mid)
        self.assertAlmostEqual(result.spread, result.ask - result.bid)

    def test_quote_is_always_inside_the_tradeable_range(self) -> None:
        for mid in (0.002, 0.05, 0.5, 0.95, 0.998):
            result = quote(mid)
            self.assertGreaterEqual(result.bid, MIN_TICK)
            self.assertLessEqual(result.ask, 1.0 - MIN_TICK)
            self.assertLess(result.bid, result.ask)

    def test_quote_is_never_wide_enough_to_span_the_whole_book(self) -> None:
        result = quote(0.5, sigma=0.5)

        self.assertLessEqual(result.spread, MAX_QUOTE_WIDTH + 2 * MIN_TICK)

    def test_prices_snap_to_the_tick(self) -> None:
        result = quote(0.62, tick=0.01)

        self.assertAlmostEqual(result.bid * 100, round(result.bid * 100))
        self.assertAlmostEqual(result.ask * 100, round(result.ask * 100))

    def test_inventory_skews_both_sides_down(self) -> None:
        flat = quote(0.62, inventory=0.0)
        long_yes = quote(0.62, inventory=100.0)

        self.assertLess(long_yes.bid, flat.bid)
        self.assertLess(long_yes.ask, flat.ask)

    def test_inventory_skews_both_sides_up_when_short(self) -> None:
        flat = quote(0.62, inventory=0.0)
        short_yes = quote(0.62, inventory=-100.0)

        self.assertGreater(short_yes.bid, flat.bid)
        self.assertGreater(short_yes.ask, flat.ask)

    def test_quote_is_centred_on_the_reservation_price(self) -> None:
        result = quote(0.62, inventory=100.0)

        self.assertAlmostEqual((result.bid + result.ask) / 2.0, result.reservation, places=2)

    def test_edge_per_share_is_half_the_quoted_spread(self) -> None:
        result = quote(0.62)

        self.assertAlmostEqual(result.edge_per_share, result.spread / 2.0, places=12)

    def test_edge_bps_scales_with_price(self) -> None:
        cheap = quote(0.30)
        rich = quote(0.94)

        self.assertGreater(cheap.edge_bps, rich.edge_bps)

    def test_invalid_mid_is_rejected(self) -> None:
        for bad in (0.0, 1.0, -0.5, 1.5):
            with self.assertRaises(ValueError):
                quote(bad)

    def test_non_positive_tick_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            quote(0.62, tick=0.0)

    def test_as_dict_is_rounded(self) -> None:
        payload = quote(0.62).as_dict()

        self.assertEqual(
            set(payload), {"mid", "bid", "ask", "inventory", "reservation", "spread", "edge_bps"}
        )
        self.assertEqual(payload["mid"], 0.62)


class RebateTests(unittest.TestCase):
    def test_rebate_grows_with_taker_volume(self) -> None:
        small = rebate_value(1_000.0, 0.05)
        large = rebate_value(10_000.0, 0.05)

        self.assertGreater(large, small)

    def test_rebate_is_zero_without_taker_fees(self) -> None:
        self.assertEqual(rebate_value(10_000.0, 0.0), 0.0)

    def test_rebate_is_zero_without_volume(self) -> None:
        self.assertEqual(rebate_value(0.0, 0.05), 0.0)

    def test_rebate_share_is_clamped(self) -> None:
        self.assertAlmostEqual(
            rebate_value(10_000.0, 0.05, rebate_share=5.0),
            rebate_value(10_000.0, 0.05, rebate_share=1.0),
        )

    def test_rebate_is_small_relative_to_the_volume(self) -> None:
        # 25% of the fee is the rebate pool, and the fee itself is a fraction of
        # notional: a maker should never count on rebates as the main income.
        self.assertLess(rebate_value(10_000.0, 0.05), 10_000.0 * 0.05)


if __name__ == "__main__":
    unittest.main()
