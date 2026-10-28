"""Bounded-price microstructure: logit diffusion and realistic order sizing.

Section 4 of the quant memo is the part worth keeping, and it is worth keeping
for one reason: **probability is not a price.** A bounded variable cannot follow
arithmetic Brownian motion, because arithmetic Brownian motion walks through zero
and through one. The fix is a change of variable:

    x = logit(p) = ln(p / (1 - p))        p = expit(x)

``x`` lives on the whole real line, has no boundary, and the mapping back is the
logistic curve. Applying Ito to that substitution gives the sensitivity

    dp/dx = p * (1 - p)

which is the whole story. At p = 0.50 a unit of belief moves the price by 0.25;
at p = 0.95 it moves it by 0.0475; at p = 0.99, by 0.0099. That single factor
explains why the same news is worth 25 cents early and a tenth of a cent late,
and why quoting a fixed cent width near certainty is not conservative -- it is
quoting a spread several standard deviations wide.

Hence the reservation price, in probability space:

    r = p - q * gamma * sigma_b^2 * [p(1-p)]^2 * (T - t)

and the optimal half-spread scaled the same way. Inventory risk genuinely
vanishes as a market resolves: there is no inventory risk in a coin that has
already landed.

The second half of this module is unglamorous and matters more. A book is a
ladder, not a price. "Buy at 0.94" is a fiction the moment the top level holds
less than the order: the fill is an average over every level consumed, and the
difference is the entire edge on a near-resolution snipe. ``walk_book`` measures
that cost rather than assuming it away.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from scanner import Level, OrderBook

# Prices live in (0, 1); logit is undefined at the endpoints.
_EPSILON = 1e-9


def logit(price: float) -> float:
    """Map a probability to log-odds. Clamped away from the endpoints."""
    if not 0.0 < price < 1.0:
        raise ValueError("logit needs a price strictly inside (0, 1)")
    p = min(1.0 - _EPSILON, max(_EPSILON, price))
    return math.log(p / (1.0 - p))


def expit(x: float) -> float:
    """Map log-odds back to a probability, without overflowing on large |x|."""
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def delta(price: float) -> float:
    """dp/dx = p(1-p): the price sensitivity to one unit of belief."""
    if not 0.0 <= price <= 1.0:
        raise ValueError("delta needs a price in [0, 1]")
    return price * (1.0 - price)


def risk_share(
    price: float,
    *,
    gamma: float = 0.1,
    belief_volatility: float = 0.05,
    horizon: float = 1.0,
    k: float = 100.0,
) -> float:
    """Fraction of the optimal width that is inventory risk rather than adverse selection.

    This is the number that deflates the memo's presentation of the model. With
    plausible inputs (``sigma_b`` = 0.05, one day of horizon) the risk term is
    ~1e-6 and the adverse-selection premium is ~2e-2, so the ratio is about
    1e-4: essentially all of the quoted width is the rent charged against being
    picked off, and almost none of it is compensation for holding inventory.

    Two consequences, both practical:

    * The ``[p(1-p)]^2`` scaling is real but it governs **skew and sensitivity**,
      not width. It is not a reason to quote a wider spread at 0.50.
    * Quoting a fixed cent width near certainty is not conservative -- the risk
      term there is ~0, so the width is pure adverse-selection rent, and a maker
      who cannot estimate that rent should not be quoting at all.
    """
    risk_term, adverse_term = spread_components(
        price,
        gamma=gamma,
        belief_volatility=belief_volatility,
        horizon=horizon,
        k=k,
    )
    total = risk_term + adverse_term
    return risk_term / total if total > 0 else 0.0


def logit_volatility(price: float, belief_volatility: float) -> float:
    """Standard deviation of the price itself: sigma_b * p(1-p)."""
    return belief_volatility * delta(price)


def reservation_price(
    price: float,
    *,
    inventory: float = 0.0,
    gamma: float = 0.1,
    belief_volatility: float = 0.05,
    horizon: float = 1.0,
) -> float:
    """Inventory-skewed fair value in probability space.

    ``inventory`` is normalised deviation in ``[-1, 1]`` (1 = fully long YES).
    The ``p(1-p)`` factor means the skew collapses as the market resolves, which
    is correct: a certain outcome carries no inventory risk.
    """
    if not 0.0 < price < 1.0:
        raise ValueError("price must be inside (0, 1)")
    if gamma < 0:
        raise ValueError("gamma must be non-negative")
    sensitivity = delta(price)
    return price - inventory * gamma * belief_volatility**2 * sensitivity**2 * horizon


def optimal_spread(
    price: float,
    *,
    gamma: float = 0.1,
    belief_volatility: float = 0.05,
    horizon: float = 1.0,
    k: float = 100.0,
) -> float:
    """Total width, scaled by the same ``p(1-p)`` uncertainty factor.

    ``k`` is the order-arrival decay per unit of log-odds. Because the state
    variable is now unbounded, ``k`` is comparable to the logit-scale parameter
    in the classical model, not to a per-cent one.
    """
    if not 0.0 < price < 1.0:
        raise ValueError("price must be inside (0, 1)")
    if k <= 0:
        raise ValueError("k must be positive")
    if gamma <= 0 or belief_volatility <= 0 or horizon <= 0:
        return 0.0
    sensitivity = delta(price)
    risk_term = gamma * belief_volatility**2 * sensitivity**2 * horizon
    adverse_term = (2.0 / gamma) * math.log(1.0 + gamma / k)
    return risk_term + adverse_term


def spread_components(
    price: float,
    *,
    gamma: float = 0.1,
    belief_volatility: float = 0.05,
    horizon: float = 1.0,
    k: float = 100.0,
) -> tuple[float, float]:
    """The two halves of the optimal width: (inventory risk, adverse selection).

    Worth separating because they behave completely differently, and the memo
    conflates them. Only the *risk* half carries the ``[p(1-p)]^2`` factor and
    collapses as the market resolves or the clock runs out. The adverse-selection
    half ``(2/gamma) * ln(1 + gamma/k)`` is a floor: it does not care how certain
    the outcome is, because it prices the chance that whoever is hitting your
    quote knows something. So a spread near certainty does **not** narrow to
    zero -- it narrows to that floor, which at the defaults is already ~2 cents.
    """
    if not 0.0 < price < 1.0:
        raise ValueError("price must be inside (0, 1)")
    if k <= 0:
        raise ValueError("k must be positive")
    if gamma <= 0:
        raise ValueError("gamma must be positive")
    risk_term = gamma * belief_volatility**2 * delta(price) ** 2 * max(0.0, horizon)
    adverse_term = (2.0 / gamma) * math.log(1.0 + gamma / k)
    return risk_term, adverse_term


@dataclass(frozen=True, slots=True)
class ProbabilityQuote:
    """A two-sided quote in probability space, clamped into the tradeable band."""

    mid: float
    bid: float
    ask: float
    reservation: float
    spread: float
    risk_component: float
    adverse_component: float

    @property
    def edge_per_share(self) -> float:
        return max(0.0, (self.ask - self.bid) / 2.0)

    @property
    def edge_bps(self) -> float:
        return self.edge_per_share / self.mid * 10_000.0 if self.mid > 0 else 0.0

    def as_dict(self) -> dict[str, float]:
        return {
            "mid": round(self.mid, 6),
            "bid": round(self.bid, 6),
            "ask": round(self.ask, 6),
            "reservation": round(self.reservation, 6),
            "spread": round(self.spread, 6),
            "risk_component": round(self.risk_component, 6),
            "adverse_component": round(self.adverse_component, 6),
            "edge_bps": round(self.edge_bps, 2),
        }


def probability_quote(
    price: float,
    *,
    inventory: float = 0.0,
    gamma: float = 0.1,
    belief_volatility: float = 0.05,
    horizon: float = 1.0,
    k: float = 100.0,
    tick: float = 0.001,
    max_width: float = 0.20,
) -> ProbabilityQuote:
    """Quote around a bounded price, with the width clamped to sanity.

    The clamp is not cosmetic. A 2-cent spread on a market trading at 0.02 is
    100% of the price; the model will happily produce that, and a maker who
    quotes it is offering to buy at zero. Near the boundaries the *absolute*
    width has to give way to the tick size or to the price itself.
    """
    if not 0.0 < price < 1.0:
        raise ValueError("price must be inside (0, 1)")
    if tick <= 0:
        raise ValueError("tick must be positive")
    risk_term, adverse_term = spread_components(
        price,
        gamma=gamma,
        belief_volatility=belief_volatility,
        horizon=horizon,
        k=k,
    )
    width = min(risk_term + adverse_term, max_width)
    reservation = reservation_price(
        price,
        inventory=inventory,
        gamma=gamma,
        belief_volatility=belief_volatility,
        horizon=horizon,
    )
    centre = reservation
    bid = round((centre - width / 2.0) / tick) * tick
    ask = round((centre + width / 2.0) / tick) * tick
    bid = min(1.0 - tick, max(tick, bid))
    ask = min(1.0 - tick, max(tick, ask))
    if bid >= ask:
        bid = max(tick, min(bid, centre - tick))
        ask = min(1.0 - tick, max(ask, centre + tick))
    return ProbabilityQuote(
        mid=price,
        bid=bid,
        ask=ask,
        reservation=reservation,
        spread=ask - bid,
        risk_component=risk_term,
        adverse_component=adverse_term,
    )


@dataclass(frozen=True, slots=True)
class FillEstimate:
    """What an order actually costs once the ladder is walked."""

    shares: float
    notional: float
    average_price: float
    worst_price: float
    levels_consumed: int
    unfilled: float

    @property
    def filled(self) -> bool:
        return self.unfilled <= 1e-9

    def slippage_vs(self, quoted_price: float) -> float:
        """Extra cost per share versus the price someone assumed they'd pay."""
        return max(0.0, self.average_price - quoted_price)

    def as_dict(self) -> dict[str, object]:
        return {
            "shares": round(self.shares, 4),
            "notional": round(self.notional, 4),
            "average_price": round(self.average_price, 6),
            "worst_price": round(self.worst_price, 6),
            "levels_consumed": self.levels_consumed,
            "unfilled": round(self.unfilled, 4),
        }


