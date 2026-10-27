"""The research CLI's pure parts, tested without a network.

`scripts/strategy_lab.py` has to turn Gamma's messy closed-market records into
(yes/no, price-at-lead-time) observations. Those two conversions decide whether
the study measures reality or garbage, so they are tested directly.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("strategy_lab", ROOT / "scripts" / "strategy_lab.py")
assert SPEC and SPEC.loader
lab = importlib.util.module_from_spec(SPEC)
# dataclasses resolves annotations through sys.modules, so register first.
sys.modules["strategy_lab"] = lab
SPEC.loader.exec_module(lab)


def closed_market(**overrides):
    payload = {
        "conditionId": "0xabc",
        "question": "Did it happen?",
        "closed": True,
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(["1", "0"]),
        "clobTokenIds": json.dumps(["token-yes", "token-no"]),
        "endDate": "2026-10-20T18:00:00Z",
        "feesEnabled": True,
        "feeSchedule": {"rate": 0.05, "exponent": 1},
    }
    payload.update(overrides)
    return payload


class ParseResolvedTests(unittest.TestCase):
    def test_reads_a_clean_yes_resolution(self) -> None:
        market = lab.parse_resolved(closed_market())
        assert market is not None
        self.assertTrue(market.resolved_yes)
        self.assertEqual(market.token_yes, "token-yes")
        self.assertAlmostEqual(market.fee_rate, 0.05)

    def test_outcome_order_is_respected(self) -> None:
        market = lab.parse_resolved(
            closed_market(
                outcomes=json.dumps(["No", "Yes"]),
                outcomePrices=json.dumps(["0", "1"]),
                clobTokenIds=json.dumps(["token-no", "token-yes"]),
            )
        )
        assert market is not None
        self.assertTrue(market.resolved_yes)
        self.assertEqual(market.token_yes, "token-yes")

    def test_a_no_resolution_is_kept_as_a_negative_sample(self) -> None:
        market = lab.parse_resolved(closed_market(outcomePrices=json.dumps(["0", "1"])))
        assert market is not None
        self.assertFalse(market.resolved_yes)

    def test_open_markets_are_not_evidence(self) -> None:
        self.assertIsNone(lab.parse_resolved(closed_market(closed=False)))

    def test_ambiguous_resolutions_are_dropped(self) -> None:
        # 0.5/0.5, voided or still trading: none of these are a label.
        self.assertIsNone(
            lab.parse_resolved(closed_market(outcomePrices=json.dumps(["0.5", "0.5"])))
        )
        self.assertIsNone(
            lab.parse_resolved(closed_market(outcomePrices=json.dumps(["0.99", "0.01"])))
        )

    def test_malformed_rows_are_dropped_not_crashed(self) -> None:
        self.assertIsNone(lab.parse_resolved(closed_market(clobTokenIds=json.dumps(["one"]))))
        self.assertIsNone(lab.parse_resolved(closed_market(outcomePrices="not json")))
        self.assertIsNone(lab.parse_resolved(closed_market(outcomePrices=json.dumps(["a", "b"]))))

    def test_missing_fee_block_assumes_the_conservative_rate(self) -> None:
        payload = closed_market()
        payload.pop("feeSchedule")
        payload.pop("feesEnabled")
        market = lab.parse_resolved(payload)
        assert market is not None
        self.assertGreater(market.fee_rate, 0.0)


class ObservationWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.market = lab.parse_resolved(closed_market())
        assert self.market is not None

    def test_price_is_taken_before_trading_stopped(self) -> None:
        # Trading stopped at t=100000; two hours earlier is t=92800.
        points = [(90_000, 0.90), (92_800, 0.93), (95_000, 0.97), (99_000, 0.99), (100_000, 1.0)]
        observation = lab.observation_from_history(self.market, points, lead_hours=2.0)
        assert observation is not None
        self.assertAlmostEqual(observation.price, 0.93)
        self.assertEqual(observation.lead_hours, 2.0)

    def test_a_late_listing_yields_nothing(self) -> None:
        # First observation is after the requested lead: no honest sample.
        points = [(99_000, 0.95), (100_000, 1.0)]
        self.assertIsNone(lab.observation_from_history(self.market, points, lead_hours=2.0))

    def test_a_settled_price_is_not_evidence(self) -> None:
        points = [(99_000, 1.0), (100_000, 1.0)]
        self.assertIsNone(lab.observation_from_history(self.market, points, lead_hours=0.1))

    def test_too_few_points_is_not_an_observation(self) -> None:
        self.assertIsNone(lab.observation_from_history(self.market, [], lead_hours=1.0))
        self.assertIsNone(
            lab.observation_from_history(self.market, [(100_000, 0.5)], lead_hours=1.0)
        )


class CliTests(unittest.TestCase):
    def test_help_works_without_a_network(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            lab.main(["--help"])
        self.assertEqual(caught.exception.code, 0)

    def test_the_default_band_and_targets_are_the_documented_ones(self) -> None:
        parser_defaults = {}
        # Reflect the parser defaults instead of duplicating them in prose.
        for action in lab.main.__globals__["argparse"].ArgumentParser()._actions:
            parser_defaults[action.dest] = action.default
        self.assertEqual(lab.DEFAULT_EDGES[0], 0.0)
        self.assertEqual(lab.DEFAULT_EDGES[-1], 1.0)
        self.assertIn(0.93, lab.DEFAULT_EDGES)
