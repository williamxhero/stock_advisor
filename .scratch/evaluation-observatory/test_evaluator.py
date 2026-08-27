import json
import unittest
from pathlib import Path

from evaluator import delivery_forecast, evidence_maturity, schedule_guard, timing_assessment, timing_attribution


FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "scenarios.json").read_text(encoding="utf-8"))


class EvaluationObservatoryTests(unittest.TestCase):
    def test_forecast_has_conditional_duration_and_qualification_probability(self):
        result = delivery_forecast(FIXTURES["forecast"])
        self.assertEqual(result["conditional_qualified_duration_minutes"], 31)
        self.assertEqual(result["duration_sample_count"], 2)
        self.assertEqual(result["qualification_probability_by_1030"], 2 / 6)
        self.assertEqual(result["pending"], 1)
        self.assertEqual(
            result["conditional_qualified_delivery_empirical_interval_minutes"],
            {
                "method": "empirical qualified-observation range",
                "small_sample": True,
                "lower": 28,
                "upper": 34,
                "sample_count": 2,
            },
        )
        interval = result["qualification_probability_by_1030_wilson_90_interval"]
        self.assertEqual((interval["method"], interval["confidence_level"]), ("Wilson score", 0.90))
        self.assertAlmostEqual(interval["lower"], 0.11727609410228962)
        self.assertAlmostEqual(interval["upper"], 0.6529852329996819)

    def test_nonqualified_outcomes_never_supply_qualified_delivery_durations(self):
        observations = FIXTURES["forecast"] + [
            {"id": "invented-rejected", "outcome": "rejected", "qualified_duration_minutes": 1},
            {"id": "invented-failed", "outcome": "failed", "qualified_duration_minutes": 2},
            {"id": "invented-late", "outcome": "late", "qualified_duration_minutes": 3},
            {"id": "invented-missed", "outcome": "missed", "qualified_duration_minutes": 4},
            {"id": "invented-pending", "outcome": "pending", "qualified_duration_minutes": 5},
        ]
        result = delivery_forecast(observations)
        self.assertEqual(result["conditional_qualified_duration_minutes"], 31)
        self.assertEqual(result["duration_sample_count"], 2)
        self.assertEqual(result["conditional_qualified_delivery_empirical_interval_minutes"]["lower"], 28)
        self.assertEqual(result["conditional_qualified_delivery_empirical_interval_minutes"]["upper"], 34)

    def test_hard_fault_rejects_after_four_observations(self):
        result = evidence_maturity(FIXTURES["hard_fault_four"])
        self.assertEqual((result["verdict"], result["count"]), ("reject", 4))

    def test_six_observations_can_remain_insufficient_without_regime_coverage(self):
        result = evidence_maturity(FIXTURES["missing_coverage_six"])
        self.assertEqual((result["verdict"], result["count"], result["missing_regimes"]), ("insufficient", 6, ["sideways"]))

    def test_faster_quality_regression_requires_user_choice(self):
        result = timing_assessment(FIXTURES["baseline"], FIXTURES["faster_regression"])
        self.assertEqual(result["action"], "ask_user")

    def test_noninferior_diverse_evidence_only_recommends_promotion(self):
        assessment = timing_assessment(FIXTURES["baseline"], FIXTURES["promotion_candidate"])
        guard = schedule_guard(FIXTURES["observation"], assessment)
        self.assertEqual(assessment["action"], "recommend_promotion")
        self.assertFalse(guard["candidate_schedule_changed"])

    def test_prefetch_and_user_wait_have_separate_origins(self):
        result = timing_attribution(FIXTURES["attribution"])
        self.assertEqual(result, {"prefetch_minutes": 18, "user_wait_minutes": 15, "user_wait_starts_at": "2026-08-20T09:47:00"})

    def test_observation_preserves_baseline_and_invents_no_deadline(self):
        guard = schedule_guard(FIXTURES["observation"], {"action": "observe"})
        self.assertEqual(guard["baseline_start"], "09:45")
        self.assertIsNone(guard["delivery_deadline"])


if __name__ == "__main__":
    unittest.main()
