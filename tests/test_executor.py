"""Executor safety tests: DRY-RUN must never reach the network."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from executor import ExecutionResult, OrderExecutor


class DryRunExecutorTests(unittest.TestCase):
    def test_dry_run_initialization_requests_no_credentials(self) -> None:
        executor = OrderExecutor(live=False)
        with mock.patch.dict(os.environ, {}, clear=True):
            executor.initialize()
        self.assertFalse(executor.initialized)
        self.assertIsNone(executor.client)

    def test_dry_run_refuses_to_place_orders(self) -> None:
        executor = OrderExecutor(live=False)
        with self.assertRaises(RuntimeError):
            executor.place_limit_order("token", 0.5, 10.0, "BUY")

    def test_cancellation_requires_initialization(self) -> None:
        executor = OrderExecutor(live=False)
        with self.assertRaises(RuntimeError):
            executor.cancel("order-1")
        with self.assertRaises(RuntimeError):
            executor.cancel_all()


class LiveGuardTests(unittest.TestCase):
    def test_live_without_private_key_fails_fast(self) -> None:
        executor = OrderExecutor(live=True)
        with (
            mock.patch.dict(os.environ, {"POLYGON_PRIVATE_KEY": "   "}, clear=True),
            self.assertRaises(RuntimeError) as context,
        ):
            executor.initialize()
        self.assertIn("POLYGON_PRIVATE_KEY", str(context.exception))

    def test_blank_private_key_is_never_treated_as_present(self) -> None:
        executor = OrderExecutor(live=True)
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            self.assertRaises(RuntimeError),
        ):
            executor.initialize()

    def test_uninitialized_live_executor_refuses_orders(self) -> None:
        executor = OrderExecutor(live=True)
        with self.assertRaises(RuntimeError):
            executor.place_limit_order("token", 0.5, 10.0, "BUY")


class ExecutionResultTests(unittest.TestCase):
    def test_accepted_requires_an_order_id(self) -> None:
        self.assertFalse(ExecutionResult(None, "submitted", 10.0, {}).accepted)
        self.assertFalse(ExecutionResult("id", "rejected", 10.0, {}).accepted)
        self.assertFalse(ExecutionResult("id", "unmatched", 10.0, {}).accepted)
        self.assertTrue(ExecutionResult("id", "submitted", 10.0, {}).accepted)
        self.assertTrue(ExecutionResult("id", "LIVE", 10.0, {}).accepted)


if __name__ == "__main__":
    unittest.main()
