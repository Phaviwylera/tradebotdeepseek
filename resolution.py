"""Resolution risk: the N/A settlement that silently eats near-certain trades.

A Polymarket market does not always resolve YES or NO. If the underlying event is
cancelled, ambiguous, or unresolvable under its rules, UMA settles the market
**N/A**, and every outcome token -- YES and NO alike -- redeems at exactly
$0.50. For anyone who bought at 0.94 expecting 1.00, that is a 47% loss on a
position that was "95% safe".

This is not hypothetical: Gamma publishes the fallback on every market as
``fallbackResolutionType``, and ``"equal_split"`` means precisely "resolve 50-50
if the event cannot be determined". Sports markets carry it routinely ("if the
game is canceled entirely, with no make-up game, this market will resolve
50-50").

The arithmetic is unforgiving, and it is the reason this module exists. Buying at
0.94 with a 0.95 hold assumption has a +1.06% return -- about one cent. But the
expected payout with N/A probability ``q`` is

    E[payout] = (1 - q) * hold + q * 0.50

so ``q`` is charged at ``(hold - 0.50)`` per unit, i.e. 45 cents per unit here. A
1% N/A chance costs 0.45 cents, which is 42% of the entire edge; the breakeven is
a **0.13%** N/A probability. At 5% the trade is a certain loser:

    hold=0.95  q=0.00  E=0.9500  roi=+1.06%
    hold=0.95  q=0.01  E=0.9455  roi=+0.59%   <- under a 1% gate
    hold=0.95  q=0.05  E=0.9275  roi=-1.33%

Nothing else in this repository has that leverage per unit of assumption. A gate
that models fees to five decimal places and ignores N/A is measuring the wrong
thing.
"""

from __future__ import annotations

from dataclasses import dataclass

# Both outcome tokens redeem at 0.50 when a market resolves N/A.
NA_PAYOUT = 0.50

# Gamma's `fallbackResolutionType` values that admit a 50-50 fallback. Anything
# else (notably "none") states the market will not settle at 0.50 on ambiguity.
AMBIGUOUS_FALLBACKS = frozenset({"equal_split", "equal-split", "split", "50_50", "50-50"})

# Below this a market is treated as unable to settle 0.50 on ambiguity.
SAFE_FALLBACK = "none"


def fallback_is_ambiguous(fallback_type: str | None) -> bool:
    """Whether this market's rules admit a 50-50 (N/A) settlement.

    A *missing* fallback is treated as not-ambiguous rather than ambiguous: older
    Gamma payloads simply omit the field, and refusing every one of them would
    disable the strategy on a parsing gap rather than on evidence.
    """
    if not fallback_type:
        return False
    return str(fallback_type).strip().lower() in AMBIGUOUS_FALLBACKS


def n_a_adjusted_hold(hold_probability: float, n_a_probability: float) -> float:
    """Expected payout per share bought at 1.00, given a chance of N/A.

    ``hold_probability`` is P(YES) conditional on the market resolving normally.
    N/A pays ``NA_PAYOUT`` regardless of side, so it is blended in directly.
    """
    if not 0.0 <= n_a_probability <= 1.0:
        raise ValueError("n_a_probability must be in [0, 1]")
    if not 0.0 <= hold_probability <= 1.0:
        raise ValueError("hold_probability must be in [0, 1]")
    return (1.0 - n_a_probability) * hold_probability + n_a_probability * NA_PAYOUT


def n_a_budget(hold_probability: float, ask: float, min_ev: float = 0.0) -> float:
    """Largest N/A probability that still clears ``min_ev`` at this ask.

    The inverse of :func:`n_a_adjusted_hold`, and the honest way to ask "how
    much resolution ambiguity can this trade survive?". At a 0.94 ask with a 0.95
    hold and a 1% target the answer is 0.13% -- essentially none.
    """
    if not 0.0 < ask < 1.0:
        return 0.0
    required = ask * (1.0 + min_ev)
    room = hold_probability - required
    if room <= 0.0:
        return 0.0
    per_unit = hold_probability - NA_PAYOUT
    if per_unit <= 0.0:
        # A hold at or below 0.50 cannot be hurt by N/A; it is already worse.
        return 1.0
    return min(1.0, max(0.0, room / per_unit))


@dataclass(frozen=True, slots=True)
class ResolutionRisk:
    """What a market's own rules say about settling somewhere other than 0/1."""

    fallback_type: str
    ambiguous: bool
    n_a_probability: float
    adjusted_hold: float

    @property
    def safe(self) -> bool:
        return not self.ambiguous

    def as_dict(self) -> dict[str, object]:
        return {
            "fallback_type": self.fallback_type or None,
            "ambiguous": self.ambiguous,
            "n_a_probability": round(self.n_a_probability, 6),
            "adjusted_hold": round(self.adjusted_hold, 6),
        }


def assess_resolution(
    fallback_type: str | None,
    *,
    hold_probability: float,
    n_a_probability: float = 0.02,
) -> ResolutionRisk:
    """Blend the N/A assumption into the hold probability for one market."""
    ambiguous = fallback_is_ambiguous(fallback_type)
    # A market that cannot settle 0.50 carries no N/A risk at all, whatever the
    # global assumption is. Charging it anyway would be double-counting.
    effective = n_a_probability if ambiguous else 0.0
    return ResolutionRisk(
        fallback_type=str(fallback_type or ""),
        ambiguous=ambiguous,
        n_a_probability=effective,
        adjusted_hold=n_a_adjusted_hold(hold_probability, effective),
    )
