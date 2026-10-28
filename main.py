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
from typing import Any

import web
from config import Settings
from evidence import EvidenceGate, load_evidence
from executor import ExecutionResult, OrderExecutor
from features import build_features
from ledger import Ledger
from model import ProbabilityModel
from negrisk import BasketLeg, evaluate_basket, plan_basket
from resolution import fallback_is_ambiguous
from risk import RiskManager
from scanner import Market, MarketScanner, OrderBook, select_markets
from snapshots import SnapshotRecorder
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
        self.model_info: dict[str, Any] = {}
        # What the SNIPE hold assumption is actually based on (or not).
        self.evidence: dict[str, Any] = {}
        # Round-robin cursor for the NegRisk lane: the per-cycle event budget is
        # small (free host), so coverage has to rotate rather than starve.
        self._negrisk_cursor = 0
        # Published on /healthz so "no signals" can be explained per cycle
        # instead of guessed at from a token-protected table.
        self.diagnostics: dict[str, Any] = {
            "near_resolution_candidates": 0,
            "near_resolution_selected": 0,
            "markets_without_book": 0,
            "markets_in_snipe_band": 0,
            "closest_snipe_price": None,
            "best_arb_sum": None,
            "closest_ttl_hours": None,
            # NegRisk basket arbitrage: the one structural edge that needs no
            # forecast, only a complete basket priced under 1.00 after fees.
            "negrisk_groups": 0,
            "negrisk_checked": 0,
            "negrisk_incomplete": 0,
            "negrisk_best_cost": None,
            "negrisk_best_profit": None,
            # Published so the console can quote the *live* band instead of a
            # number baked into the page.
            "snipe_band": None,
            "snipe_band_5pct": None,
            "snipe_band_na": None,
            "snipe_band_na_5pct": None,
            "na_probability": None,
            "markets_ambiguous_resolution": 0,
            "min_ev": None,
            "arb_threshold": None,
            "effective_hold": None,
        }
        self.snapshots = SnapshotRecorder(getattr(settings, "snapshot_path", None))
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
        low, high = self.strategy.snipe_band()
        low_fee, high_fee = self.strategy.snipe_band(0.05)
        self.diagnostics["snipe_band"] = {"min": round(low, 4), "max": round(high, 4)}
        self.diagnostics["snipe_band_5pct"] = {"min": round(low_fee, 4), "max": round(high_fee, 4)}
        low_na, high_na = self.strategy.na_adjusted_band()
        low_na_fee, high_na_fee = self.strategy.na_adjusted_band(0.05)
        self.diagnostics["snipe_band_na"] = {"min": round(low_na, 4), "max": round(high_na, 4)}
        self.diagnostics["snipe_band_na_5pct"] = {
            "min": round(low_na_fee, 4),
            "max": round(high_na_fee, 4),
        }
        self.diagnostics["na_probability"] = round(self.strategy.n_a_probability, 4)
        self.diagnostics["min_ev"] = self.strategy.min_ev
        self.diagnostics["arb_threshold"] = self.strategy.arb_threshold
        self.diagnostics["effective_hold"] = round(self.strategy.effective_hold, 6)
        logger.info("Signal gates: %s", self.strategy.describe_gates())
        if self.snapshots.enabled:
            logger.info(
                "Feature snapshots -> %s (for training_data; %d rows so far)",
                self.snapshots.path,
                self.snapshots.rows_written,
            )

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
                model_info=self.model_info,
                diagnostics=self.diagnostics,
                evidence=self.evidence,
            )
        except OSError:
            # A busy port must never stop the trading loop.
            logger.exception("Status server could not bind; trading continues without HTTP status")

    async def run_cycle(self) -> None:
        volume_lane = await self.scanner.scan()
        try:
            near_lane = await self.scanner.scan_near_resolution(
                window_hours=self.settings.near_resolution_window_hours,
                limit=200,
            )
        except Exception:
            # Discovery of the second lane must never break the cycle; the
            # arbitrage lane still runs.
            logger.exception("Near-resolution discovery failed")
            near_lane = []

        markets = select_markets(
            volume_lane,
            near_lane,
            max_total=self.settings.max_markets_per_cycle,
            near_slots=self.settings.near_resolution_slots,
        )
        near_ids = {market.condition_id for market in near_lane}
        self.diagnostics["near_resolution_candidates"] = len(near_lane)
        self.diagnostics["near_resolution_selected"] = sum(
            1 for market in markets if market.condition_id in near_ids
        )
        # Rebuilt every cycle: stale diagnostics are worse than none.
        self.diagnostics["markets_in_snipe_band"] = 0
        self.diagnostics["closest_snipe_price"] = None
        self.diagnostics["best_arb_sum"] = None
        self.diagnostics["closest_ttl_hours"] = None
        self.diagnostics["markets_without_book"] = 0
        self.diagnostics["markets_ambiguous_resolution"] = 0
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
                # Counted, not silently skipped: a market with no usable book is
                # the difference between "nothing qualified" and "nothing was
                # even looked at".
                self.diagnostics["markets_without_book"] += 1
                continue

            evaluated += 1
            if fallback_is_ambiguous(market.fallback_resolution_type):
                # Counted because it changes what the bot will buy: a market that
                # can settle 50-50 cannot produce a snipe signal on this config.
                self.diagnostics["markets_ambiguous_resolution"] += 1
            try:
                generated += await self._evaluate_market(market, yes_book, no_book)
            except Exception:
                logger.exception(
                    "Strategy evaluation failed for market %s",
                    market.condition_id,
                )

        try:
            generated += await self._run_negrisk_lane(markets)
        except Exception:
            logger.exception("NegRisk lane failed")

        risk_status = self.risk.status()
        logger.info(
            "Cycle complete: markets=%d books=%d new_signals=%d risk=%s snipes_in_band=%d",
            len(markets),
            evaluated,
            generated,
            risk_status,
            self.diagnostics["markets_in_snipe_band"],
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

        # Capture the inputs before acting on them: this row is training data
        # only if it was written while the market was still unresolved.
        if self.snapshots.enabled:
            await asyncio.to_thread(
                self.snapshots.record,
                market,
                yes_book,
                news_sentiment=self.settings.news_sentiment,
            )

        arbitrage = self.strategy.check_arbitrage(market, yes_book, no_book)
        if arbitrage:
            generated += await self._handle_arbitrage(arbitrage)

        for snipe_signal in self.strategy.check_snipe(market, yes_book, no_book):
            generated += await self._handle_signal(snipe_signal, "SNIPE")

        if self.model.loaded:
            signal = self._model_signal(market, yes_book, no_book)
            if signal is not None:
                generated += await self._handle_signal(signal, "MODEL")

        self._record_gate_proximity(market, yes_book, no_book)
        return generated

    async def _run_negrisk_lane(self, markets: list[Market]) -> int:
        """Look for complete NegRisk baskets priced below 1.00 after fees.

        Separated from the per-market loop because it is a different kind of
        evidence: it does not care about ttl, price bands or the model. If a
        basket costs less than 1.00, one leg must pay 1.00 at settlement.
        """
        # getattr: tests (and older configs) construct partial settings objects.
        if not getattr(self.settings, "negrisk_enabled", False):
            return 0
        max_events = int(getattr(self.settings, "negrisk_max_events", 0) or 0)
        if max_events <= 0:
            return 0

        groups: dict[str, str] = {}
        for market in markets:
            if market.neg_risk and market.event_id and market.neg_risk_id:
                groups.setdefault(market.event_id, market.neg_risk_id)
        self.diagnostics["negrisk_groups"] = len(groups)
        # Reset per cycle, like best_arb_sum: a running minimum since boot would
        # publish a basket price from hours ago as if it were the current one.
        self.diagnostics["negrisk_best_cost"] = None
        self.diagnostics["negrisk_best_profit"] = None
        self.diagnostics["negrisk_checked"] = 0
        self.diagnostics["negrisk_incomplete"] = 0
        if not groups:
            return 0

        generated = 0
        checked = 0
        # Rotate the window across cycles. With more groups than budget, a fixed
        # first-N window would price the same events forever and never look at
        # the rest.
        ordered = list(groups)
        start = self._negrisk_cursor % len(ordered)
        budget = min(max_events, len(ordered))
        window = [ordered[(start + offset) % len(ordered)] for offset in range(budget)]
        self._negrisk_cursor = (start + budget) % len(ordered)
        for event_id in window:
            try:
                title, outcomes = await self.scanner.fetch_event_outcomes(event_id)
            except Exception:
                logger.exception("NegRisk event %s could not be fetched", event_id)
                continue
            checked += 1
            if not outcomes:
                continue
            # An incomplete basket is not evidence: if one member is closed or
            # not accepting orders, the sum means nothing.
            complete = all(
                outcome.tradable and outcome.best_ask is not None for outcome in outcomes
            )
            legs = [
                BasketLeg(
                    token_id=outcome.token_yes,
                    label=outcome.label,
                    ask=float(outcome.best_ask or 0.0),
                    fee_rate=outcome.fee_rate,
                    fee_exponent=outcome.fee_exponent,
                )
                for outcome in outcomes
                if outcome.tradable and outcome.best_ask is not None
            ]
            verdict = evaluate_basket(
                legs,
                event_id=event_id,
                title=title,
                complete=complete,
                min_profit=float(getattr(self.settings, "negrisk_min_profit", 0.005)),
            )
            if verdict.complete and (
                self.diagnostics["negrisk_best_cost"] is None
                or verdict.cost < self.diagnostics["negrisk_best_cost"]
            ):
                self.diagnostics["negrisk_best_cost"] = round(verdict.cost, 6)
                self.diagnostics["negrisk_best_profit"] = round(verdict.profit, 6)
            if not verdict.complete:
                # Counted, not hidden: "checked 2, best cost null" is ambiguous,
                # "checked 2, 2 incomplete" says the members could not be priced.
                self.diagnostics["negrisk_incomplete"] += 1
            if not verdict.profitable:
                logger.debug("NegRisk %s: %s", title or event_id, verdict.reason)
                continue
            generated += await self._record_basket(verdict)

        self.diagnostics["negrisk_checked"] = checked
        return generated

    async def _record_basket(self, verdict) -> int:
        """Record a basket as a signal, never as an order.

        A basket is only risk-free if *every* leg fills. The executor places one
        order per signal, so sending M signals would leave a partial basket --
        i.e. a naked directional bet -- the moment one leg missed. Until the
        executor can submit a coordinated multi-leg order, this records the
        opportunity instead of pretending to take it.
        """
        plan = plan_basket(verdict, max_bet=self.settings.max_bet)
        if plan.baskets <= 0:
            return 0
        # One cooldown for the whole basket, keyed by event. Checking per leg
        # would let a partially-cooled basket record only half its legs -- the
        # partial basket this lane exists to prevent.
        if self._is_cooling_down(verdict.event_id, "NEGRISK"):
            logger.debug("NegRisk basket %s is cooling down", verdict.event_id)
            return 0
        recorded = 0
        for leg in verdict.legs:
            size = plan.baskets * leg.ask
            self.ledger.record_signal(
                market_id=verdict.event_id,
                token_id=leg.token_id,
                side="BUY",
                price=leg.ask,
                size_usd=size,
                reason=(
                    f"NEGRISK {verdict.title or verdict.event_id} basket_cost={verdict.cost:.4f} "
                    f"profit_per_basket={verdict.profit:.4f} baskets={plan.baskets} "
                    f"fee={leg.fee:.5f} | DRY_RUN multi-leg execution not implemented"
                ),
                expected_value=plan.baskets * (1.0 - verdict.cost) * (leg.ask / verdict.cost),
                executed=False,
            )
            recorded += 1
        if recorded:
            logger.info(
                "NegRisk basket: %s cost=%.4f profit/basket=%.4f baskets=%d (+%d legs)",
                verdict.title or verdict.event_id,
                verdict.cost,
                verdict.profit,
                plan.baskets,
                recorded,
            )
        return recorded

    def _record_gate_proximity(
        self, market: Market, yes_book: OrderBook, no_book: OrderBook
    ) -> None:
        """Track how close each market came to a gate, for /healthz.

        "No signals" and "no signals because the closest market was 6 cents away"
        are different answers; this makes the second one visible.
        """
        strategy = self.strategy
        yes_ask, no_ask = yes_book.best_ask, no_book.best_ask
        if yes_ask is not None and no_ask is not None:
            total = float(yes_ask) + float(no_ask)
            best = self.diagnostics.get("best_arb_sum")
            if best is None or total < best:
                self.diagnostics["best_arb_sum"] = round(total, 4)

        ttl_days = market.ttl_days()
        if ttl_days is not None:
            hours = round(ttl_days * 24.0, 2)
            closest = self.diagnostics.get("closest_ttl_hours")
            if closest is None or abs(hours) < abs(closest):
                self.diagnostics["closest_ttl_hours"] = hours

        low, high = strategy.snipe_band(market.fee_rate)
        candidates = [price for price in (yes_ask, no_ask) if price is not None]
        if not candidates:
            return
        # Both gates, not just price: a market 66 days from resolution sitting
        # in the price band is not "in the snipe band", it is a false positive
        # that made a quiet bot look broken.
        ttl_days = market.ttl_days()
        time_ok = (
            ttl_days is not None
            and ttl_days < strategy.snipe_max_ttl_days
            and ttl_days >= -strategy.snipe_overdue_hours / 24.0
        )
        in_band = [price for price in candidates if low <= float(price) < high]
        if in_band and time_ok:
            self.diagnostics["markets_in_snipe_band"] += 1

        def band_distance(price: float) -> float:
            """0 inside the tradeable band, otherwise the gap to the nearest edge.

            Reporting a non-zero distance for a price that *is* inside the band
            made /healthz look self-contradictory: "nearest ask 0.93, distance
            0.0106" next to a band of [0.9000, 0.9406).
            """
            value = float(price)
            if low <= value < high:
                return 0.0
            return min(abs(value - low), abs(value - high))

        nearest = min(candidates, key=band_distance)
        distance = band_distance(nearest)
        # Publish *both* gates. `distance` is a price gap, so a price inside the
        # band reports 0.0 even when the market is hours away from the time
        # window -- which made /healthz read "nearest ask 0.91, distance 0.0"
        # next to "in_band: 0". Those are not contradictory once the time gate
        # is named.
        hours_to_window: float | None = None
        if ttl_days is not None:
            hours = ttl_days * 24.0
            hours_to_window = max(0.0, round(hours - strategy.snipe_max_ttl_days * 24.0, 2))
        previous = self.diagnostics.get("closest_snipe_price")
        # Rank by how much has to change before this can fire: a market blocked
        # only by the clock is nearer than one six cents outside the band.
        score = (distance, 0.0 if time_ok else 1.0, hours_to_window or 0.0)
        if previous is None or score < previous.get("_score", (float("inf"), 1.0, 0.0)):
            self.diagnostics["closest_snipe_price"] = {
                "ask": round(float(nearest), 4),
                "distance": round(distance, 4),
                "in_time_window": bool(time_ok),
                "hours_to_window": hours_to_window,
                "_score": score,
            }

    def _model_signal(
        self, market: Market, yes_book: OrderBook, no_book: OrderBook
    ) -> Signal | None:
        # The model was trained on two-way books. On a one-sided book mid_price
        # falls back to the last trade, which for a near-resolution market can be
        # 0.001 -- feeding that in would produce confident nonsense.
        if yes_book.best_bid is None or yes_book.best_ask is None:
            return None
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
            if age <= self.settings.book_staleness_seconds and book.has_liquidity:
                return book

        async with self._book_semaphore:
            try:
                book = await self.scanner.get_orderbook(token_id)
            except Exception as exc:  # noqa: BLE001 - one fetch must not stop the cycle
                logger.warning("Order book fetch failed for %s: %s", token_id[:12], exc)
                return None
        if not book.has_liquidity:
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
        if book.has_liquidity:
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


def _load_model(settings: Settings) -> tuple[ProbabilityModel, dict[str, Any]]:
    """Load the model artifact, fetching it from MODEL_URL when needed.

    Returns the model plus a small dict the status API publishes, so ``/healthz``
    can say whether the MODEL strategy is actually participating: a quiet bot
    with two strategies running is a very different thing from a quiet bot with
    three, and without this the difference was only visible in the startup log.
    """
    model = ProbabilityModel()
    info: dict[str, Any] = {
        "loaded": False,
        "path": str(settings.model_path),
        "source": None,
        "backend": None,
        "trained_at": None,
        "samples": None,
        "positive_rate": None,
        "error": None,
    }

    if not settings.model_path.exists() and settings.model_url:
        try:
            ProbabilityModel.fetch_artifact(settings.model_url, settings.model_path)
            info["source"] = "downloaded"
        except Exception as exc:  # noqa: BLE001 - any failure keeps the bot trading
            logger.exception("Could not download the model from MODEL_URL")
            info["error"] = f"download failed: {type(exc).__name__}: {exc}"

    try:
        if model.load(settings.model_path):
            info.update(
                {
                    "loaded": True,
                    "source": info["source"] or "disk",
                    "backend": model.metadata.get("backend"),
                    "trained_at": model.metadata.get("trained_at"),
                    "samples": model.metadata.get("samples"),
                    "positive_rate": model.metadata.get("positive_rate"),
                }
            )
            logger.info(
                "Calibrated model enabled (backend=%s trained_at=%s samples=%s)",
                info["backend"] or "unknown",
                info["trained_at"] or "unknown",
                info["samples"] or "unknown",
            )
        else:
            info["source"] = info["source"] or "absent"
            logger.warning(
                "No model at %s; running ARBITRAGE + SNIPE strategies only"
                " (set MODEL_URL to fetch an artifact at startup)",
                settings.model_path,
            )
    except Exception as exc:  # noqa: BLE001 - a bad artifact must not stop trading
        logger.exception(
            "Failed to load model at %s; running ARBITRAGE + SNIPE only",
            settings.model_path,
        )
        info["source"] = info["source"] or "disk"
        info["error"] = f"load failed: {type(exc).__name__}: {exc}"
    return model, info


async def async_main() -> None:
    settings = Settings.from_env()
    configure_logging(settings.log_level)

    model, model_info = _load_model(settings)
    ledger = Ledger(settings.ledger_path)
    scanner = MarketScanner(
        settings.gamma_api_url,
        settings.clob_host,
        page_size=settings.scan_page_size,
        max_pages=settings.scan_max_pages,
        near_window_hours=settings.near_resolution_window_hours,
        near_overdue_hours=settings.near_resolution_overdue_hours,
    )
    evidence_gate = EvidenceGate(load_evidence(getattr(settings, "evidence_path", None)))
    measured = evidence_gate.measured
    if measured is not None:
        logger.info(
            "Measured hold evidence: %s", evidence_gate.describe(settings.snipe_hold_probability)
        )
    strategy = StrategyEngine(
        min_ev=settings.min_ev,
        max_bet=settings.max_bet,
        snipe_overdue_hours=settings.snipe_overdue_hours,
        arb_threshold=settings.arb_threshold,
        snipe_min_ask=settings.snipe_min_ask,
        snipe_max_ask=settings.snipe_max_ask,
        snipe_hold_probability=settings.snipe_hold_probability,
        snipe_max_ttl_days=settings.snipe_max_ttl_hours / 24.0,
        kelly_fraction=settings.kelly_fraction,
        n_a_probability=settings.na_probability,
        require_unambiguous_resolution=settings.require_unambiguous_resolution,
        measured_hold=measured.ci_low if measured is not None else None,
        measured_hold_source=evidence_gate.report.source if measured is not None else "",
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
    bot.model_info = model_info
    bot.evidence = evidence_gate.as_summary(settings.snipe_hold_probability)

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
