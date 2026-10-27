"""Configuration parsing tests: defaults, validation, secret handling."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from config import Settings


def build(**env: str) -> Settings:
    with mock.patch.dict(os.environ, env, clear=True):
        return Settings.from_env()


class SettingsTests(unittest.TestCase):
    def test_dry_run_is_the_default(self) -> None:
        settings = build()
        self.assertFalse(settings.live)
        self.assertEqual(settings.bankroll, 200.0)
        self.assertEqual(settings.max_bet, 20.0)
        self.assertEqual(settings.scan_interval_seconds, 60)
        self.assertTrue(settings.web_enabled)
        self.assertTrue(settings.serve_dashboard)

    def test_live_flag_accepts_common_truthy_spellings(self) -> None:
        for value in ("1", "true", "TRUE", "yes", "on"):
            self.assertTrue(build(LIVE=value).live, value)
        for value in ("0", "false", "no", "off", ""):
            self.assertFalse(build(LIVE=value).live, value)

    def test_invalid_boolean_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build(LIVE="maybe")

    def test_max_bet_cannot_exceed_bankroll(self) -> None:
        with self.assertRaises(ValueError):
            build(BANKROLL="10", MAX_BET="20")

    def test_risk_percentages_must_be_in_range(self) -> None:
        for name in ("MAX_DAILY_LOSS_PCT", "MAX_DRAWDOWN_PCT", "MAX_EXPOSURE_PCT"):
            with self.assertRaises(ValueError):
                build(**{name: "0"})
            with self.assertRaises(ValueError):
                build(**{name: "1.5"})

    def test_signature_type_is_validated(self) -> None:
        self.assertEqual(build(POLYMARKET_SIGNATURE_TYPE="2").signature_type, 2)
        with self.assertRaises(ValueError):
            build(POLYMARKET_SIGNATURE_TYPE="3")

    def test_scan_interval_has_a_floor(self) -> None:
        with self.assertRaises(ValueError):
            build(SCAN_INTERVAL_SECONDS="1")

    def test_public_summary_never_leaks_the_private_key(self) -> None:
        settings = build(
            POLYGON_PRIVATE_KEY="0xdeadbeef",
            POLYMARKET_FUNDER="0xfunder",
            DASHBOARD_TOKEN="super-secret",
        )
        summary = settings.public_summary()
        serialized = repr(summary)
        self.assertNotIn("deadbeef", serialized)
        self.assertNotIn("super-secret", serialized)
        self.assertNotIn("PRIVATE", serialized.upper())
        self.assertTrue(summary["funder_configured"])
        self.assertNotIn("0xfunder", serialized)

    def test_ledger_and_model_paths_are_configurable(self) -> None:
        settings = build(LEDGER_PATH="/var/data/ledger.db", MODEL_PATH="/var/data/model.joblib")
        self.assertEqual(str(settings.ledger_path), "/var/data/ledger.db")
        self.assertEqual(str(settings.model_path), "/var/data/model.joblib")

    def test_web_bind_settings(self) -> None:
        settings = build(API_HOST="0.0.0.0", API_PORT="10000", SERVE_DASHBOARD="false")
        self.assertEqual(settings.api_host, "0.0.0.0")
        self.assertEqual(settings.api_port, 10_000)
        self.assertFalse(settings.serve_dashboard)


class SignalGateSettingsTests(unittest.TestCase):
    """Widening the gates must be a visible env change, not a code edit."""

    def test_defaults_match_the_strict_band(self) -> None:
        settings = build()
        self.assertEqual(settings.min_ev, 0.02)
        self.assertEqual(settings.arb_threshold, 0.985)
        self.assertEqual(settings.snipe_min_ask, 0.93)
        self.assertEqual(settings.snipe_hold_probability, 0.95)
        self.assertEqual(settings.snipe_max_ttl_hours, 2.4)

    def test_widened_gates_are_read_from_the_environment(self) -> None:
        settings = build(MIN_EV="0.01", SNIPE_MIN_ASK="0.90")
        self.assertEqual(settings.min_ev, 0.01)
        self.assertEqual(settings.snipe_min_ask, 0.90)
        self.assertIn("snipe_band", settings.public_summary())

    def test_summary_publishes_the_band_without_secrets(self) -> None:
        summary = build(MIN_EV="0.01", SNIPE_MIN_ASK="0.90").public_summary()
        self.assertEqual(summary["snipe_band"], [0.90, 0.97])
        self.assertEqual(summary["min_ev"], 0.01)

    def test_inverted_band_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build(SNIPE_MIN_ASK="0.95", SNIPE_MAX_ASK="0.90")

    def test_a_band_at_or_above_one_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build(SNIPE_MAX_ASK="1.01")
        with self.assertRaises(ValueError):
            build(SNIPE_MIN_ASK="1.0")

    def test_hold_probability_outside_zero_one_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build(SNIPE_HOLD_PROBABILITY="0")
        with self.assertRaises(ValueError):
            build(SNIPE_HOLD_PROBABILITY="1.2")

    def test_arb_threshold_above_one_is_rejected(self) -> None:
        # 1.01 would call a *loss* an arbitrage.
        with self.assertRaises(ValueError):
            build(ARB_THRESHOLD="1.01")


if __name__ == "__main__":
    unittest.main()
