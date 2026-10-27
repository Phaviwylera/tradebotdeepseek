"""Tests for the read-only HTTP surface (API + static dashboard)."""

from __future__ import annotations

import json
import socket
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import web
from ledger import Ledger


def raw_request(host: str, port: int, request_line: str) -> bytes:
    """Send a hand-written request line so paths are not normalised first."""
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.sendall(
            f"{request_line} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode()
        )
        chunks = []
        while True:
            data = sock.recv(4096)
            if not data:
                break
            chunks.append(data)
    return b"".join(chunks)


class StatusServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.ledger_path = Path(cls.tmp.name) / "ledger.db"
        ledger = Ledger(cls.ledger_path)
        ledger.record_start(mode="dry_run")
        ledger.record_signal(
            market_id="0xmarket1234567890",
            token_id="token-yes",
            side="BUY",
            price=0.51,
            size_usd=9.5,
            reason="SNIPE YES | DRY_RUN",
            expected_value=0.25,
        )
        ledger.record_order(
            order_id="order-1",
            token_id="token-yes",
            side="BUY",
            price=0.51,
            size_shares=18.6,
            status="submitted",
            response={"orderID": "order-1"},
        )
        ledger.record_cycle(
            markets_scanned=12,
            markets_evaluated=11,
            signals_emitted=1,
            risk={
                "equity": 200.0,
                "cash": 190.5,
                "exposure": 9.5,
                "realized_pnl": 0.25,
                "drawdown_pct": 0.01,
                "killed": False,
                "kill_reason": None,
            },
        )
        ledger.close()

        cls.static_dir = Path(cls.tmp.name) / "public"
        cls.static_dir.mkdir()
        (cls.static_dir / "index.html").write_text("<h1>console</h1>", encoding="utf-8")
        (cls.static_dir / "app.js").write_text("export {};", encoding="utf-8")
        (cls.static_dir / "secret.txt").write_text("nope", encoding="utf-8")

        cls.server, cls.thread = web.start_status_server(
            cls.ledger_path,
            host="127.0.0.1",
            port=0,
            dashboard_token="test-token",
            dashboard_dir=cls.static_dir,
        )
        cls.host, cls.port = cls.server.server_address[:2]

    @classmethod
    def tearDownClass(cls) -> None:
        web.stop_status_server(cls.server, cls.thread)
        cls.tmp.cleanup()

    def request(self, path: str, token: str | None = "test-token") -> tuple[int, str, bytes]:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        request = urllib.request.Request(f"http://{self.host}:{self.port}{path}", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.headers.get("content-type", ""), response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers.get("content-type", ""), error.read()

    def test_health_is_open_and_cheap(self) -> None:
        status, _, body = self.request("/healthz", token=None)
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertIn(payload["loop"], {"ok", "stalled"})
        self.assertEqual(payload["mode"], "dry_run")

    def test_health_answers_without_the_token_but_leaks_no_market_data(self) -> None:
        status, _, body = self.request("/healthz", token=None)
        self.assertEqual(status, 200)
        payload = json.loads(body)
        # Aggregates are public; the actual signals/orders are not.
        self.assertEqual(payload["counts"]["signals_total"], 1)
        self.assertEqual(payload["counts"]["orders_total"], 1)
        self.assertNotIn("recent_signals", payload)
        self.assertNotIn("recent_orders", payload)
        self.assertNotIn(b"token-yes", body)
        self.assertNotIn(b"0xmarket", body)

    def test_status_requires_token(self) -> None:
        status, _, body = self.request("/api/status", token=None)
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"], "unauthorized")

    def test_token_can_be_supplied_as_query_parameter(self) -> None:
        status, _, body = self.request("/api/status?token=test-token", token=None)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ready"])

    def test_status_payload_shape(self) -> None:
        status, content_type, body = self.request("/api/status")
        self.assertEqual(status, 200)
        self.assertIn("application/json", content_type)
        payload = json.loads(body)
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["runtime"]["mode"], "dry_run")
        self.assertEqual(payload["runtime"]["markets_scanned"], 12)
        self.assertFalse(payload["runtime"]["killed"])
        self.assertIsInstance(payload["runtime"]["killed"], bool)
        self.assertEqual(payload["counts"]["orders_total"], 1)
        self.assertEqual(len(payload["recent_signals"]), 1)
        self.assertIsInstance(payload["recent_signals"][0]["executed"], bool)
        self.assertGreaterEqual(payload["stale_seconds"], 0.0)
        self.assertIsNotNone(payload["uptime_seconds"])

    def test_signals_and_orders_endpoints(self) -> None:
        status, _, body = self.request("/api/signals?limit=1")
        self.assertEqual(status, 200)
        signals = json.loads(body)
        self.assertEqual(len(signals["signals"]), 1)
        self.assertEqual(signals["signals"][0]["market_id"], "0xmarket1234567890")

        status, _, body = self.request("/api/orders")
        self.assertEqual(status, 200)
        orders = json.loads(body)
        self.assertEqual(orders["orders"][0]["order_id"], "order-1")

    def test_limit_is_clamped(self) -> None:
        _, _, body = self.request("/api/status?limit=9999")
        self.assertIn("recent_signals", json.loads(body))
        _, _, body = self.request("/api/signals?limit=not-a-number")
        self.assertEqual(json.loads(body)["limit"], web.DEFAULT_LIMIT)

    def test_unknown_api_path_is_404(self) -> None:
        status, _, body = self.request("/api/nope")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"], "not_found")

    def test_static_dashboard_is_served(self) -> None:
        status, content_type, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        self.assertIn(b"console", body)

        status, content_type, _ = self.request("/app.js")
        self.assertEqual(status, 200)
        self.assertIn("text/javascript", content_type)

    def test_static_whitelist_blocks_other_extensions(self) -> None:
        status, _, body = self.request("/secret.txt")
        self.assertEqual(status, 200)  # .txt is allowed for robots.txt-style files
        self.assertIn(b"nope", body)

    def test_path_traversal_is_rejected(self) -> None:
        # Encoded traversal survives client-side normalisation, so the server
        # itself must refuse it.
        response = raw_request(self.host, self.port, "GET /%2e%2e/%2e%2e/etc/passwd")
        self.assertIn(b"404", response.split(b"\r\n", 1)[0])
        self.assertNotIn(b"root:", response)

    def test_write_methods_are_rejected(self) -> None:
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            response = raw_request(self.host, self.port, f"{method} /api/status")
            self.assertIn(b"405", response.split(b"\r\n", 1)[0], method)
        response = raw_request(self.host, self.port, "POST /api/status")
        self.assertIn(b"Allow: GET, HEAD", response)


