"""Ledger persistence, cooldown and runtime heartbeat tests."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ledger import Ledger


def iso(delta_seconds: float = 0.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)).isoformat(
        timespec="milliseconds"
    )


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "nested" / "ledger.db"
        self.ledger = Ledger(self.path)

    def tearDown(self) -> None:
        self.ledger.close()
        self._tmp.cleanup()

    def record(self, **overrides) -> int:
        payload = {
            "market_id": "0xmarket",
            "token_id": "token-yes",
            "side": "BUY",
            "price": 0.5,
            "size_usd": 10.0,
            "reason": "ARB YES+NO=0.97 | DRY_RUN",
            "expected_value": 0.3,
        }
        payload.update(overrides)
        return self.ledger.record_signal(**payload)

    def test_creates_parent_directories_and_schema(self) -> None:
        self.assertTrue(self.path.exists())
        with sqlite3.connect(self.path) as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
        self.assertTrue({"signals", "orders", "runtime"} <= tables)

    def test_wal_mode_is_enabled_for_concurrent_reads(self) -> None:
        with sqlite3.connect(self.path) as connection:
            mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")

    def test_signal_round_trip(self) -> None:
        signal_id = self.record()
        self.assertGreater(signal_id, 0)
        self.ledger.mark_signal_executed(signal_id, True)
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT executed, reason, size_usd FROM signals WHERE id = ?",
                (signal_id,),
            ).fetchone()
        self.assertEqual(row[0], 1)
        self.assertEqual(row[1], "ARB YES+NO=0.97 | DRY_RUN")
        self.assertAlmostEqual(row[2], 10.0)

    def test_order_response_is_serialized(self) -> None:
        self.ledger.record_order(
            order_id="abc",
            token_id="token-yes",
            side="BUY",
            price=0.5,
            size_shares=20.0,
            status="submitted",
            response={"orderID": "abc", "nested": {"x": 1}},
        )
        with sqlite3.connect(self.path) as connection:
            response = connection.execute("SELECT response FROM orders").fetchone()[0]
        self.assertEqual(json.loads(response)["orderID"], "abc")

    def test_unserializable_response_still_records(self) -> None:
        self.ledger.record_order(
            order_id=None,
            token_id="token-yes",
            side="BUY",
            price=0.5,
            size_shares=20.0,
            status="EXCEPTION",
            response=object(),
        )
        with sqlite3.connect(self.path) as connection:
            count = connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        self.assertEqual(count, 1)

    def test_market_cooldown_window(self) -> None:
        self.assertTrue(
            not self.ledger.has_recent_market_signal(
                market_id="0xmarket", reason_prefix="ARB", since_iso=iso(-3600)
            )
        )
        self.record(ts=iso(-30))
        self.assertTrue(
            self.ledger.has_recent_market_signal(
                market_id="0xmarket", reason_prefix="ARB", since_iso=iso(-3600)
            )
        )
        # A different strategy on the same market is tracked separately.
        self.assertFalse(
            self.ledger.has_recent_market_signal(
                market_id="0xmarket", reason_prefix="SNIPE", since_iso=iso(-3600)
            )
        )
        # Too old to matter.
        self.assertFalse(
            self.ledger.has_recent_market_signal(
                market_id="0xmarket", reason_prefix="ARB", since_iso=iso(60)
            )
        )

    def test_token_level_cooldown_matches_side(self) -> None:
        self.record(token_id="token-yes", side="BUY", ts=iso(-10))
        self.assertTrue(
            self.ledger.has_recent_signal(
                market_id="0xmarket",
                token_id="token-yes",
                side="BUY",
                reason_prefix="ARB",
                since_iso=iso(-600),
            )
        )
        self.assertFalse(
            self.ledger.has_recent_signal(
                market_id="0xmarket",
                token_id="token-no",
                side="BUY",
                reason_prefix="ARB",
                since_iso=iso(-600),
            )
        )

    def test_recent_signal_count(self) -> None:
        self.record(ts=iso(-60))
        self.record(ts=iso(-7200))
        self.assertEqual(self.ledger.recent_signal_count(iso(-3600)), 1)
        self.assertEqual(self.ledger.recent_signal_count(iso(-86400)), 2)

    def test_runtime_heartbeat_lifecycle(self) -> None:
        self.ledger.record_start(mode="dry_run")
        self.ledger.record_cycle(
            markets_scanned=9,
            markets_evaluated=8,
            signals_emitted=2,
            risk={
                "equity": 199.5,
                "cash": 189.5,
                "exposure": 10.0,
                "realized_pnl": -0.5,
                "drawdown_pct": 0.0025,
                "killed": False,
                "kill_reason": None,
            },
        )
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT mode, markets_scanned, equity, killed FROM runtime WHERE id = 1"
            ).fetchone()
        self.assertEqual(row, ("dry_run", 9, 199.5, 0))

        self.ledger.record_error("boom")
        with sqlite3.connect(self.path) as connection:
            error = connection.execute("SELECT last_error FROM runtime WHERE id = 1").fetchone()[0]
        self.assertEqual(error, "boom")

    def test_restart_preserves_audit_rows(self) -> None:
        self.record()
        self.ledger.close()

        reopened = Ledger(self.path)
        try:
            self.assertEqual(reopened.recent_signal_count(iso(-3600)), 1)
            reopened.record_start(mode="live")
            with sqlite3.connect(self.path) as connection:
                mode = connection.execute("SELECT mode FROM runtime WHERE id = 1").fetchone()[0]
                signals = connection.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
            self.assertEqual(mode, "live")
            self.assertEqual(signals, 1)
        finally:
            reopened.close()
        self.ledger = Ledger(self.path)


if __name__ == "__main__":
    unittest.main()
