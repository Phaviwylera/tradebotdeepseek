"""Public Polymarket market-channel WebSocket client.

The callback contract is intentionally small and transport-neutral:

    on_update(asset_id, bids, asks)

``bids`` and ``asks`` are best-first lists of ``(price, size)`` float tuples.
The stream is read-only: private user channels are never subscribed here.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

UpdateCallback = Callable[
    [str, list[tuple[float, float]], list[tuple[float, float]]],
    Awaitable[None] | None,
]


class BookStream:
    def __init__(
        self,
        asset_ids: Iterable[str],
        on_update: UpdateCallback,
        *,
        url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        reconnect_backoff_seconds: float = 2.0,
        ping_interval_seconds: float = 20.0,
    ) -> None:
        self.url = url
        self.on_update = on_update
        self.reconnect_backoff_seconds = reconnect_backoff_seconds
        self.ping_interval_seconds = ping_interval_seconds
        self._assets: set[str] = set(asset_ids)
        self._sent_assets: set[str] = set()
        self._stop = asyncio.Event()
        self._subscription_changed = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.connected = False

    @property
    def assets(self) -> set[str]:
        return set(self._assets)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._run(), name="polymarket-book-stream"
            )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.debug("Book stream ended with an error", exc_info=True)
            finally:
                self._task = None

    def set_assets(self, asset_ids: Iterable[str]) -> None:
        new_assets = {str(asset_id) for asset_id in asset_ids if asset_id}
        if new_assets != self._assets:
            self._assets = new_assets
            self._subscription_changed.set()

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    self.url,
                    ping_interval=self.ping_interval_seconds,
                    ping_timeout=self.ping_interval_seconds,
                    close_timeout=5,
                    open_timeout=20,
                    max_size=2**23,
                ) as websocket:
                    self.connected = True
                    self._sent_assets = set()
                    logger.info("Market WebSocket connected (%s)", self.url)
                    await self._sync_subscriptions(websocket, initial=True)

                    while not self._stop.is_set():
                        if self._subscription_changed.is_set():
                            await self._sync_subscriptions(websocket)
                        try:
                            raw = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                        except asyncio.TimeoutError:
                            continue
                        await self._handle_message(raw)

            except ConnectionClosed as exc:
                self.connected = False
                if not self._stop.is_set():
                    logger.warning(
                        "Market WebSocket closed (%s); reconnecting in %.1fs",
                        exc,
                        self.reconnect_backoff_seconds,
                    )
                    await asyncio.sleep(self.reconnect_backoff_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on transport errors
                self.connected = False
                if not self._stop.is_set():
                    logger.warning(
                        "Market WebSocket error (%s); reconnecting in %.1fs",
                        exc,
                        self.reconnect_backoff_seconds,
                    )
                    await asyncio.sleep(self.reconnect_backoff_seconds)
            finally:
                self.connected = False

    async def _sync_subscriptions(self, websocket: Any, *, initial: bool = False) -> None:
        self._subscription_changed.clear()
        if not self._assets:
            self._sent_assets.clear()
            return

        if initial or not self._sent_assets:
            payload: dict[str, Any] = {
                "assets_ids": sorted(self._assets),
                "type": "market",
            }
            await websocket.send(json.dumps(payload))
            self._sent_assets = set(self._assets)
            return

        added = sorted(self._assets - self._sent_assets)
        removed = sorted(self._sent_assets - self._assets)
        try:
            if added:
                await websocket.send(
                    json.dumps({"assets_ids": added, "operation": "subscribe"})
                )
            if removed:
                await websocket.send(
                    json.dumps({"assets_ids": removed, "operation": "unsubscribe"})
                )
            self._sent_assets = set(self._assets)
        except ConnectionClosed:
            raise
        except Exception as exc:  # noqa: BLE001 - retry subscription after reconnect
            logger.debug("WebSocket subscription update failed: %s", exc)
            self._subscription_changed.set()

    async def _handle_message(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if raw == "PONG":
            return
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("Ignoring non-JSON WebSocket message")
            return

        events = payload if isinstance(payload, list) else [payload]
        for event in events:
            if not isinstance(event, dict):
                continue
            event_type = event.get("event_type") or event.get("type")
            if event_type not in (None, "book"):
                continue
            asset_id = event.get("asset_id")
            if not asset_id:
                continue
            bids = _normalize(event.get("bids") or [], reverse=True)
            asks = _normalize(event.get("asks") or [], reverse=False)
            try:
                result = self.on_update(str(asset_id), bids, asks)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("Order-book update callback failed for %s", asset_id)


def _normalize(
    levels: Iterable[Any], *, reverse: bool
) -> list[tuple[float, float]]:
    aggregated: dict[float, float] = {}
    for level in levels:
        if isinstance(level, dict):
            price_raw, size_raw = level.get("price"), level.get("size")
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            price_raw, size_raw = level[0], level[1]
        else:
            price_raw = getattr(level, "price", None)
            size_raw = getattr(level, "size", None)
        try:
            price = float(price_raw)
            size = float(size_raw)
        except (TypeError, ValueError):
            continue
        if not 0.0 < price <= 1.0 or size <= 0.0:
            continue
        key = round(price, 6)
        aggregated[key] = aggregated.get(key, 0.0) + size
    return sorted(aggregated.items(), reverse=reverse)