class ExplicitPortTests(unittest.TestCase):
    def test_port_zero_means_ephemeral_not_unset(self) -> None:
        """port=0 must bind an ephemeral port even when PORT is in the env."""
        import os
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.db")
            ledger.record_start(mode="dry_run")
            ledger.close()
            with mock.patch.dict(os.environ, {"PORT": "8099"}, clear=False):
                server, thread = web.start_status_server(
                    Path(tmp) / "ledger.db", host="127.0.0.1", port=0
                )
            try:
                self.assertNotEqual(server.server_address[1], 8099)
                self.assertGreater(server.server_address[1], 0)
            finally:
                web.stop_status_server(server, thread)


class LedgerlessServerTests(unittest.TestCase):
    def test_missing_ledger_reports_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server, thread = web.start_status_server(
                Path(tmp) / "absent.db", host="127.0.0.1", port=0, dashboard_token=""
            )
            try:
                host, port = server.server_address[:2]
                with urllib.request.urlopen(
                    f"http://{host}:{port}/api/status", timeout=5
                ) as response:
                    payload = json.loads(response.read())
                self.assertFalse(payload["ready"])
                self.assertIn("ledger not created yet", payload["reason"])
                self.assertEqual(payload["counts"]["signals_total"], 0)
            finally:
                web.stop_status_server(server, thread)


class BindResolutionTests(unittest.TestCase):
    def test_platform_port_binds_publicly(self) -> None:
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"PORT": "10000"}, clear=True):
            self.assertEqual(web.resolve_bind(), ("0.0.0.0", 10000))

    def test_loopback_by_default(self) -> None:
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(web.resolve_bind(), ("127.0.0.1", 8099))

    def test_api_port_wins_over_platform_port(self) -> None:
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"PORT": "10000", "API_PORT": "8099"}, clear=True):
            self.assertEqual(web.resolve_bind(), ("0.0.0.0", 8099))


class ConcurrencyTests(unittest.TestCase):
    def test_threaded_server_handles_parallel_reads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.db"
            ledger = Ledger(path)
            ledger.record_start(mode="dry_run")
            ledger.close()

            server, thread = web.start_status_server(
                path, host="127.0.0.1", port=0, dashboard_token=""
            )
            results: list[int] = []

            def hit() -> None:
                host, port = server.server_address[:2]
                with urllib.request.urlopen(
                    f"http://{host}:{port}/api/status", timeout=5
                ) as response:
                    results.append(response.status)

            try:
                workers = [threading.Thread(target=hit) for _ in range(8)]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join()
            finally:
                web.stop_status_server(server, thread)
            self.assertEqual(results, [200] * 8)


if __name__ == "__main__":
    unittest.main()


