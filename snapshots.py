"""Opt-in capture of pre-resolution feature snapshots.

Why this exists
---------------
The calibrated model needs a training set of *features observed before a market
resolved* plus that market's eventual outcome. Polymarket's public API cannot
give you the first half after the fact: a closed market now reports its final
prices (1 and 0), so any "training data" rebuilt from closed markets alone leaks
the answer into the features. A model trained that way scores beautifully and
is worthless.

So the only honest source is a snapshot taken while the market was still open.
Set ``SNAPSHOT_PATH`` and the bot appends one row per evaluated market per
cycle, timestamped. ``scripts/fetch_resolved_markets.py`` later joins those rows
to the outcomes and refuses anything captured at or after resolution.

Cost: one row is ~200 bytes. Twenty markets a cycle at one cycle a minute is
~5 MB/day, which is why this is off by default and why the free tier (no disk)
is the wrong place to keep it. Run it where you can keep the file.
"""

from __future__ import annotations

import csv
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from features import FEATURE_COLUMNS, build_features
from scanner import Market, OrderBook

logger = logging.getLogger(__name__)

METADATA_COLUMNS = ["condition_id", "captured_at", "end_date"]
SNAPSHOT_COLUMNS = METADATA_COLUMNS + list(FEATURE_COLUMNS)


class SnapshotRecorder:
    """Append-only CSV writer for pre-resolution feature rows."""

    def __init__(self, path: str | Path | None, *, flush_every: int = 1) -> None:
        self.path = Path(path) if path else None
        self.flush_every = max(1, int(flush_every))
        self._lock = threading.Lock()
        self._rows_since_flush = 0
        self.rows_written = 0
        self.errors = 0
        if self.path is not None:
            self._ensure_header()

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def _ensure_header(self) -> None:
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size > 0:
            return
        with self.path.open("w", newline="", encoding="utf-8") as handle:
            csv.DictWriter(handle, fieldnames=SNAPSHOT_COLUMNS).writeheader()

    def record(
        self,
        market: Market,
        book: OrderBook,
        *,
        news_sentiment: float = 0.0,
        captured_at: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Append one row built from the YES book. Never raises."""
        if self.path is None:
            return None
        stamp = captured_at or datetime.now(timezone.utc)
        try:
            features = build_features(market, book, news_sentiment=news_sentiment)
            row: dict[str, Any] = {
                "condition_id": market.condition_id,
                "captured_at": stamp.isoformat(timespec="seconds"),
                "end_date": market.end_date.isoformat(timespec="seconds")
                if market.end_date
                else "",
            }
            for column in FEATURE_COLUMNS:
                row[column] = float(features.iloc[0][column])
            with self._lock:
                with self.path.open("a", newline="", encoding="utf-8") as handle:
                    writer = csv.DictWriter(handle, fieldnames=SNAPSHOT_COLUMNS)
                    writer.writerow(row)
                    handle.flush()
                self._rows_since_flush += 1
                if self._rows_since_flush >= self.flush_every:
                    self._rows_since_flush = 0
                self.rows_written += 1
            return row
        except Exception:  # noqa: BLE001 - capture must never break trading
            self.errors += 1
            logger.exception("Could not write a feature snapshot for %s", market.condition_id)
            return None
