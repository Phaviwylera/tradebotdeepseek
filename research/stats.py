"""Small, dependency-free statistics for strategy evidence.

Nothing here decides whether a strategy is good; it only makes the uncertainty
visible. A measured hold rate of 0.95 from 40 markets means nothing on its own --
the interval is what the decision has to be made against.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from statistics import NormalDist

_STD_NORMAL = NormalDist()


def z_for(alpha: float) -> float:
    """One-sided normal quantile for a right-tail ``alpha``."""
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be in (0, 1)")
    return _STD_NORMAL.inv_cdf(1.0 - alpha)


def adjusted_alpha(base_alpha: float, n_trials: int) -> float:
    """Bonferroni-corrected alpha.

    Testing seven price buckets and reporting the best one is how backtests
    lie. Each bucket gets ``alpha / n_trials`` instead, so a lucky bucket cannot
    masquerade as an edge.
    """
    if not 0.0 < base_alpha < 1.0:
        raise ValueError("base_alpha must be in (0, 1)")
    if n_trials < 1:
        raise ValueError("n_trials must be >= 1")
    return base_alpha / n_trials


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    Preferred over the normal approximation because a strategy's interesting
    cases are exactly the ones with few observations and rates near 1.
    """
    if n <= 0:
        return 0.0, 1.0
    if successes < 0 or successes > n:
        raise ValueError("successes must be within [0, n]")
    p = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (p + z2 / (2.0 * n)) / denom
    margin = z * math.sqrt((p * (1.0 - p) + z2 / (4.0 * n)) / n) / denom
    return max(0.0, centre - margin), min(1.0, centre + margin)


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


@dataclass(frozen=True, slots=True)
class Bucket:
    price_low: float
    price_high: float
    n: int
    successes: int
    z: float

    @property
    def rate(self) -> float:
        return self.successes / self.n if self.n else 0.0

    @property
    def interval(self) -> tuple[float, float]:
        return wilson_interval(self.successes, self.n, self.z)


def calibration_buckets(
    samples: Iterable[tuple[float, bool]],
    edges: Sequence[float],
    *,
    z: float = 1.96,
) -> list[Bucket]:
    """Group ``(price, resolved_yes)`` pairs into half-open price buckets."""
    bounds = sorted(edges)
    counts = [0] * (len(bounds) - 1)
    wins = [0] * (len(bounds) - 1)
    for price, yes in samples:
        for index in range(len(bounds) - 1):
            if bounds[index] <= price < bounds[index + 1]:
                counts[index] += 1
                wins[index] += 1 if yes else 0
                break
    return [
        Bucket(bounds[i], bounds[i + 1], counts[i], wins[i], z)
        for i in range(len(counts))
        if counts[i]
    ]
