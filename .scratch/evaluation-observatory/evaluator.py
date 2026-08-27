"""Pure prototype-only evaluation rules over frozen observations."""

from __future__ import annotations

from datetime import datetime
from math import sqrt
from statistics import median


TERMINAL = {"qualified", "rejected", "failed", "late", "missed"}
WILSON_90_Z = 1.6448536269514722


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def wilson_interval_90(successes: int, trials: int) -> dict | None:
    """Return a deterministic two-sided 90% Wilson score interval."""
    if not trials:
        return None
    proportion = successes / trials
    z_squared = WILSON_90_Z**2
    denominator = 1 + z_squared / trials
    centre = (proportion + z_squared / (2 * trials)) / denominator
    margin = WILSON_90_Z * sqrt(
        (proportion * (1 - proportion) + z_squared / (4 * trials)) / trials
    ) / denominator
    return {
        "method": "Wilson score",
        "confidence_level": 0.90,
        "lower": max(0.0, centre - margin),
        "upper": min(1.0, centre + margin),
    }


def delivery_forecast(observations: list[dict]) -> dict:
    """Report qualification uncertainty and duration only from qualified records."""
    qualified = [item for item in observations if item["outcome"] == "qualified"]
    resolved = [item for item in observations if item["outcome"] in TERMINAL]
    durations = [item["qualified_duration_minutes"] for item in qualified if "qualified_duration_minutes" in item]
    by_1030 = [
        item
        for item in qualified
        if parse_time(item["delivered_at"]).time() <= parse_time("2000-01-01T10:30:00").time()
    ]
    qualified_interval = (
        {
            "method": "empirical qualified-observation range",
            "small_sample": True,
            "lower": min(durations),
            "upper": max(durations),
            "sample_count": len(durations),
        }
        if durations
        else None
    )
    qualification_probability = len(by_1030) / len(resolved) if resolved else None
    return {
        "conditional_qualified_duration_minutes": median(durations) if durations else None,
        "conditional_qualified_delivery_empirical_interval_minutes": qualified_interval,
        "qualification_probability_by_1030": qualification_probability,
        "qualification_probability_by_1030_wilson_90_interval": wilson_interval_90(len(by_1030), len(resolved)),
        "qualified": len(qualified),
        "resolved": len(resolved),
        "pending": sum(item["outcome"] == "pending" for item in observations),
        "duration_sample_count": len(durations),
    }


def evidence_maturity(candidate: dict) -> dict:
    """Prototype-only gates: protected fault first, then required coverage."""
    observations = candidate["observations"]
    if any(item.get("protected_hard_fault") for item in observations):
        return {"verdict": "reject", "reason": "protected hard fault", "count": len(observations)}
    covered = {item["market_regime"] for item in observations}
    missing = sorted(set(candidate["required_regimes"]) - covered)
    if missing:
        return {
            "verdict": "insufficient",
            "reason": "required market-regime coverage absent",
            "count": len(observations),
            "missing_regimes": missing,
        }
    return {"verdict": "mature", "reason": "coverage complete and no protected hard fault", "count": len(observations)}


def timing_assessment(baseline: dict, candidate: dict) -> dict:
    """Prototype-only recommendation routing; it never mutates a schedule."""
    if candidate["quality_score"] < baseline["quality_score"] - candidate["material_regression"]:
        return {"action": "ask_user", "reason": "faster candidate has material quality regression"}
    if candidate["diverse"] and candidate["consistent"] and candidate["quality_score"] >= baseline["quality_score"]:
        return {"action": "recommend_promotion", "reason": "diverse, consistent, non-inferior evidence"}
    return {"action": "observe", "reason": "evidence does not justify a recommendation"}


def timing_attribution(cycle: dict) -> dict:
    """Separate 09:00 prefetch from the user wait for the 09:45 cycle."""
    start = parse_time(cycle["cycle_actual_start"])
    delivered = parse_time(cycle["cycle_delivered_at"])
    prefetch_start = parse_time(cycle["prefetch_actual_start"])
    prefetch_end = parse_time(cycle["prefetch_completed_at"])
    return {
        "prefetch_minutes": int((prefetch_end - prefetch_start).total_seconds() / 60),
        "user_wait_minutes": int((delivered - start).total_seconds() / 60),
        "user_wait_starts_at": cycle["cycle_actual_start"],
    }


def schedule_guard(observation: dict, assessment: dict) -> dict:
    """Observation keeps its baseline and has no invented delivery promise."""
    return {
        "baseline_start": observation["baseline_start"],
        "candidate_schedule_changed": False,
        "delivery_deadline": observation.get("delivery_deadline"),
        "recommendation": assessment["action"],
    }
