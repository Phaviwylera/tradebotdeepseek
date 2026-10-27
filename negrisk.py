"""NegRisk basket arbitrage: the structural edge this bot could not see.

A NegRisk event is a set of M mutually exclusive outcomes of which exactly one
resolves YES. Polymarket's own `NegRiskAdapter` enforces that identity on-chain,
which makes it an *identity*, not a forecast:

    sum over all M outcomes of P(YES_i) == 1.00

So if the M best asks can be bought for less than 1.00 -- fees included -- the
payout is certain: exactly one leg pays 1.00 and the rest pay 0. That is the only
genuinely risk-free trade available to a bot with no wallet, because it needs
nothing but market orders.

Two dangers this module exists to prevent:

1. **A partial basket looks like a huge arbitrage.** Summing three of twelve
   outcomes gives ~0.3, which reads as a 70% edge and is really just an
   incomplete view. Only *complete* baskets are scored, and completeness is
   checked explicitly.
2. **Fees are charged per leg.** M legs means M taker fees. At 0.05 with prices
   near 1/M the fees eat most of the discount, so the threshold is applied after
   fees, exactly as with the two-legged arbitrage.

What is deliberately *not* implemented: the `convertPositions()` route (buy NO
on one outcome, convert to YES on the others, sell them). It needs an on-chain
transaction, a funded wallet and a host that Polymarket does not geoblock.
Documented in `docs/QUANT_STRATEGIES.md` rather than pretended.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from fees import FeeSchedule

# A basket with a single leg is just a directional bet; a basket with hundreds of
# legs cannot be swept with this bot's order caps.
MIN_OUTCOMES = 2
MAX_OUTCOMES = 24


@dataclass(frozen=True, slots=True)
class BasketLeg:
    """One outcome of a NegRisk event, priced on its YES side."""

    token_id: str
    label: str
    ask: float
    fee_rate: float = 0.0
    fee_exponent: int = 1

    @property
    def fee(self) -> float:
        return FeeSchedule(rate=self.fee_rate, exponent=self.fee_exponent).per_share(self.ask)

    @property
    def cost(self) -> float:
        return self.ask + self.fee


@dataclass(frozen=True, slots=True)
class BasketVerdict:
    event_id: str
    title: str
    outcomes: int
    complete: bool
    cost: float
    profit: float
    margin: float
    legs: tuple[BasketLeg, ...] = field(default_factory=tuple)
    reason: str = ""

    @property
    def profitable(self) -> bool:
        return self.complete and self.profit > 0.0 and not self.reason

    def as_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "title": self.title,
            "outcomes": self.outcomes,
            "complete": self.complete,
            "cost": round(self.cost, 6),
            "profit_per_basket": round(self.profit, 6),
            "margin": round(self.margin, 6),
            "profitable": self.profitable,
            "reason": self.reason,
        }


def basket_cost(legs: Sequence[BasketLeg]) -> float:
    """Cost of one share of every outcome, taker fees included."""
    return sum(leg.cost for leg in legs)


def evaluate_basket(
    legs: Iterable[BasketLeg],
    *,
    event_id: str = "",
    title: str = "",
    complete: bool = True,
    min_profit: float = 0.005,
    max_outcomes: int = MAX_OUTCOMES,
) -> BasketVerdict:
    """Score a complete NegRisk basket.

    ``complete`` must be ``False`` whenever even one member of the event could
    not be priced: an incomplete basket is not evidence of anything.
    """
    priced = tuple(legs)
    count = len(priced)
    if count == 0:
        return BasketVerdict(event_id, title, 0, False, 0.0, 0.0, 0.0, reason="no legs")
    cost = basket_cost(priced)
    profit = 1.0 - cost
    margin = profit / cost if cost > 0 else 0.0

    reason = ""
    if not complete:
        reason = "incomplete basket: at least one outcome could not be priced"
    elif count < MIN_OUTCOMES:
        reason = f"only {count} outcome(s): not a basket"
    elif count > max_outcomes:
        reason = f"{count} outcomes is more than this bot will sweep"
    elif profit <= min_profit:
        reason = f"no edge after fees: cost {cost:.4f}, need < {1.0 - min_profit:.4f}"

    return BasketVerdict(
        event_id=event_id,
        title=title,
        outcomes=count,
        complete=complete,
        cost=cost,
        profit=profit,
        margin=margin,
        legs=priced,
        reason=reason,
    )


def best_basket(verdicts: Iterable[BasketVerdict]) -> BasketVerdict | None:
    """The most profitable complete basket in a cycle."""
    candidates = [verdict for verdict in verdicts if verdict.profitable]
    if not candidates:
        return None
    return max(candidates, key=lambda verdict: verdict.profit)


@dataclass(frozen=True, slots=True)
class BasketPlan:
    """How many baskets to buy, and what each leg would cost."""

    baskets: int
    cost_per_basket: float
    total_cost: float
    expected_profit: float
    legs: tuple[BasketLeg, ...]

    def size_for(self, max_bet: float) -> BasketPlan:
        """Clamp the plan so its total cost stays inside one order cap."""
        if self.cost_per_basket <= 0:
            return BasketPlan(0, 0.0, 0.0, 0.0, self.legs)
        baskets = max(0, int(max_bet // self.cost_per_basket))
        return BasketPlan(
            baskets=baskets,
            cost_per_basket=self.cost_per_basket,
            total_cost=baskets * self.cost_per_basket,
            expected_profit=baskets * (1.0 - self.cost_per_basket),
            legs=self.legs,
        )


def plan_basket(verdict: BasketVerdict, *, max_bet: float) -> BasketPlan:
    """Turn a profitable verdict into an executable, capped plan."""
    if not verdict.profitable:
        return BasketPlan(0, verdict.cost, 0.0, 0.0, verdict.legs)
    return BasketPlan(
        baskets=0,
        cost_per_basket=verdict.cost,
        total_cost=0.0,
        expected_profit=0.0,
        legs=verdict.legs,
    ).size_for(max_bet)
