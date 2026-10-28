"""Bounded-price microstructure: the logit transform and what a book really costs."""

from __future__ import annotations

import unittest

from microstructure import (
    cap_to_depth,
    delta,
    expit,
    logit,
    logit_volatility,
    max_shares_within,
    optimal_spread,
    probability_quote,
    reservation_price,
    risk_share,
    spread_components,
    walk_book,
)
from scanner import OrderBook


class LogitTests(unittest.TestCase):
    def test_logit_and_expit_are_inverses(self) -> None:
        for price in (0.001, 0.02, 0.25, 0.5, 0.75, 0.94, 0.999):
            self.assertAlmostEqual(expit(logit(price)), price, places=10)

    def test_a_coin_flip_sits_at_zero_log_odds(self) -> None:
        self.assertAlmostEqual(logit(0.5), 0.0)

    def test_logit_is_symmetric_about_the_coin_flip(self) -> None:
        self.assertAlmostEqual(logit(0.9), -logit(0.1))

    def test_endpoints_are_rejected_rather_than_returning_infinity(self) -> None:
        for bad in (0.0, 1.0, -0.1, 1.1):
            with self.assertRaises(ValueError):
                logit(bad)

    def test_expit_does_not_overflow(self) -> None:
        self.assertAlmostEqual(expit(800.0), 1.0)
        self.assertAlmostEqual(expit(-800.0), 0.0)


class DeltaTests(unittest.TestCase):
    def test_delta_peaks_at_maximum_uncertainty(self) -> None:
        self.assertAlmostEqual(delta(0.5), 0.25)

    def test_delta_collapses_near_certainty(self) -> None:
        self.assertAlmostEqual(delta(0.99), 0.0099, places=10)

    def test_delta_is_symmetric(self) -> None:
        self.assertAlmostEqual(delta(0.94), delta(0.06))

    def test_out_of_range_prices_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            delta(1.5)

    def test_volatility_scales_with_the_sensitivity(self) -> None:
        # The same belief volatility moves a 0.50 market 25x more than a 0.99 one.
        self.assertGreater(logit_volatility(0.5, 0.1), logit_volatility(0.94, 0.1))
        self.assertAlmostEqual(logit_volatility(0.5, 1.0), 0.25)


class ReservationTests(unittest.TestCase):
    def test_zero_inventory_reserves_the_mid(self) -> None:
        self.assertAlmostEqual(reservation_price(0.62), 0.62)

    def test_long_inventory_reserves_below_the_mid(self) -> None:
        self.assertLess(reservation_price(0.62, inventory=1.0), 0.62)

    def test_short_inventory_reserves_above_the_mid(self) -> None:
        self.assertGreater(reservation_price(0.62, inventory=-1.0), 0.62)

    def test_skew_vanishes_as_the_market_resolves(self) -> None:
        # No inventory risk in a coin that has already landed: the [p(1-p)]^2
        # factor is 0.0099^2 at 0.99 against 0.25^2 at 0.50, i.e. 640x smaller.
        at_099 = reservation_price(0.99, inventory=1.0)
        at_050 = reservation_price(0.50, inventory=1.0)

        self.assertAlmostEqual(0.99 - at_099, 2.4502e-8, places=10)
        self.assertGreater(0.50 - at_050, 500 * (0.99 - at_099))

    def test_the_skew_is_sub_tick_at_realistic_parameters(self) -> None:
        # The deflating result: at sigma_b=0.05 the inventory adjustment is
        # 1.4e-5 at a 0.50 mid, far below the 0.001 tick, so it cannot move a
        # quote at all. Inventory management here is a risk-limit problem, not
        # a pricing problem.
        skew = 0.50 - reservation_price(0.50, inventory=1.0)

        self.assertLess(skew, 0.001)

    def test_the_skew_becomes_tradeable_when_belief_volatility_is_high(self) -> None:
        # sigma_b=1.0 in logit units/day is a market in genuine flux.
        skew = 0.50 - reservation_price(0.50, inventory=1.0, belief_volatility=1.0)

        self.assertGreater(skew, 0.001)

    def test_invalid_inputs_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            reservation_price(0.0)
        with self.assertRaises(ValueError):
            reservation_price(0.5, gamma=-1.0)


