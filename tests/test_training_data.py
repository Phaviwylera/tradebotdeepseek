"""Tests for the training-data path: snapshots, labels, and the join.

These are the pieces that decide whether the MODEL strategy can ever be honest.
A leak here would produce a model that backtests beautifully and is worthless,
so the leakage guards get tested as hard as the happy path.
"""

from __future__ import annotations

import csv
import importlib.util
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from features import FEATURE_COLUMNS
from scanner import Market, OrderBook
from snapshots import SNAPSHOT_COLUMNS, SnapshotRecorder

REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "scripts" / "fetch_resolved_markets.py"

spec = importlib.util.spec_from_file_location("fetch_resolved_markets", MODULE_PATH)
assert spec and spec.loader
fetch = importlib.util.module_from_spec(spec)
sys.modules["fetch_resolved_markets"] = fetch
spec.loader.exec_module(fetch)


def make_market(end_date: datetime | None = None) -> Market:
    return Market(
        condition_id="0xabc123",
        question="Will the test resolve YES?",
        token_yes="yes-token",
        token_no="no-token",
        volume=50_000.0,
        liquidity=10_000.0,
        end_date=end_date or datetime.now(timezone.utc) + timedelta(days=3),
    )


def make_book() -> OrderBook:
    return OrderBook.from_raw("yes-token", [(0.44, 100.0)], [(0.46, 100.0)])


class SnapshotRecorderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "nested" / "snapshots.csv"

    def test_disabled_recorder_writes_nothing(self) -> None:
        recorder = SnapshotRecorder(None)
        self.assertFalse(recorder.enabled)
        self.assertIsNone(recorder.record(make_market(), make_book()))

    def test_creates_header_and_appends_rows(self) -> None:
        recorder = SnapshotRecorder(self.path)
        self.assertTrue(recorder.enabled)
        first = recorder.record(make_market(), make_book())
        second = recorder.record(make_market(), make_book())
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(recorder.rows_written, 2)

        with self.path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 2)
        self.assertEqual(list(rows[0].keys()), SNAPSHOT_COLUMNS)
        self.assertEqual(rows[0]["condition_id"], "0xabc123")
        for column in FEATURE_COLUMNS:
            self.assertAlmostEqual(float(rows[0][column]), float(first[column]), places=9)
        # mid of a 0.44/0.46 book
        self.assertAlmostEqual(float(rows[0]["mid_price"]), 0.45, places=6)

    def test_reopening_does_not_duplicate_the_header(self) -> None:
        SnapshotRecorder(self.path).record(make_market(), make_book())
        SnapshotRecorder(self.path).record(make_market(), make_book())
        with self.path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 2)

    def test_capture_never_raises_on_a_broken_path(self) -> None:
        # A directory where the file should be: the write must fail quietly.
        broken = Path(self.tmp.name) / "as_dir"
        broken.mkdir()
        recorder = SnapshotRecorder(broken / "x.csv")
        recorder.path = broken  # now opening it for append raises
        self.assertIsNone(recorder.record(make_market(), make_book()))
        self.assertEqual(recorder.errors, 1)


class LabelExtractionTests(unittest.TestCase):
    def test_reads_a_resolved_yes(self) -> None:
        row = fetch.label_from_market(
            {
                "conditionId": "0x1",
                "outcomes": '["Yes", "No"]',
                "outcomePrices": '["1", "0"]',
                "closedTime": "2026-01-02T03:04:05Z",
                "volumeNum": 1234.5,
            }
        )
        assert row is not None
        self.assertEqual(row["resolved_yes"], 1)
        self.assertEqual(row["condition_id"], "0x1")
        self.assertTrue(row["end_date"].startswith("2026-01-02T03:04:05"))
        self.assertAlmostEqual(row["volume"], 1234.5)

    def test_reads_a_resolved_no_with_reordered_outcomes(self) -> None:
        row = fetch.label_from_market(
            {
                "conditionId": "0x2",
                "outcomes": '["No", "Yes"]',
                "outcomePrices": '["1", "0"]',
            }
        )
        assert row is not None
        self.assertEqual(row["resolved_yes"], 0)

    def test_ignores_an_unresolved_market(self) -> None:
        # This is the leak that matters: a live market's prices are not a label.
        self.assertIsNone(
            fetch.label_from_market(
                {
                    "conditionId": "0x3",
                    "outcomes": '["Yes", "No"]',
                    "outcomePrices": '["0.62", "0.38"]',
                }
            )
        )

    def test_ignores_missing_or_malformed_records(self) -> None:
        self.assertIsNone(fetch.label_from_market({}))
        self.assertIsNone(fetch.label_from_market({"conditionId": "0x4"}))
        self.assertIsNone(
            fetch.label_from_market({"conditionId": "0x5", "outcomePrices": "not json"})
        )


