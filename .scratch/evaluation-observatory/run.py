"""One-command text UI: run focused tests, then render frozen prototype state."""

import json
import sys
import unittest
from pathlib import Path

from evaluator import delivery_forecast, evidence_maturity, schedule_guard, timing_assessment, timing_attribution


ROOT = Path(__file__).parent
FIXTURES = json.loads((ROOT / "fixtures" / "scenarios.json").read_text(encoding="utf-8"))


def main():
    suite = unittest.defaultTestLoader.discover(str(ROOT), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    if not result.wasSuccessful():
        return 1

    forecast = delivery_forecast(FIXTURES["forecast"])
    hard_fault = evidence_maturity(FIXTURES["hard_fault_four"])
    coverage = evidence_maturity(FIXTURES["missing_coverage_six"])
    regression = timing_assessment(FIXTURES["baseline"], FIXTURES["faster_regression"])
    promotion = timing_assessment(FIXTURES["baseline"], FIXTURES["promotion_candidate"])
    attribution = timing_attribution(FIXTURES["attribution"])
    guard = schedule_guard(FIXTURES["observation"], promotion)
    frame = {
        "delivery_forecast": forecast,
        "evidence_maturity": {"four_with_hard_fault": hard_fault, "six_missing_regime": coverage},
        "timing_change": {"faster_with_regression": regression, "noninferior_candidate": promotion, "schedule_guard": guard},
        "attribution": attribution,
        "prototype_only_statistical_spike": "Decide confidence model, weighting, dependence, regime taxonomy, and promotion thresholds."
    }
    print("\n=== Evaluation Observatory (frozen-data prototype) ===")
    print(json.dumps(frame, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
