"""The three independent, auditable edge strategies."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from scanner import Market, OrderBook

ARBITRAGE_THRESHOLD = 0.985
SNIPE_MAX_TTL_DAYS = 0.10  # 2.4 hours
SNIPE_MIN_ASK = 0.93
SNIPE_MAX_ASK = 0.97
SNIPE_HOLD_PROBABILITY = 0.95
CONFIDENCE_FLOOR = 0.60


@dataclass(frozen=True, slots=True)
class Signal:
    market_id: str
    token_id: str
    side: str
    price: float
    size_usd: float
    reason: str
    expected_value: float


class StrategyEngine:
    def __init__(self, min_ev: float = 0.02, max_bet: float = 20.0) -> None:
        if min_ev < 0:
            raise ValueError("min_ev must be non-negative")
        if max_bet <= 0:
            raise ValueError("max_bet must be positive")
        self.min_ev = float(min_ev)
        self.max_bet = float(max_bet)

    def check_arbitrage(
        self, market: Market, yes_book: OrderBook, no_book: OrderBook
    ) -> list[Signal]:
        """Buy both outcome ask sides when a complete set costs < $0.985.

        Both legs use the same share count so the pair is hedged. The pair's
        total cost is capped at ``max_bet`` (not ``2 * max_bet``).
        """
        yes_ask = yes_book.best_ask
        no_ask = no_book.best_ask
        if yes_ask is None or no_ask is None:
            return []

        total = yes_ask + no_ask
        if total <= 0.0 or total >= ARBITRAGE_THRESHOLD:
            return []

        shares = math.floor((self.max_bet / total) * 100.0) / 100.0
        if shares <= 0.0:
            return []

        yes_cost = round(shares * yes_ask, 2)
        no_cost = round(shares * no_ask, 2)
        if yes_cost <= 0.0 or no_cost <= 0.0:
            return []

        pair_cost = yes_cost + no_cost
        pair_profit = shares * (1.0 - total)
        yes_share_of_cost = yes_cost / pair_cost if pair_cost > 0 else 0.5
        reason = f"ARB YES+NO={total:.4f} " f"profit_per_pair={1.0 - total:.4f} shares={shares:.2f}"

        return [
            Signal(
                market_id=market.condition_id,
                token_id=market.token_yes,
                side="BUY",
                price=float(yes_ask),
                size_usd=yes_cost,
                reason=reason,
                expected_value=pair_profit * yes_share_of_cost,
            ),
            Signal(
                market_id=market.condition_id,
                token_id=market.token_no,
                side="BUY",
                price=float(no_ask),
                size_usd=no_cost,
                reason=reason,
                expected_value=pair_profit * (1.0 - yes_share_of_cost),
            ),
        ]

    def check_snipe(
        self,
        market: Market,
        yes_book: OrderBook,
        no_book: OrderBook,
        now: datetime | None = None,
    ) -> list[Signal]:
        """Buy a very likely winner only in the final 2.4 hours.

        EV is return on capital: ``(hold_probability - ask) / ask``. The
        threshold is deliberately stricter than raw edge because a 95% hold
        assumption is a model, not a fact, and resolution can be disputed.
        """
        current = now or datetime.now(timezone.utc)
        ttl_days = market.ttl_days(current)
        if ttl_days is None or ttl_days < 0.0 or ttl_days >= SNIPE_MAX_TTL_DAYS:
            return []

        candidates: list[Signal] = []
        for label, token_id, book in (
            ("YES", market.token_yes, yes_book),
            ("NO", market.token_no, no_book),
        ):
            ask = book.best_ask
            if ask is None or not (SNIPE_MIN_ASK <= ask <= SNIPE_MAX_ASK):
                continue
            ev_roi = (SNIPE_HOLD_PROBABILITY - ask) / ask
            if ev_roi <= self.min_ev:
                continue
            size_usd = self.max_bet
            candidates.append(
                Signal(
                    market_id=market.condition_id,
                    token_id=token_id,
                    side="BUY",
                    price=float(ask),
                    size_usd=size_usd,
                    reason=(
                        f"SNIPE {label} ttl_h={ttl_days * 24.0:.2f} "
                        f"ask={ask:.4f} hold={SNIPE_HOLD_PROBABILITY:.2f} "
                        f"ev_roi={ev_roi:.4f}"
                    ),
                    expected_value=size_usd * ev_roi,
                )
            )

        if not candidates:
            return []
        return [max(candidates, key=lambda signal: signal.expected_value)]

    def check_model_edge(
        self,
        market: Market,
        yes_book: OrderBook,
        no_book: OrderBook,
        anchored_prob: float,
        bankroll: float,
        confidence: float | None = None,
    ) -> Signal | None:
        """Emit a directional signal only when calibrated P(YES) beats the ask.

        Buying YES at ``yes_ask`` is compared with ``anchored_prob``. Buying NO
        at ``no_ask`` is compared with ``1 - anchored_prob``. Quarter-Kelly
        sizes the bet and ``max_bet`` is a hard cap.
        """
        if bankroll <= 0.0:
            return None

        anchored_prob = min(1.0, max(0.0, float(anchored_prob)))
        candidates: list[tuple[float, Signal]] = []

        for label, token_id, book, probability in (
            ("YES", market.token_yes, yes_book, anchored_prob),
            ("NO", market.token_no, no_book, 1.0 - anchored_prob),
        ):
            price = book.best_ask
            if price is None or not 0.0 < price < 1.0:
                continue
            edge = probability - price
            if edge < self.min_ev:
                continue

            side_confidence = probability if confidence is None else confidence
            if side_confidence < CONFIDENCE_FLOOR:
                continue

            kelly_full = edge / (1.0 - price)
            if kelly_full <= 0.0:
                continue
            size_usd = min(self.max_bet, bankroll * 0.25 * kelly_full)
            if size_usd < 0.01:
                continue

            ev_usd = size_usd * edge / price
            signal = Signal(
                market_id=market.condition_id,
                token_id=token_id,
                side="BUY",
                price=float(price),
                size_usd=round(size_usd, 2),
                reason=(
                    f"MODEL {label} edge={edge:.4f} p={probability:.4f} "
                    f"ask={price:.4f} conf={side_confidence:.3f}"
                ),
                expected_value=ev_usd,
            )
            candidates.append((abs(edge), signal))

        if not candidates:
            return None
        return max(candidates, key=lambda item: item[0])[1]