class HealthProbeTests(unittest.TestCase):
    """`/healthz` is the only token-free window into the trading loop."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger_path = Path(self.tmp.name) / "ledger.db"

    def _ledger(self) -> Ledger:
        ledger = Ledger(self.ledger_path)
        self.addCleanup(ledger.close)
        return ledger

    def _cycle(self, ledger: Ledger, *, scanned: int = 12, evaluated: int = 11) -> None:
        ledger.record_cycle(
            markets_scanned=scanned,
            markets_evaluated=evaluated,
            signals_emitted=1,
            risk={
                "equity": 200.0,
                "cash": 190.5,
                "exposure": 9.5,
                "realized_pnl": 0.0,
                "drawdown_pct": 0.0,
                "killed": False,
                "kill_reason": None,
            },
        )

    def test_healthy_loop_reports_vitals(self) -> None:
        ledger = self._ledger()
        ledger.record_start(mode="dry_run")
        self._cycle(ledger)
        payload = web.build_health(self.ledger_path)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["loop"], "ok")
        self.assertEqual(payload["mode"], "dry_run")
        self.assertEqual(payload["markets_scanned"], 12)
        self.assertEqual(payload["markets_evaluated"], 11)
        self.assertEqual(payload["signals_last_cycle"], 1)
        self.assertIsInstance(payload["seconds_since_cycle"], float)
        self.assertIsNone(payload["last_error"])

    def test_publishes_the_error_text_instead_of_see_logs(self) -> None:
        ledger = self._ledger()
        ledger.record_start(mode="dry_run")
        ledger.record_error("trading cycle failed: HTTPStatusError: 403 Forbidden")
        payload = web.build_health(self.ledger_path)
        self.assertEqual(payload["loop"], "error")
        self.assertIn("403 Forbidden", payload["last_error"])
        self.assertIn("last_error", payload["reason"])

    def test_flags_a_stalled_loop(self) -> None:
        ledger = self._ledger()
        ledger.record_start(mode="dry_run")
        self._cycle(ledger)
        ledger.close()
        with sqlite3.connect(str(self.ledger_path)) as connection:
            connection.execute(
                "UPDATE runtime SET updated_at = '2020-01-01T00:00:00+00:00' WHERE id = 1"
            )
        payload = web.build_health(self.ledger_path)
        self.assertEqual(payload["loop"], "stalled")
        self.assertGreater(payload["seconds_since_cycle"], web.HEALTH_STALL_SECONDS)
        self.assertIn("pipeline is not cycling", payload["reason"])

    def test_missing_ledger_is_still_ok(self) -> None:
        payload = web.build_health(Path(self.tmp.name) / "nope.db")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["loop"], "unknown")
        self.assertIn("ledger not created yet", payload["reason"])

    def test_unreadable_ledger_never_raises(self) -> None:
        broken = Path(self.tmp.name) / "broken.db"
        broken.write_bytes(b"not a sqlite database")
        payload = web.build_health(broken)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["loop"], "unknown")


class GateDiagnosticsTests(unittest.TestCase):
    """`/healthz` must explain "no signals" without giving the token away."""

    def test_summary_keeps_only_aggregates(self) -> None:
        summary = web.summarize_diagnostics(
            {
                "near_resolution_candidates": 7,
                "near_resolution_selected": 4,
                "markets_in_snipe_band": 0,
                "closest_snipe_price": {"ask": 0.9123, "distance": 0.0177, "token_id": "leak"},
                "best_arb_sum": 0.9902,
                "closest_ttl_hours": 3.5,
                "secret": "x",
            }
        )
        assert summary is not None
        self.assertEqual(summary["near_resolution_candidates"], 7)
        self.assertEqual(summary["closest_snipe_price"], {"ask": 0.9123, "distance": 0.0177})
        self.assertNotIn("secret", summary)

    def test_summary_publishes_the_negrisk_lane(self) -> None:
        summary = web.summarize_diagnostics(
            {
                "negrisk_groups": 2,
                "negrisk_checked": 2,
                "negrisk_best_cost": 1.0142,
                "negrisk_best_profit": -0.0142,
            }
        )
        assert summary is not None
        self.assertEqual(summary["negrisk_groups"], 2)
        self.assertEqual(summary["negrisk_checked"], 2)
        self.assertAlmostEqual(summary["negrisk_best_cost"], 1.0142)
        self.assertAlmostEqual(summary["negrisk_best_profit"], -0.0142)

    def test_summary_publishes_the_incomplete_count(self) -> None:
        summary = web.summarize_diagnostics({"negrisk_checked": 2, "negrisk_incomplete": 2})
        assert summary is not None
        self.assertEqual(summary["negrisk_incomplete"], 2)

    def test_negrisk_keys_are_present_even_when_unset(self) -> None:
        # A missing key reads as "not implemented"; an explicit null reads as
        # "looked, found nothing". The difference matters when debugging.
        summary = web.summarize_diagnostics({})
        assert summary is not None
        self.assertIn("negrisk_groups", summary)
        self.assertIn("negrisk_best_cost", summary)
        self.assertIsNone(summary["negrisk_best_cost"])

    def test_no_diagnostics_means_null_not_empty(self) -> None:
        self.assertIsNone(web.summarize_diagnostics(None))

    def test_health_includes_the_gates_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = web.build_health(
                Path(tmp) / "missing.db",
                None,
                {"near_resolution_candidates": 3, "markets_in_snipe_band": 1},
            )
        self.assertEqual(payload["gates"]["near_resolution_candidates"], 3)
        self.assertEqual(payload["gates"]["markets_in_snipe_band"], 1)
        self.assertIsNone(payload["model"])
        self.assertTrue(payload["ok"])
