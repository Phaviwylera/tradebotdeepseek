"""Turn a pile of resolved markets into a keep-or-kill decision.

The methodology this implements is deliberately boring, and that is the point:

1. State the hypothesis before looking (e.g. "a market priced 0.93 with two
   hours left resolves YES at least 95.2% of the time, which is what the 2%
   EV gate needs").
2. Measure it on data the strategy never saw, with a confidence interval.
3. Charge the venue's real fees before declaring anything profitable.
4. Correct for the number of things tested.
5. Record the rejections too, so the same dead idea is not re-proposed next week.

A "perfect idea" cannot be produced by any of this. What it can do is stop a
bot from trading an idea that is arithmetically impossible.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from fees import arbitrage_cost, net_edge, required_hold
from research.stats import adjusted_alpha, calibration_buckets, wilson_interval, z_for

ACCEPT = "ACCEPT"
REVISE = "REVISE"
REJECT = "REJECT"
INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class Observation:
    """One resolved market, priced at a known time before resolution."""

    condition_id: str
    price: float
    resolved_yes: bool
    lead_hours: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.price <= 1.0:
            raise ValueError("price must be in [0, 1]")


@dataclass(frozen=True, slots=True)
class Verdict:
    hypothesis: str
    verdict: str
    n: int
    measured_rate: float
    ci_low: float
    ci_high: float
    required_rate: float
    avg_price: float
    fee_per_share: float
    net_roi: float
    net_roi_lower: float
    min_ev: float
    alpha_used: float
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.verdict == ACCEPT

    def as_dict(self) -> dict[str, object]:
        return {
            "hypothesis": self.hypothesis,
            "verdict": self.verdict,
            "n": self.n,
            "measured_rate": round(self.measured_rate, 6),
            "ci_low": round(self.ci_low, 6),
            "ci_high": round(self.ci_high, 6),
            "required_rate": round(self.required_rate, 6),
            "avg_price": round(self.avg_price, 6),
            "fee_per_share": round(self.fee_per_share, 6),
            "net_roi": round(self.net_roi, 6),
            "net_roi_lower": round(self.net_roi_lower, 6),
            "min_ev": self.min_ev,
            "alpha_used": self.alpha_used,
            "notes": list(self.notes),
        }

    def describe(self) -> str:
        return (
            f"{self.verdict}: {self.hypothesis}\n"
            f"  n={self.n} measured={self.measured_rate:.4f} "
            f"CI=[{self.ci_low:.4f}, {self.ci_high:.4f}] "
            f"required={self.required_rate:.4f}\n"
            f"  avg_price={self.avg_price:.4f} fee={self.fee_per_share:.5f}/share "
            f"net_roi={self.net_roi:+.4%} (pessimistic {self.net_roi_lower:+.4%}) "
            f"min_ev={self.min_ev:.2%}"
        )


def evaluate_snipe_hypothesis(
    observations: Sequence[Observation],
    *,
    price_low: float,
    price_high: float,
    fee_rate: float,
    min_ev: float = 0.02,
    assumed_hold: float = 0.95,
    min_samples: int = 30,
    base_alpha: float = 0.05,
    n_trials: int = 1,
) -> Verdict:
    """Is the SNIPE hold assumption true in this price band, after fees?

    The decision rule is the pessimistic one on purpose: the *lower* confidence
    bound must clear the required hold, not the point estimate. A strategy that
    only works at the top of its own error bar is a strategy that does not work.
    """
    if price_low >= price_high:
        raise ValueError("price_low must be below price_high")
    selected = [o for o in observations if price_low <= o.price < price_high]
    n = len(selected)
    alpha = adjusted_alpha(base_alpha, max(1, n_trials))
    z = z_for(alpha)
    hypothesis = (
        f"asks in [{price_low:.4f}, {price_high:.4f}) resolve YES often enough "
        f"to clear {min_ev:.2%} at a {fee_rate:.1%} taker fee"
    )
    if n == 0:
        return Verdict(
            hypothesis=hypothesis,
            verdict=INSUFFICIENT,
            n=0,
            measured_rate=0.0,
            ci_low=0.0,
            ci_high=1.0,
            required_rate=0.0,
            avg_price=0.0,
            fee_per_share=0.0,
            net_roi=0.0,
            net_roi_lower=0.0,
            min_ev=min_ev,
            alpha_used=alpha,
            notes=["no observations in this price band"],
        )

    successes = sum(1 for o in selected if o.resolved_yes)
    rate = successes / n
    ci_low, ci_high = wilson_interval(successes, n, z)
    avg_price = sum(o.price for o in selected) / n
    fee = fee_rate * avg_price * (1.0 - avg_price)
    requirement = required_hold(avg_price, min_ev, fee_rate)
    roi, _ = net_edge(avg_price, rate, fee_rate)
    roi_lower, _ = net_edge(avg_price, ci_low, fee_rate)

    notes: list[str] = []
    if n < min_samples:
        notes.append(f"only {n} observations (need {min_samples})")
        verdict = INSUFFICIENT
    elif ci_low > requirement:
        verdict = ACCEPT
    elif rate > requirement:
        # The point estimate clears, the pessimistic end does not: plausible, and
        # not yet proven. More data decides, not a looser threshold.
        verdict = REVISE
        notes.append(
            f"point estimate {rate:.4f} clears the {requirement:.4f} requirement but "
            f"the pessimistic {ci_low:.4f} does not: collect more samples before enabling"
        )
    else:
        verdict = REJECT
        notes.append(
            f"measured {rate:.4f} against a requirement of {requirement:.4f}: the "
            f"assumption is not supported by this data"
        )
    if assumed_hold < requirement:
        notes.append(
            f"the configured hold assumption {assumed_hold:.4f} is itself below the "
            f"required {requirement:.4f}: that setting cannot clear EV by construction"
        )
    return Verdict(
        hypothesis=hypothesis,
        verdict=verdict,
        n=n,
        measured_rate=rate,
        ci_low=ci_low,
        ci_high=ci_high,
        required_rate=requirement,
        avg_price=avg_price,
        fee_per_share=fee,
        net_roi=roi,
        net_roi_lower=roi_lower,
        min_ev=min_ev,
        alpha_used=alpha,
        notes=notes,
    )


def evaluate_arbitrage_hypothesis(
    observations: Sequence[tuple[float, float]],
    *,
    fee_rate: float,
    threshold: float = 0.985,
    min_samples: int = 30,
) -> Verdict:
    """Would a pair below ``threshold`` actually pay, once both legs are charged?

    ``observations`` are ``(yes_ask, no_ask)`` pairs seen at the same instant.
    """
    n = len(observations)
    hypothesis = (
        f"a YES+NO pair under {threshold:.3f} is still profitable after "
        f"{fee_rate:.1%} taker fees on both legs"
    )
    profitable = 0
    totals: list[float] = []
    for yes_price, no_price in observations:
        total, profit = arbitrage_cost(yes_price, no_price, fee_rate)
        totals.append(total)
        profitable += 1 if profit > 0 else 0
    rate = profitable / n if n else 0.0
    ci_low, ci_high = wilson_interval(profitable, n) if n else (0.0, 1.0)
    avg_total = sum(totals) / n if n else 0.0
    if n == 0 or n < min_samples:
        verdict = INSUFFICIENT
    elif ci_low > 0.5:
        verdict = ACCEPT
    else:
        verdict = REJECT
    return Verdict(
        hypothesis=hypothesis,
        verdict=verdict,
        n=n,
        measured_rate=rate,
        ci_low=ci_low,
        ci_high=ci_high,
        required_rate=0.5,
        avg_price=avg_total,
        fee_per_share=fee_rate * avg_total * (1.0 - min(1.0, avg_total)),
        net_roi=(1.0 - avg_total) / avg_total if avg_total else 0.0,
        net_roi_lower=0.0,
        min_ev=0.0,
        alpha_used=0.05,
        notes=[
            "an arbitrage must clear 1.000 *after* both legs are charged; a "
            "pre-fee threshold alone can call a losing pair profitable"
        ]
        if n and avg_total >= 1.0
        else [],
    )


def bucket_report(
    observations: Iterable[Observation],
    edges: Sequence[float],
    *,
    fee_rate: float,
    min_ev: float,
    n_trials: int | None = None,
    base_alpha: float = 0.05,
) -> list[dict[str, object]]:
    """Per-bucket calibration with the multiple-testing correction applied."""
    samples = [(o.price, o.resolved_yes) for o in observations]
    bounds = sorted(edges)
    trials = n_trials if n_trials is not None else max(1, len(bounds) - 1)
    z = z_for(adjusted_alpha(base_alpha, trials))
    rows: list[dict[str, object]] = []
    for bucket in calibration_buckets(samples, bounds, z=z):
        low, high = bucket.interval
        requirement = required_hold((bucket.price_low + bucket.price_high) / 2, min_ev, fee_rate)
        roi, _ = net_edge((bucket.price_low + bucket.price_high) / 2, bucket.rate, fee_rate)
        rows.append(
            {
                "band": [bucket.price_low, bucket.price_high],
                "n": bucket.n,
                "rate": round(bucket.rate, 6),
                "ci_low": round(low, 6),
                "ci_high": round(high, 6),
                "required": round(requirement, 6),
                "net_roi": round(roi, 6),
                "clears": bool(bucket.n and low > requirement),
            }
        )
    return rows
