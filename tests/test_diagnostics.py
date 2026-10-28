"""The per-cycle gate diagnostics that explain a quiet bot.

"Nothing happens" is a legitimate outcome for this bot — both live gates are
rare — but it must be an *explained* outcome. These tests pin the numbers that
/healthz publishes so a silent strategy can never again look like a broken bot.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from main import TradingBot
from scanner import Market, OrderBook
from strategy import StrategyEngine


def make_settings(**overrides) -> SimpleNamespace:
    base = {
        "ws_market_url": "wss://invalid",
        "stream_backoff_seconds": 1.0,
        "snapshot_path": None,
        "news_sentiment": 0.0,
        "live": False,
        "bankroll": 200.0,
        "max_bet": 20.0,
        "book_staleness_seconds": 20.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def make_bot(**settings_overrides) -> TradingBot:
    return TradingBot(
        settings=make_settings(**settings_overrides),
        scanner=SimpleNamespace(),
        ledger=SimpleNamespace(),
        strategy=StrategyEngine(min_ev=0.02, max_bet=20.0),
        risk=SimpleNamespace(),
        model=SimpleNamespace(loaded=False),
        executor=SimpleNamespace(),
    )


def book(bid: float | None, ask: float | None) -> OrderBook:
    return OrderBook.from_raw(
        "token",
        [(bid, 100.0)] if bid is not None else [],
        [(ask, 100.0)] if ask is not None else [],
    )


def market(end_date: datetime | None) -> Market:
    return Market(
        condition_id="0xabc",
        question="q",
        token_yes="yes",
        token_no="no",
        volume=1_000.0,
        liquidity=100.0,
        end_date=end_date,
    )


class GateProximityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bot = make_bot()

    def test_counts_a_market_inside_the_snipe_band(self) -> None:
        self.bot._record_gate_proximity(
            market(datetime.now(timezone.utc) + timedelta(hours=1)),
            book(0.9200, 0.9300),
            book(0.0600, 0.0700),
        )
        self.assertEqual(self.bot.diagnostics["markets_in_snipe_band"], 1)
        self.assertAlmostEqual(self.bot.diagnostics["best_arb_sum"], 1.0, places=6)

    def test_does_not_count_a_market_outside_the_band(self) -> None:
        # 0.94 can never clear MIN_EV, so it must not be counted as "in band".
        self.bot._record_gate_proximity(
            market(datetime.now(timezone.utc) + timedelta(hours=1)),
            book(0.9300, 0.9400),
            book(0.0500, 0.0600),
        )
        self.assertEqual(self.bot.diagnostics["markets_in_snipe_band"], 0)

    def test_records_the_closest_ask_and_its_distance(self) -> None:
        self.bot._record_gate_proximity(
            market(datetime.now(timezone.utc) + timedelta(hours=1)),
            book(0.8600, 0.8700),
            book(0.1200, 0.1300),
        )
        closest = self.bot.diagnostics["closest_snipe_price"]
        assert closest is not None
        self.assertEqual(closest["ask"], 0.87)
        # Default band floor is 0.93, so 0.87 is six cents below it.
        self.assertAlmostEqual(closest["distance"], 0.06, places=6)

    def test_a_price_inside_the_band_reports_zero_distance(self) -> None:
        # /healthz used to print "nearest ask 0.93, distance 0.0106" beside a
        # band of [0.9000, 0.9406): a price inside its own band, apparently
        # outside it.
        self.bot._record_gate_proximity(
            market(datetime.now(timezone.utc) + timedelta(hours=1)),
            book(0.9200, 0.9300),
            book(0.0600, 0.0700),
        )
        closest = self.bot.diagnostics["closest_snipe_price"]
        assert closest is not None
        self.assertEqual(closest["ask"], 0.93)
        self.assertEqual(closest["distance"], 0.0)

    def test_a_price_in_the_band_but_outside_the_window_is_explained(self) -> None:
        # The live contradiction: a 0.91 ask is inside [0.90, 0.9406) so the
        # price distance is 0.0, yet it resolves in 6.16h -- well past the 2.4h
        # snipe window. Publishing the time gate makes the pair read correctly.
        # The production deployment widens the band to [0.90, 0.9406).
        self.bot.strategy = StrategyEngine(min_ev=0.01, snipe_min_ask=0.90)
        self.bot._record_gate_proximity(
            market(datetime.now(timezone.utc) + timedelta(hours=6.16)),
            book(0.9000, 0.9100),
            book(0.0800, 0.0900),
        )
        closest = self.bot.diagnostics["closest_snipe_price"]
        assert closest is not None
        self.assertEqual(closest["distance"], 0.0)
        self.assertFalse(closest["in_time_window"])
        self.assertAlmostEqual(closest["hours_to_window"], 3.76, places=2)

    def test_a_market_inside_both_gates_reports_an_open_window(self) -> None:
        self.bot._record_gate_proximity(
            market(datetime.now(timezone.utc) + timedelta(hours=1)),
            book(0.9200, 0.9300),
            book(0.0600, 0.0700),
        )
        closest = self.bot.diagnostics["closest_snipe_price"]
        assert closest is not None
        self.assertTrue(closest["in_time_window"])
        self.assertEqual(closest["hours_to_window"], 0.0)

    def test_a_market_blocked_only_by_the_clock_outranks_one_outside_the_band(self) -> None:
        # 0.91 is one gate away (time); 0.80 is two gates away (price and time).
        self.bot.strategy = StrategyEngine(min_ev=0.01, snipe_min_ask=0.90)
        self.bot._record_gate_proximity(
            market(datetime.now(timezone.utc) + timedelta(hours=6)),
            book(0.7900, 0.8000),
            book(0.1900, 0.2000),
        )
        self.bot._record_gate_proximity(
            market(datetime.now(timezone.utc) + timedelta(hours=6)),
            book(0.9000, 0.9100),
            book(0.0800, 0.0900),
        )
        closest = self.bot.diagnostics["closest_snipe_price"]
        assert closest is not None
        self.assertEqual(closest["ask"], 0.91)

    def test_keeps_the_best_arb_sum_of_the_cycle(self) -> None:
        soon = datetime.now(timezone.utc) + timedelta(hours=1)
        self.bot._record_gate_proximity(market(soon), book(0.40, 0.41), book(0.60, 0.61))
        self.bot._record_gate_proximity(market(soon), book(0.45, 0.46), book(0.52, 0.53))
        self.assertAlmostEqual(self.bot.diagnostics["best_arb_sum"], 0.99, places=6)

    def test_records_the_closest_time_to_resolution(self) -> None:
        now = datetime.now(timezone.utc)
        self.bot._record_gate_proximity(
            market(now + timedelta(hours=10)), book(0.5, 0.51), book(0.49, 0.5)
        )
        self.bot._record_gate_proximity(
            market(now + timedelta(minutes=30)), book(0.5, 0.51), book(0.49, 0.5)
        )
        self.assertAlmostEqual(self.bot.diagnostics["closest_ttl_hours"], 0.5, places=1)

    def test_missing_books_do_not_crash_or_invent_numbers(self) -> None:
        self.bot._record_gate_proximity(
            market(datetime.now(timezone.utc) + timedelta(hours=1)),
            book(None, None),
            book(None, None),
        )
        self.assertEqual(self.bot.diagnostics["markets_in_snipe_band"], 0)
        self.assertIsNone(self.bot.diagnostics["best_arb_sum"])
        self.assertIsNone(self.bot.diagnostics["closest_snipe_price"])

    def test_market_without_an_end_date_is_tolerated_but_not_counted(self) -> None:
        # No ttl means the time gate cannot pass, so it is not "in the band"
        # even though the price qualifies.
        self.bot._record_gate_proximity(market(None), book(0.92, 0.93), book(0.06, 0.07))
        self.assertEqual(self.bot.diagnostics["markets_in_snipe_band"], 0)
        self.assertIsNone(self.bot.diagnostics["closest_ttl_hours"])

    def test_a_price_in_band_but_far_from_resolution_is_not_counted(self) -> None:
        # This is the false positive that made a quiet bot look broken: 1 cent
        # from the band, 66 days from resolution, reported as "in band".
        self.bot._record_gate_proximity(
            market(datetime.now(timezone.utc) + timedelta(days=66)),
            book(0.92, 0.93),
            book(0.06, 0.07),
        )
        self.assertEqual(self.bot.diagnostics["markets_in_snipe_band"], 0)

    def test_an_overdue_market_inside_the_window_counts(self) -> None:
        self.bot._record_gate_proximity(
            market(datetime.now(timezone.utc) - timedelta(hours=6)),
            book(0.92, 0.93),
            book(0.06, 0.07),
        )
        self.assertEqual(self.bot.diagnostics["markets_in_snipe_band"], 1)

    def test_a_market_beyond_the_overdue_window_does_not_count(self) -> None:
        self.bot._record_gate_proximity(
            market(datetime.now(timezone.utc) - timedelta(hours=30)),
            book(0.92, 0.93),
            book(0.06, 0.07),
        )
        self.assertEqual(self.bot.diagnostics["markets_in_snipe_band"], 0)


if __name__ == "__main__":
    unittest.main()


class OneSidedBookPipelineTests(unittest.TestCase):
    """A market whose book has only one side must still be evaluated.

    Regression guard for the bug that silently emptied the snipe lane: the book
    cache and fetch path required a two-way quote, so every near-resolution
    market (one side empty by construction) was dropped before any strategy saw
    it. `markets_evaluated` stayed at 12 of 20 and `signals_total` at 0.
    """

    def setUp(self) -> None:
        import asyncio

        self.asyncio = asyncio

    def test_a_book_with_only_asks_is_returned(self) -> None:
        class Scanner:
            async def get_orderbook(self, token_id):
                return OrderBook.from_raw(token_id, [], [(0.93, 100.0)])

        bot = TradingBot(
            settings=make_settings(book_staleness_seconds=20.0),
            scanner=Scanner(),
            ledger=SimpleNamespace(),
            strategy=StrategyEngine(min_ev=0.01, snipe_min_ask=0.90),
            risk=SimpleNamespace(),
            model=SimpleNamespace(loaded=False),
            executor=SimpleNamespace(),
        )
        book = self.asyncio.run(bot._get_book("token"))
        assert book is not None
        self.assertAlmostEqual(book.best_ask, 0.93)
        self.assertIsNone(book.best_bid)

    def test_an_empty_book_is_still_refused(self) -> None:
        class Scanner:
            async def get_orderbook(self, token_id):
                return OrderBook.from_raw(token_id, [], [])

        bot = TradingBot(
            settings=make_settings(book_staleness_seconds=20.0),
            scanner=Scanner(),
            ledger=SimpleNamespace(),
            strategy=StrategyEngine(),
            risk=SimpleNamespace(),
            model=SimpleNamespace(loaded=False),
            executor=SimpleNamespace(),
        )
        self.assertIsNone(self.asyncio.run(bot._get_book("token")))

    def test_a_one_sided_market_can_produce_a_signal(self) -> None:
        # End to end through the strategies, without a network.
        bot = TradingBot(
            settings=make_settings(),
            scanner=SimpleNamespace(),
            ledger=SimpleNamespace(),
            strategy=StrategyEngine(min_ev=0.01, snipe_min_ask=0.90, snipe_max_ask=0.97),
            risk=SimpleNamespace(),
            model=SimpleNamespace(loaded=False),
            executor=SimpleNamespace(),
        )
        soon = datetime.now(timezone.utc) + timedelta(minutes=30)
        yes_book = OrderBook.from_raw("yes", [], [(0.905, 100.0)])
        no_book = OrderBook.from_raw("no", [(0.08, 100.0)], [])
        bot._record_gate_proximity(market(soon), yes_book, no_book)
        self.assertEqual(bot.diagnostics["markets_in_snipe_band"], 1)
        signals = bot.strategy.check_snipe(market(soon), yes_book, no_book)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].token_id, "yes")


class BookAccountingTests(unittest.TestCase):
    def test_markets_without_a_book_are_counted(self) -> None:
        bot = make_bot()
        self.assertEqual(bot.diagnostics["markets_without_book"], 0)
