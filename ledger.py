"""SQLite audit ledger for every signal and every submitted order."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Ledger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(
            str(self.path), timeout=30.0, check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._initialize()

    def _initialize(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    token_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    price REAL NOT NULL,
                    size_usd REAL NOT NULL,
                    reason TEXT NOT NULL,
                    expected_value REAL NOT NULL,
                    executed INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    order_id TEXT,
                    token_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    price REAL NOT NULL,
                    size_shares REAL NOT NULL,
                    status TEXT NOT NULL,
                    response TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_signals_ts
                    ON signals(ts);
                CREATE INDEX IF NOT EXISTS idx_signals_dedupe
                    ON signals(market_id, token_id, side, ts);
                CREATE INDEX IF NOT EXISTS idx_orders_ts
                    ON orders(ts);
                """
            )

    def record_signal(
        self,
        *,
        market_id: str,
        token_id: str,
        side: str,
        price: float,
        size_usd: float,
        reason: str,
        expected_value: float,
        executed: bool = False,
        ts: str | None = None,
    ) -> int:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO signals
                    (ts, market_id, token_id, side, price, size_usd, reason,
                     expected_value, executed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts or utc_iso(),
                    market_id,
                    token_id,
                    side,
                    float(price),
                    float(size_usd),
                    reason,
                    float(expected_value),
                    1 if executed else 0,
                ),
            )
            return int(cursor.lastrowid)

    def mark_signal_executed(self, signal_id: int, executed: bool = True) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE signals SET executed = ? WHERE id = ?",
                (1 if executed else 0, int(signal_id)),
            )

    def record_order(
        self,
        *,
        order_id: str | None,
        token_id: str,
        side: str,
        price: float,
        size_shares: float,
        status: str,
        response: Any,
        ts: str | None = None,
    ) -> int:
        if isinstance(response, str):
            serialized = response
        else:
            try:
                serialized = json.dumps(response, default=str, separators=(",", ":"))
            except (TypeError, ValueError):
                serialized = json.dumps({"unserializable_response": str(response)})
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO orders
                    (ts, order_id, token_id, side, price, size_shares, status,
                     response)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts or utc_iso(),
                    order_id,
                    token_id,
                    side,
                    float(price),
                    float(size_shares),
                    status,
                    serialized,
                ),
            )
            return int(cursor.lastrowid)

    def has_recent_signal(
        self,
        *,
        market_id: str,
        token_id: str,
        side: str,
        reason_prefix: str,
        since_iso: str,
    ) -> bool:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT 1
                FROM signals
                WHERE ts >= ?
                  AND market_id = ?
                  AND token_id = ?
                  AND side = ?
                  AND reason LIKE ?
                LIMIT 1
                """,
                (since_iso, market_id, token_id, side, f"{reason_prefix}%"),
            ).fetchone()
        return row is not None

    def has_recent_market_signal(
        self,
        *,
        market_id: str,
        reason_prefix: str,
        since_iso: str,
    ) -> bool:
        """Cooldown at market level prevents opposite-side signal churn."""
        with self._lock:
            row = self._connection.execute(
                """
                SELECT 1
                FROM signals
                WHERE ts >= ?
                  AND market_id = ?
                  AND reason LIKE ?
                LIMIT 1
                """,
                (since_iso, market_id, f"{reason_prefix}%"),
            ).fetchone()
        return row is not None

    def recent_signal_count(self, since_iso: str) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) AS count FROM signals WHERE ts >= ?",
                (since_iso,),
            ).fetchone()
        return int(row["count"]) if row else 0

    def close(self) -> None:
        with self._lock:
            self._connection.close()
