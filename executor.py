"""Authenticated CLOB execution boundary.

The private key is read from ``POLYGON_PRIVATE_KEY`` only. It is never written
to logs, SQLite, or source code. In DRY-RUN the executor is never initialized
and no authenticated request is made.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    order_id: str | None
    status: str
    size_shares: float
    response: Any

    @property
    def accepted(self) -> bool:
        if not self.order_id:
            return False
        status = self.status.lower()
        return status not in {"error", "failed", "rejected", "unmatched"}


class OrderExecutor:
    def __init__(
        self,
        *,
        live: bool,
        host: str = "https://clob.polymarket.com",
        chain_id: int = 137,
        signature_type: int = 0,
        funder: str = "",
    ) -> None:
        self.live = live
        self.host = host
        self.chain_id = chain_id
        self.signature_type = signature_type
        self.funder = funder
        self.client: Any | None = None
        self.initialized = False

    def initialize(self) -> None:
        """Authenticate and install L2 API credentials. Live mode only."""
        if not self.live:
            logger.info("Executor in DRY-RUN mode; no API credentials requested")
            return

        private_key = os.getenv("POLYGON_PRIVATE_KEY", "").strip()
        if not private_key:
            raise RuntimeError("LIVE=true requires POLYGON_PRIVATE_KEY in the environment")

        from py_clob_client.client import ClobClient

        kwargs: dict[str, Any] = {
            "key": private_key,
            "chain_id": self.chain_id,
            "signature_type": self.signature_type,
        }
        if self.funder:
            kwargs["funder"] = self.funder
        client = ClobClient(self.host, **kwargs)

        credentials = client.create_or_derive_api_creds()
        if credentials is None:
            raise RuntimeError(
                "Could not derive/create CLOB API credentials; check wallet and network"
            )
        client.set_api_creds(credentials)
        self.client = client
        self.initialized = True
        logger.info(
            "CLOB executor authenticated (signature_type=%d, funder_configured=%s)",
            self.signature_type,
            bool(self.funder),
        )

    def place_limit_order(
        self,
        token_id: str,
        price: float,
        size_usd: float,
        side: str,
    ) -> ExecutionResult:
        if not self.live:
            raise RuntimeError("Refusing order placement: DRY-RUN mode")
        if not self.initialized or self.client is None:
            raise RuntimeError("Executor is not initialized")
        if not 0.0 < price < 1.0:
            raise ValueError("price must be in (0, 1)")
        if size_usd <= 0:
            raise ValueError("size_usd must be positive")

        side = side.upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")

        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL

        size_shares = round(size_usd / price, 2)
        if size_shares <= 0:
            raise ValueError("order size rounds to zero shares")

        order_args = OrderArgs(
            token_id=token_id,
            price=round(float(price), 4),
            size=size_shares,
            side=BUY if side == "BUY" else SELL,
        )
        signed_order = self.client.create_order(order_args)
        response = self.client.post_order(signed_order, OrderType.GTC)

        order_id = None
        status = "submitted"
        if isinstance(response, dict):
            order_id = (
                response.get("orderID")
                or response.get("orderId")
                or response.get("order_id")
                or response.get("id")
            )
            if not order_id and isinstance(response.get("data"), dict):
                order_id = response["data"].get("orderID") or response["data"].get("id")
            status = str(
                response.get("status")
                or response.get("errorMsg")
                or response.get("error")
                or ("submitted" if order_id else "error")
            )
        elif response is not None:
            status = "submitted"

        logger.info(
            "Order response token=%s side=%s price=%.4f shares=%.2f order_id=%s status=%s",
            token_id,
            side,
            price,
            size_shares,
            order_id,
            status,
        )
        return ExecutionResult(
            order_id=str(order_id) if order_id else None,
            status=status,
            size_shares=size_shares,
            response=response,
        )

    def cancel(self, order_id: str) -> Any:
        if not self.live or not self.initialized or self.client is None:
            raise RuntimeError("Executor is not initialized for cancellation")
        return self.client.cancel(order_id)

    def cancel_all(self) -> Any:
        if not self.live or not self.initialized or self.client is None:
            raise RuntimeError("Executor is not initialized for cancellation")
        logger.warning("Cancelling all open CLOB orders")
        return self.client.cancel_all()
