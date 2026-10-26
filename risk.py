"""Hard, fail-closed risk controls.

Equity is cash plus the cost basis of open positions. A fill moves cash into
an open position (equity unchanged); settlement realizes the gain or loss and
can permanently trip the session kill switch.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Position:
    shares: float = 0.0
    cost_usd: float = 0.0


class RiskManager:
    def __init__(
        self,
        bankroll: float,
        max_daily_loss_pct: float = 0.20,
        max_drawdown_pct: float = 0.40,
        max_exposure_pct: float = 0.30,
        *,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        if bankroll <= 0:
            raise ValueError("bankroll must be positive")
        for name, value in (
            ("max_daily_loss_pct", max_daily_loss_pct),
            ("max_drawdown_pct", max_drawdown_pct),
            ("max_exposure_pct", max_exposure_pct),
        ):
            if not 0.0 < value <= 1.0:
                raise ValueError(f"{name} must be in (0, 1]")

        self.initial_bankroll = float(bankroll)
        self.cash = float(bankroll)
        self.max_daily_loss_pct = float(max_daily_loss_pct)
        self.max_drawdown_pct = float(max_drawdown_pct)
        self.max_exposure_pct = float(max_exposure_pct)
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))

        self.positions: dict[str, Position] = {}
        self.realized_pnl = 0.0
        self.daily_realized_pnl = 0.0
        self.peak_equity = float(bankroll)
        self.day_start_equity = float(bankroll)
        self.current_day: date = self._today()
        self.killed = False
        self.kill_reason: str | None = None

    @property
    def equity(self) -> float:
        return self.cash + sum(position.cost_usd for position in self.positions.values())

    @property
    def bankroll(self) -> float:
        """Current mark-to-cost equity (kept for a familiar public name)."""
        return self.equity

    @property
    def exposure(self) -> float:
        return sum(position.cost_usd for position in self.positions.values())

    @property
    def available_cash(self) -> float:
        return self.cash

    def can_trade(self, size_usd: float) -> tuple[bool, str]:
        self._roll_day()
        self._evaluate_kill()
        if self.killed:
            return False, f"kill switch active: {self.kill_reason}"
        if size_usd <= 0:
            return False, "size must be positive"
        if size_usd < 0.01:
            return False, "size below $0.01 minimum"
        if self.equity <= 0:
            return False, "equity is zero"
        if size_usd > self.cash + 1e-9:
            return False, "insufficient cash"
        exposure_limit = self.equity * self.max_exposure_pct
        if self.exposure + size_usd > exposure_limit + 1e-9:
            return (
                False,
                (
                    "exposure cap: "
                    f"current={self.exposure:.2f} "
                    f"add={size_usd:.2f} "
                    f"limit={exposure_limit:.2f}"
                ),
            )
        return True, "ok"

    def record_fill(
        self,
        token_id: str,
        price: float,
        size_shares: float,
        size_usd: float | None = None,
    ) -> None:
        """Reserve cash for an accepted order. Equity does not change yet."""
        if price <= 0 or size_shares <= 0:
            raise ValueError("fill price and shares must be positive")
        cost = float(size_usd) if size_usd is not None else price * size_shares
        if cost <= 0:
            raise ValueError("fill cost must be positive")
        if cost > self.cash + 1e-9:
            raise ValueError("fill would make cash negative")

        position = self.positions.setdefault(token_id, Position())
        position.shares += float(size_shares)
        position.cost_usd += cost
        self.cash -= cost
        self._roll_day()

    def record_settlement(
        self,
        token_id: str,
        winning: bool,
        *,
        payout_per_share: float = 1.0,
    ) -> float:
        """Settle a binary position and return realized PnL for that position."""
        self._roll_day()
        position = self.positions.pop(token_id, None)
        if position is None or position.shares <= 0:
            logger.warning("Settlement received for unknown token %s", token_id)
            return 0.0

        proceeds = position.shares * payout_per_share if winning else 0.0
        pnl = proceeds - position.cost_usd
        self.cash += proceeds
        self.realized_pnl += pnl
        self.daily_realized_pnl += pnl
        self.peak_equity = max(self.peak_equity, self.equity)
        self._evaluate_kill()
        return pnl

    def status(self) -> dict[str, float | bool | str | None]:
        self._roll_day()
        equity = self.equity
        daily_loss_pct = (
            max(0.0, (self.day_start_equity - equity) / self.day_start_equity)
            if self.day_start_equity > 0
            else 0.0
        )
        drawdown_pct = (
            max(0.0, (self.peak_equity - equity) / self.peak_equity)
            if self.peak_equity > 0
            else 0.0
        )
        return {
            "equity": equity,
            "cash": self.cash,
            "exposure": self.exposure,
            "exposure_pct": self.exposure / equity if equity > 0 else 0.0,
            "realized_pnl": self.realized_pnl,
            "daily_realized_pnl": self.daily_realized_pnl,
            "daily_loss_pct": daily_loss_pct,
            "drawdown_pct": drawdown_pct,
            "killed": self.killed,
            "kill_reason": self.kill_reason,
        }

    def _today(self) -> date:
        current = self._now_fn()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc).date()

    def _roll_day(self) -> None:
        today = self._today()
        if today != self.current_day:
            self.current_day = today
            self.day_start_equity = self.equity
            self.daily_realized_pnl = 0.0
            # A kill switch is intentionally permanent for the process session.
            logger.info("New UTC trading day; daily loss baseline reset")

    def _evaluate_kill(self) -> None:
        if self.killed:
            return
        equity = self.equity
        self.peak_equity = max(self.peak_equity, equity)

        if self.day_start_equity > 0:
            daily_loss = (self.day_start_equity - equity) / self.day_start_equity
            if daily_loss >= self.max_daily_loss_pct:
                self.killed = True
                self.kill_reason = (
                    f"daily loss {daily_loss:.2%} >= {self.max_daily_loss_pct:.2%}"
                )
                logger.critical("KILL SWITCH: %s", self.kill_reason)
                return

        if self.peak_equity > 0:
            drawdown = (self.peak_equity - equity) / self.peak_equity
            if drawdown >= self.max_drawdown_pct:
                self.killed = True
                self.kill_reason = (
                    f"drawdown {drawdown:.2%} >= {self.max_drawdown_pct:.2%}"
                )
                logger.critical("KILL SWITCH: %s", self.kill_reason)
