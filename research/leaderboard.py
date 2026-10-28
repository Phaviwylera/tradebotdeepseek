"""Audit leaderboard wallet statistics instead of quoting them.

Section 3 of the quant memo lists top traders with PnL, volume, trade count and
win rate, then draws conclusions about strategy from them. Some of those numbers
are internally impossible, and a stat that cannot be true is worse than no stat:
it is a strategy argument resting on arithmetic that does not close.

The checks here are exact identities, not opinions:

* **Average trade size.** ``volume / trades``. A wallet with $385M across 220k
  trades averaged $1,750 a clip. That is not directional conviction; it is a
  spread harvester, and the label should follow the arithmetic.
* **Win rate as a count.** ``win_rate * trades`` must be a near-integer. A rate
  quoted to two decimals over 14 trades is quantised to ~7% steps, so "~90%" over
  14 trades is 13 of 14 at best and could be 12.
* **PnL versus taker fees.** A wallet that pays the taker fee on all of that
  volume and still nets the reported PnL is claiming to beat a number that can
  be computed. This is what separates "market maker earning rebates" from
  "directional trader who happened to be right".
* **Trades per active day.** 220,000 trades is a machine. Calling it a "whale"
  and copying its direction is a category error.

The point is not to debunk any particular trader. It is to refuse to inherit a
strategy from a table nobody checked.
"""

from __future__ import annotations

from dataclasses import dataclass

# Below this many trades a win rate has no meaningful resolution.
MIN_TRADES_FOR_RATE = 30

# Trades per day at which the label changes. 100 is the line where a human stops
# being able to do it by hand; 5 separates a busy trader from a conviction bet.
MARKET_MAKER_TRADES_PER_DAY = 100.0
ACTIVE_TRADES_PER_DAY = 5.0


@dataclass(frozen=True, slots=True)
class WalletProfile:
    """One row of a leaderboard, plus what it implies about execution style."""

    name: str
    pnl: float
    volume: float
    trades: int
    win_rate: float | None = None
    days: float = 1.0

    @property
    def average_trade(self) -> float:
        return self.volume / self.trades if self.trades > 0 else 0.0

    @property
    def trades_per_day(self) -> float:
        return self.trades / self.days if self.days > 0 else 0.0

    @property
    def return_on_volume(self) -> float:
        return self.pnl / self.volume if self.volume > 0 else 0.0

    @property
    def style(self) -> str:
        """Classify from trade frequency, which is far harder to fake than a label."""
        per_day = self.trades_per_day
        if per_day >= MARKET_MAKER_TRADES_PER_DAY:
            return "market_maker"
        if per_day >= ACTIVE_TRADES_PER_DAY:
            return "active_trader"
        return "directional"

    @property
    def win_rate_resolution(self) -> float:
        """The step size between adjacent win rates at this sample size."""
        return 1.0 / self.trades if self.trades > 0 else 1.0

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "pnl": self.pnl,
            "volume": self.volume,
            "trades": self.trades,
            "average_trade": round(self.average_trade, 2),
            "trades_per_day": round(self.trades_per_day, 2),
            "return_on_volume_bps": round(self.return_on_volume * 10_000, 2),
            "style": self.style,
            "win_rate": self.win_rate,
            "win_rate_step": round(self.win_rate_resolution, 4),
        }


@dataclass(frozen=True, slots=True)
class Finding:
    """One reason to distrust a quoted stat."""

    field: str
    severity: str  # "error" | "warn" | "info"
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"field": self.field, "severity": self.severity, "message": self.message}


def implied_wins(profile: WalletProfile) -> float | None:
    """Number of winning trades implied by the quoted win rate."""
    if profile.win_rate is None or profile.trades <= 0:
        return None
    return profile.win_rate * profile.trades


def win_rate_is_quantised(profile: WalletProfile, tolerance: float = 0.05) -> bool:
    """True when the quoted rate is inconsistent with any whole number of wins."""
    wins = implied_wins(profile)
    if wins is None:
        return False
    return abs(wins - round(wins)) > tolerance