class SpreadTests(unittest.TestCase):
    def test_adverse_selection_is_a_floor_independent_of_certainty(self) -> None:
        # The memo implies spreads collapse toward zero near certainty. They do
        # not: this component prices being picked off and does not care how
        # certain the outcome looks.
        floors = [spread_components(price)[1] for price in (0.05, 0.5, 0.94, 0.99)]

        for value in floors:
            self.assertAlmostEqual(value, floors[0], places=12)
        self.assertGreater(floors[0], 0.01)

    def test_inventory_risk_shrinks_near_certainty(self) -> None:
        risky = spread_components(0.50)[0]
        calm = spread_components(0.99)[0]

        self.assertGreater(risky, calm)

    def test_inventory_risk_is_negligible_at_realistic_parameters(self) -> None:
        # The deflating number: at sigma_b=0.05 and one day of horizon, ~0.1% of
        # the quoted width compensates inventory risk. The rest is a rent.
        self.assertLess(risk_share(0.5), 0.01)
        self.assertLess(risk_share(0.94), 0.001)

    def test_longer_horizon_raises_the_risk_share(self) -> None:
        self.assertGreater(risk_share(0.5, horizon=30.0), risk_share(0.5, horizon=1.0))

    def test_zero_horizon_leaves_only_the_floor(self) -> None:
        risk, adverse = spread_components(0.5, horizon=0.0)

        self.assertEqual(risk, 0.0)
        self.assertGreater(adverse, 0.0)

    def test_optimal_spread_matches_the_components(self) -> None:
        risk, adverse = spread_components(0.62)

        self.assertAlmostEqual(optimal_spread(0.62), risk + adverse)

    def test_invalid_parameters_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            spread_components(0.5, k=0.0)
        with self.assertRaises(ValueError):
            spread_components(1.0)


class ProbabilityQuoteTests(unittest.TestCase):
    def test_quote_brackets_the_price(self) -> None:
        quote = probability_quote(0.62)

        self.assertLess(quote.bid, quote.mid)
        self.assertGreater(quote.ask, quote.mid)

    def test_quote_stays_inside_the_tradeable_range(self) -> None:
        for price in (0.002, 0.05, 0.5, 0.95, 0.998):
            quote = probability_quote(price)
            self.assertGreaterEqual(quote.bid, 0.001)
            self.assertLessEqual(quote.ask, 0.999)
            self.assertLess(quote.bid, quote.ask)

    def test_a_wide_model_spread_cannot_span_the_whole_book(self) -> None:
        quote = probability_quote(0.5, max_width=0.9)

        self.assertLessEqual(quote.spread, 0.9 + 0.002)

    def test_prices_snap_to_the_tick(self) -> None:
        quote = probability_quote(0.62, tick=0.01)

        self.assertAlmostEqual(quote.bid * 100, round(quote.bid * 100), places=6)

    def test_inventory_skews_both_sides_when_the_skew_is_above_the_tick(self) -> None:
        flat = probability_quote(0.62, belief_volatility=1.0)
        long_yes = probability_quote(0.62, inventory=1.0, belief_volatility=1.0)

        self.assertLess(long_yes.bid, flat.bid)
        self.assertLess(long_yes.ask, flat.ask)

    def test_inventory_skew_cannot_move_a_quote_below_one_tick(self) -> None:
        # At the default sigma_b the skew is 1.4e-5, so the rounded quote is
        # identical. Reporting a "skew" here would be inventing precision.
        self.assertEqual(
            probability_quote(0.62, inventory=1.0).as_dict()["bid"],
            probability_quote(0.62).as_dict()["bid"],
        )

    def test_edge_bps_is_larger_on_a_cheap_contract(self) -> None:
        self.assertGreater(probability_quote(0.20).edge_bps, probability_quote(0.94).edge_bps)

    def test_as_dict_exposes_both_components(self) -> None:
        payload = probability_quote(0.5).as_dict()

        self.assertIn("risk_component", payload)
        self.assertIn("adverse_component", payload)
        self.assertAlmostEqual(
            payload["risk_component"] + payload["adverse_component"],
            payload["spread"],
            places=4,
        )

    def test_invalid_inputs_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            probability_quote(0.0)
        with self.assertRaises(ValueError):
            probability_quote(0.5, tick=0.0)


