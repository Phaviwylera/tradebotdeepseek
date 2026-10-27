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
from datetime import datetime, timedelta, timezone
from typing import Any

from fees import from_gamma

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
    # Taker fee this market charges (Gamma's feeSchedule). Needed because a
    # 1-cent edge at 0.94 is 28% fee at the sports rate.
    fee_rate: float = 0.0
    fee_exponent: int = 1

    def fee_per_share(self, price: float) -> float:
        from fees import FeeSchedule

        return FeeSchedule(rate=self.fee_rate, exponent=self.fee_exponent).per_share(price)

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
        near_window_hours: float = 6.0,
        near_overdue_hours: float = 24.0,
        near_min_volume: float = 0.0,
        near_min_liquidity: float = 0.0,
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
        # Near-resolution discovery is its own lane on purpose: those markets are
        # small, settle within hours, and are never in the top-volume set the
        # arbitrage lane scans. Filtering them with the volume floor above is what
        # used to make the SNIPE strategy unreachable in practice.
        self.near_window_hours = near_window_hours
        self.near_overdue_hours = near_overdue_hours
        self.near_min_volume = near_min_volume
        self.near_min_liquidity = near_min_liquidity

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

    async def scan_near_resolution(
        self,
        *,
        now: datetime | None = None,
        window_hours: float | None = None,
        limit: int = 200,
    ) -> list[Market]:
        """Discover markets that settle within hours, ignoring the volume floor.

        Gamma's ``endDate`` is the scheduled close, which for sports markets is
        the event start: a football market is still tradeable while the match is
        in play, so the window deliberately starts ``near_overdue_hours`` in the
        *past*. Without that, in-play markets — where prices actually reach the
        snipe band — were invisible.
        """
        current = now or utc_now()
        window = self.near_window_hours if window_hours is None else window_hours
        start = current - timedelta(hours=self.near_overdue_hours)
        end = current + timedelta(hours=window)
        params = {
            "active": "true",
            "closed": "false",
            "limit": str(max(1, limit)),
            "order": "endDate",
            "ascending": "true",
            "end_date_min": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_date_max": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        http = self._get_http()
        payload = await self._get_json(http, "/markets", params)
        if isinstance(payload, Mapping):
            payload = payload.get("data", payload.get("markets", []))
        if not isinstance(payload, Sequence) or isinstance(payload, str | bytes):
            raise TypeError("Unexpected Gamma markets response")

        markets: list[Market] = []
        seen: set[str] = set()
        for raw in payload:
            if not isinstance(raw, Mapping):
                continue
            market = self._parse_market(
                raw, min_volume=self.near_min_volume, min_liquidity=self.near_min_liquidity
            )
            if market is None or market.condition_id in seen:
                continue
            seen.add(market.condition_id)
            markets.append(market)

        logger.info(
            "Near-resolution lane: %d markets closing within %.1fh (%.0fh overdue allowed)",
            len(markets),
            window,
            self.near_overdue_hours,
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

    def _parse_market(
        self,
        raw: Mapping[str, Any],
        *,
        min_volume: float | None = None,
        min_liquidity: float | None = None,
    ) -> Market | None:
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
        floor_volume = self.min_volume if min_volume is None else min_volume
        floor_liquidity = self.min_liquidity if min_liquidity is None else min_liquidity
        if volume <= floor_volume or liquidity <= floor_liquidity:
            return None

        end_date = parse_datetime(
            raw.get("endDate") or raw.get("endDateIso") or raw.get("end_date")
        )
        question = str(raw.get("question") or raw.get("title") or condition_id)

        schedule = from_gamma(raw)
        return Market(
            condition_id=condition_id,
            question=question,
            token_yes=token_yes,
            token_no=token_no,
            volume=volume,
            liquidity=liquidity,
            end_date=end_date,
            fee_rate=schedule.effective_rate,
            fee_exponent=schedule.exponent,
        )


def select_markets(
    volume_lane: Sequence[Market],
    near_lane: Sequence[Market],
    *,
    max_total: int,
    near_slots: int,
    now: datetime | None = None,
) -> list[Market]:
    """Reserve book-fetch slots for near-resolution markets, then fill by volume.

    Root cause of "the bot never signals": the snipe strategy only fires inside
    the final hours before a market closes, but the pipeline evaluated the top
    markets *by volume* — a set that essentially never contains them. Reserving
    slots makes the strategy reachable without throwing away the liquid markets
    the arbitrage check needs.
    """
    current = now or utc_now()
    chosen: list[Market] = []
    seen: set[str] = set()

    def ttl(market: Market) -> float:
        value = market.ttl_days(current)
        return float("inf") if value is None else value

    budget_near = max(0, min(near_slots, max_total))
    for market in sorted(near_lane, key=ttl):
        if len(chosen) >= budget_near:
            break
        if market.condition_id in seen:
            continue
        seen.add(market.condition_id)
        chosen.append(market)

    for market in volume_lane:
        if len(chosen) >= max_total:
            break
        if market.condition_id in seen:
            continue
        seen.add(market.condition_id)
        chosen.append(market)

    return chosen


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
