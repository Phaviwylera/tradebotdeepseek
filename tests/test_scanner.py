"""Market discovery parsing and order-book normalization tests."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone

from scanner import Market, MarketScanner, OrderBook, parse_datetime


def raw_market(**overrides):
    payload = {
        "conditionId": "0xcondition",
        "question": "Will the test resolve YES?",
        "clobTokenIds": json.dumps(["token-yes", "token-no"]),
        "outcomes": json.dumps(["Yes", "No"]),
        "volumeNum": 25_000.0,
        "liquidityNum": 4_000.0,
        "active": True,
        "closed": False,
        "archived": False,
        "acceptingOrders": True,
        "enableOrderBook": True,
        "endDate": "2026-12-01T00:00:00Z",
    }
    payload.update(overrides)
    return payload


class MarketParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scanner = MarketScanner("https://gamma.invalid", "https://clob.invalid")

    def parse(self, payload):
        return self.scanner._parse_market(payload)

    def test_accepts_a_well_formed_market(self) -> None:
        market = self.parse(raw_market())
        self.assertIsNotNone(market)
        assert market is not None
        self.assertEqual(market.condition_id, "0xcondition")
        self.assertEqual(market.token_yes, "token-yes")
        self.assertEqual(market.token_no, "token-no")
        self.assertGreater(market.volume, 24_000)
        self.assertEqual(market.end_date, datetime(2026, 12, 1, tzinfo=timezone.utc))

    def test_rejects_closed_archived_or_inactive(self) -> None:
        self.assertIsNone(self.parse(raw_market(closed=True)))
        self.assertIsNone(self.parse(raw_market(archived=True)))
        self.assertIsNone(self.parse(raw_market(active=False)))
        self.assertIsNone(self.parse(raw_market(acceptingOrders=False)))
        self.assertIsNone(self.parse(raw_market(enableOrderBook=False)))

    def test_rejects_thin_markets(self) -> None:
        self.assertIsNone(self.parse(raw_market(volumeNum=100.0)))
        self.assertIsNone(self.parse(raw_market(liquidityNum=10.0)))

    def test_rejects_markets_without_two_tokens(self) -> None:
        self.assertIsNone(self.parse(raw_market(clobTokenIds=json.dumps(["only-one"]))))
        self.assertIsNone(self.parse(raw_market(clobTokenIds="not json")))
        self.assertIsNone(self.parse(raw_market(conditionId="")))

    def test_outcome_order_is_respected(self) -> None:
        market = self.parse(
            raw_market(
                clobTokenIds=json.dumps(["token-no", "token-yes"]),
                outcomes=json.dumps(["No", "Yes"]),
            )
        )
        assert market is not None
        self.assertEqual(market.token_yes, "token-yes")
        self.assertEqual(market.token_no, "token-no")

    def test_non_binary_outcomes_fall_back_to_positional_tokens(self) -> None:
        market = self.parse(
            raw_market(
                clobTokenIds=json.dumps(["a", "b"]),
                outcomes=json.dumps(["Alpha", "Beta"]),
            )
        )
        assert market is not None
        self.assertEqual((market.token_yes, market.token_no), ("a", "b"))

    def test_accepts_string_flags_and_numeric_strings(self) -> None:
        market = self.parse(raw_market(active="true", closed="false", volumeNum="25000.5"))
        assert market is not None
        self.assertEqual(market.volume, 25_000.5)


class OrderBookTests(unittest.TestCase):
    def test_levels_are_sorted_and_aggregated(self) -> None:
        book = OrderBook.from_raw(
            "token",
            bids=[{"price": "0.41", "size": "10"}, {"price": "0.43", "size": "5"}],
            asks=[("0.45", "7"), {"price": "0.44", "size": "2"}],
        )
        self.assertEqual([level.price for level in book.bids], [0.43, 0.41])
        self.assertEqual([level.price for level in book.asks], [0.44, 0.45])
        self.assertEqual(book.best_bid, 0.43)
        self.assertEqual(book.best_ask, 0.44)
        self.assertAlmostEqual(book.spread or 0.0, 0.01)
        self.assertAlmostEqual(book.mid_price or 0.0, 0.435)
        self.assertTrue(book.is_tradable())

    def test_duplicate_levels_are_summed(self) -> None:
        book = OrderBook.from_raw(
            "token",
            bids=[(0.4, 10.0), (0.4, 5.0)],
            asks=[(0.6, 1.0)],
        )
        self.assertEqual(len(book.bids), 1)
        self.assertAlmostEqual(book.bids[0].size, 15.0)

    def test_invalid_levels_are_dropped(self) -> None:
        book = OrderBook.from_raw(
            "token",
            bids=[(0.0, 10.0), (1.5, 2.0), ("x", "y"), (0.4, -1.0), (0.39, 3.0)],
            asks=[(0.61, 1.0)],
        )
        self.assertEqual([level.price for level in book.bids], [0.39])

    def test_depth_uses_nearest_levels(self) -> None:
        book = OrderBook.from_raw(
            "token",
            bids=[(0.40, 1.0), (0.39, 2.0), (0.38, 4.0)],
            asks=[(0.61, 1.0), (0.62, 3.0), (0.63, 0.5)],
        )
        self.assertAlmostEqual(book.depth("bid", 2), 3.0)
        self.assertAlmostEqual(book.depth("ask", 3), 4.5)

    def test_empty_book_is_not_tradable(self) -> None:
        book = OrderBook.from_raw("token", [], [])
        self.assertFalse(book.is_tradable())
        self.assertIsNone(book.mid_price)

    def test_mid_price_falls_back_to_last_trade(self) -> None:
        book = OrderBook.from_raw("token", [], [], last_trade_price="0.77")
        self.assertAlmostEqual(book.mid_price or 0.0, 0.77)


class TimeParsingTests(unittest.TestCase):
    def test_iso_with_z(self) -> None:
        self.assertEqual(
            parse_datetime("2026-05-01T12:00:00Z"),
            datetime(2026, 5, 1, 12, tzinfo=timezone.utc),
        )

    def test_naive_datetime_is_utc(self) -> None:
        parsed = parse_datetime("2026-05-01T12:00:00")
        self.assertEqual(parsed, datetime(2026, 5, 1, 12, tzinfo=timezone.utc))

    def test_date_only(self) -> None:
        self.assertEqual(parse_datetime("2026-05-01"), datetime(2026, 5, 1, tzinfo=timezone.utc))

    def test_garbage_and_empty(self) -> None:
        self.assertIsNone(parse_datetime("not a date"))
        self.assertIsNone(parse_datetime(None))
        self.assertIsNone(parse_datetime(""))

    def test_market_ttl_days(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        market = Market(
            condition_id="0x",
            question="q",
            token_yes="a",
            token_no="b",
            volume=1.0,
            liquidity=1.0,
            end_date=now + timedelta(days=2),
        )
        self.assertAlmostEqual(market.ttl_days(now) or 0.0, 2.0)
        self.assertIsNone(
            Market(
                condition_id="0x",
                question="q",
                token_yes="a",
                token_no="b",
                volume=1.0,
                liquidity=1.0,
                end_date=None,
            ).ttl_days(now)
        )


if __name__ == "__main__":
    unittest.main()
