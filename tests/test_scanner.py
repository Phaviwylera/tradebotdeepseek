"""Market discovery parsing and order-book normalization tests."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone

from scanner import (
    Market,
    MarketScanner,
    OrderBook,
    parse_datetime,
    select_markets,
)


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


class FakeHTTP:
    """Minimal stand-in for httpx.AsyncClient that records the request."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def get(self, path, params=None):
        self.calls.append({"path": path, "params": dict(params or {})})

        class Response:
            def __init__(self, body):
                self._body = body

            def raise_for_status(self):
                return None

            def json(self):
                return self._body

        return Response(self.payload)


class NearResolutionLaneTests(unittest.IsolatedAsyncioTestCase):
    def make_scanner(self, payload):
        http = FakeHTTP(payload)
        scanner = MarketScanner(
            "https://gamma.invalid",
            "https://clob.invalid",
            http_client=http,
            near_window_hours=6.0,
            near_overdue_hours=24.0,
        )
        return scanner, http

    async def test_asks_gamma_for_markets_closing_soon(self) -> None:
        now = datetime(2026, 10, 26, 20, 0, tzinfo=timezone.utc)
        scanner, http = self.make_scanner([raw_market(endDate="2026-10-26T22:00:00Z")])
        markets = await scanner.scan_near_resolution(now=now, limit=50)
        params = http.calls[0]["params"]
        self.assertEqual(http.calls[0]["path"], "/markets")
        self.assertEqual(params["order"], "endDate")
        self.assertEqual(params["ascending"], "true")
        self.assertEqual(params["end_date_min"], "2026-10-25T20:00:00Z")  # overdue window
        self.assertEqual(params["end_date_max"], "2026-10-27T02:00:00Z")  # +6h
        self.assertEqual(params["limit"], "50")
        self.assertEqual(len(markets), 1)

    async def test_keeps_low_volume_markets_the_main_lane_would_drop(self) -> None:
        scanner, _ = self.make_scanner(
            [raw_market(volumeNum=120.0, liquidityNum=40.0, endDate="2026-10-26T22:00:00Z")]
        )
        markets = await scanner.scan_near_resolution(
            now=datetime(2026, 10, 26, 20, 0, tzinfo=timezone.utc)
        )
        self.assertEqual([m.condition_id for m in markets], ["0xcondition"])

    async def test_unwraps_a_paginated_payload(self) -> None:
        scanner, _ = self.make_scanner({"data": [raw_market(endDate="2026-10-26T22:00:00Z")]})
        markets = await scanner.scan_near_resolution(
            now=datetime(2026, 10, 26, 20, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(len(markets), 1)


def market(condition_id: str, end_date: datetime | None, volume: float = 10_000.0) -> Market:
    return Market(
        condition_id=condition_id,
        question=f"market {condition_id}",
        token_yes=f"{condition_id}-yes",
        token_no=f"{condition_id}-no",
        volume=volume,
        liquidity=1_000.0,
        end_date=end_date,
    )


class SelectMarketsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 10, 26, 20, 0, tzinfo=timezone.utc)
        self.volume_lane = [
            market(f"0xv{i}", self.now + timedelta(days=30), volume=1_000_000.0 - i)
            for i in range(10)
        ]

    def test_near_markets_get_reserved_slots(self) -> None:
        # The bug this fixes: a huge-volume market must not crowd out the one
        # that closes in an hour.
        near = [market("0xnear", self.now + timedelta(hours=1), volume=10.0)]
        chosen = select_markets(self.volume_lane, near, max_total=5, near_slots=2, now=self.now)
        self.assertEqual(len(chosen), 5)
        self.assertIn("0xnear", [m.condition_id for m in chosen])
        self.assertEqual(chosen[0].condition_id, "0xnear")

    def test_nearest_first_and_deduplicated(self) -> None:
        near = [
            market("0xlate", self.now + timedelta(hours=5)),
            market("0xsoon", self.now + timedelta(minutes=30)),
        ]
        chosen = select_markets(
            self.volume_lane + near, near, max_total=6, near_slots=2, now=self.now
        )
        ids = [m.condition_id for m in chosen]
        self.assertEqual(ids[:2], ["0xsoon", "0xlate"])
        self.assertEqual(len(ids), len(set(ids)))

    def test_zero_slots_keeps_old_behaviour(self) -> None:
        near = [market("0xnear", self.now + timedelta(hours=1))]
        chosen = select_markets(self.volume_lane, near, max_total=3, near_slots=0, now=self.now)
        self.assertEqual(len(chosen), 3)
        self.assertNotIn("0xnear", [m.condition_id for m in chosen])

    def test_slots_are_capped_by_the_total_budget(self) -> None:
        near = [market(f"0xn{i}", self.now + timedelta(hours=i + 1)) for i in range(5)]
        chosen = select_markets(self.volume_lane, near, max_total=2, near_slots=9, now=self.now)
        self.assertEqual(len(chosen), 2)

    def test_markets_without_an_end_date_sort_last(self) -> None:
        near = [
            market("0xnone", None),
            market("0xsoon", self.now + timedelta(minutes=10)),
        ]
        chosen = select_markets(self.volume_lane, near, max_total=2, near_slots=2, now=self.now)
        self.assertEqual([m.condition_id for m in chosen], ["0xsoon", "0xnone"])


class FeeParsingTests(unittest.TestCase):
    """Gamma's feeSchedule has to survive parsing, or the EV gate is blind."""

    def setUp(self) -> None:
        self.scanner = MarketScanner("https://gamma.invalid", "https://clob.invalid")

    def parse(self, payload):
        return self.scanner._parse_market(payload)

    def test_sports_fee_is_attached_to_the_market(self) -> None:
        market = self.parse(
            raw_market(
                feesEnabled=True,
                feeType="sports_fees_v3",
                feeSchedule={"exponent": 1, "rate": 0.05, "takerOnly": True, "rebateRate": 0.15},
            )
        )
        assert market is not None
        self.assertAlmostEqual(market.fee_rate, 0.05)
        self.assertAlmostEqual(market.fee_per_share(0.94), 0.05 * 0.94 * 0.06, places=9)

    def test_fee_free_markets_carry_a_zero_rate(self) -> None:
        market = self.parse(
            raw_market(
                feesEnabled=True, feeType="zero_fees", feeSchedule={"rate": 0, "exponent": 1}
            )
        )
        assert market is not None
        self.assertEqual(market.fee_rate, 0.0)
        self.assertEqual(market.fee_per_share(0.5), 0.0)

    def test_a_market_without_a_fee_block_assumes_the_conservative_rate(self) -> None:
        market = self.parse(raw_market())
        assert market is not None
        self.assertGreater(market.fee_rate, 0.0)


class NegRiskParsingTests(unittest.TestCase):
    """A binary view cannot see a basket, so membership must survive parsing."""

    def setUp(self) -> None:
        self.scanner = MarketScanner("https://gamma.invalid", "https://clob.invalid")

    def test_negrisk_flags_and_event_id_are_captured(self) -> None:
        market = self.scanner._parse_market(
            raw_market(
                negRisk=True,
                negRiskMarketID="0xabc",
                events=[{"id": "32225", "title": "House 2026", "slug": "house-2026"}],
            )
        )
        assert market is not None
        self.assertTrue(market.neg_risk)
        self.assertEqual(market.neg_risk_id, "0xabc")
        self.assertEqual(market.event_id, "32225")

    def test_a_binary_market_is_not_negrisk(self) -> None:
        market = self.scanner._parse_market(raw_market())
        assert market is not None
        self.assertFalse(market.neg_risk)
        self.assertEqual(market.neg_risk_id, "")
        self.assertEqual(market.event_id, "")

    def test_negrisk_without_an_event_still_parses(self) -> None:
        market = self.scanner._parse_market(raw_market(negRisk=True, negRiskMarketID="0xabc"))
        assert market is not None
        self.assertTrue(market.neg_risk)
        self.assertEqual(market.event_id, "")


class FetchEventOutcomesTests(unittest.IsolatedAsyncioTestCase):
    """Basket membership comes from the event, not from /markets filters."""

    def scanner_for(self, payload):
        scanner = MarketScanner("https://gamma.invalid", "https://clob.invalid")

        async def fake_get_json(http, path, params):
            self.assertEqual(path, "/events")
            self.assertIn("id", params)
            return payload

        scanner._get_json = fake_get_json  # type: ignore[method-assign]
        return scanner

    async def test_reads_every_member_of_the_event(self) -> None:
        scanner = self.scanner_for(
            [
                {
                    "id": "32225",
                    "title": "House 2026",
                    "markets": [
                        raw_market(
                            groupItemTitle="Democrats",
                            bestAsk=0.30,
                            negRisk=True,
                            feeType="zero_fees",
                            feeSchedule={"rate": 0, "exponent": 1},
                        ),
                        raw_market(
                            groupItemTitle="Republicans",
                            bestAsk=None,
                            closed=True,
                            feeType="zero_fees",
                            feeSchedule={"rate": 0, "exponent": 1},
                        ),
                    ],
                }
            ]
        )

        title, outcomes = await scanner.fetch_event_outcomes("32225")

        self.assertEqual(title, "House 2026")
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(outcomes[0].label, "Democrats")
        self.assertAlmostEqual(outcomes[0].best_ask, 0.30)
        self.assertEqual(outcomes[0].token_yes, "token-yes")
        self.assertTrue(outcomes[0].tradable)
        self.assertIsNone(outcomes[1].best_ask)
        self.assertFalse(outcomes[1].tradable)

    async def test_unwraps_a_data_envelope(self) -> None:
        scanner = self.scanner_for(
            {"data": [{"id": "1", "title": "Event", "markets": [raw_market(bestAsk=0.4)]}]}
        )

        title, outcomes = await scanner.fetch_event_outcomes("1")

        self.assertEqual(title, "Event")
        self.assertEqual(len(outcomes), 1)

    async def test_empty_event_id_makes_no_request(self) -> None:
        scanner = self.scanner_for([{"title": "should not be used", "markets": []}])

        self.assertEqual(await scanner.fetch_event_outcomes(""), ("", []))

    async def test_a_response_for_another_event_is_refused(self) -> None:
        # The filter-ignored case: Gamma returns a list that does not contain the
        # requested id. Pricing payload[0] here would invent a basket.
        scanner = self.scanner_for(
            [{"id": "999", "title": "wrong", "markets": [raw_market(bestAsk=0.1)]}]
        )

        with self.assertLogs("scanner", level="WARNING"):
            self.assertEqual(await scanner.fetch_event_outcomes("32225"), ("", []))

    async def test_the_matching_event_is_selected_out_of_many(self) -> None:
        scanner = self.scanner_for(
            [
                {"id": "1", "title": "other", "markets": [raw_market(bestAsk=0.9)]},
                {"id": "32225", "title": "House 2026", "markets": [raw_market(bestAsk=0.4)]},
            ]
        )

        title, outcomes = await scanner.fetch_event_outcomes("32225")

        self.assertEqual(title, "House 2026")
        self.assertAlmostEqual(outcomes[0].best_ask, 0.4)

    async def test_an_unknown_event_returns_nothing(self) -> None:
        scanner = self.scanner_for([])

        self.assertEqual(await scanner.fetch_event_outcomes("999"), ("", []))

    async def test_members_without_both_tokens_are_dropped(self) -> None:
        scanner = self.scanner_for(
            [
                {
                    "id": "1",
                    "title": "E",
                    "markets": [raw_market(clobTokenIds=json.dumps(["only-one"]))],
                }
            ]
        )

        _, outcomes = await scanner.fetch_event_outcomes("1")

        self.assertEqual(outcomes, [])


class OneSidedBookTests(unittest.TestCase):
    """A market about to resolve has one side empty -- and it is still tradeable.

    Live example (Polymarket, 2026-10-27): a market closing that day quoted YES
    asks from 0.001 to 0.999 with **no bids at all**, and NO bids with no asks.
    Requiring a two-way book discarded every such market, which is exactly the
    set the snipe strategy is meant to trade.
    """

    def test_no_bids_is_still_readable(self) -> None:
        book = OrderBook.from_raw("token", [], [("0.93", 100), ("0.99", 50)])
        self.assertIsNone(book.best_bid)
        self.assertEqual(book.best_ask, 0.93)
        self.assertFalse(book.is_tradable())  # not two-way...
        self.assertTrue(book.has_liquidity)  # ...but usable

    def test_no_asks_is_still_readable(self) -> None:
        book = OrderBook.from_raw("token", [("0.93", 100)], [])
        self.assertEqual(book.best_bid, 0.93)
        self.assertIsNone(book.best_ask)
        self.assertFalse(book.is_tradable())
        self.assertTrue(book.has_liquidity)

    def test_a_completely_empty_book_is_not_usable(self) -> None:
        book = OrderBook.from_raw("token", [], [])
        self.assertFalse(book.has_liquidity)
        self.assertFalse(book.is_tradable())

    def test_levels_are_still_sorted_with_one_side(self) -> None:
        book = OrderBook.from_raw("token", [], [("0.999", 1), ("0.93", 99), ("0.9", 70)])
        self.assertEqual(book.best_ask, 0.9)
        self.assertEqual([level.price for level in book.asks], [0.9, 0.93, 0.999])


class NearLaneOrderingTests(unittest.TestCase):
    """Reserved slots must go to markets that can still be priced.

    Sorting the near lane by raw ttl put the *most* overdue market first (its
    ttl is the most negative). Those books are usually dead, so all eight
    reserved slots could be spent before a market closing in 20 minutes was
    ever fetched.
    """

    def setUp(self) -> None:
        self.now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)

    def test_imminent_beats_overdue(self) -> None:
        near = [
            market("0xdead", self.now - timedelta(hours=23)),  # most negative ttl
            market("0xsoon", self.now + timedelta(minutes=20)),
        ]
        chosen = select_markets([], near, max_total=1, near_slots=1, now=self.now)
        self.assertEqual([m.condition_id for m in chosen], ["0xsoon"])

    def test_overdue_markets_are_ordered_by_least_late(self) -> None:
        near = [
            market("0xlate", self.now - timedelta(hours=20)),
            market("0xfresh", self.now - timedelta(hours=1)),
        ]
        chosen = select_markets([], near, max_total=2, near_slots=2, now=self.now)
        self.assertEqual([m.condition_id for m in chosen], ["0xfresh", "0xlate"])

    def test_full_priority_order(self) -> None:
        near = [
            market("0xnone", None),
            market("0xoverdue", self.now - timedelta(hours=5)),
            market("0xlater", self.now + timedelta(hours=4)),
            market("0xsoon", self.now + timedelta(minutes=10)),
        ]
        chosen = select_markets([], near, max_total=4, near_slots=4, now=self.now)
        self.assertEqual(
            [m.condition_id for m in chosen],
            ["0xsoon", "0xlater", "0xoverdue", "0xnone"],
        )
