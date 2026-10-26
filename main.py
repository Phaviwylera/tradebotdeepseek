"""Async pipeline entry point for the Render background worker.

Every loop discovers markets, obtains order books (WebSocket cache first, REST
fallback), evaluates all three strategies, enforces risk, and writes signals
and orders to SQLite. DRY-RUN is the default and performs no authenticated
requests or order placement.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import web
from config import Settings
from executor import ExecutionResult, OrderExecutor
from features import build_features
from ledger import Ledger
from model import ProbabilityModel
from risk import RiskManager
from scanner import Market, MarketScanner, OrderBook
from strategy import Signal, StrategyEngine
from stream import BookStream

logger = logging.getLogger(__name__)

MAX_BOOK_CONCURRENCY = 8
BOOK_CACHE_RETENTION_SECONDS = 300


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
        stream=sys.stdout,
        force=True,
    )
    logging.Formatter.converter = time.gmtime
    # These libraries are useful only at debug level in this service.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)


class TradingBot:
    def __init__(
        self,
        settings: Settings,
        scanner: MarketScanner,
        ledger: Ledger,
        strategy: StrategyEngine,
        risk: RiskManager,
        model: ProbabilityModel,
        executor: OrderExecutor,
    ) -> None:
        self.settings = settings
        self.scanner = scanner
        self.ledger = ledger
        self.strategy = strategy
        self.risk = risk
        self.model = model
        self.executor = executor

        self._books: dict[str, tuple[float, OrderBook]] = {}
        self._book_semaphore = asyncio.Semaphore(MAX_BOOK_CONCURRENCY)
        self._stop = asyncio.Event()
        self._shutdown_done = False
        self._status_server: web.StatusHTTPServer | None = None
        self._status_thread: threading.Thread | None = None
        self.stream = BookStream(
            [],
            self._on_book_update,
            url=settings.ws_market_url,
            reconnect_backoff_seconds=settings.stream_backoff_seconds,
        )

    async def start(self) -> None:
        logger.info("Starting trading bot: %s", self.settings.public_summary())
        if self.settings.live:
            logger.warning(
                "LIVE MODE ENABLED: real GTC orders will be submitted. "
                "Bankroll=$%.2f max_bet=$%.2f",
                self.settings.bankroll,
                self.settings.max_bet,
            )
        else:
            logger.warning("DRY-RUN MODE: no authenticated CLOB requests or orders")
        # Without this, an idle console looks like a fault. The gates explain a
        # quiet bot: both remaining strategies are deliberately rare.
        logger.info("Signal gates: %s", self.strategy.describe_gates())

        await asyncio.to_thread(self.executor.initialize)
        self.stream.start()
        self.ledger.record_start(mode="live" if self.settings.live else "dry_run")
        self._start_status_server()

        since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
        logger.info(
            "Ledger %s ready; %d signals in the last 24h",
            self.ledger.path,
            self.ledger.recent_signal_count(since),
        )

    def stop(self) -> None:
        logger.info("Shutdown requested")
        self._stop.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            cycle_started = time.monotonic()
            try:
                await self.run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(
                    "Trading cycle failed; sleeping %ds before retry",
                    self.settings.error_sleep_seconds,
                )
                # Publish the reason, not "see logs": /healthz is public, and it
                # is the only place a user without the token can see why the
                # bot stopped producing signals.
                await asyncio.to_thread(
                    self.ledger.record_error,
                    f"trading cycle failed: {type(exc).__name__}: {exc}",
                )
                await self._wait_or_stop(self.settings.error_sleep_seconds)
                continue

            elapsed = time.monotonic() - cycle_started
            delay = max(1.0, self.settings.scan_interval_seconds - elapsed)
            await self._wait_or_stop(delay)

    async def shutdown(self) -> None:
        if self._shutdown_done:
            return
        self._shutdown_done = True
        logger.info("Shutting down")
        if self.settings.live and self.settings.cancel_on_shutdown and self.executor.initialized:
            try:
                await asyncio.to_thread(self.executor.cancel_all)
            except Exception:
                logger.exception("Failed to cancel open orders during shutdown")
        await self.stream.stop()
        await self.scanner.close()
        if self._status_server is not None:
            await asyncio.to_thread(
                web.stop_status_server, self._status_server, self._status_thread
            )
            self._status_server = None
            self._status_thread = None
        self.ledger.close()
        logger.info("Shutdown complete")

    def _start_status_server(self) -> None:
        """Serve the read-only API and dashboard without blocking the loop."""
        if not self.settings.web_enabled:
            logger.info("Status server disabled (WEB_ENABLED=false)")
            return
        try:
            self._status_server, self._status_thread = web.start_status_server(
                self.ledger.path,
                host=self.settings.api_host or None,
                port=self.settings.api_port or None,
                serve_dashboard=self.settings.serve_dashboard,
            )
        except OSError:
            # A busy port must never stop the trading loop.
            logger.exception("Status server could not bind; trading continues without HTTP status")

    async def run_cycle(self) -> None:
        markets = await self.scanner.scan()
        markets = markets[: self.settings.max_markets_per_cycle]
        self.stream.set_assets(
            token for market in markets for token in (market.token_yes, market.token_no)
        )
        self._prune_book_cache()

        fetched = await asyncio.gather(
            *(self._fetch_books(market) for market in markets),
            return_exceptions=True,
        )

        evaluated = 0
        generated = 0
        book_failures: list[BaseException] = []
        for market, result in zip(markets, fetched, strict=False):
            if isinstance(result, BaseException):
                book_failures.append(result)
                logger.warning("Skipping market %s: %s", market.condition_id[:12], result)
                continue
            yes_book, no_book = result
            if yes_book is None or no_book is None:
                continue

            evaluated += 1
            try:
                generated += await self._evaluate_market(market, yes_book, no_book)
            except Exception:
                logger.exception(
                    "Strategy evaluation failed for market %s",
                    market.condition_id,
                )

        risk_status = self.risk.status()
        logger.info(
            "Cycle complete: markets=%d books=%d new_signals=%d risk=%s",
            len(markets),
            evaluated,
            generated,
            risk_status,
        )
        try:
            self.ledger.record_cycle(
                markets_scanned=len(markets),
                markets_evaluated=evaluated,
                signals_emitted=generated,
                risk=risk_status,
            )
        except Exception:
            logger.exception("Could not publish runtime status")

        if markets and evaluated == 0 and book_failures:
            # Every book fetch failed: the market data source is unreachable
            # (blocked region, API change, DNS). Nothing will ever trade in this
            # state, so say so where a token-less /healthz reader can see it.
            first = book_failures[0]
            try:
                await asyncio.to_thread(
                    self.ledger.record_error,
                    f"all {len(book_failures)}/{len(markets)} order-book fetches failed: "
                    f"{type(first).__name__}: {first}",
                )
            except Exception:
                logger.exception("Could not publish book-fetch failure")

    async def _evaluate_market(
        self, market: Market, yes_book: OrderBook, no_book: OrderBook
    ) -> int:
        generated = 0

        arbitrage = self.strategy.check_arbitrage(market, yes_book, no_book)
        if arbitrage:
            generated += await self._handle_arbitrage(arbitrage)

        for snipe_signal in self.strategy.check_snipe(market, yes_book, no_book):
            generated += await self._handle_signal(snipe_signal, "SNIPE")

        if self.model.loaded:
            signal = self._model_signal(market, yes_book, no_book)
            if signal is not None:
                generated += await self._handle_signal(signal, "MODEL")

        return generated

    def _model_signal(
        self, market: Market, yes_book: OrderBook, no_book: OrderBook
    ) -> Signal | None:
        market_probability = yes_book.mid_price
        if market_probability is None or not 0.0 < market_probability < 1.0:
            return None

        try:
            features = build_features(market, yes_book, news_sentiment=self.settings.news_sentiment)
            raw_probability = self.model.predict_proba(features)
        except Exception:
            logger.exception("Model inference failed for market %s", market.condition_id)
            return None

        if not math.isfinite(raw_probability):
            return None

        anchored = self.model.bayesian_anchor(
            raw_probability, market_probability, prior_strength=0.3
        )
        logger.debug(
            "Model %s raw=%.4f market=%.4f anchored=%.4f",
            market.condition_id[:12],
            raw_probability,
            market_probability,
            anchored,
        )
        return self.strategy.check_model_edge(
            market,
            yes_book,
            no_book,
            anchored_prob=anchored,
            bankroll=self.risk.equity,
        )

    async def _handle_signal(self, signal: Signal, prefix: str) -> int:
        if self._is_cooling_down(signal.market_id, prefix):
            logger.debug("Cooldown active for %s %s", prefix, signal.market_id)
            return 0

        can_trade, reason = self.risk.can_trade(signal.size_usd)
        if not can_trade:
            self.ledger.record_signal(
                market_id=signal.market_id,
                token_id=signal.token_id,
                side=signal.side,
                price=signal.price,
                size_usd=signal.size_usd,
                reason=f"{signal.reason} | RISK_BLOCKED {reason}",
                expected_value=signal.expected_value,
                executed=False,
            )
            logger.warning(
                "Risk blocked %s %s: %s",
                prefix,
                signal.market_id[:12],
                reason,
            )
            return 1

        if not self.settings.live:
            self.ledger.record_signal(
                market_id=signal.market_id,
                token_id=signal.token_id,
                side=signal.side,
                price=signal.price,
                size_usd=signal.size_usd,
                reason=f"{signal.reason} | DRY_RUN",
                expected_value=signal.expected_value,
                executed=False,
            )
            logger.info(
                "DRY RUN signal %s token=%s price=%.4f size=$%.2f ev=$%.2f " "reason=%s",
                prefix,
                signal.token_id[:12],
                signal.price,
                signal.size_usd,
                signal.expected_value,
                signal.reason,
            )
            return 1

        signal_id = self.ledger.record_signal(
            market_id=signal.market_id,
            token_id=signal.token_id,
            side=signal.side,
            price=signal.price,
            size_usd=signal.size_usd,
            reason=signal.reason,
            expected_value=signal.expected_value,
            executed=False,
        )
        result = await self._submit(signal)
        if result is not None and result.accepted:
            self._record_accepted(signal_id, signal, result)
        elif result is not None:
            logger.warning(
                "Order not accepted token=%s status=%s",
                signal.token_id[:12],
                result.status,
            )
        return 1

    async def _handle_arbitrage(self, signals: list[Signal]) -> int:
        if len(signals) != 2:
            logger.error("Arbitrage pair must contain exactly two legs")
            return 0
        if self._is_cooling_down(signals[0].market_id, "ARB"):
            logger.debug("Cooldown active for ARB %s", signals[0].market_id)
            return 0

        total_cost = sum(signal.size_usd for signal in signals)
        can_trade, reason = self.risk.can_trade(total_cost)

        signal_ids = [
            self.ledger.record_signal(
                market_id=signal.market_id,
                token_id=signal.token_id,
                side=signal.side,
                price=signal.price,
                size_usd=signal.size_usd,
                reason=(signal.reason if can_trade else f"{signal.reason} | RISK_BLOCKED {reason}"),
                expected_value=signal.expected_value,
                executed=False,
            )
            for signal in signals
        ]

        if not can_trade:
            logger.warning(
                "Risk blocked ARB %s total=$%.2f: %s",
                signals[0].market_id[:12],
                total_cost,
                reason,
            )
            return 2

        if not self.settings.live:
            logger.info(
                "DRY RUN signal ARB %s total_cost=$%.2f profit=$%.2f reason=%s",
                signals[0].market_id[:12],
                total_cost,
                sum(signal.expected_value for signal in signals),
                signals[0].reason,
            )
            return 2

        first, second = signals
        first_result = await self._submit(first)
        if first_result is None or not first_result.accepted:
            logger.error(
                "ARB first leg failed for %s; no second leg submitted",
                first.market_id[:12],
            )
            return 2

        second_result = await self._submit(second)
        if second_result is None or not second_result.accepted:
            logger.critical(
                "ARB UNHEDGED: second leg failed for %s; cancelling first order %s",
                second.market_id[:12],
                first_result.order_id,
            )
            if first_result.order_id:
                try:
                    await asyncio.to_thread(self.executor.cancel, first_result.order_id)
                except Exception:
                    logger.exception("Failed to cancel unhedged ARB leg")
            # Conservatively book the accepted first leg even if cancellation
            # succeeds before a fill.
            self._record_accepted(signal_ids[0], first, first_result)
            return 2

        self._record_accepted(signal_ids[0], first, first_result)
        self._record_accepted(signal_ids[1], second, second_result)
        logger.info(
            "ARB filled/resting both legs for %s profit_target=$%.2f",
            first.market_id[:12],
            sum(signal.expected_value for signal in signals),
        )
        return 2

    async def _submit(self, signal: Signal) -> ExecutionResult | None:
        try:
            result = await asyncio.to_thread(
                self.executor.place_limit_order,
                signal.token_id,
                signal.price,
                signal.size_usd,
                signal.side,
            )
        except Exception as exc:
            self.ledger.record_order(
                order_id=None,
                token_id=signal.token_id,
                side=signal.side,
                price=signal.price,
                size_shares=round(signal.size_usd / signal.price, 2),
                status="EXCEPTION",
                response={"error": type(exc).__name__, "detail": str(exc)},
            )
            logger.exception("Order submission failed token=%s", signal.token_id[:12])
            return None

        self.ledger.record_order(
            order_id=result.order_id,
            token_id=signal.token_id,
            side=signal.side,
            price=signal.price,
            size_shares=result.size_shares,
            status=result.status,
            response=result.response,
        )
        return result

    def _record_accepted(self, signal_id: int, signal: Signal, result: ExecutionResult) -> None:
        self.ledger.mark_signal_executed(signal_id, True)
        try:
            self.risk.record_fill(
                signal.token_id,
                signal.price,
                result.size_shares,
                signal.size_usd,
            )
        except Exception:
            logger.exception(
                "Accepted order could not be booked into risk ledger token=%s",
                signal.token_id[:12],
            )

    async def _fetch_books(self, market: Market) -> tuple[OrderBook | None, OrderBook | None]:
        yes_book, no_book = await asyncio.gather(
            self._get_book(market.token_yes),
            self._get_book(market.token_no),
        )
        return yes_book, no_book

    async def _get_book(self, token_id: str) -> OrderBook | None:
        cached = self._books.get(token_id)
        if cached is not None:
            updated_at, book = cached
            age = time.monotonic() - updated_at
            if age <= self.settings.book_staleness_seconds and book.is_tradable():
                return book

        async with self._book_semaphore:
            try:
                book = await self.scanner.get_orderbook(token_id)
            except Exception as exc:  # noqa: BLE001 - one fetch must not stop the cycle
                logger.warning("Order book fetch failed for %s: %s", token_id[:12], exc)
                return None
        if not book.is_tradable():
            return None
        self._books[token_id] = (time.monotonic(), book)
        return book

    async def _on_book_update(
        self,
        asset_id: str,
        bids: list[tuple[float, float]],
        asks: list[tuple[float, float]],
    ) -> None:
        book = OrderBook.from_raw(asset_id, bids, asks)
        if book.is_tradable():
            self._books[asset_id] = (time.monotonic(), book)

    async def _wait_or_stop(self, seconds: float) -> None:
        # A timeout simply means "keep going"; a set event means shutdown.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    def _is_cooling_down(self, market_id: str, reason_prefix: str) -> bool:
        cooldown = self.settings.signal_cooldown_seconds
        if cooldown <= 0:
            return False
        since = (datetime.now(timezone.utc) - timedelta(seconds=cooldown)).isoformat(
            timespec="milliseconds"
        )
        return self.ledger.has_recent_market_signal(
            market_id=market_id,
            reason_prefix=reason_prefix,
            since_iso=since,
        )

    def _prune_book_cache(self) -> None:
        cutoff = time.monotonic() - BOOK_CACHE_RETENTION_SECONDS
        stale = [
            token_id for token_id, (updated_at, _) in self._books.items() if updated_at < cutoff
        ]
        for token_id in stale:
            self._books.pop(token_id, None)


def _load_model(settings: Settings) -> ProbabilityModel:
    model = ProbabilityModel()
    try:
        if model.load(settings.model_path):
            logger.info("Calibrated model enabled")
        else:
            logger.warning(
                "No model at %s; running ARBITRAGE + SNIPE strategies only",
                settings.model_path,
            )
    except Exception:
        logger.exception(
            "Failed to load model at %s; running ARBITRAGE + SNIPE only",
            settings.model_path,
        )
    return model


async def async_main() -> None:
    settings = Settings.from_env()
    configure_logging(settings.log_level)

    model = _load_model(settings)
    ledger = Ledger(settings.ledger_path)
    scanner = MarketScanner(
        settings.gamma_api_url,
        settings.clob_host,
        page_size=settings.scan_page_size,
        max_pages=settings.scan_max_pages,
    )
    strategy = StrategyEngine(
        min_ev=settings.min_ev,
        max_bet=settings.max_bet,
    )
    risk = RiskManager(
        bankroll=settings.bankroll,
        max_daily_loss_pct=settings.max_daily_loss_pct,
        max_drawdown_pct=settings.max_drawdown_pct,
        max_exposure_pct=settings.max_exposure_pct,
    )
    executor = OrderExecutor(
        live=settings.live,
        host=settings.clob_host,
        signature_type=settings.signature_type,
        funder=settings.funder,
    )
    bot = TradingBot(
        settings=settings,
        scanner=scanner,
        ledger=ledger,
        strategy=strategy,
        risk=risk,
        model=model,
        executor=executor,
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Windows and some embedded loops do not support signal handlers.
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, bot.stop)

    try:
        await bot.start()
        await bot.run()
    finally:
        await bot.shutdown()


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        logger.info("Interrupted")
    except Exception:
        logger.exception("Fatal startup/runtime error")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
