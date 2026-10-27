"""The research layer: does the strategy's own assumption survive measurement?

These tests are written against synthetic data with a known answer, because the
point of the layer is to reach the *right* conclusion on data whose truth we
already know. If it cannot tell a 99%-hold world from a 90%-hold world, it
cannot be trusted with real data either.
"""

from __future__ import annotations

import json
import random
import tempfile
import unittest
from pathlib import Path

import evidence
from evidence import EvidenceGate, EvidenceReport, HoldEvidence, build_report, load_evidence
from research.screening import (
    ACCEPT,
    INSUFFICIENT,
    REJECT,
    Observation,
    bucket_report,
    evaluate_arbitrage_hypothesis,
    evaluate_snipe_hypothesis,
)
from research.stats import adjusted_alpha, calibration_buckets, wilson_interval, z_for


def make_observations(n: int, price: float, hold_rate: float, seed: int = 7) -> list[Observation]:
    """Exactly ``round(n * hold_rate)`` winners, so the measured rate is exact."""
    successes = max(0, min(n, round(n * hold_rate)))
    flags = [True] * successes + [False] * (n - successes)
    random.Random(seed).shuffle(flags)
    return [
        Observation(
            condition_id=f"0x{index:04d}",
            price=price,
            resolved_yes=resolved,
            lead_hours=2.0,
        )
        for index, resolved in enumerate(flags)
    ]


class WilsonIntervalTests(unittest.TestCase):
    def test_is_symmetric_and_bounded(self) -> None:
        low, high = wilson_interval(50, 100)
        self.assertLess(low, 0.5)
        self.assertGreater(high, 0.5)
        self.assertAlmostEqual((low + high) / 2, 0.5, places=6)

    def test_narrows_as_n_grows(self) -> None:
        small = wilson_interval(95, 100)
        large = wilson_interval(9_500, 10_000)
        self.assertLess(large[1] - large[0], small[1] - small[0])

    def test_degenerate_cases(self) -> None:
        self.assertEqual(wilson_interval(0, 0), (0.0, 1.0))
        low, high = wilson_interval(100, 100)
        self.assertLess(low, 1.0)  # never claims certainty
        self.assertAlmostEqual(high, 1.0, places=9)

    def test_rejects_impossible_counts(self) -> None:
        with self.assertRaises(ValueError):
            wilson_interval(5, 4)
        with self.assertRaises(ValueError):
            wilson_interval(-1, 4)


class MultipleTestingTests(unittest.TestCase):
    def test_bonferroni_shrinks_alpha(self) -> None:
        self.assertAlmostEqual(adjusted_alpha(0.05, 10), 0.005)
        with self.assertRaises(ValueError):
            adjusted_alpha(0.05, 0)

    def test_a_stricter_alpha_widens_the_interval(self) -> None:
        loose = wilson_interval(90, 100, z_for(0.05))
        strict = wilson_interval(90, 100, z_for(0.05 / 20))
        self.assertLess(strict[0], loose[0])

    def test_bucketing_assigns_each_sample_once(self) -> None:
        buckets = calibration_buckets(
            [(0.91, True), (0.925, False), (0.95, True)], [0.90, 0.93, 0.96]
        )
        self.assertEqual([bucket.n for bucket in buckets], [2, 1])
        self.assertEqual(sum(bucket.n for bucket in buckets), 3)


