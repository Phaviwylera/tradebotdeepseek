"""Read-only HTTP surface: JSON status API plus the static dashboard.

The trading loop writes state to SQLite. This module exposes that state for a
browser without ever accepting a write:

- only ``GET``/``HEAD`` are served, every other method returns 405;
- the SQLite connection is opened ``query_only`` and only ``SELECT`` runs;
- no order route, no wallet access, and no private key is ever read;
- static files are limited to a fixed set of extensions inside one directory,
  with path traversal rejected after resolution.

It runs two ways from the same code path:

1. **In-process** (``web.start_status_server``), started by ``main.py`` next to
   the trading loop. This lets a single Render Web Service serve both the bot
   and the dashboard, which is what the free plan supports.
2. **Standalone** (``dashboard_api.py``), the original sidecar used by the EC2
   compose file, which keeps the status API on loopback only.

Environment:

- ``LEDGER_PATH``      ledger file to read (default ``/data/ledger.db``)
- ``API_HOST``         bind address (default ``127.0.0.1``, or ``0.0.0.0`` when
                       a platform ``PORT`` is present)
- ``API_PORT``         bind port (default ``8099``; ``PORT`` wins when both exist)
- ``DASHBOARD_TOKEN``  optional shared secret: ``Authorization: Bearer <token>``
                       or ``?token=``. Health checks never require it.
- ``ALLOWED_ORIGIN``   optional CORS origin for a dashboard hosted elsewhere
- ``DASHBOARD_DIR``    static asset directory (default ``dashboard/public``)
- ``SERVE_DASHBOARD``  set to ``false`` to expose only the JSON API
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger("web")

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
SERVER_NAME = "polymarket-status/2.0"

REPO_ROOT = Path(__file__).resolve().parent
STATIC_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".webmanifest": "application/manifest+json",
    ".txt": "text/plain; charset=utf-8",
    ".map": "application/json; charset=utf-8",
}
BLOCKED_STATIC_NAMES = {"", ".", "..", "vercel.json"}


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _stale_seconds(updated_at: str | None) -> float | None:
    if not updated_at:
        return None
    try:
        parsed = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())


def uptime_seconds(started_at: str | None) -> float | None:
    """Seconds since the bot process started, or ``None`` if unparseable."""
    if not started_at:
        return None
    try:
        parsed = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())


def connect(ledger_path: Path) -> sqlite3.Connection:
    """Open the ledger for reading only (no writes, no schema changes)."""
    connection = sqlite3.connect(str(ledger_path), timeout=10.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    # sqlite3.Row iteration yields values, so .keys() is required here.
    return {key: row[key] for key in row.keys()}  # noqa: SIM118


def clamp_limit(raw: Any) -> int:
    try:
        value = int(raw) if raw is not None else DEFAULT_LIMIT
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(MAX_LIMIT, value))


def empty_status(reason: str) -> dict[str, Any]:
    return {
        "ready": False,
        "reason": reason,
        "server_time": utc_iso(),
        "runtime": None,
        "stale_seconds": None,
        "uptime_seconds": None,
        "counts": {"signals_total": 0, "signals_24h": 0, "orders_total": 0},
        "recent_signals": [],
        "recent_orders": [],
    }


def build_status(ledger_path: Path, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """Read one consistent snapshot of the ledger into a JSON-safe payload."""
    ledger_path = Path(ledger_path)
    if not ledger_path.exists():
        return empty_status(f"ledger not created yet: {ledger_path}")

    connection = connect(ledger_path)
    try:
        runtime_row = connection.execute("SELECT * FROM runtime WHERE id = 1").fetchone()
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
        signals_total = connection.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        signals_24h = connection.execute(
            """
            SELECT COUNT(*) FROM signals
            WHERE ts >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-1 day')
            """
        ).fetchone()[0]
        orders_total = connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    finally:
        connection.close()

    runtime = _row_to_dict(runtime_row) if runtime_row is not None else None
    stale = _stale_seconds(runtime.get("updated_at") if runtime else None)
    if runtime is not None:
        runtime["equity"] = _as_float(runtime.get("equity"))
        runtime["cash"] = _as_float(runtime.get("cash"))
        runtime["exposure"] = _as_float(runtime.get("exposure"))
        runtime["realized_pnl"] = _as_float(runtime.get("realized_pnl"))
        runtime["drawdown_pct"] = _as_float(runtime.get("drawdown_pct"))
        runtime["killed"] = bool(runtime.get("killed"))

    recent_signals = [_row_to_dict(row) for row in signals]
    for signal in recent_signals:
        signal["executed"] = bool(signal.get("executed"))

    return {
        "ready": True,
        "reason": None,
        "server_time": utc_iso(),
        "ledger_path": str(ledger_path),
        "runtime": runtime,
        "stale_seconds": stale,
        "uptime_seconds": uptime_seconds(runtime.get("started_at") if runtime else None),
        "counts": {
            "signals_total": int(signals_total),
            "signals_24h": int(signals_24h),
            "orders_total": int(orders_total),
        },
        "recent_signals": recent_signals,
        "recent_orders": [_row_to_dict(row) for row in orders],
    }


def build_signals(ledger_path: Path, limit: int) -> dict[str, Any]:
    payload = build_status(ledger_path, limit)
    return {
        "ready": payload["ready"],
        "server_time": payload["server_time"],
        "limit": limit,
        "counts": payload["counts"],
        "signals": payload["recent_signals"],
    }


def build_orders(ledger_path: Path, limit: int) -> dict[str, Any]:
    payload = build_status(ledger_path, limit)
    return {
        "ready": payload["ready"],
        "server_time": payload["server_time"],
        "limit": limit,
        "counts": payload["counts"],
        "orders": payload["recent_orders"],
    }


class StatusHandler(BaseHTTPRequestHandler):
    """GET-only handler bound to one ledger, one static root, one token."""

    server_version = SERVER_NAME
    protocol_version = "HTTP/1.1"

    # Injected by ``create_server``.
    ledger_path: Path = Path("ledger.db")
    dashboard_token: str = ""
    allowed_origin: str = ""
    dashboard_dir: Path | None = None

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.debug("%s - %s", self.address_string(), fmt % args)

    # -- helpers ---------------------------------------------------------

    def _authorized(self, query: dict[str, list[str]]) -> bool:
        if not self.dashboard_token:
            return True
        header = self.headers.get("Authorization", "")
        supplied = ""
        if header.lower().startswith("bearer "):
            supplied = header[7:].strip()
        if not supplied:
            supplied = (query.get("token") or [""])[0]
        return hmac.compare_digest(supplied, self.dashboard_token)

    def _cors_headers(self) -> dict[str, str]:
        if not self.allowed_origin:
            return {}
        return {
            "Access-Control-Allow-Origin": self.allowed_origin,
            "Vary": "Origin",
            "Access-Control-Allow-Headers": "Authorization",
            "Access-Control-Allow-Methods": "GET, HEAD",
        }

    def _send(
        self,
        status: int,
        payload: dict[str, Any] | None = None,
        *,
        body: bytes | None = None,
        content_type: str = "application/json; charset=utf-8",
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        if body is None:
            body = json.dumps(payload, default=str, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in self._cors_headers().items():
            self.send_header(key, value)
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _resolve_static(self, raw_path: str) -> Path | None:
        root = self.dashboard_dir
        if root is None:
            return None
        relative = "index.html" if raw_path == "/" else raw_path.lstrip("/")
        if relative in BLOCKED_STATIC_NAMES or relative.endswith("/"):
            relative = f"{relative}index.html".lstrip("/")
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        if not candidate.is_file():
            return None
        if candidate.suffix.lower() not in STATIC_CONTENT_TYPES:
            return None
        if candidate.name.startswith("."):
            return None
        return candidate

    def _serve_static(self, raw_path: str) -> bool:
        path = self._resolve_static(raw_path)
        if path is None:
            return False
        try:
            body = path.read_bytes()
        except OSError:
            logger.exception("Could not read static asset %s", path)
            return False
        content_type = STATIC_CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
        self._send(200, body=body, content_type=content_type)
        return True

    # -- routing ---------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path in {"/healthz", "/health", "/api/health"}:
            self._send(
                200,
                {"ok": True, "service": "polymarket-status", "server_time": utc_iso()},
            )
            return

        if path.startswith("/api/"):
            if not self._authorized(query):
                self._send(401, {"error": "unauthorized"})
                return
            limit = clamp_limit((query.get("limit") or [None])[0])
            builder = {
                "/api/status": build_status,
                "/api/signals": build_signals,
                "/api/orders": build_orders,
            }.get(path)
            if builder is None:
                self._send(404, {"error": "not_found"})
                return
            try:
                payload = builder(self.ledger_path, limit)
            except sqlite3.Error as exc:
                logger.exception("Ledger read failed")
                self._send(503, {"error": "ledger_unavailable", "detail": str(exc)})
                return
            self._send(200, payload)
            return

        # Static shell is public (it contains no account data); every API read
        # below is what the optional dashboard token protects.
        if self._serve_static(path):
            return

        if path in {"/api", "/api/"} and not self._authorized(query):
            self._send(401, {"error": "unauthorized"})
            return

        self._send(404, {"error": "not_found"})

    def do_HEAD(self) -> None:
        self.do_GET()

    def _reject(self) -> None:
        self._send(405, {"error": "method_not_allowed"}, extra_headers={"Allow": "GET, HEAD"})

    do_POST = _reject
    do_PUT = _reject
    do_PATCH = _reject
    do_DELETE = _reject
    do_OPTIONS = _reject


class StatusHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def create_server(
    *,
    ledger_path: Path,
    host: str,
    port: int,
    dashboard_token: str = "",
    allowed_origin: str = "",
    dashboard_dir: Path | None = None,
    handler: type[StatusHandler] = StatusHandler,
) -> StatusHTTPServer:
    """Build (but do not start) a bound read-only status server."""
    resolved_dir: Path | None = None
    if dashboard_dir is not None:
        candidate = Path(dashboard_dir).resolve()
        if candidate.is_dir():
            resolved_dir = candidate
        else:
            logger.warning("Dashboard directory %s not found; API only", candidate)

    bound_handler = type(
        "BoundStatusHandler",
        (handler,),
        {
            "ledger_path": Path(ledger_path),
            "dashboard_token": dashboard_token,
            "allowed_origin": allowed_origin,
            "dashboard_dir": resolved_dir,
        },
    )
    return StatusHTTPServer((host, port), bound_handler)


def default_dashboard_dir() -> Path:
    configured = os.getenv("DASHBOARD_DIR", "").strip()
    if configured:
        return Path(configured)
    return REPO_ROOT / "dashboard" / "public"


def resolve_bind() -> tuple[str, int]:
    """Resolve bind address/port, treating a platform ``PORT`` as public."""
    platform_port = os.getenv("PORT", "").strip()
    api_port = os.getenv("API_PORT", "").strip()
    if api_port:
        port = int(api_port)
    elif platform_port:
        port = int(platform_port)
    else:
        port = 8099

    host = os.getenv("API_HOST", "").strip()
    if not host:
        host = "0.0.0.0" if platform_port else "127.0.0.1"
    return host, port


def start_status_server(
    ledger_path: str | Path,
    *,
    host: str | None = None,
    port: int | None = None,
    dashboard_token: str | None = None,
    dashboard_dir: str | Path | None = None,
    serve_dashboard: bool | None = None,
) -> tuple[StatusHTTPServer, threading.Thread]:
    """Start the read-only server on a daemon thread and return it."""
    resolved_host, resolved_port = resolve_bind()
    # Explicit values win, including 0: `port=0` means "pick an ephemeral port",
    # which tests and embedded callers rely on.
    if host is not None:
        resolved_host = host
    if port is not None:
        resolved_port = port

    token = (
        dashboard_token if dashboard_token is not None else os.getenv("DASHBOARD_TOKEN", "").strip()
    )
    origin = os.getenv("ALLOWED_ORIGIN", "").strip()

    if serve_dashboard is None:
        serve_dashboard = os.getenv("SERVE_DASHBOARD", "true").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
    directory = Path(dashboard_dir) if dashboard_dir else default_dashboard_dir()

    server = create_server(
        ledger_path=Path(ledger_path),
        host=resolved_host,
        port=resolved_port,
        dashboard_token=token,
        allowed_origin=origin,
        dashboard_dir=directory if serve_dashboard else None,
    )
    actual_host, actual_port = server.server_address[:2]
    if actual_host not in {"127.0.0.1", "::1", "localhost"} and not token:
        logger.warning(
            "Status API is publicly bound on %s:%s without DASHBOARD_TOKEN; "
            "set one to require a bearer token",
            actual_host,
            actual_port,
        )
    logger.info(
        "Read-only status server on http://%s:%s (ledger=%s, auth=%s, dashboard=%s)",
        actual_host,
        actual_port,
        ledger_path,
        "token" if token else "disabled",
        "on" if serve_dashboard and server.RequestHandlerClass.dashboard_dir else "off",
    )
    thread = threading.Thread(
        target=server.serve_forever,
        name="status-server",
        kwargs={"poll_interval": 0.5},
        daemon=True,
    )
    thread.start()
    return server, thread


def stop_status_server(server: StatusHTTPServer | None, thread: threading.Thread | None) -> None:
    if server is not None:
        server.shutdown()
        server.server_close()
    if thread is not None:
        thread.join(timeout=5.0)


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    ledger_path = Path(os.getenv("LEDGER_PATH", "/data/ledger.db"))
    server, _ = start_status_server(ledger_path)
    try:
        while True:
            threading.Event().wait(3600)
    except KeyboardInterrupt:
        stop_status_server(server, None)


if __name__ == "__main__":
    main()
