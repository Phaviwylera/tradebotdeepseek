"""The three independent, auditable edge strategies."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from fees import arbitrage_cost, max_price_for_hold, net_edge
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
    def __init__(
        self,
        min_ev: float = 0.02,
        max_bet: float = 20.0,
        snipe_overdue_hours: float = 24.0,
        *,
        arb_threshold: float = ARBITRAGE_THRESHOLD,
        snipe_min_ask: float = SNIPE_MIN_ASK,
        snipe_max_ask: float = SNIPE_MAX_ASK,
        snipe_hold_probability: float = SNIPE_HOLD_PROBABILITY,
        snipe_max_ttl_days: float = SNIPE_MAX_TTL_DAYS,
        measured_hold: float | None = None,
        measured_hold_source: str = "",
        kelly_fraction: float = 0.25,
    ) -> None:
        if min_ev < 0:
            raise ValueError("min_ev must be non-negative")
        if max_bet <= 0:
            raise ValueError("max_bet must be positive")
        if snipe_overdue_hours < 0:
            raise ValueError("snipe_overdue_hours must be non-negative")
        if not 0.0 < snipe_min_ask < snipe_max_ask <= 1.0:
            raise ValueError("need 0 < snipe_min_ask < snipe_max_ask <= 1")
        if not 0.0 < snipe_hold_probability <= 1.0:
            raise ValueError("snipe_hold_probability must be in (0, 1]")
        if snipe_max_ttl_days <= 0:
            raise ValueError("snipe_max_ttl_days must be positive")
        self.min_ev = float(min_ev)
        self.max_bet = float(max_bet)
        # Every gate is configurable so widening the band is a deliberate,
        # visible decision in the environment, not an edit to constants.
        self.arb_threshold = float(arb_threshold)
        self.snipe_min_ask = float(snipe_min_ask)
        self._snipe_max_ask_setting = float(snipe_max_ask)
        self.snipe_hold_probability = float(snipe_hold_probability)
        # Measured evidence may only make the assumption *more* conservative.
        self.measured_hold = None if measured_hold is None else float(measured_hold)
        if self.measured_hold is not None and not 0.0 < self.measured_hold <= 1.0:
            raise ValueError("measured_hold must be in (0, 1]")
        self.measured_hold_source = measured_hold_source
        # Fractional Kelly: full Kelly maximises growth *and* drawdown, and the
        # probability feeding it here is a model output, not a fact.
        if not 0.0 < kelly_fraction <= 1.0:
            raise ValueError("kelly_fraction must be in (0, 1]")
        self.kelly_fraction = float(kelly_fraction)
        self.snipe_max_ttl_days = float(snipe_max_ttl_days)
        # Gamma's endDate is the scheduled close. For a sports market that is the
        # kick-off, so a match that is still being played has a *negative* ttl —
        # exactly the window where a 0.93 ask means "almost certainly resolves
        # YES". Rejecting ttl < 0 hid the whole in-play case.
        self.snipe_overdue_hours = float(snipe_overdue_hours)

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
        if total <= 0.0 or total >= self.arb_threshold:
            return []
        # That check is pre-fee arithmetic. On a fee-bearing market a 0.985 pair
        # near 0.49 costs 0.985 + 2 * 0.0123 = 1.0096 -- a guaranteed loss -- so
        # the pair is only taken when it clears 1.000 *after* both legs are
        # charged, and still clears the configured margin.
        cost, profit = arbitrage_cost(yes_ask, no_ask, market.fee_rate, market.fee_exponent)
        if profit <= 0.0 or cost >= self.arb_threshold:
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

    @property
    def effective_hold(self) -> float:
        """The hold probability the EV gate uses: measured if available."""
        if self.measured_hold is not None:
            return min(self.measured_hold, self.snipe_hold_probability)
        return self.snipe_hold_probability

    def snipe_max_ask(self, fee_rate: float = 0.0) -> float:
        """Highest ask that clears ``MIN_EV`` against the hold, fees included.

        ``SNIPE_MAX_ASK`` is the band the strategy is willing to look at; the EV
        gate is what actually binds. Solving ``(hold - ask - fee) / ask > min_ev``
        gives a *fee-dependent* cap: with a 5% sports fee and a 2% target the cap
        is 0.9281, which is **below** the old 0.93 floor -- i.e. the default band
        was empty once fees were counted.
        """
        cap = max_price_for_hold(
            self.effective_hold,
            self.min_ev,
            fee_rate,
            ceiling=self._snipe_max_ask_setting,
        )
        return cap if cap is not None else 0.0

    def snipe_band(self, fee_rate: float = 0.0) -> tuple[float, float]:
        """Half-open ask range the snipe strategy can actually trade."""
        return max(self.snipe_min_ask, 0.0), self.snipe_max_ask(fee_rate)

    def describe_gates(self) -> str:
        """One line a log can print so "why is it quiet?" is answerable."""
        low = max(self.snipe_min_ask, 0.0)
        high = self.snipe_max_ask()
        strict_high = self.snipe_max_ask(0.05)
        return (
            f"ARB fires when yes_ask + no_ask < {self.arb_threshold:.3f}; "
            f"SNIPE fires when ttl_days < {self.snipe_max_ttl_days:.2f} (or up to "
            f"{self.snipe_overdue_hours:.0f}h past the scheduled close) and the best ask "
            f"is in [{low:.4f}, {high:.4f}) fee-free, or [{low:.4f}, {strict_high:.4f}) "
            f"with a 5% taker fee, at hold={self.effective_hold:.4f} "
            f"min_ev={self.min_ev:.4f}"
            + (
                f" (measured: {self.measured_hold_source})"
                if self.measured_hold and self.measured_hold_source
                else " (hold is assumed, not measured)"
            )
            + ("" if high > low else " (this band is empty: raise SNIPE hold or lower MIN_EV)")
        )

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
        if ttl_days is None or ttl_days >= self.snipe_max_ttl_days:
            return []
        # Past the scheduled close but not yet resolved (in-play, or awaiting
        # settlement): still inside the thesis, up to a bounded lateness so a
        # permanently stuck market cannot be bought forever.
        overdue_floor = -self.snipe_overdue_hours / 24.0
        if ttl_days < overdue_floor:
            return []

        candidates: list[Signal] = []
        for label, token_id, book in (
            ("YES", market.token_yes, yes_book),
            ("NO", market.token_no, no_book),
        ):
            ask = book.best_ask
            # One-sided books are normal near resolution, so `ask` is often
            # None here: check it before touching it.
            if ask is None or not (self.snipe_min_ask <= ask < self.snipe_max_ask(market.fee_rate)):
                continue
            fee = market.fee_per_share(float(ask))
            ev_roi, _ = net_edge(
                float(ask), self.effective_hold, market.fee_rate, market.fee_exponent
            )
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
                        f"ask={ask:.4f} hold={self.effective_hold:.4f} "
                        f"fee={fee:.4f} "
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
            size_usd = min(self.max_bet, bankroll * self.kelly_fraction * kelly_full)
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
