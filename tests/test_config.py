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


if __name__ == "__main__":
    unittest.main()
