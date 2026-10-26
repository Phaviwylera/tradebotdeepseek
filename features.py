"""Feature construction for the calibrated probability model."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from scanner import Market, OrderBook

FEATURE_COLUMNS = [
    "mid_price",
    "spread",
    "bid_depth_5",
    "ask_depth_5",
    "imbalance",
    "log_volume",
    "log_liquidity",
    "ttl_days",
    "news_sentiment",
    "spread_over_mid",
]


def build_features(
    market: Market,
    orderbook: OrderBook,
    news_sentiment: float = 0.0,
) -> pd.DataFrame:
    """Build one model row in the canonical training column order."""
    best_bid = orderbook.best_bid
    best_ask = orderbook.best_ask
    if best_bid is None and best_ask is None:
        mid_price = orderbook.last_trade_price or 0.5
        spread = 0.0
    elif best_bid is None:
        mid_price = float(best_ask)
        spread = 0.0
    elif best_ask is None:
        mid_price = float(best_bid)
        spread = 0.0
    else:
        mid_price = (best_bid + best_ask) / 2.0
        spread = max(0.0, best_ask - best_bid)

    bid_depth_5 = orderbook.depth("bid", 5)
    ask_depth_5 = orderbook.depth("ask", 5)
    total_depth = bid_depth_5 + ask_depth_5
    imbalance = (
        (bid_depth_5 - ask_depth_5) / total_depth if total_depth > 0.0 else 0.0
    )

    ttl_days = market.ttl_days(datetime.now(timezone.utc))
    if ttl_days is None:
        ttl_days = 0.0

    row = {
        "mid_price": float(mid_price),
        "spread": float(spread),
        "bid_depth_5": float(bid_depth_5),
        "ask_depth_5": float(ask_depth_5),
        "imbalance": float(imbalance),
        "log_volume": math.log1p(max(0.0, float(market.volume))),
        "log_liquidity": math.log1p(max(0.0, float(market.liquidity))),
        "ttl_days": float(ttl_days),
        "news_sentiment": float(news_sentiment),
        "spread_over_mid": float(spread / mid_price) if mid_price > 0 else 0.0,
    }

    clean = {
        key: float(value) if np.isfinite(value) else 0.0
        for key, value in row.items()
    }
    return pd.DataFrame([clean], columns=FEATURE_COLUMNS)


def features_from_mapping(
    market: Market, orderbook: Any, news_sentiment: float = 0.0
) -> pd.DataFrame:
    """Convenience adapter for callers holding raw book dictionaries."""
    if isinstance(orderbook, OrderBook):
        return build_features(market, orderbook, news_sentiment)
    if isinstance(orderbook, Mapping):
        book = OrderBook.from_raw(
            str(orderbook.get("asset_id") or market.token_yes),
            orderbook.get("bids") or [],
            orderbook.get("asks") or [],
            last_trade_price=orderbook.get("last_trade_price"),
            timestamp=orderbook.get("timestamp"),
        )
        return build_features(market, book, news_sentiment)
    raise TypeError("orderbook must be an OrderBook or a mapping")
