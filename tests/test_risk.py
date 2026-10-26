from __future__ import annotations

import unittest
from datetime import datetime, timezone

from risk import RiskManager


class RiskManagerKillSwitchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

    def test_daily_loss_kills_and_stays_killed(self) -> None:
        risk = RiskManager(
            1_000.0,
            max_daily_loss_pct=0.20,
            max_drawdown_pct=1.0,
            max_exposure_pct=1.0,
            now_fn=lambda: self.now,
        )
        allowed, _ = risk.can_trade(300.0)
        self.assertTrue(allowed)

        risk.record_fill("token-yes", price=0.50, size_shares=600.0, size_usd=300.0)
        pnl = risk.record_settlement("token-yes", winning=False)

        self.assertAlmostEqual(pnl, -300.0)
        self.assertTrue(risk.killed)
        self.assertIn("daily loss", risk.kill_reason or "")
        allowed, reason = risk.can_trade(1.0)
        self.assertFalse(allowed)
        self.assertIn("kill switch", reason)

        # Even after equity would recover, the session stays killed.
        risk.cash += 1_000.0
        risk.day_start_equity += 1_000.0
        allowed, _ = risk.can_trade(1.0)
        self.assertFalse(allowed)

    def test_drawdown_kill(self) -> None:
        risk = RiskManager(
            1_000.0,
            max_daily_loss_pct=1.0,
            max_drawdown_pct=0.40,
            max_exposure_pct=1.0,
            now_fn=lambda: self.now,
        )
        risk.record_fill("token", price=1.0, size_shares=400.0, size_usd=400.0)
        risk.record_settlement("token", winning=False)

        self.assertTrue(risk.killed)
        self.assertIn("drawdown", risk.kill_reason or "")

    def test_exposure_cap(self) -> None:
        risk = RiskManager(
            1_000.0,
            max_daily_loss_pct=0.20,
            max_drawdown_pct=0.40,
            max_exposure_pct=0.30,
            now_fn=lambda: self.now,
        )
        allowed, reason = risk.can_trade(301.0)
        self.assertFalse(allowed)
        self.assertIn("exposure cap", reason)

        risk.record_fill("token", price=0.50, size_shares=600.0, size_usd=300.0)
        allowed, _ = risk.can_trade(1.0)
        self.assertFalse(allowed)
        self.assertAlmostEqual(risk.exposure, 300.0)


if __name__ == "__main__":
    unittest.main()
