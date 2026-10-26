"""Feature-vector contract tests (the model depends on this column order)."""

from __future__ import annotations

import math
import unittest
from datetime import datetime, timedelta, timezone

import numpy as np

from features import FEATURE_COLUMNS, build_features, features_from_mapping
from scanner import Market, OrderBook


def market(end_date: datetime | None = None) -> Market:
    return Market(
        condition_id="0xcondition",
        question="Will it rain?",
        token_yes="token-yes",
        token_no="token-no",
        volume=12_000.0,
        liquidity=3_000.0,
        end_date=end_date or datetime.now(timezone.utc) + timedelta(days=5),
    )


class FeatureTests(unittest.TestCase):
    def test_columns_match_training_contract(self) -> None:
        frame = build_features(market(), OrderBook.from_raw("t", [(0.4, 10)], [(0.42, 8)]))
        self.assertEqual(list(frame.columns), FEATURE_COLUMNS)
        self.assertEqual(len(frame), 1)

    def test_values_are_finite_and_well_formed(self) -> None:
        frame = build_features(market(), OrderBook.from_raw("t", [(0.40, 30)], [(0.44, 10)]))
        row = frame.iloc[0]
        self.assertAlmostEqual(row["mid_price"], 0.42)
        self.assertAlmostEqual(row["spread"], 0.04)
        self.assertAlmostEqual(row["bid_depth_5"], 30.0)
        self.assertAlmostEqual(row["ask_depth_5"], 10.0)
        self.assertAlmostEqual(row["imbalance"], 0.5)
        self.assertAlmostEqual(row["spread_over_mid"], 0.04 / 0.42)
        self.assertAlmostEqual(row["log_volume"], math.log1p(12_000.0))
        self.assertGreater(row["ttl_days"], 0.0)
        self.assertTrue(np.isfinite(frame.to_numpy(dtype=float)).all())

    def test_one_sided_book_has_zero_spread(self) -> None:
        book = OrderBook.from_raw("t", [(0.55, 10)], [])
        row = build_features(market(), book).iloc[0]
        self.assertAlmostEqual(row["mid_price"], 0.55)
        self.assertEqual(row["spread"], 0.0)
        self.assertAlmostEqual(row["imbalance"], 1.0)

    def test_empty_book_uses_neutral_defaults(self) -> None:
        row = build_features(market(), OrderBook.from_raw("t", [], [])).iloc[0]
        self.assertAlmostEqual(row["mid_price"], 0.5)
        self.assertEqual(row["imbalance"], 0.0)

    def test_missing_end_date_yields_zero_ttl(self) -> None:
        row = build_features(
            Market(
                condition_id="0x",
                question="q",
                token_yes="a",
                token_no="b",
                volume=1.0,
                liquidity=1.0,
                end_date=None,
            ),
            OrderBook.from_raw("t", [(0.4, 1)], [(0.6, 1)]),
        ).iloc[0]
        self.assertEqual(row["ttl_days"], 0.0)

    def test_news_sentiment_is_passed_through(self) -> None:
        row = build_features(
            market(),
            OrderBook.from_raw("t", [(0.4, 1)], [(0.6, 1)]),
            news_sentiment=0.35,
        ).iloc[0]
        self.assertAlmostEqual(row["news_sentiment"], 0.35)

    def test_mapping_adapter_matches_native_path(self) -> None:
        native = build_features(market(), OrderBook.from_raw("yes", [(0.4, 5)], [(0.6, 5)]))
        mapped = features_from_mapping(
            market(),
            {
                "asset_id": "yes",
                "bids": [{"price": 0.4, "size": 5}],
                "asks": [{"price": 0.6, "size": 5}],
            },
        )
        self.assertEqual(list(native.columns), list(mapped.columns))
        self.assertTrue(np.allclose(native.to_numpy(), mapped.to_numpy()))

    def test_mapping_adapter_rejects_bad_input(self) -> None:
        with self.assertRaises(TypeError):
            features_from_mapping(market(), "not-a-book")


if __name__ == "__main__":
    unittest.main()
