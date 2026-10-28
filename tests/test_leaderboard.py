"""Auditing leaderboard rows instead of inheriting strategy from them."""

from __future__ import annotations

import unittest

from research.leaderboard import (
    ACTIVE_TRADES_PER_DAY,
    MARKET_MAKER_TRADES_PER_DAY,
    MIN_TRADES_FOR_RATE,
    Finding,
    WalletProfile,
    audit,
    audit_table,
    fee_drag,
    implied_wins,
    rebate_ceiling,
    summarise,
    win_rate_is_quantised,
)


def wallet(**overrides) -> WalletProfile:
    base = {
        "name": "wallet",
        "pnl": 1_000_000.0,
        "volume": 50_000_000.0,
        "trades": 10_000,
        "win_rate": 0.58,
        "days": 365.0,
    }
    base.update(overrides)
    return WalletProfile(**base)


def severities(findings: list[Finding]) -> list[str]:
    return [finding.severity for finding in findings]


class DerivedMetricTests(unittest.TestCase):
    def test_average_trade_is_volume_over_count(self) -> None:
        self.assertAlmostEqual(wallet(volume=1_000_000.0, trades=500).average_trade, 2000.0)

    def test_trades_per_day_is_computed_from_the_window(self) -> None:
        self.assertAlmostEqual(wallet(trades=730, days=365).trades_per_day, 2.0)

    def test_return_on_volume_is_bps_scale(self) -> None:
        self.assertAlmostEqual(wallet(pnl=500_000.0, volume=50_000_000.0).return_on_volume, 0.01)

    def test_zero_inputs_do_not_divide_by_zero(self) -> None:
        empty = wallet(volume=0.0, trades=0, days=0.0)

        self.assertEqual(empty.average_trade, 0.0)
        self.assertEqual(empty.trades_per_day, 0.0)
        self.assertEqual(empty.return_on_volume, 0.0)


class StyleClassificationTests(unittest.TestCase):
    def test_hundreds_of_trades_a_day_is_a_market_maker(self) -> None:
        self.assertEqual(wallet(trades=200_000, days=365).style, "market_maker")

    def test_a_few_dozen_trades_is_a_directional_trader(self) -> None:
        self.assertEqual(wallet(trades=45, days=365).style, "directional")

    def test_the_boundaries_are_explicit_constants(self) -> None:
        self.assertEqual(MARKET_MAKER_TRADES_PER_DAY, 100.0)
        self.assertEqual(ACTIVE_TRADES_PER_DAY, 5.0)

    def test_a_human_pace_is_not_a_market_maker(self) -> None:
        # One trade a day is a conviction bet, however large the clips.
        self.assertEqual(wallet(trades=365, days=365).style, "directional")
        self.assertEqual(wallet(trades=10 * 365, days=365).style, "active_trader")
        self.assertEqual(wallet(trades=150 * 365, days=365).style, "market_maker")

    def test_summarise_counts_the_archetypes(self) -> None:
        counts = summarise(
            [
                wallet(name="mm", trades=200_000),
                wallet(name="mm2", trades=300_000),
                wallet(name="whale", trades=20),
            ]
        )

        self.assertEqual(counts["market_maker"], 2)
        self.assertEqual(counts["directional"], 1)


class WinRateTests(unittest.TestCase):
    def test_implied_wins_is_rate_times_count(self) -> None:
        self.assertAlmostEqual(implied_wins(wallet(trades=100, win_rate=0.58)), 58.0)

    def test_a_rate_that_cannot_be_a_whole_number_of_wins_is_flagged(self) -> None:
        # 90% of 45 trades is 40.5 wins, which cannot happen.
        self.assertTrue(win_rate_is_quantised(wallet(trades=45, win_rate=0.90)))

    def test_a_consistent_rate_is_not_flagged(self) -> None:
        self.assertFalse(win_rate_is_quantised(wallet(trades=100, win_rate=0.58)))

    def test_a_small_sample_is_flagged_as_noise(self) -> None:
        findings = audit(wallet(trades=14, win_rate=0.90))

        self.assertIn("warn", severities(findings))
        messages = " ".join(finding.message for finding in findings)
        self.assertIn("noise", messages)

    def test_resolution_is_one_over_the_trade_count(self) -> None:
        self.assertAlmostEqual(wallet(trades=50).win_rate_resolution, 0.02)

    def test_the_threshold_is_documented(self) -> None:
        self.assertEqual(MIN_TRADES_FOR_RATE, 30)