def fee_drag(profile: WalletProfile, taker_fee_rate: float, average_price: float = 0.5) -> float:
    """Taker fees paid if every trade crossed the spread at ``average_price``.

    ``fee = shares * rate * p * (1-p)``, and ``volume`` already includes the
    price, so shares is ``volume / price``. This is the floor a directional
    taker must beat; a maker pays none of it.
    """
    if profile.volume <= 0 or not 0.0 < average_price <= 1.0:
        return 0.0
    shares = profile.volume / average_price
    return shares * taker_fee_rate * average_price * (1.0 - average_price)


def audit(
    profile: WalletProfile,
    *,
    taker_fee_rate: float = 0.05,
    average_price: float = 0.5,
) -> list[Finding]:
    """Everything that does not close in one wallet's published row."""
    findings: list[Finding] = []

    if profile.trades <= 0:
        findings.append(Finding("trades", "error", "no trade count: nothing can be checked"))
    if profile.volume <= 0:
        findings.append(Finding("volume", "error", "no volume: PnL cannot be put in context"))
    if profile.pnl > profile.volume:
        findings.append(
            Finding(
                "pnl",
                "error",
                f"PnL ({profile.pnl:,.0f}) exceeds volume ({profile.volume:,.0f})",
            )
        )

    if profile.win_rate is not None and not 0.0 <= profile.win_rate <= 1.0:
        findings.append(Finding("win_rate", "error", "win rate is not a probability"))

    if profile.trades < MIN_TRADES_FOR_RATE and profile.win_rate is not None:
        findings.append(
            Finding(
                "win_rate",
                "warn",
                f"{profile.trades} trades give a rate resolution of "
                f"{profile.win_rate_resolution:.1%}: the quoted figure is noise",
            )
        )

    if win_rate_is_quantised(profile):
        wins = implied_wins(profile)
        assert wins is not None
        findings.append(
            Finding(
                "win_rate",
                "warn",
                f"{profile.win_rate:.2%} over {profile.trades} trades implies "
                f"{wins:.2f} wins, but wins are integers",
            )
        )

    if profile.style == "market_maker" and profile.win_rate is not None:
        findings.append(
            Finding(
                "style",
                "info",
                f"{profile.trades_per_day:,.0f} trades/day: this is a maker earning the "
                "spread, so a win rate is a poor description of its edge",
            )
        )

    fees = fee_drag(profile, taker_fee_rate, average_price)
    if fees > 0 and profile.pnl > 0:
        share = fees / profile.pnl
        relative = f"{share:.1%} of the reported PnL" if share < 1 else f"{share:.1f}x the PnL"
        findings.append(
            Finding(
                "fees",
                "info",
                f"a taker on this volume would have paid ~${fees:,.0f} in fees "
                f"({relative}), so this wallet is either a maker or it is not "
                "paying the headline rate",
            )
        )

    return findings


def audit_table(
    profiles: list[WalletProfile],
    *,
    taker_fee_rate: float = 0.05,
    average_price: float = 0.5,
) -> list[dict[str, object]]:
    """Per-wallet summary plus findings, for printing or JSON."""
    rows = []
    for profile in profiles:
        rows.append(
            {
                "profile": profile.as_dict(),
                "findings": [
                    finding.as_dict()
                    for finding in audit(
                        profile, taker_fee_rate=taker_fee_rate, average_price=average_price
                    )
                ],
            }
        )
    return rows


def summarise(profiles: list[WalletProfile]) -> dict[str, int]:
    """How many of a leaderboard's wallets are actually market makers."""
    counts: dict[str, int] = {}
    for profile in profiles:
        counts[profile.style] = counts.get(profile.style, 0) + 1
    return counts


def rebate_ceiling(
    volume: float, taker_fee_rate: float, rebate_share: float = 0.25, average_price: float = 0.5
) -> float:
    """Upper bound on maker rebates a wallet could earn on a given volume."""
    if volume <= 0 or not 0.0 < average_price <= 1.0:
        return 0.0
    shares = volume / average_price
    collected = shares * taker_fee_rate * average_price * (1.0 - average_price)
    return collected * max(0.0, min(1.0, rebate_share))
