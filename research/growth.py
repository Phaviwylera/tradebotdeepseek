"""Compound-growth feasibility: what a target return actually requires.

Written because "turn $20 into $6,000 in 72 hours" is a real request that has a
real answer. It is not "probably hard"; it is arithmetic, and the arithmetic
says the survival probability is about 1 in 300 before costs and worse after.

No strategy here. Just the numbers that decide whether a plan is a plan or a
lottery ticket.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Below this a plan is treated as gambling regardless of how it is dressed up.
RUINOUS_SURVIVAL = 0.50
MARGINAL_SURVIVAL = 0.90


@dataclass(frozen=True, slots=True)
class GrowthRequirement:
    start: float
    target: float
    days: float

    @property
    def multiple(self) -> float:
        return self.target / self.start if self.start > 0 else float("inf")

    @property
    def total_return(self) -> float:
        return self.multiple - 1.0

    @property
    def daily_rate(self) -> float:
        if self.start <= 0 or self.days <= 0 or self.target <= self.start:
            return 0.0
        return self.multiple ** (1.0 / self.days) - 1.0

    @property
    def hourly_rate(self) -> float:
        if self.start <= 0 or self.days <= 0 or self.target <= self.start:
            return 0.0
        return self.multiple ** (1.0 / (self.days * 24.0)) - 1.0


def trades_needed(start: float, target: float, entry_price: float) -> float:
    """Consecutive all-in wins at ``entry_price`` needed to compound start→target.

    A contract bought at p pays 1/p per share on a win, so a fully reinvested win
    multiplies the bankroll by 1/p. This is the doc's ``n = ln(C_T/C_0)/ln(1/p)``.
    """
    if start <= 0 or target <= start or not 0.0 < entry_price < 1.0:
        return 0.0
    return math.log(target / start) / math.log(1.0 / entry_price)


def survival_probability(
    start: float, target: float, entry_price: float, *, win_rate: float | None = None
) -> float:
    """Probability of winning the whole chain, if the market is fairly priced.

    ``win_rate`` defaults to the price itself: a fair market offers no edge, so
    the chance of each leg is exactly what you paid for it. Any shortcut that
    claims otherwise is claiming an edge, which is a separate hypothesis that
    has to be measured.
    """
    probability = entry_price if win_rate is None else win_rate
    needed = trades_needed(start, target, entry_price)
    if needed <= 0:
        return 1.0
    if not 0.0 < probability < 1.0:
        return 0.0
    return probability**needed


def required_win_rate(start: float, target: float, entry_price: float, confidence: float) -> float:
    """Win rate the chain would need for ``confidence`` odds of surviving.

    The useful inversion: rather than asking "will it work", ask "what would
    have to be true". At a fair price the answer is exactly the price, which is
    why the plan is ruinous by construction.
    """
    needed = trades_needed(start, target, entry_price)
    if needed <= 0:
        return 1.0
    if not 0.0 < confidence <= 1.0:
        return 0.0
    return confidence ** (1.0 / needed)


def annualised_return(entry_price: float, hours: float) -> float:
    """Annualised return of buying at ``entry_price`` and settling at 1.00.

    A 5% gain banked in 2.4 hours is a (1/0.95)^3650 multiple when compounded
    over a year, which is why the number explodes and why "APY" on a short
    settlement arb is a decoration rather than a forecast. Returns ``inf`` when
    the compounding overflows a float -- that is the honest answer.
    """
    if not 0.0 < entry_price < 1.0 or hours <= 0:
        return 0.0
    periods_per_year = (365.0 * 24.0) / hours
    try:
        return entry_price ** (-periods_per_year) - 1.0
    except OverflowError:
        return float("inf")


@dataclass(frozen=True, slots=True)
class Feasibility:
    requirement: GrowthRequirement
    entry_price: float
    trades: float
    survival: float
    required_win_rate: float
    annualised: float

    @property
    def verdict(self) -> str:
        if self.survival >= MARGINAL_SURVIVAL:
            return "plausible"
        if self.survival >= RUINOUS_SURVIVAL:
            return "marginal"
        return "ruinous"

    def as_dict(self) -> dict[str, object]:
        return {
            "start": self.requirement.start,
            "target": self.requirement.target,
            "days": self.requirement.days,
            "multiple": round(self.requirement.multiple, 3),
            "total_return": round(self.requirement.total_return, 5),
            "daily_rate": round(self.requirement.daily_rate, 5),
            "hourly_rate": round(self.requirement.hourly_rate, 5),
            "entry_price": self.entry_price,
            "trades_needed": round(self.trades, 3),
            "survival": round(self.survival, 8),
            "ruin": round(1.0 - self.survival, 8),
            "required_win_rate": round(self.required_win_rate, 6),
            "annualised": round(self.annualised, 4),
            "verdict": self.verdict,
        }

    def describe(self) -> str:
        return (
            f"${self.requirement.start:,.2f} -> ${self.requirement.target:,.2f} in "
            f"{self.requirement.days:g} days is {self.requirement.multiple:,.1f}x "
            f"({self.requirement.total_return:+.0%}), i.e. "
            f"{self.requirement.daily_rate:+.2%}/day.\n"
            f"At an entry price of {self.entry_price:.4f} that needs "
            f"{self.trades:.2f} consecutive all-in wins.\n"
            f"Fair-priced survival: {self.survival:.4%} (ruin {1 - self.survival:.4%}).\n"
            f"Verdict: {self.verdict.upper()}."
        )


def feasibility(
    start: float,
    target: float,
    days: float,
    *,
    entry_price: float = 0.20,
    win_rate: float | None = None,
    confidence: float = 0.5,
) -> Feasibility:
    requirement = GrowthRequirement(start=float(start), target=float(target), days=float(days))
    return Feasibility(
        requirement=requirement,
        entry_price=float(entry_price),
        trades=trades_needed(start, target, entry_price),
        survival=survival_probability(start, target, entry_price, win_rate=win_rate),
        required_win_rate=required_win_rate(start, target, entry_price, confidence),
        annualised=annualised_return(entry_price, hours=days * 24.0),
    )