class SnipeScreeningTests(unittest.TestCase):
    def test_a_genuine_edge_is_accepted(self) -> None:
        # 97% hold against a 94.7% requirement: real, and big enough to see.
        verdict = evaluate_snipe_hypothesis(
            make_observations(400, 0.93, 0.97),
            price_low=0.92,
            price_high=0.94,
            fee_rate=0.05,
            min_ev=0.01,
        )
        self.assertEqual(verdict.verdict, ACCEPT, verdict.describe())
        self.assertTrue(verdict.passed)

    def test_the_assumption_being_used_live_is_rejected(self) -> None:
        # The world where a 0.94 ask resolves YES 94% of the time -- one point
        # below the 0.95 the bot assumes -- must NOT be tradeable.
        verdict = evaluate_snipe_hypothesis(
            make_observations(400, 0.94, 0.94),
            price_low=0.93,
            price_high=0.95,
            fee_rate=0.05,
            min_ev=0.01,
            assumed_hold=0.95,
        )
        self.assertEqual(verdict.verdict, REJECT, verdict.describe())
        self.assertGreater(verdict.required_rate, 0.95)

    def test_a_coin_flip_in_the_band_is_rejected(self) -> None:
        verdict = evaluate_snipe_hypothesis(
            make_observations(200, 0.90, 0.55),
            price_low=0.89,
            price_high=0.91,
            fee_rate=0.05,
        )
        self.assertEqual(verdict.verdict, REJECT)

    def test_too_few_samples_is_not_a_verdict(self) -> None:
        verdict = evaluate_snipe_hypothesis(
            make_observations(5, 0.93, 1.0),
            price_low=0.92,
            price_high=0.94,
            fee_rate=0.05,
            min_samples=30,
        )
        self.assertEqual(verdict.verdict, INSUFFICIENT)
        self.assertIn("only 5 observations", verdict.notes[0])

    def test_empty_band_is_insufficient_not_accept(self) -> None:
        verdict = evaluate_snipe_hypothesis(
            make_observations(400, 0.60, 1.0),
            price_low=0.92,
            price_high=0.94,
            fee_rate=0.05,
        )
        self.assertEqual(verdict.verdict, INSUFFICIENT)
        self.assertEqual(verdict.n, 0)

    def test_the_configured_assumption_is_flagged_when_it_cannot_work(self) -> None:
        verdict = evaluate_snipe_hypothesis(
            make_observations(400, 0.945, 0.99),
            price_low=0.94,
            price_high=0.95,
            fee_rate=0.05,
            min_ev=0.01,
            assumed_hold=0.95,
        )
        self.assertTrue(any("below the required" in note for note in verdict.notes))

    def test_a_plausible_but_unproven_band_is_told_to_revise(self) -> None:
        # 96% measured against a 95.19% requirement: the point estimate clears,
        # the pessimistic bound does not. That is "collect more data", not "go".
        verdict = evaluate_snipe_hypothesis(
            make_observations(200, 0.93, 0.96),
            price_low=0.92,
            price_high=0.94,
            fee_rate=0.05,
            min_ev=0.02,
        )
        self.assertEqual(verdict.verdict, "REVISE", verdict.describe())
        self.assertTrue(verdict.notes)

    def test_band_bounds_are_validated(self) -> None:
        with self.assertRaises(ValueError):
            evaluate_snipe_hypothesis([], price_low=0.95, price_high=0.90, fee_rate=0.0)

    def test_verdict_serialises_without_losing_the_interval(self) -> None:
        verdict = evaluate_snipe_hypothesis(
            make_observations(400, 0.93, 0.97),
            price_low=0.92,
            price_high=0.94,
            fee_rate=0.05,
            min_ev=0.01,
        )
        payload = verdict.as_dict()
        self.assertEqual(payload["verdict"], ACCEPT)
        self.assertLessEqual(payload["ci_low"], payload["measured_rate"])


class ArbitrageScreeningTests(unittest.TestCase):
    def test_pre_fee_arbitrage_is_rejected_after_fees(self) -> None:
        pairs = [(0.4925, 0.4925)] * 100
        verdict = evaluate_arbitrage_hypothesis(pairs, fee_rate=0.05)
        self.assertEqual(verdict.verdict, REJECT)
        self.assertGreater(verdict.avg_price, 1.0)

    def test_a_deep_mispricing_survives(self) -> None:
        pairs = [(0.45, 0.45)] * 100
        verdict = evaluate_arbitrage_hypothesis(pairs, fee_rate=0.05)
        self.assertEqual(verdict.verdict, ACCEPT)

    def test_no_data_is_insufficient(self) -> None:
        self.assertEqual(evaluate_arbitrage_hypothesis([], fee_rate=0.05).verdict, INSUFFICIENT)


