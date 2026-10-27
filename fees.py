"""Polymarket taker fees.

A gate that ignores fees is not a gate. Polymarket moved to category-based
taker fees, and the formula is published:

    fee = shares x feeRate x price x (1 - price) ** exponent

so the cost is highest near 0.50 and shrinks toward the ends of the book. For a
near-resolution trade at 0.94 the fee is small in absolute terms (~0.28 cents a
share) but the *edge* is small too (0.95 - 0.94 = 1 cent), so the fee is ~28% of
the entire thesis. At 0.93 with a 2% target the fee takes a sixth of the edge.

That is why this module exists as its own thing, with `required_hold()` as the
inverse: instead of assuming a 0.95 hold and hoping, compute the hold rate the
assumption would have to beat, fees included, and compare.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# Gamma ships `feeSchedule: {rate, exponent, takerOnly, rebateRate}` on every
# market. Every payload observed so far uses exponent 1.
DEFAULT_FEE_EXPONENT = 1


def _clamp_price(price: float) -> float:
    return min(1.0, max(0.0, float(price)))


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """Per-share taker cost for one market."""

    rate: float = 0.0
    exponent: int = DEFAULT_FEE_EXPONENT
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.rate < 0:
            raise ValueError("fee rate must be non-negative")
        if self.exponent < 0:
            raise ValueError("fee exponent must be non-negative")

    @property
    def effective_rate(self) -> float:
        return self.rate if self.enabled else 0.0

    def per_share(self, price: float) -> float:
        """Fee for one share bought at ``price``."""
        rate = self.effective_rate
        if rate <= 0:
            return 0.0
        p = _clamp_price(price)
        return rate * p * ((1.0 - p) ** self.exponent)

    def cost(self, price: float, shares: float) -> float:
        return self.per_share(price) * max(0.0, float(shares))

    @classmethod
    def from_gamma(cls, raw: Mapping[str, Any] | None) -> FeeSchedule:
        """Read Gamma's ``feeSchedule`` block, tolerating every shape seen.

        A market with ``feesEnabled: true`` but ``rate: 0`` (``feeType:
        zero_fees``) is genuinely free; ``feesEnabled: false`` means the rate is
        not applied at all.
        """
        if not isinstance(raw, Mapping):
            return FeeSchedule()
        schedule = raw.get("feeSchedule")
        schedule = schedule if isinstance(schedule, Mapping) else {}
        rate = schedule.get("rate", 0.0)
        exponent = schedule.get("exponent", DEFAULT_FEE_EXPONENT)
        try:
            rate_value = float(rate)
        except (TypeError, ValueError):
            rate_value = 0.0
        try:
            exponent_value = int(float(exponent))
        except (TypeError, ValueError):
            exponent_value = DEFAULT_FEE_EXPONENT
        enabled = raw.get("feesEnabled")
        enabled = bool(enabled) if enabled is not None else True
        return cls(rate=max(0.0, rate_value), exponent=max(0, exponent_value), enabled=enabled)

    def describe(self) -> str:
        if self.effective_rate <= 0:
            return "fee-free"
        return f"taker fee {self.effective_rate:.0%} x p x (1-p)"


ZERO_FEE = FeeSchedule()

# When Gamma omits the fee block entirely we cannot prove the market is free, so
# assume the sports rate rather than silently counting a fee-free edge.
ASSUMED_UNKNOWN_FEE_RATE = 0.05
UNKNOWN_FEE = FeeSchedule(rate=ASSUMED_UNKNOWN_FEE_RATE)


def from_gamma(raw: Mapping[str, Any] | None) -> FeeSchedule:
    """Fee schedule for a Gamma market, defaulting to a *conservative* guess."""
    if not isinstance(raw, Mapping):
        return UNKNOWN_FEE
    if "feeSchedule" not in raw and "feesEnabled" not in raw:
        return UNKNOWN_FEE
    return FeeSchedule.from_gamma(raw)


def required_hold(price: float, min_ev: float, fee_rate: float, exponent: int = 1) -> float:
    """Hold probability needed for `(hold - price - fee) / price` to clear ``min_ev``.

    Rearranged: ``hold >= price * (1 + min_ev) + fee(price)``.

    Compare this with the assumption actually used. If the assumed hold is below
    the requirement, the trade cannot clear EV *by construction* -- no amount of
    "the market looks safe" fixes arithmetic.
    """
    p = _clamp_price(price)
    fee = FeeSchedule(rate=max(0.0, float(fee_rate)), exponent=exponent).per_share(p)
    return p * (1.0 + float(min_ev)) + fee


def net_edge(price: float, hold: float, fee_rate: float, exponent: int = 1) -> tuple[float, float]:
    """Return ``(net_roi, net_profit_per_share)`` after the taker fee."""
    p = _clamp_price(price)
    if p <= 0.0:
        return 0.0, 0.0
    fee = FeeSchedule(rate=max(0.0, float(fee_rate)), exponent=exponent).per_share(p)
    profit = float(hold) - p - fee
    return profit / p, profit


def max_price_for_hold(
    hold: float,
    min_ev: float,
    fee_rate: float,
    exponent: int = 1,
    ceiling: float = 1.0,
) -> float | None:
    """Highest price that still clears ``min_ev`` against ``hold``, fees included.

    Exponent 1 (every Gamma payload seen) has a closed form; anything else is
    bracketed numerically, because a wrong closed form is worse than a slow one.
    Returns ``None`` when even a very low price cannot clear the bar.
    """
    hold = float(hold)
    min_ev = float(min_ev)
    rate = max(0.0, float(fee_rate))
    if hold <= 0:
        return None
    if rate <= 0:
        cap = min(hold / (1.0 + min_ev), ceiling)
        return cap if cap > 0 else None

    def clears(p: float) -> bool:
        return net_edge(p, hold, rate, exponent)[0] > min_ev

    if exponent == 1:
        # rate*p^2 - (1 + min_ev + rate)*p + hold = 0
        b = 1.0 + min_ev + rate
        disc = b * b - 4.0 * rate * hold
        if disc < 0:
            return None
        cap = (b - disc**0.5) / (2.0 * rate)
    else:
        lo, hi = 0.0, 1.0
        if clears(1.0):
            cap = 1.0
        else:
            for _ in range(80):
                mid = (lo + hi) / 2.0
                if clears(mid):
                    lo = mid
                else:
                    hi = mid
            cap = lo
    if cap <= 0:
        return None
    return min(cap, ceiling)


def arbitrage_cost(
    yes_price: float, no_price: float, fee_rate: float, exponent: int = 1
) -> tuple[float, float]:
    """Return ``(total_cost, net_profit)`` for buying one YES and one NO share.

    A pair priced at 0.985 with a 5% fee near 0.49 costs
    ``0.985 + 2 * 0.0123 = 1.0096``: an "arbitrage" that loses money. The
    threshold has to be applied *after* fees, never before.
    """
    schedule = FeeSchedule(rate=max(0.0, float(fee_rate)), exponent=exponent)
    total = float(yes_price) + float(no_price)
    total += schedule.per_share(yes_price) + schedule.per_share(no_price)
    return total, 1.0 - total
