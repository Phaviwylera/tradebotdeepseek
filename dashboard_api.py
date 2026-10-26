"""Read-only JSON bridge between the trading bot and a dashboard.

The bot writes its state and audit rows to SQLite. This process exposes that
state over HTTP for the EC2 dashboard. It is deliberately narrow:

- only GET requests are allowed;
- only SELECT statements are executed (connection is ``query_only``);
- no order endpoint, no wallet access, and no private key is ever read;
- binds to ``127.0.0.1`` by default, so it is not publicly exposed unless an
  operator puts a reverse proxy in front of it.

Run it with the standard library only, using the same Python image as the bot:

    LEDGER_PATH=/data/ledger.db python dashboard_api.py
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger("dashboard_api")

LEDGER_PATH = Path(os.getenv("LEDGER_PATH", "/data/ledger.db"))
API_HOST = os.getenv("API_HOST", "127.0.0.1").strip()
API_PORT = int(os.getenv("API_PORT", "8099"))
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "").strip()
ALLOWED_ORIGIN = os.getenv("ALLOWED_ORIGIN", "").strip()
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    """Open the ledger for reading only (no writes, no schema changes)."""
    connection = sqlite3.connect(str(LEDGER_PATH), timeout=10.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row}


def _stale_seconds(updated_at: str | None) -> float | None:
    if not updated_at:
        return None
    try:
        parsed = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())


def _clamp_limit(raw: str | None) -> int:
    try:
        value = int(raw) if raw is not None else DEFAULT_LIMIT
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(MAX_LIMIT, value))


def _empty(reason: str) -> dict[str, Any]:
    return {
        "ready": False,
        "reason": reason,
        "server_time": utc_iso(),
        "runtime": None,
        "counts": {"signals_total": 0, "signals_24h": 0, "orders_total": 0},
        "recent_signals": [],
        "recent_orders": [],
    }


def build_status(limit: int) -> dict[str, Any]:
    if not LEDGER_PATH.exists():
        return _empty(f"ledger not created yet: {LEDGER_PATH}")

    connection = connect()
    try:
        runtime_row = connection.execute(
            "SELECT * FROM runtime WHERE id = 1"
        ).fetchone()
        signals = connection.execute(
            """
            SELECT id, ts, market_id, token_id, side, price, size_usd, reason,
                   expected_value, executed
            FROM signals
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        orders = connection.execute(
            """
            SELECT id, ts, order_id, token_id, side, price, size_shares,
                   status, substr(response, 1, 500) AS response
            FROM orders
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        signals_total = connection.execute(
            "SELECT COUNT(*) FROM signals"
        ).fetchone()[0]
        signals_24h = connection.execute(
            """
            SELECT COUNT(*) FROM signals
            WHERE ts >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-1 day')
            """
        ).fetchone()[0]
        orders_total = connection.execute(
            "SELECT COUNT(*) FROM orders"
        ).fetchone()[0]
    finally:
        connection.close()

    runtime = _row_to_dict(runtime_row) if runtime_row is not None else None
    stale = _stale_seconds(runtime.get("updated_at") if runtime else None)

    return {
        "ready": True,
        "reason": None,
        "server_time": utc_iso(),
        "ledger_path": str(LEDGER_PATH),
        "runtime": runtime,
        "stale_seconds": stale,
        "counts": {
            "signals_total": int(signals_total),
            "signals_24h": int(signals_24h),
            "orders_total": int(orders_total),
        },
        "recent_signals": [_row_to_dict(row) for row in signals],
        "recent_orders": [_row_to_dict(row) for row in orders],
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "polymarket-status/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), fmt % args)

    def _authorized(self, query: dict[str, list[str]]) -> bool:
        if not DASHBOARD_TOKEN:
            return True
        header = self.headers.get("Authorization", "")
        supplied = ""
        if header.lower().startswith("bearer "):
            supplied = header[7:].strip()
        if not supplied:
            supplied = (query.get("token") or [""])[0]
        return hmac.compare_digest(supplied, DASHBOARD_TOKEN)

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, default=str, separators=(",", ":")).encode(
            "utf-8"
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if ALLOWED_ORIGIN:
            self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGIN)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Authorization")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path in {"/healthz", "/api/health"}:
            self._send(200, {"ok": True, "server_time": utc_iso()})
            return

        if not self._authorized(query):
            self._send(401, {"error": "unauthorized"})
            return

        limit = _clamp_limit((query.get("limit") or [None])[0])

        if parsed.path == "/api/status":
            try:
                payload = build_status(limit)
            except sqlite3.Error as exc:
                logger.exception("Ledger read failed")
                self._send(503, {"error": "ledger_unavailable", "detail": str(exc)})
                return
            self._send(200, payload)
            return

        if parsed.path == "/api/signals":
            try:
                payload = build_status(limit)
            except sqlite3.Error as exc:
                self._send(503, {"error": "ledger_unavailable", "detail": str(exc)})
                return
            self._send(
                200,
                {
                    "server_time": payload["server_time"],
                    "limit": limit,
                    "signals": payload["recent_signals"],
                },
            )
            return

        if parsed.path == "/api/orders":
            try:
                payload = build_status(limit)
            except sqlite3.Error as exc:
                self._send(503, {"error": "ledger_unavailable", "detail": str(exc)})
                return
            self._send(
                200,
                {
                    "server_time": payload["server_time"],
                    "limit": limit,
                    "orders": payload["recent_orders"],
                },
            )
            return

        self._send(404, {"error": "not_found"})

    def _reject(self) -> None:
        self._send(405, {"error": "method_not_allowed"})

    do_POST = _reject
    do_PUT = _reject
    do_PATCH = _reject
    do_DELETE = _reject


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    server = ThreadingHTTPServer((API_HOST, API_PORT), Handler)
    logger.info(
        "Read-only status API on http://%s:%d (ledger=%s, auth=%s)",
        API_HOST,
        API_PORT,
        LEDGER_PATH,
        "token" if DASHBOARD_TOKEN else "disabled",
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