def walk_book(levels: Iterable[Level], shares: float) -> FillEstimate:
    """Average fill price for ``shares`` swept through an ask ladder.

    This is the number that decides whether a near-resolution snipe is real. At
    an ask of 0.94 against a 0.95 hold the entire edge is one cent, so a second
    level half a cent higher erases half of it -- while the quoted price still
    reads 0.94.
    """
    if shares <= 0:
        raise ValueError("shares must be positive")
    remaining = float(shares)
    notional = 0.0
    consumed = 0
    worst = 0.0
    for level in levels:
        if remaining <= 1e-12:
            break
        take = min(remaining, level.size)
        if take <= 0:
            continue
        notional += take * level.price
        remaining -= take
        consumed += 1
        worst = level.price
    filled = float(shares) - remaining
    average = notional / filled if filled > 0 else 0.0
    return FillEstimate(
        shares=filled,
        notional=notional,
        average_price=average,
        worst_price=worst,
        levels_consumed=consumed,
        unfilled=max(0.0, remaining),
    )


def max_shares_within(levels: Sequence[Level], shares: float, max_price: float) -> float:
    """How many shares can be bought without paying worse than ``max_price``.

    Used to keep an order inside the EV gate: consuming a level above the cap
    turns a profitable snipe into a losing one, so the size is trimmed instead.
    """
    if shares <= 0 or max_price <= 0:
        return 0.0
    affordable = 0.0
    for level in levels:
        if level.price > max_price:
            break
        affordable += level.size
        if affordable >= shares:
            return float(shares)
    return affordable


