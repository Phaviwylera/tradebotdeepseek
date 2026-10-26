"""Tests for the model artifact path: light backend, save/load, MODEL_URL fetch."""

from __future__ import annotations

import http.server
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path

import numpy as np
import pandas as pd

import web
from features import FEATURE_COLUMNS
from model import BACKENDS, ProbabilityModel, _build_estimator


def synthetic_frame(rows: int = 90, seed: int = 7) -> tuple[pd.DataFrame, np.ndarray]:
    rng = np.random.default_rng(seed)
    mid = rng.uniform(0.2, 0.8, size=rows)
    frame = pd.DataFrame(
        {
            "mid_price": mid,
            "spread": rng.uniform(0.0, 0.03, size=rows),
            "bid_depth_5": rng.uniform(10.0, 500.0, size=rows),
            "ask_depth_5": rng.uniform(10.0, 500.0, size=rows),
            "imbalance": rng.uniform(-1.0, 1.0, size=rows),
            "log_volume": rng.uniform(5.0, 12.0, size=rows),
            "log_liquidity": rng.uniform(4.0, 11.0, size=rows),
            "ttl_days": rng.uniform(0.0, 30.0, size=rows),
            "news_sentiment": np.zeros(rows),
            "spread_over_mid": rng.uniform(0.0, 0.1, size=rows),
        },
        columns=FEATURE_COLUMNS,
    )
    labels = (rng.uniform(size=rows) < mid).astype(int)
    return frame, labels


class BackendTests(unittest.TestCase):
    def test_light_backend_needs_no_boosting_libraries(self) -> None:
        estimator = _build_estimator("light")
        names = [name for name, _ in estimator.estimator.base_estimators]
        self.assertEqual(names, ["hgb", "extra_trees", "random_forest"])
        self.assertNotIn("xgb", names)
        self.assertNotIn("lgbm", names)

    def test_unknown_backend_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _build_estimator("turbo")
        self.assertEqual(BACKENDS, ("full", "light"))


class LightModelRoundTripTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        frame, labels = synthetic_frame()
        cls.model = ProbabilityModel().fit(frame, labels, backend="light")
        cls.frame = frame

    def test_metadata_records_the_backend(self) -> None:
        self.assertEqual(self.model.metadata["backend"], "light")
        self.assertEqual(self.model.metadata["samples"], 90)

    def test_predictions_are_probabilities(self) -> None:
        probability = self.model.predict_proba(self.frame.iloc[[0]])
        self.assertGreaterEqual(probability, 0.0)
        self.assertLessEqual(probability, 1.0)

    def test_round_trips_through_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "model.joblib"
            self.model.save(path)
            self.assertTrue(path.exists())
            restored = ProbabilityModel()
            self.assertTrue(restored.load(path))
            self.assertEqual(restored.metadata["backend"], "light")
            first = self.model.predict_proba(self.frame.iloc[[3]])
            second = restored.predict_proba(self.frame.iloc[[3]])
            self.assertAlmostEqual(first, second, places=9)


class FetchArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = b"joblib-bytes" * 100

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib API
                if self.path == "/missing":
                    self.send_error(404)
                    return
                body = b"" if self.path == "/empty" else cls.payload
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                return

        cls.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def test_downloads_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "model.joblib"
            ProbabilityModel.fetch_artifact(f"{self.base}/model.joblib", path)
            self.assertEqual(path.read_bytes(), self.payload)
            self.assertEqual(sorted(item.name for item in path.parent.iterdir()), ["model.joblib"])

    def test_http_error_leaves_no_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "model.joblib"
            with self.assertRaises(urllib.error.HTTPError):
                ProbabilityModel.fetch_artifact(f"{self.base}/missing", path)
            self.assertFalse(path.exists())

    def test_empty_body_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "model.joblib"
            with self.assertRaises(RuntimeError):
                ProbabilityModel.fetch_artifact(f"{self.base}/empty", path)
            self.assertFalse(path.exists())

    def test_oversized_artifact_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "model.joblib"
            with self.assertRaises(RuntimeError):
                ProbabilityModel.fetch_artifact(f"{self.base}/model.joblib", path, max_bytes=50)
            self.assertFalse(path.exists())


class ModelHealthTests(unittest.TestCase):
    def test_absent_model_reports_not_loaded(self) -> None:
        summary = web.summarize_model({"loaded": False, "source": "absent", "secret": "x"})
        assert summary is not None
        self.assertFalse(summary["loaded"])
        self.assertEqual(summary["source"], "absent")
        self.assertNotIn("secret", summary)

    def test_cannot_see_model_is_none_not_false(self) -> None:
        self.assertIsNone(web.summarize_model(None))

    def test_health_reports_the_model_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = web.build_health(
                Path(tmp) / "missing.db",
                {"loaded": True, "backend": "light", "trained_at": "2026-01-01T00:00:00+00:00"},
            )
        self.assertTrue(payload["model"]["loaded"])
        self.assertEqual(payload["model"]["backend"], "light")
        # A model decision must never change the liveness contract.
        self.assertTrue(payload["ok"])


if __name__ == "__main__":
    unittest.main()
