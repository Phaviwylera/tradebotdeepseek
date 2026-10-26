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
            book(0.9000, 0.9100),
            book(0.0800, 0.0900),
        )
        closest = self.bot.diagnostics["closest_snipe_price"]
        assert closest is not None
        self.assertEqual(closest["ask"], 0.91)
        self.assertAlmostEqual(closest["distance"], 0.02, places=6)

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

    def test_market_without_an_end_date_is_tolerated(self) -> None:
        self.bot._record_gate_proximity(market(None), book(0.92, 0.93), book(0.06, 0.07))
        self.assertEqual(self.bot.diagnostics["markets_in_snipe_band"], 1)
        self.assertIsNone(self.bot.diagnostics["closest_ttl_hours"])


if __name__ == "__main__":
    unittest.main()
