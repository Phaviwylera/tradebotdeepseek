"""NegRisk basket arbitrage: complete baskets only, fees included, capped."""

from __future__ import annotations

import unittest

from negrisk import (
    MAX_OUTCOMES,
    BasketLeg,
    basket_cost,
    best_basket,
    evaluate_basket,
    plan_basket,
)


def legs(*asks: float, fee_rate: float = 0.0) -> tuple[BasketLeg, ...]:
    return tuple(
        BasketLeg(
            token_id=f"t{index}",
            label=f"outcome {index}",
            ask=ask,
            fee_rate=fee_rate,
        )
        for index, ask in enumerate(asks)
    )


class BasketCostTests(unittest.TestCase):
    def test_cost_is_the_sum_of_asks_when_fee_free(self) -> None:
        self.assertAlmostEqual(basket_cost(legs(0.30, 0.30, 0.30)), 0.90)

    def test_fee_is_charged_against_the_price_not_the_payout(self) -> None:
        leg = BasketLeg("t", "A", ask=0.50, fee_rate=0.05)

        self.assertAlmostEqual(leg.fee, 0.05 * 0.25, places=12)
        self.assertAlmostEqual(leg.cost, 0.5125, places=12)

    def test_every_leg_pays_its_own_fee(self) -> None:
        free = basket_cost(legs(0.30, 0.30, 0.30))
        paid = basket_cost(legs(0.30, 0.30, 0.30, fee_rate=0.05))

        self.assertEqual(sum(1 for _ in legs(0.30, 0.30, 0.30, fee_rate=0.05)), 3)
        # p*(1-p) per leg, and there are three legs.
        self.assertAlmostEqual(paid, free + 3 * 0.05 * 0.30 * 0.70, places=12)


class EvaluateBasketTests(unittest.TestCase):
    def test_cheap_complete_basket_is_profitable(self) -> None:
        verdict = evaluate_basket(legs(0.30, 0.30, 0.30), event_id="e1")

        self.assertTrue(verdict.complete)
        self.assertTrue(verdict.profitable)
        self.assertAlmostEqual(verdict.cost, 0.90)
        self.assertAlmostEqual(verdict.profit, 0.10, places=12)
        self.assertEqual(verdict.reason, "")

    def test_expensive_basket_is_rejected_with_a_reason(self) -> None:
        verdict = evaluate_basket(legs(0.40, 0.40, 0.40))

        self.assertFalse(verdict.profitable)
        self.assertIn("no edge after fees", verdict.reason)

    def test_incomplete_basket_is_never_profitable(self) -> None:
        # Three of twelve outcomes sums to ~0.3 and would read as a 70% edge.
        verdict = evaluate_basket(legs(0.10, 0.10, 0.10), complete=False)

        self.assertFalse(verdict.profitable)
        self.assertIn("incomplete", verdict.reason)

    def test_incomplete_basket_cannot_be_laundered_with_a_big_discount(self) -> None:
        verdict = evaluate_basket(legs(0.01, 0.01), complete=False)

        self.assertAlmostEqual(verdict.profit, 0.98, places=12)
        self.assertFalse(verdict.profitable)

    def test_empty_basket_is_rejected(self) -> None:
        verdict = evaluate_basket([])

        self.assertFalse(verdict.profitable)
        self.assertEqual(verdict.reason, "no legs")
        self.assertEqual(verdict.outcomes, 0)

    def test_single_leg_is_a_directional_bet_not_a_basket(self) -> None:
        verdict = evaluate_basket(legs(0.50))

        self.assertFalse(verdict.profitable)
        self.assertIn("not a basket", verdict.reason)

    def test_too_many_outcomes_is_rejected(self) -> None:
        many = legs(*[0.01] * (MAX_OUTCOMES + 1))
        verdict = evaluate_basket(many)

        self.assertFalse(verdict.profitable)
        self.assertIn("more than this bot will sweep", verdict.reason)

    def test_min_profit_threshold_is_applied_after_fees(self) -> None:
        # 0.96 pre-fee reads as a 4% edge. Three 5% legs each cost
        # 0.05 * 0.32 * 0.68 = 0.01088, so the real edge is 0.7% - rejected.
        thin = legs(0.32, 0.32, 0.32, fee_rate=0.05)
        verdict = evaluate_basket(thin, min_profit=0.01)

        self.assertAlmostEqual(verdict.profit + 0.03264, 0.04, places=12)
        self.assertFalse(verdict.profitable)
        self.assertIn("no edge after fees", verdict.reason)

    def test_the_same_basket_passes_without_fees(self) -> None:
        verdict = evaluate_basket(legs(0.32, 0.32, 0.32), min_profit=0.01)

        self.assertTrue(verdict.profitable)
        self.assertAlmostEqual(verdict.profit, 0.04, places=12)

    def test_fee_free_thin_basket_survives_a_small_threshold(self) -> None:
        verdict = evaluate_basket(legs(0.99, 0.0), min_profit=0.005)

        self.assertTrue(verdict.profitable)

    def test_as_dict_rounds_for_display(self) -> None:
        payload = evaluate_basket(
            legs(0.30, 0.30, 0.30), event_id="e1", title="Three way"
        ).as_dict()

        self.assertEqual(payload["event_id"], "e1")
        self.assertEqual(payload["outcomes"], 3)
        self.assertEqual(payload["cost"], 0.9)
        self.assertIs(payload["profitable"], True)


