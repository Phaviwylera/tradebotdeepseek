"""Avellaneda-Stoikov quoting for Polymarket's CLOB.

The structural argument for market making on this venue is stronger than for
taking: **makers pay no protocol fee at all**, and a slice of every taker fee is
redistributed to whoever was resting near the top of the book. So the same
market that costs a taker 0.0028 a share on a 5% sports market costs a maker
nothing and can earn a rebate.

The quote is the classic stochastic-control one:

    reservation price   r = S - q * gamma * sigma^2 * (T - t)
    optimal spread      d* = gamma * sigma^2 * (T - t) + (2/gamma) * ln(1 + gamma/k)
    bid                 S - d*/2 + (S - r)/2
    ask                 S + d*/2 + (S - r)/2

``q`` is inventory in shares (positive = long YES), ``gamma`` is risk aversion,
``sigma`` is price volatility per unit time, ``(T - t)`` is the remaining
horizon in the same unit, and ``k`` is how fast order arrival decays with
distance from the mid.

This module only *computes* quotes. It does not place them, and that is a
deliberate limit: resting orders need L2 API credentials, a funded wallet and a
host Polymarket does not geoblock. What it does give you is the number that
matters -- how much spread a maker could capture on a given market -- so the
idea can be measured before anyone risks money on it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

MIN_TICK = 0.001
MAX_TICK = 0.01

# ``k`` is order-arrival decay *per unit of price*, and prices here live in
# (0, 1). The classic FX/equity defaults (k ~ 1.5) assume a completely different
# price scale: fed 0.62 they produce a quote of 0.001 / 0.999, i.e. the whole
# book. For a market around 0.60 the adverse term is about 2/k, so k = 100 gives
# a two-cent spread, which is the right order of magnitude on this venue.
DEFAULT_K = 100.0
DEFAULT_GAMMA = 0.1
DEFAULT_SIGMA = 0.02
MAX_QUOTE_WIDTH = 0.20


@dataclass(frozen=True, slots=True)
class Quote:
    """One two-sided quote around a mid price."""

    mid: float
    bid: float
    ask: float
    inventory: float
    reservation: float
    spread: float

    @property
    def edge_per_share(self) -> float:
        """Half-spread captured on a round trip that fills both sides."""
        return max(0.0, (self.ask - self.bid) / 2.0)

    @property
    def edge_bps(self) -> float:
        return self.edge_per_share / self.mid * 10_000.0 if self.mid > 0 else 0.0

    def as_dict(self) -> dict[str, float]:
        return {
            "mid": round(self.mid, 6),
            "bid": round(self.bid, 6),
            "ask": round(self.ask, 6),
            "inventory": round(self.inventory, 4),
            "reservation": round(self.reservation, 6),
            "spread": round(self.spread, 6),
            "edge_bps": round(self.edge_bps, 2),
        }


def _round_to_tick(price: float, tick: float = MIN_TICK) -> float:
    if tick <= 0:
        return price
    steps = round(price / tick)
    return min(0.999, max(0.001, steps * tick))


def reservation_price(
    mid: float, inventory: float, gamma: float, sigma: float, horizon: float
) -> float:
    """Inventory-skewed fair price: long inventory pulls the quote down."""
    if mid <= 0:
        return 0.0
    if gamma <= 0 or sigma <= 0 or horizon <= 0:
        return mid
    return mid - inventory * gamma * sigma * sigma * horizon


def optimal_spread(gamma: float, sigma: float, horizon: float, k: float) -> float:
    """Total bid-ask width from the HJB solution, in price units."""
    if sigma <= 0 or horizon <= 0:
        return 0.0
    if gamma <= 0:
        return 0.0
    if k <= 0:
        raise ValueError("k must be positive: it is the order-arrival decay")
    risk_term = gamma * sigma * sigma * horizon
    adverse_term = (2.0 / gamma) * math.log(1.0 + gamma / k)
    return risk_term + adverse_term


def quote(
    mid: float,
    *,
    inventory: float = 0.0,
    gamma: float = DEFAULT_GAMMA,
    sigma: float = DEFAULT_SIGMA,
    horizon: float = 1.0,
    k: float = DEFAULT_K,
    tick: float = MIN_TICK,
    max_width: float = MAX_QUOTE_WIDTH,
) -> Quote:
    """Two-sided quote around ``mid``, clamped into the tradeable range."""
    if not 0.0 < mid < 1.0:
        raise ValueError("mid must be inside (0, 1)")
    if tick <= 0:
        raise ValueError("tick must be positive")
    width = min(optimal_spread(gamma, sigma, horizon, k), max_width)
    reservation = reservation_price(mid, inventory, gamma, sigma, horizon)
    # Both quotes are centred on the reservation price: p_bid = r - d/2 and
    # p_ask = r + d/2. Long inventory lowers r, so both sides move down.
    skew = reservation - mid
    bid = _round_to_tick(mid - width / 2.0 + skew, tick)
    ask = _round_to_tick(mid + width / 2.0 + skew, tick)
    if bid >= ask:
        # Never emit a crossed quote: widen to one tick either side.
        bid = _round_to_tick(max(0.001, mid - tick), tick)
        ask = _round_to_tick(min(0.999, mid + tick), tick)
    return Quote(
        mid=float(mid),
        bid=bid,
        ask=ask,
        inventory=float(inventory),
        reservation=reservation,
        spread=ask - bid,
    )


def rebate_value(
    traded_notional: float, taker_fee_rate: float, rebate_share: float = 0.25
) -> float:
    """Rough rebate earned by a maker on ``traded_notional`` of taker volume.

    The real allocation is time-weighted depth near the mid, so this is an upper
    bound on a single market rather than a forecast. It exists to keep the idea
    in proportion: rebates are a bonus on top of the spread, never the reason.
    """
    if traded_notional <= 0 or taker_fee_rate <= 0:
        return 0.0
    fees_collected = traded_notional * taker_fee_rate * 0.25  # p*(1-p) peaks at 0.25
    return fees_collected * max(0.0, min(1.0, rebate_share))
