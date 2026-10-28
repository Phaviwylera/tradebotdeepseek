"""The quant report script's pure parts, tested without a network."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("quant_report", ROOT / "scripts" / "quant_report.py")
assert SPEC and SPEC.loader
report = importlib.util.module_from_spec(SPEC)
sys.modules["quant_report"] = report
SPEC.loader.exec_module(report)


def member(price: float | None, **overrides) -> dict:
    payload = {
        "conditionId": "0xcondition",
        "question": "Will it resolve YES?",
        "groupItemTitle": "Member",
        "clobTokenIds": json.dumps(["yes-token", "no-token"]),
        "outcomes": json.dumps(["Yes", "No"]),
        "bestAsk": price,
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "enableOrderBook": True,
    }
    payload.update(overrides)
    return payload


def free_member(price: float | None, **overrides) -> dict:
    """A member of a fee-free NegRisk event (the common case on Gamma)."""
    fees = {"feesEnabled": True, "feeType": "zero_fees", "feeSchedule": {"rate": 0, "exponent": 1}}
    fees.update(overrides)
    return member(price, **fees)


def event(*members: dict) -> dict:
    return {"id": "32225", "title": "House 2026", "markets": list(members)}


class EventLegsTests(unittest.TestCase):
    def test_prices_every_member(self) -> None:
        legs, complete = report.event_legs(event(member(0.30), member(0.35), member(0.35)))

        self.assertTrue(complete)
        self.assertEqual(len(legs), 3)
        self.assertAlmostEqual(sum(leg.ask for leg in legs), 1.0)

    def test_a_member_without_a_price_makes_the_basket_incomplete(self) -> None:
        legs, complete = report.event_legs(event(member(0.30), member(None)))

        self.assertFalse(complete)
        self.assertEqual(len(legs), 1)

    def test_a_closed_member_makes_the_basket_incomplete(self) -> None:
        _, complete = report.event_legs(event(member(0.30), member(0.30, closed=True)))

        self.assertFalse(complete)

    def test_a_member_not_accepting_orders_makes_the_basket_incomplete(self) -> None:
        _, complete = report.event_legs(event(member(0.30, acceptingOrders=False)))

        self.assertFalse(complete)

    def test_a_member_without_two_tokens_makes_the_basket_incomplete(self) -> None:
        _, complete = report.event_legs(event(member(0.30, clobTokenIds=json.dumps(["only"]))))

        self.assertFalse(complete)

    def test_the_yes_token_is_selected_by_outcome_name_not_position(self) -> None:
        swapped = member(
            0.30,
            clobTokenIds=json.dumps(["no-token", "yes-token"]),
            outcomes=json.dumps(["No", "Yes"]),
        )

        legs, _ = report.event_legs(event(swapped))

        self.assertEqual(legs[0].token_id, "yes-token")

    def test_a_sports_fee_is_carried_onto_the_leg(self) -> None:
        ticketed = member(
            0.30,
            feesEnabled=True,
            feeType="sports_fees_v3",
            feeSchedule={"rate": 0.05, "exponent": 1},
        )

        legs, _ = report.event_legs(event(ticketed))

        self.assertAlmostEqual(legs[0].fee_rate, 0.05)

    def test_an_empty_event_yields_nothing(self) -> None:
        self.assertEqual(report.event_legs({"id": "1", "markets": []}), ([], True))


class ScanNegRiskTests(unittest.TestCase):
    def fake_get_json(self, events):
        def get_json(url, params=None, timeout=20.0):
            assert url.endswith("/events")
            return events

        return get_json

    def test_baskets_are_sorted_cheapest_first(self) -> None:
        events = [
            {"id": "1", "title": "expensive", "markets": [member(0.40), member(0.40)]},
            {"id": "2", "title": "cheap", "markets": [member(0.30), member(0.30)]},
        ]

        baskets = report.scan_negrisk(get_json=self.fake_get_json(events))

        self.assertEqual([basket["title"] for basket in baskets], ["cheap", "expensive"])

    def test_the_cheap_basket_is_flagged_profitable(self) -> None:
        events = [{"id": "2", "title": "cheap", "markets": [free_member(0.30), free_member(0.30)]}]

        baskets = report.scan_negrisk(get_json=self.fake_get_json(events))

        self.assertTrue(baskets[0]["profitable"])
        self.assertAlmostEqual(baskets[0]["cost"], 0.60)

    def test_an_unknown_fee_block_is_charged_conservatively(self) -> None:
        # No feeSchedule in the payload: assume the venue's fee rather than zero,
        # which is the difference between 0.600 and 0.621 on this basket.
        events = [{"id": "2", "title": "cheap", "markets": [member(0.30), member(0.30)]}]

        baskets = report.scan_negrisk(get_json=self.fake_get_json(events))

        self.assertGreater(baskets[0]["cost"], 0.60)

    def test_an_incomplete_event_is_reported_but_not_profitable(self) -> None:
        events = [{"id": "3", "title": "partial", "markets": [member(0.05), member(None)]}]

        baskets = report.scan_negrisk(get_json=self.fake_get_json(events))

        self.assertFalse(baskets[0]["complete"])
        self.assertFalse(baskets[0]["profitable"])

    def test_events_without_markets_are_skipped(self) -> None:
        events = [{"id": "4", "title": "empty", "markets": []}, {"id": "5", "markets": []}]

        self.assertEqual(report.scan_negrisk(get_json=self.fake_get_json(events)), [])

    def test_a_data_envelope_is_unwrapped(self) -> None:
        events = {
            "data": [{"id": "6", "title": "wrapped", "markets": [member(0.45), member(0.45)]}]
        }

        baskets = report.scan_negrisk(get_json=self.fake_get_json(events))

        self.assertEqual(len(baskets), 1)
        self.assertEqual(baskets[0]["title"], "wrapped")


class MainTests(unittest.TestCase):
    def test_json_mode_reports_the_ruin_verdict(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = report.main(["--json", "--start", "20", "--target", "6000", "--days", "3"])

        payload = json.loads(buffer.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["feasibility"]["verdict"], "ruinous")
        self.assertAlmostEqual(payload["feasibility"]["multiple"], 300.0)
        self.assertIn("bid", payload["quote"])

    def test_json_mode_publishes_the_na_budget(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            report.main(["--json", "--ask", "0.94", "--hold", "0.95", "--min-ev", "0.01"])

        payload = json.loads(buffer.getvalue())
        self.assertAlmostEqual(payload["resolution"]["n_a_budget"], 0.001333, places=5)
        self.assertAlmostEqual(payload["resolution"]["adjusted_hold"], 0.9494, places=4)

    def test_text_mode_publishes_the_na_budget(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            report.main(["--ask", "0.94", "--hold", "0.95"])
        text = buffer.getvalue()

        self.assertIn("0.1333%", text)
        self.assertIn("inventory risk", text)

    def test_text_mode_explains_itself_without_a_network(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = report.main(["--start", "20", "--target", "6000", "--days", "3"])
        text = buffer.getvalue()

        self.assertEqual(code, 0)
        self.assertIn("RUINOUS", text)
        self.assertIn("Market-making quote", text)


if __name__ == "__main__":
    unittest.main()
