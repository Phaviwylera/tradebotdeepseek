"""Market discovery plus normalized order-book types.

Gamma is used for discovery. CLOB is used for REST order-book snapshots. The
WebSocket stream produces the same ``OrderBook`` shape so strategies never
need to care which transport supplied the data.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        # Gamma sometimes uses a date without a timezone.
        try:
            parsed = datetime.strptime(text[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class Market:
    condition_id: str
    question: str
    token_yes: str
    token_no: str
    volume: float
    liquidity: float
    end_date: datetime | None

    def ttl_days(self, now: datetime | None = None) -> float | None:
        if self.end_date is None:
            return None
        current = now or utc_now()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return (self.end_date - current).total_seconds() / 86400.0


@dataclass(frozen=True, slots=True)
class Level:
    price: float
    size: float


@dataclass(frozen=True, slots=True)
class OrderBook:
    token_id: str
    bids: tuple[Level, ...] = field(default_factory=tuple)
    asks: tuple[Level, ...] = field(default_factory=tuple)
    last_trade_price: float | None = None
    timestamp: datetime | None = None

    @classmethod
    def from_raw(
        cls,
        token_id: str,
        bids: Iterable[Any],
        asks: Iterable[Any],
        *,
        last_trade_price: Any = None,
        timestamp: Any = None,
    ) -> OrderBook:
        return cls(
            token_id=token_id,
            bids=_normalize_levels(bids, reverse=True),
            asks=_normalize_levels(asks, reverse=False),
            last_trade_price=_optional_float(last_trade_price),
            timestamp=parse_datetime(timestamp),
        )

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid_price(self) -> float | None:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2.0
        return self.last_trade_price

    @property
    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return max(0.0, self.best_ask - self.best_bid)

    def depth(self, side: str, levels: int = 5) -> float:
        book = self.bids if side.upper() == "BID" else self.asks
        return sum(level.size for level in book[:levels])

    def is_tradable(self) -> bool:
        return self.best_bid is not None and self.best_ask is not None


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _level_parts(level: Any) -> tuple[Any, Any] | None:
    if isinstance(level, Level):
        return level.price, level.size
    if isinstance(level, Mapping):
        return level.get("price"), level.get("size")
    if isinstance(level, tuple | list) and len(level) >= 2:
        return level[0], level[1]
    price = getattr(level, "price", None)
    size = getattr(level, "size", None)
    if price is not None and size is not None:
        return price, size
    return None


def _normalize_levels(levels: Iterable[Any] | None, *, reverse: bool) -> tuple[Level, ...]:
    aggregated: dict[float, float] = {}
    for raw in levels or ():
        parts = _level_parts(raw)
        if parts is None:
            continue
        price = _optional_float(parts[0])
        size = _optional_float(parts[1])
        if price is None or size is None:
            continue
        if not 0.0 < price <= 1.0 or size <= 0.0:
            continue
        key = round(price, 6)
        aggregated[key] = aggregated.get(key, 0.0) + size
    ordered = sorted(aggregated.items(), reverse=reverse)
    return tuple(Level(price=price, size=size) for price, size in ordered)


class MarketScanner:
    def __init__(
        self,
        gamma_api_url: str,
        clob_host: str,
        *,
        clob_client: Any | None = None,
        http_client: Any | None = None,
        page_size: int = 100,
        max_pages: int = 5,
        min_volume: float = 5_000.0,
        min_liquidity: float = 1_000.0,
        timeout_seconds: float = 20.0,
    ) -> None:
        self.gamma_api_url = gamma_api_url.rstrip("/")
        self.clob_host = clob_host.rstrip("/")
        self._clob = clob_client
        self._http = http_client
        self._owns_http = False
        self.page_size = page_size
        self.max_pages = max_pages
        self.min_volume = min_volume
        self.min_liquidity = min_liquidity
        self.timeout_seconds = timeout_seconds

    async def scan(self) -> list[Market]:
        http = self._get_http()
        markets: list[Market] = []
        seen: set[str] = set()

        for page in range(self.max_pages):
            params = {
                "active": "true",
                "closed": "false",
                "limit": str(self.page_size),
                "offset": str(page * self.page_size),
                "order": "volume",
                "ascending": "false",
            }
            payload = await self._get_json(http, "/markets", params)
            if isinstance(payload, Mapping):
                payload = payload.get("data", payload.get("markets", []))
            if not isinstance(payload, Sequence) or isinstance(payload, str | bytes):
                raise TypeError("Unexpected Gamma markets response")

            if not payload:
                break

            for raw in payload:
                if not isinstance(raw, Mapping):
                    continue
                market = self._parse_market(raw)
                if market is None or market.condition_id in seen:
                    continue
                seen.add(market.condition_id)
                markets.append(market)

            if len(payload) < self.page_size:
                break

        markets.sort(key=lambda item: item.volume, reverse=True)
        logger.info(
            "Scanned %d eligible markets (volume>%.0f, liquidity>%.0f)",
            len(markets),
            self.min_volume,
            self.min_liquidity,
        )
        return markets

    async def get_orderbook(self, token_id: str) -> OrderBook:
        client = self._get_clob()
        raw = await asyncio.to_thread(client.get_order_book, token_id)
        if raw is None:
            raise RuntimeError(f"No order book returned for token {token_id}")
        if isinstance(raw, Mapping):
            bids = raw.get("bids", [])
            asks = raw.get("asks", [])
            last = raw.get("last_trade_price")
            timestamp = raw.get("timestamp")
        else:
            bids = getattr(raw, "bids", []) or []
            asks = getattr(raw, "asks", []) or []
            last = getattr(raw, "last_trade_price", None)
            timestamp = getattr(raw, "timestamp", None)
        return OrderBook.from_raw(
            token_id,
            bids,
            asks,
            last_trade_price=last,
            timestamp=timestamp,
        )

    async def close(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    def _get_clob(self) -> Any:
        if self._clob is None:
            from py_clob_client.client import ClobClient

            self._clob = ClobClient(self.clob_host)
        return self._clob

    def _get_http(self) -> Any:
        if self._http is None:
            import httpx

            self._http = httpx.AsyncClient(
                base_url=self.gamma_api_url,
                timeout=self.timeout_seconds,
                headers={"User-Agent": "polymarket-calibrated-bot/1.0"},
            )
            self._owns_http = True
        return self._http

    async def _get_json(self, http: Any, path: str, params: Mapping[str, str]) -> Any:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = await http.get(path, params=params)
                response.raise_for_status()
                return response.json()
            except Exception as exc:  # noqa: BLE001 - retry HTTP/JSON failures
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(1.0 * (2**attempt))
        raise RuntimeError(f"Gamma request failed after retries: {last_error}") from last_error

    def _parse_market(self, raw: Mapping[str, Any]) -> Market | None:
        if _truthy(raw.get("closed")) or not _truthy(raw.get("active", True)):
            return None
        if _truthy(raw.get("archived")):
            return None
        if "acceptingOrders" in raw and not _truthy(raw.get("acceptingOrders")):
            return None
        if "enableOrderBook" in raw and not _truthy(raw.get("enableOrderBook")):
            return None

        condition_id = str(raw.get("conditionId") or "").strip()
        if not condition_id:
            return None

        token_ids = _json_list(raw.get("clobTokenIds"))
        if len(token_ids) != 2 or not all(str(token).strip() for token in token_ids):
            return None

        outcomes = _json_list(raw.get("outcomes"))
        normalized = [str(outcome).strip().lower() for outcome in outcomes]
        if len(normalized) == 2 and "yes" in normalized and "no" in normalized:
            token_yes = str(token_ids[normalized.index("yes")])
            token_no = str(token_ids[normalized.index("no")])
        else:
            token_yes = str(token_ids[0])
            token_no = str(token_ids[1])

        volume = _first_float(raw, "volumeNum", "volume", default=0.0)
        liquidity = _first_float(raw, "liquidityNum", "liquidity", default=0.0)
        if volume <= self.min_volume or liquidity <= self.min_liquidity:
            return None

        end_date = parse_datetime(
            raw.get("endDate") or raw.get("endDateIso") or raw.get("end_date")
        )
        question = str(raw.get("question") or raw.get("title") or condition_id)

        return Market(
            condition_id=condition_id,
            question=question,
            token_yes=token_yes,
            token_no=token_no,
            volume=volume,
            liquidity=liquidity,
            end_date=end_date,
        )


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return bool(value)


def _json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _first_float(raw: Mapping[str, Any], *keys: str, default: float) -> float:
    for key in keys:
        value = raw.get(key)
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return default
