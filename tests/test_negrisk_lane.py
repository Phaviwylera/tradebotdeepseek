"""The NegRisk lane inside the bot: grouped by event, never a naked leg.

The dangerous failure mode is silent: record M signals for an M-outcome basket,
have one leg miss, and the bot is left holding a directional bet it never
intended. These tests pin that baskets are recorded as *baskets* and that an
unpriceable member disables the whole event.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from main import TradingBot
from scanner import EventOutcome, Market
from strategy import StrategyEngine


def make_settings(**overrides) -> SimpleNamespace:
    base = {
        "ws_market_url": "wss://invalid",
        "stream_backoff_seconds": 1.0,
        "snapshot_path": None,
        "news_sentiment": 0.0,
        "live": False,
        "bankroll": 200.0,
        "max_bet": 20.0,
        "book_staleness_seconds": 20.0,
        "negrisk_enabled": True,
        "negrisk_max_events": 3,
        "negrisk_min_profit": 0.005,
        "signal_cooldown_seconds": 0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class FakeLedger:
    def __init__(self) -> None:
        self.signals: list[dict[str, object]] = []
        self.cooling: set[str] = set()

    def record_signal(self, **kwargs: object) -> int:
        self.signals.append(kwargs)
        return len(self.signals)

    def has_recent_market_signal(self, *, market_id, reason_prefix, since_iso) -> bool:
        return market_id in self.cooling


def make_bot(outcomes: list[EventOutcome], settings: SimpleNamespace | None = None):
    ledger = FakeLedger()

    class Scanner:
        async def fetch_event_outcomes(self, event_id: str):
            return "House 2026", outcomes

    bot = TradingBot(
        settings=settings or make_settings(),
        scanner=Scanner(),
        ledger=ledger,
        strategy=StrategyEngine(min_ev=0.02, max_bet=20.0),
        risk=SimpleNamespace(),
        model=SimpleNamespace(loaded=False),
        executor=SimpleNamespace(),
    )
    return bot, ledger


def outcome(
    label: str, ask: float | None, *, fee: float = 0.0, tradable: bool = True
) -> EventOutcome:
    return EventOutcome(
        condition_id=f"c-{label}",
        question=f"Will {label} win?",
        label=label,
        token_yes=f"t-{label}",
        best_ask=ask,
        fee_rate=fee,
        fee_exponent=1,
        tradable=tradable,
    )


def negrisk_market(event_id: str = "32225") -> Market:
    return Market(
        condition_id="c1",
        question="Will the Democrats win the House?",
        token_yes="ty",
        token_no="tn",
        volume=10_000.0,
        liquidity=10_000.0,
        end_date=None,
        fee_rate=0.0,
        fee_exponent=1,
        neg_risk=True,
        neg_risk_id="nr1",
        event_id=event_id,
    )


class NegRiskGroupingTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_negrisk_markets_means_no_work(self) -> None:
        bot, ledger = make_bot([outcome("A", 0.3)])

        generated = await bot._run_negrisk_lane([])

        self.assertEqual(generated, 0)
        self.assertEqual(ledger.signals, [])
        self.assertEqual(bot.diagnostics["negrisk_groups"], 0)

    async def test_markets_without_an_event_id_are_skipped(self) -> None:
        market = negrisk_market(event_id="")
        bot, ledger = make_bot([outcome("A", 0.3)])

        self.assertEqual(await bot._run_negrisk_lane([market]), 0)
        self.assertEqual(bot.diagnostics["negrisk_groups"], 0)

    async def test_disabled_lane_does_nothing(self) -> None:
        bot, ledger = make_bot(
            [outcome("A", 0.3), outcome("B", 0.3)], make_settings(negrisk_enabled=False)
        )

        self.assertEqual(await bot._run_negrisk_lane([negrisk_market()]), 0)
        self.assertEqual(ledger.signals, [])


class NegRiskBasketTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_cheap_complete_basket_records_every_leg(self) -> None:
        bot, ledger = make_bot([outcome("A", 0.30), outcome("B", 0.30), outcome("C", 0.30)])

        generated = await bot._run_negrisk_lane([negrisk_market()])

        self.assertEqual(generated, 3)
        self.assertEqual(len(ledger.signals), 3)
        self.assertEqual({signal["token_id"] for signal in ledger.signals}, {"t-A", "t-B", "t-C"})
        self.assertEqual(bot.diagnostics["negrisk_checked"], 1)
        self.assertAlmostEqual(bot.diagnostics["negrisk_best_cost"], 0.90, places=6)

    async def test_basket_signals_are_never_marked_executed(self) -> None:
        bot, ledger = make_bot([outcome("A", 0.30), outcome("B", 0.30)])

        await bot._run_negrisk_lane([negrisk_market()])

        self.assertTrue(all(signal["executed"] is False for signal in ledger.signals))

    async def test_basket_reason_names_the_cost_and_the_missing_executor(self) -> None:
        bot, ledger = make_bot([outcome("A", 0.30), outcome("B", 0.30)])

        await bot._run_negrisk_lane([negrisk_market()])

        reason = str(ledger.signals[0]["reason"])
        self.assertIn("NEGRISK", reason)
        self.assertIn("multi-leg execution not implemented", reason)

    async def test_a_leg_that_cannot_be_priced_disables_the_event(self) -> None:
        # 0.10 + 0.10 + 0.10 on 12 outcomes would look like a 70% edge; with one
        # member unpriceable the honest answer is "no".
        bot, ledger = make_bot(
            [outcome("A", 0.10), outcome("B", 0.10), outcome("C", 0.10)]
            + [outcome(f"member {n}", None) for n in range(9)]
        )

        generated = await bot._run_negrisk_lane([negrisk_market()])

        self.assertEqual(generated, 0)
        self.assertEqual(ledger.signals, [])

    async def test_a_closed_leg_disables_the_event(self) -> None:
        bot, ledger = make_bot(
            [outcome("A", 0.10), outcome("B", 0.10), outcome("C", 0.10, tradable=False)]
        )

        self.assertEqual(await bot._run_negrisk_lane([negrisk_market()]), 0)
        self.assertEqual(ledger.signals, [])

    async def test_prices_that_sum_above_one_record_nothing(self) -> None:
        bot, ledger = make_bot([outcome("A", 0.40), outcome("B", 0.40), outcome("C", 0.40)])

        self.assertEqual(await bot._run_negrisk_lane([negrisk_market()]), 0)
        self.assertEqual(ledger.signals, [])
        self.assertAlmostEqual(bot.diagnostics["negrisk_best_cost"], 1.20, places=6)

    async def test_fees_can_close_an_edge_that_looks_open(self) -> None:
        # 0.33 * 3 = 0.99 pre-fee, i.e. a 1% margin. At 5% per leg the fees add
        # 0.033, so the basket costs 1.023 and the "edge" is gone.
        bot, ledger = make_bot([outcome(name, 0.33, fee=0.05) for name in "ABC"])

        self.assertEqual(await bot._run_negrisk_lane([negrisk_market()]), 0)
        self.assertEqual(ledger.signals, [])

    async def test_the_fee_free_version_of_the_same_basket_is_taken(self) -> None:
        bot, ledger = make_bot([outcome(name, 0.33) for name in "ABC"])

        self.assertEqual(await bot._run_negrisk_lane([negrisk_market()]), 3)

    async def test_a_basket_inside_the_cooldown_is_recorded_once(self) -> None:
        # Without a cooldown the same basket would be re-recorded every minute.
        bot, ledger = make_bot(
            [outcome("A", 0.30), outcome("B", 0.30)],
            make_settings(signal_cooldown_seconds=3600),
        )
        ledger.cooling.add("32225")

        self.assertEqual(await bot._run_negrisk_lane([negrisk_market()]), 0)
        self.assertEqual(ledger.signals, [])

    async def test_a_basket_outside_the_cooldown_is_recorded(self) -> None:
        bot, ledger = make_bot(
            [outcome("A", 0.30), outcome("B", 0.30)],
            make_settings(signal_cooldown_seconds=3600),
        )

        self.assertEqual(await bot._run_negrisk_lane([negrisk_market()]), 2)

    async def test_legs_are_keyed_to_the_event_so_the_cooldown_is_whole(self) -> None:
        bot, ledger = make_bot([outcome("A", 0.30), outcome("B", 0.30)])

        await bot._run_negrisk_lane([negrisk_market()])

        self.assertEqual({signal["market_id"] for signal in ledger.signals}, {"32225"})

    async def test_a_failing_event_fetch_does_not_kill_the_cycle(self) -> None:
        class Exploding:
            async def fetch_event_outcomes(self, event_id: str):
                raise RuntimeError("gamma down")

        bot = TradingBot(
            settings=make_settings(),
            scanner=Exploding(),
            ledger=FakeLedger(),
            strategy=StrategyEngine(min_ev=0.02, max_bet=20.0),
            risk=SimpleNamespace(),
            model=SimpleNamespace(loaded=False),
            executor=SimpleNamespace(),
        )

        with self.assertLogs("main", level="ERROR"):
            self.assertEqual(await bot._run_negrisk_lane([negrisk_market()]), 0)

    async def test_only_the_configured_number_of_events_is_checked(self) -> None:
        bot, _ = make_bot(
            [outcome("A", 0.30), outcome("B", 0.30)], make_settings(negrisk_max_events=2)
        )
        markets = [negrisk_market("1"), negrisk_market("2"), negrisk_market("3")]

        await bot._run_negrisk_lane(markets)

        self.assertEqual(bot.diagnostics["negrisk_checked"], 2)
        self.assertEqual(bot.diagnostics["negrisk_groups"], 3)

    async def test_the_best_cost_is_reset_each_cycle(self) -> None:
        # A running minimum since boot would publish an old price as current.
        current = [outcome("A", 0.30), outcome("B", 0.30)]

        class Scanner:
            async def fetch_event_outcomes(self, event_id: str):
                return "House 2026", current

        bot = TradingBot(
            settings=make_settings(),
            scanner=Scanner(),
            ledger=FakeLedger(),
            strategy=StrategyEngine(min_ev=0.02, max_bet=20.0),
            risk=SimpleNamespace(),
            model=SimpleNamespace(loaded=False),
            executor=SimpleNamespace(),
        )

        await bot._run_negrisk_lane([negrisk_market()])
        self.assertAlmostEqual(bot.diagnostics["negrisk_best_cost"], 0.60, places=6)

        current = [outcome("A", 0.60), outcome("B", 0.60)]
        await bot._run_negrisk_lane([negrisk_market()])

        self.assertAlmostEqual(bot.diagnostics["negrisk_best_cost"], 1.20, places=6)

    async def test_a_cycle_without_negrisk_markets_clears_the_numbers(self) -> None:
        bot, _ = make_bot([outcome("A", 0.30), outcome("B", 0.30)])
        await bot._run_negrisk_lane([negrisk_market()])

        self.assertEqual(await bot._run_negrisk_lane([]), 0)

        self.assertIsNone(bot.diagnostics["negrisk_best_cost"])
        self.assertIsNone(bot.diagnostics["negrisk_best_profit"])
        self.assertEqual(bot.diagnostics["negrisk_checked"], 0)

    async def test_coverage_rotates_across_cycles(self) -> None:
        # Nine events, a budget of two: a fixed first-N window would price the
        # same two forever and never look at the other seven.
        seen: list[str] = []

        class Recording:
            async def fetch_event_outcomes(self, event_id: str):
                seen.append(event_id)
                return "Event", [outcome("A", 0.30), outcome("B", 0.30)]

        bot = TradingBot(
            settings=make_settings(negrisk_max_events=2),
            scanner=Recording(),
            ledger=FakeLedger(),
            strategy=StrategyEngine(min_ev=0.02, max_bet=20.0),
            risk=SimpleNamespace(),
            model=SimpleNamespace(loaded=False),
            executor=SimpleNamespace(),
        )
        markets = [negrisk_market(str(index)) for index in range(5)]

        for _ in range(5):
            await bot._run_negrisk_lane(markets)

        self.assertEqual(len(seen), 10)
        self.assertEqual(len(set(seen)), 5)
        self.assertEqual([seen.count(str(index)) for index in range(5)], [2, 2, 2, 2, 2])

    async def test_a_budget_larger_than_the_group_count_does_not_duplicate(self) -> None:
        bot, ledger = make_bot(
            [outcome("A", 0.30), outcome("B", 0.30)], make_settings(negrisk_max_events=5)
        )

        await bot._run_negrisk_lane([negrisk_market()])

        self.assertEqual(bot.diagnostics["negrisk_checked"], 1)
        self.assertEqual(len(ledger.signals), 2)

    async def test_duplicate_markets_of_one_event_are_fetched_once(self) -> None:
        bot, _ = make_bot([outcome("A", 0.30), outcome("B", 0.30)])
        markets = [negrisk_market("32225"), negrisk_market("32225")]

        await bot._run_negrisk_lane(markets)

        self.assertEqual(bot.diagnostics["negrisk_groups"], 1)
        self.assertEqual(bot.diagnostics["negrisk_checked"], 1)


if __name__ == "__main__":
    unittest.main()