class WalkBookTests(unittest.TestCase):
    def book(self) -> OrderBook:
        return OrderBook.from_raw(
            "token",
            [(0.93, 50.0)],
            [(0.94, 10.0), (0.945, 10.0), (0.95, 100.0)],
        )

    def test_an_order_inside_the_top_level_pays_the_quoted_price(self) -> None:
        fill = walk_book(self.book().asks, 10.0)

        self.assertAlmostEqual(fill.average_price, 0.94)
        self.assertEqual(fill.levels_consumed, 1)
        self.assertTrue(fill.filled)

    def test_walking_two_levels_averages_the_prices(self) -> None:
        fill = walk_book(self.book().asks, 20.0)

        self.assertAlmostEqual(fill.average_price, (10 * 0.94 + 10 * 0.945) / 20)
        self.assertEqual(fill.worst_price, 0.945)

    def test_slippage_against_the_quoted_price_is_measured(self) -> None:
        # The whole point: the book still reads 0.94 but the fill is worse.
        fill = walk_book(self.book().asks, 30.0)

        self.assertAlmostEqual(fill.average_price, 0.945, places=6)
        self.assertAlmostEqual(fill.slippage_vs(0.94), 0.005, places=6)

    def test_an_order_larger_than_the_book_is_partially_filled(self) -> None:
        fill = walk_book(self.book().asks, 500.0)

        self.assertEqual(fill.shares, 120.0)
        self.assertAlmostEqual(fill.unfilled, 380.0)
        self.assertFalse(fill.filled)

    def test_slippage_can_erase_a_one_cent_edge(self) -> None:
        # A 0.94 snipe holds 0.95, so the edge is one cent. Half a cent of
        # slippage takes half of it, and the quoted price never showed it.
        fill = walk_book(self.book().asks, 30.0)

        self.assertGreater(fill.slippage_vs(0.94), 0.004)
        self.assertLess(fill.slippage_vs(0.94), 0.01)

    def test_an_empty_book_fills_nothing(self) -> None:
        fill = walk_book(OrderBook.from_raw("t", [], []).asks, 10.0)

        self.assertEqual(fill.shares, 0.0)
        self.assertFalse(fill.filled)

    def test_non_positive_size_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            walk_book(self.book().asks, 0.0)

    def test_as_dict_rounds_for_display(self) -> None:
        payload = walk_book(self.book().asks, 30.0).as_dict()

        self.assertEqual(payload["average_price"], 0.945)
        self.assertEqual(payload["levels_consumed"], 3)


class MaxSharesTests(unittest.TestCase):
    def book(self) -> OrderBook:
        return OrderBook.from_raw("token", [], [(0.94, 10.0), (0.95, 10.0), (0.96, 100.0)])

    def test_a_cap_at_the_top_level_stops_at_that_level(self) -> None:
        self.assertEqual(max_shares_within(self.book().asks, 100.0, 0.94), 10.0)

    def test_a_cap_two_levels_deep_allows_both(self) -> None:
        self.assertEqual(max_shares_within(self.book().asks, 100.0, 0.95), 20.0)

    def test_a_cap_beyond_the_book_is_the_whole_book(self) -> None:
        self.assertEqual(max_shares_within(self.book().asks, 100.0, 0.999), 100.0)

    def test_a_small_request_is_not_inflated(self) -> None:
        self.assertEqual(max_shares_within(self.book().asks, 5.0, 0.999), 5.0)

    def test_invalid_inputs_return_zero(self) -> None:
        self.assertEqual(max_shares_within(self.book().asks, 0.0, 0.94), 0.0)
        self.assertEqual(max_shares_within(self.book().asks, 10.0, 0.0), 0.0)


class DepthCapTests(unittest.TestCase):
    def book(self) -> OrderBook:
        return OrderBook.from_raw("token", [(0.93, 50.0)], [(0.94, 100.0)])

    def test_an_order_is_trimmed_to_a_fraction_of_depth(self) -> None:
        cap = cap_to_depth(self.book(), 500.0, participation=0.25)

        self.assertEqual(cap.shares, 25.0)
        self.assertTrue(cap.binding)

    def test_a_small_order_is_left_alone(self) -> None:
        cap = cap_to_depth(self.book(), 5.0, participation=0.25)

        self.assertEqual(cap.shares, 5.0)
        self.assertFalse(cap.binding)

    def test_an_empty_book_caps_to_zero(self) -> None:
        cap = cap_to_depth(OrderBook.from_raw("t", [], []), 10.0)

        self.assertEqual(cap.shares, 0.0)

    def test_participation_must_be_a_fraction(self) -> None:
        for bad in (0.0, 1.5):
            with self.assertRaises(ValueError):
                cap_to_depth(self.book(), 10.0, participation=bad)

    def test_as_dict_reports_the_book_depth(self) -> None:
        payload = cap_to_depth(self.book(), 500.0).as_dict()

        self.assertEqual(payload["top_of_book_depth"], 100.0)


if __name__ == "__main__":
    unittest.main()