class BestBasketTests(unittest.TestCase):
    def test_picks_the_largest_profit(self) -> None:
        small = evaluate_basket(legs(0.95, 0.0), event_id="small")
        large = evaluate_basket(legs(0.80, 0.0), event_id="large")
        negative = evaluate_basket(legs(1.2), event_id="negative")

        chosen = best_basket([small, negative, large])

        assert chosen is not None
        self.assertEqual(chosen.event_id, "large")

    def test_returns_none_when_nothing_is_profitable(self) -> None:
        self.assertIsNone(best_basket([evaluate_basket(legs(1.5))]))


class PlanBasketTests(unittest.TestCase):
    def test_plan_is_capped_to_max_bet(self) -> None:
        verdict = evaluate_basket(legs(0.30, 0.30, 0.30), min_profit=0.005)
        plan = plan_basket(verdict, max_bet=2.0)

        self.assertEqual(plan.baskets, 2)
        self.assertAlmostEqual(plan.total_cost, 1.80, places=12)
        self.assertAlmostEqual(plan.expected_profit, 0.20, places=12)

    def test_plan_never_exceeds_the_cap(self) -> None:
        verdict = evaluate_basket(legs(0.30, 0.30, 0.30))
        plan = plan_basket(verdict, max_bet=20.0)

        self.assertLessEqual(plan.total_cost, 20.0)
        self.assertEqual(plan.baskets, 22)

    def test_plan_of_an_unprofitable_basket_is_empty(self) -> None:
        plan = plan_basket(evaluate_basket(legs(1.5)), max_bet=20.0)

        self.assertEqual(plan.baskets, 0)
        self.assertEqual(plan.total_cost, 0.0)

    def test_plan_with_no_affordable_basket_is_empty(self) -> None:
        verdict = evaluate_basket(legs(0.60, 0.30), min_profit=0.005)
        plan = plan_basket(verdict, max_bet=0.5)

        self.assertEqual(plan.baskets, 0)
        self.assertEqual(plan.expected_profit, 0.0)

    def test_size_for_guards_against_non_positive_cost(self) -> None:
        plan = plan_basket(evaluate_basket(legs(0.30, 0.30)), max_bet=10.0)
        empty = plan.size_for(0.0)

        self.assertEqual(empty.baskets, 0)


if __name__ == "__main__":
    unittest.main()