class ConsistencyTests(unittest.TestCase):
    def test_pnl_above_volume_is_an_error(self) -> None:
        # The doc's Theo4B row claims $22.05M PnL on $20.1M of volume.
        findings = audit(wallet(name="Theo4B", pnl=22_050_000.0, volume=20_100_000.0, trades=45))

        self.assertEqual(severities(findings)[0], "error")
        self.assertIn("exceeds volume", findings[0].message)

    def test_a_win_rate_outside_zero_one_is_an_error(self) -> None:
        findings = audit(wallet(win_rate=1.4))

        self.assertIn("error", severities(findings))

    def test_no_trades_or_volume_is_an_error(self) -> None:
        findings = audit(wallet(trades=0, volume=0.0))
        errors = [finding for finding in findings if finding.severity == "error"]

        self.assertGreaterEqual(len(errors), 2)
        fields = {finding.field for finding in errors}
        self.assertIn("trades", fields)
        self.assertIn("volume", fields)

    def test_a_plausible_row_audits_clean(self) -> None:
        findings = audit(wallet(pnl=500_000.0, volume=50_000_000.0, trades=100_000, win_rate=0.58))
        errors = [finding for finding in findings if finding.severity == "error"]

        self.assertEqual(errors, [])


class FeeTests(unittest.TestCase):
    def test_fee_drag_matches_the_published_formula(self) -> None:
        # volume/price = shares; fee = shares * rate * p * (1-p)
        profile = wallet(volume=1_000_000.0, pnl=100_000.0)
        expected = (1_000_000.0 / 0.5) * 0.05 * 0.5 * 0.5

        self.assertAlmostEqual(fee_drag(profile, 0.05, 0.5), expected)

    def test_a_fee_free_venue_costs_nothing(self) -> None:
        self.assertEqual(fee_drag(wallet(), 0.0), 0.0)

    def test_fee_drag_is_zero_for_empty_volume(self) -> None:
        self.assertEqual(fee_drag(wallet(volume=0.0), 0.05), 0.0)

    def test_the_audit_compares_fees_to_reported_pnl(self) -> None:
        findings = audit(wallet(pnl=1_000.0, volume=50_000_000.0))
        messages = " ".join(finding.message for finding in findings)

        self.assertIn("fees", messages)

    def test_rebate_ceiling_is_below_the_fee_pool(self) -> None:
        ceiling = rebate_ceiling(50_000_000.0, 0.05, rebate_share=0.25)
        pool = fee_drag(wallet(volume=50_000_000.0), 0.05)

        self.assertLess(ceiling, pool)
        self.assertAlmostEqual(ceiling, pool * 0.25)

    def test_rebate_share_is_clamped(self) -> None:
        self.assertEqual(
            rebate_ceiling(1_000_000.0, 0.05, 9.0), rebate_ceiling(1_000_000.0, 0.05, 1.0)
        )

    def test_rebate_ceiling_is_zero_without_volume(self) -> None:
        self.assertEqual(rebate_ceiling(0.0, 0.05), 0.0)


class AuditTableTests(unittest.TestCase):
    def test_each_row_carries_its_findings(self) -> None:
        rows = audit_table([wallet(name="a"), wallet(name="b", pnl=1e9)])

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["profile"]["name"], "a")
        self.assertEqual(rows[1]["findings"][0]["severity"], "error")

    def test_an_empty_table_is_empty(self) -> None:
        self.assertEqual(audit_table([]), [])


if __name__ == "__main__":
    unittest.main()
