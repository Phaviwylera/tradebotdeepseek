from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from scanner import Market, OrderBook
from strategy import StrategyEngine


def make_market(end_date: datetime | None = None) -> Market:
    return Market(
        condition_id="0xcondition",
        question="Will the test resolve YES?",
        token_yes="yes-token",
        token_no="no-token",
        volume=50_000.0,
        liquidity=10_000.0,
        end_date=end_date or datetime.now(timezone.utc) + timedelta(days=10),
    )


def make_book(token_id: str, bid: float, ask: float, size: float = 100.0) -> OrderBook:
    return OrderBook.from_raw(
        token_id,
        [(bid, size)],
        [(ask, size)],
    )


class ArbitrageTests(unittest.TestCase):
    def test_arbitrage_emits_two_hedged_legs(self) -> None:
        engine = StrategyEngine(min_ev=0.02, max_bet=20.0)
        signals = engine.check_arbitrage(
            make_market(),
            make_book("yes-token", 0.47, 0.48),
            make_book("no-token", 0.49, 0.50),
        )

        self.assertEqual(len(signals), 2)
        self.assertEqual({signal.token_id for signal in signals}, {"yes-token", "no-token"})
        self.assertTrue(all(signal.side == "BUY" for signal in signals))
        self.assertTrue(all(signal.reason.startswith("ARB YES+NO=") for signal in signals))
        self.assertGreater(sum(signal.expected_value for signal in signals), 0.0)
        self.assertLessEqual(sum(signal.size_usd for signal in signals), 20.01)

    def test_no_arbitrage_when_pair_costs_more_than_threshold(self) -> None:
        engine = StrategyEngine(min_ev=0.02, max_bet=20.0)
        signals = engine.check_arbitrage(
            make_market(),
            make_book("yes-token", 0.48, 0.49),
            make_book("no-token", 0.49, 0.50),
        )
        self.assertEqual(signals, [])

    def test_no_arbitrage_without_both_asks(self) -> None:
        engine = StrategyEngine(min_ev=0.02, max_bet=20.0)
        empty = OrderBook.from_raw("no-token", [], [])
        signals = engine.check_arbitrage(
            make_market(),
            make_book("yes-token", 0.40, 0.41),
            empty,
        )
        self.assertEqual(signals, [])


class SnipeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        self.engine = StrategyEngine(min_ev=0.02, max_bet=20.0)

    def test_snipe_fires_near_resolution_in_price_band(self) -> None:
        market = make_market(self.now + timedelta(days=0.08))
        signals = self.engine.check_snipe(
            market,
            make_book("yes-token", 0.92, 0.93),
            make_book("no-token", 0.06, 0.07),
            now=self.now,
        )
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].token_id, "yes-token")
        self.assertTrue(signals[0].reason.startswith("SNIPE YES"))
        self.assertGreater(signals[0].expected_value, 0.0)

    def test_snipe_band_is_capped_by_the_ev_breakeven(self) -> None:
        # Buying above hold / (1 + min_ev) can never clear the EV gate, so the
        # advertised [0.93, 0.97] band must collapse to what can actually fire.
        self.assertAlmostEqual(self.engine.snipe_max_ask(), 0.95 / 1.02, places=6)
        self.assertLess(self.engine.snipe_max_ask(), 0.97)

    def test_snipe_stops_exactly_at_the_breakeven_ask(self) -> None:
        market = make_market(self.now + timedelta(days=0.08))
        inside = self.engine.check_snipe(
            market,
            make_book("yes-token", 0.9300, 0.9313),
            make_book("no-token", 0.0600, 0.0687),
            now=self.now,
        )
        self.assertEqual(len(inside), 1)
        outside = self.engine.check_snipe(
            market,
            make_book("yes-token", 0.9300, 0.9314),
            make_book("no-token", 0.0600, 0.0686),
            now=self.now,
        )
        self.assertEqual(outside, [])

    def test_describe_gates_prints_the_numbers_a_quiet_bot_needs(self) -> None:
        text = self.engine.describe_gates()
        self.assertIn("0.985", text)
        self.assertIn("0.9314", text)
        self.assertIn("min_ev=0.0200", text)
        self.assertNotIn("band is empty", text)

    def test_snipe_rejects_insufficient_ev_in_band(self) -> None:
        market = make_market(self.now + timedelta(days=0.08))
        signals = self.engine.check_snipe(
            market,
            make_book("yes-token", 0.93, 0.94),
            make_book("no-token", 0.05, 0.06),
            now=self.now,
        )
        self.assertEqual(signals, [])

    def test_snipe_rejects_outside_time_and_price(self) -> None:
        too_early = make_market(self.now + timedelta(days=0.20))
        self.assertEqual(
            self.engine.check_snipe(
                too_early,
                make_book("yes-token", 0.92, 0.93),
                make_book("no-token", 0.06, 0.07),
                now=self.now,
            ),
            [],
        )
        too_cheap = make_market(self.now + timedelta(days=0.08))
        self.assertEqual(
            self.engine.check_snipe(
                too_cheap,
                make_book("yes-token", 0.91, 0.92),
                make_book("no-token", 0.07, 0.08),
                now=self.now,
            ),
            [],
        )


class ModelEdgeTests(unittest.TestCase):
    def test_model_edge_sizes_quarter_kelly_and_caps(self) -> None:
        engine = StrategyEngine(min_ev=0.02, max_bet=20.0)
        signal = engine.check_model_edge(
            make_market(),
            make_book("yes-token", 0.58, 0.60),
            make_book("no-token", 0.39, 0.41),
            anchored_prob=0.70,
            bankroll=200.0,
        )
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.token_id, "yes-token")
        self.assertTrue(signal.reason.startswith("MODEL YES"))
        self.assertAlmostEqual(signal.size_usd, 12.50)
        self.assertLessEqual(signal.size_usd, 20.0)

    def test_model_edge_respects_confidence_floor(self) -> None:
        engine = StrategyEngine(min_ev=0.02, max_bet=20.0)
        signal = engine.check_model_edge(
            make_market(),
            make_book("yes-token", 0.68, 0.69),
            make_book("no-token", 0.30, 0.31),
            anchored_prob=0.58,
            bankroll=200.0,
            confidence=0.55,
        )
        self.assertIsNone(signal)


if __name__ == "__main__":
    unittest.main()