@dataclass(frozen=True, slots=True)
class DepthCap:
    """A size limit expressed as a share of visible top-of-book depth."""

    shares: float
    depth: float
    participation: float
    requested: float = 0.0

    @property
    def binding(self) -> bool:
        """Whether the cap actually reduced the order."""
        return self.shares < self.requested

    def as_dict(self) -> dict[str, float]:
        return {
            "shares": round(self.shares, 4),
            "top_of_book_depth": round(self.depth, 4),
            "requested": round(self.requested, 4),
        }


def cap_to_depth(
    book: OrderBook, shares: float, *, participation: float = 0.25, levels: int = 1
) -> DepthCap:
    """Trim an order so it is a fraction of what is visibly resting.

    Sending more than a small share of the book is how a paper edge becomes a
    real loss: the market moves because of the order. 25% is a deliberately
    timid default for a 0.1 CPU bot on a free host.
    """
    if not 0.0 < participation <= 1.0:
        raise ValueError("participation must be in (0, 1]")
    depth = sum(level.size for level in book.asks[:levels])
    if depth <= 0:
        return DepthCap(shares=0.0, depth=0.0, participation=participation, requested=float(shares))
    return DepthCap(
        shares=min(float(shares), depth * participation),
        depth=depth,
        participation=participation,
        requested=float(shares),
    )