class JoinTests(unittest.TestCase):
    def _snapshot(self, captured: str, condition_id: str = "0xabc123") -> dict[str, object]:
        row: dict[str, object] = {
            "condition_id": condition_id,
            "captured_at": captured,
            "end_date": "2026-02-01T00:00:00+00:00",
        }
        for index, column in enumerate(FEATURE_COLUMNS):
            row[column] = 0.1 * (index + 1)
        return row

    def _label(self, end_date: str, condition_id: str = "0xabc123") -> dict[str, object]:
        return {"condition_id": condition_id, "resolved_yes": 1, "end_date": end_date}

    def test_keeps_the_last_snapshot_before_resolution(self) -> None:
        rows, stats = fetch.build_training_rows(
            [self._label("2026-02-01T00:00:00+00:00")],
            [
                self._snapshot("2026-01-30T00:00:00+00:00"),
                self._snapshot("2026-01-31T00:00:00+00:00"),
            ],
        )
        self.assertEqual(stats["kept"], 1)
        self.assertEqual(rows[0]["captured_at"], "2026-01-31T00:00:00+00:00")
        self.assertEqual(rows[0]["resolved_yes"], 1)

    def test_drops_snapshots_taken_at_or_after_resolution(self) -> None:
        rows, stats = fetch.build_training_rows(
            [self._label("2026-02-01T00:00:00+00:00")],
            [
                self._snapshot("2026-01-31T00:00:00+00:00"),
                self._snapshot("2026-02-01T00:00:00+00:00"),
                self._snapshot("2026-02-02T00:00:00+00:00"),
            ],
        )
        self.assertEqual(stats["too_late"], 2)
        self.assertEqual(rows[0]["captured_at"], "2026-01-31T00:00:00+00:00")

    def test_drops_snapshots_too_close_to_resolution(self) -> None:
        rows, stats = fetch.build_training_rows(
            [self._label("2026-02-01T00:00:00+00:00")],
            [self._snapshot("2026-01-31T23:45:00+00:00")],
            min_lead_minutes=30.0,
        )
        self.assertEqual(rows, [])
        self.assertEqual(stats["too_late"], 1)

    def test_skips_snapshots_without_a_label(self) -> None:
        rows, stats = fetch.build_training_rows(
            [self._label("2026-02-01T00:00:00+00:00")],
            [self._snapshot("2026-01-30T00:00:00+00:00", condition_id="0xother")],
        )
        self.assertEqual(rows, [])
        self.assertEqual(stats["no_label"], 1)

    def test_output_is_chronological_and_complete(self) -> None:
        rows, _ = fetch.build_training_rows(
            [
                self._label("2026-03-01T00:00:00+00:00", condition_id="0xlate"),
                self._label("2026-02-01T00:00:00+00:00", condition_id="0xearly"),
            ],
            [
                self._snapshot("2026-02-25T00:00:00+00:00", condition_id="0xlate"),
                self._snapshot("2026-01-25T00:00:00+00:00", condition_id="0xearly"),
            ],
        )
        self.assertEqual([row["condition_id"] for row in rows], ["0xearly", "0xlate"])
        self.assertEqual(len(rows[0]), 4 + len(FEATURE_COLUMNS))


if __name__ == "__main__":
    unittest.main()