class BucketReportTests(unittest.TestCase):
    def test_covers_only_buckets_with_samples_and_applies_correction(self) -> None:
        observations = make_observations(300, 0.93, 0.96) + make_observations(300, 0.5, 0.5)
        rows = bucket_report(
            observations,
            [0.0, 0.6, 0.9, 0.95, 1.0],
            fee_rate=0.05,
            min_ev=0.01,
            n_trials=4,
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(row["n"] for row in rows), 600)
        for row in rows:
            self.assertLessEqual(row["ci_low"], row["ci_high"])


class EvidenceReportTests(unittest.TestCase):
    def make_report(self) -> evidence.EvidenceReport:
        return build_report(
            rate=0.91,
            ci_low=0.884,
            ci_high=0.933,
            n=250,
            price_low=0.90,
            price_high=0.94,
            lead_hours=2.0,
            fee_rate=0.05,
            verdict=REJECT,
            source="test",
        )

    def test_round_trips_through_json(self) -> None:
        report = self.make_report()
        restored = EvidenceReport.from_dict(json.loads(report.to_json()))
        self.assertEqual(restored.hold.rate, 0.91)
        self.assertEqual(restored.hold.n, 250)

    def test_save_and_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "hold.json"
            self.make_report().save(path)
            loaded = load_evidence(path)
            assert loaded is not None
            self.assertEqual(loaded.hold.verdict, REJECT)

    def test_a_missing_file_is_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(load_evidence(Path(tmp) / "absent.json"))
        self.assertIsNone(load_evidence(None))

    def test_corrupt_or_inconsistent_files_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertIsNone(load_evidence(path))
            path.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
            self.assertIsNone(load_evidence(path))
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 99,
                        "hold": {"rate": 1, "ci_low": 0, "ci_high": 1, "n": 1},
                    }
                ),
                encoding="utf-8",
            )
            self.assertIsNone(load_evidence(path))


class EvidenceGateTests(unittest.TestCase):
    def report(self, n: int = 200, ci_low: float = 0.884) -> EvidenceReport:
        return build_report(
            rate=min(0.99, ci_low + 0.03),
            ci_low=ci_low,
            ci_high=min(0.999, ci_low + 0.06),
            n=n,
            price_low=0.90,
            price_high=0.94,
            lead_hours=2.0,
            fee_rate=0.05,
            verdict=ACCEPT,
            source="test",
        )

    def test_without_evidence_the_assumption_stands(self) -> None:
        gate = EvidenceGate(None)
        self.assertEqual(gate.effective_hold(0.95), 0.95)
        self.assertEqual(gate.as_summary(0.95)["source"], "assumed")
        self.assertIn("unverified", gate.describe(0.95))

    def test_evidence_lowers_the_hold_to_the_pessimistic_bound(self) -> None:
        gate = EvidenceGate(self.report(ci_low=0.884))
        self.assertAlmostEqual(gate.effective_hold(0.95), 0.884)
        self.assertEqual(gate.as_summary(0.95)["source"], "measured")

    def test_evidence_never_raises_the_assumption(self) -> None:
        # A suspiciously good measurement cannot make the bot more optimistic.
        gate = EvidenceGate(self.report(ci_low=0.99))
        self.assertAlmostEqual(gate.effective_hold(0.90), 0.90)

    def test_thin_evidence_is_not_used(self) -> None:
        gate = EvidenceGate(self.report(n=5, ci_low=0.5))
        self.assertEqual(gate.effective_hold(0.95), 0.95)
        self.assertEqual(gate.as_summary(0.95)["source"], "assumed")

    def test_summary_carries_the_provenance(self) -> None:
        summary = EvidenceGate(self.report()).as_summary(0.95)
        self.assertEqual(summary["n"], 200)
        self.assertIsNotNone(summary["ci"])
        self.assertEqual(summary["verdict"], ACCEPT)


class HoldEvidenceTests(unittest.TestCase):
    def test_usable_requires_enough_samples(self) -> None:
        self.assertFalse(HoldEvidence(0.9, 0.85, 0.94, 3, 0.9, 0.94, 2.0, 0.05).usable)
        self.assertTrue(HoldEvidence(0.9, 0.85, 0.94, 300, 0.9, 0.94, 2.0, 0.05).usable)


if __name__ == "__main__":
    unittest.main()
