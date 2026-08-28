"""Privacy-preserving Provider quality aggregation.

Only technical attempt metadata enters this module. Business inputs and model
outputs deliberately have no representation in its row contract.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable


WINDOWS = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30), "all": None}
INSUFFICIENT_SAMPLE_SIZE = 20
SUSPICIOUS_CANCELLATIONS = {"hedge_cancelled_maybe_billed", "deadline_cancelled_maybe_billed"}


def parse_time(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def nearest_rank(values: Iterable[float | int | None], percentile: float) -> float | int | None:
    ordered = sorted(value for value in values if value is not None)
    if not ordered:
        return None
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


class ProviderStatistics:
    """Compute versioned quality metrics from provider_llm_attempt rows."""

    def __init__(self, rows: Iterable[dict[str, Any]], *, as_of: datetime | None = None) -> None:
        self.rows = [dict(row) for row in rows]
        self.as_of = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)

    def summary(self, *, window: str = "24h", endpoint: str | None = None,
                model_family: str | None = None, stage: str | None = None,
                sort: str = "product_success_rate", descending: bool = True) -> dict[str, Any]:
        if window not in WINDOWS:
            raise ValueError("unsupported provider statistics window")
        if sort not in {"product_success_rate", "protocol_success_rate", "estimated_cost_total", "actual_cost_total",
                        "ttft_p50", "duration_p50", "sample_size"}:
            raise ValueError("unsupported provider statistics sort")
        threshold = self.as_of - WINDOWS[window] if WINDOWS[window] else None
        selected = [row for row in self.rows if (threshold is None or parse_time(row["recorded_at"]) >= threshold)]
        if endpoint:
            selected = [row for row in selected if row.get("endpoint_id") == endpoint]
        if model_family:
            selected = [row for row in selected if row.get("model_family") == model_family]
        if stage:
            selected = [row for row in selected if row.get("stage") == stage]
        groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in selected:
            groups[(row.get("endpoint_id") or "", row.get("model") or "", row.get("model_family") or "",
                    row.get("stage") or "")].append(row)
        items = [self._aggregate(key, values) for key, values in groups.items()]
        sort_key = {"ttft_p50": "ttft_ms", "duration_p50": "duration_ms"}.get(sort, sort)
        def value(item: dict[str, Any]) -> float:
            candidate = item[sort_key]["p50"] if sort in {"ttft_p50", "duration_p50"} else item.get(sort_key)
            return float(candidate) if candidate is not None else (-math.inf if descending else math.inf)
        items.sort(key=lambda item: (value(item), item["endpoint_id"], item["model"], item["stage"]), reverse=descending)
        return {
            "contract": "provider-quality/v1", "window": window, "as_of": self.as_of.isoformat().replace("+00:00", "Z"),
            "insufficient_sample_threshold": INSUFFICIENT_SAMPLE_SIZE, "items": items,
        }

    @staticmethod
    def _aggregate(key: tuple[str, str, str, str], rows: list[dict[str, Any]]) -> dict[str, Any]:
        endpoint, model, family, stage = key
        suspicious = [row for row in rows if row.get("cancellation_class") in SUSPICIOUS_CANCELLATIONS]
        eligible = [row for row in rows if row not in suspicious]
        count = len(eligible)
        ttft = [row.get("ttft_ms") for row in eligible if row.get("ttft_ms") is not None]
        duration = [row.get("duration_ms") for row in eligible
                    if row.get("protocol_success") and row.get("duration_ms") is not None]
        product_successes = sum(bool(row.get("product_success")) for row in eligible)
        calibrated_estimates = [float(row["estimated_cost"]) for row in eligible
                                if row.get("estimated_cost") is not None]
        total_estimated = sum(calibrated_estimates)
        actual_values = [float(row["actual_cost"]) for row in eligible if row.get("actual_cost") is not None]
        suspicious_actual_values = [float(row["actual_cost"]) for row in suspicious if row.get("actual_cost") is not None]
        error_counts = Counter(str(row["terminal_error"]) for row in eligible if row.get("terminal_error"))
        costs_by_currency: dict[str, dict[str, float]] = defaultdict(lambda: {"estimated": 0.0, "actual": 0.0})
        for row in eligible:
            if row.get("estimated_cost") is not None or row.get("actual_cost") is not None:
                currency = str(row.get("currency") or "UNKNOWN")
                costs_by_currency[currency]["estimated"] += float(row.get("estimated_cost") or 0)
                costs_by_currency[currency]["actual"] += float(row.get("actual_cost") or 0)
        currencies = sorted(costs_by_currency)
        currency = currencies[0] if len(currencies) == 1 else None
        return {
            "endpoint_id": endpoint, "model": model, "model_family": family, "stage": stage,
            "sample_size": count, "insufficient_data": count < INSUFFICIENT_SAMPLE_SIZE,
            "protocol_success_rate": round(sum(bool(row.get("protocol_success")) for row in eligible) / count, 6) if count else None,
            "product_success_rate": round(product_successes / count, 6) if count else None,
            "no_first_token_rate": round(sum(row.get("ttft_ms") is None for row in eligible) / count, 6) if count else None,
            "ttft_ms": ProviderStatistics._percentiles(ttft),
            "duration_ms": ProviderStatistics._percentiles(duration),
            "error_counts": dict(sorted(error_counts.items())),
            "error_rates": {name: round(value / count, 6) if count else None
                            for name, value in sorted(error_counts.items())},
            "race_participation_count": count,
            "win_count": sum(bool(row.get("winner")) for row in eligible),
            "win_rate": round(sum(bool(row.get("winner")) for row in eligible) / count, 6) if count else None,
            "delayed_start_count": sum(bool(row.get("delayed_start")) for row in eligible),
            "cancel_count": sum(bool(row.get("cancellation_class")) for row in eligible),
            "suspicious_cancel_count": len(suspicious),
            "suspicious_cancel_estimated_cost": round(sum(float(row.get("estimated_cost") or 0) for row in suspicious), 8),
            "suspicious_cancel_actual_cost": round(sum(suspicious_actual_values), 8) if suspicious_actual_values else None,
            "input_tokens": sum(int(row.get("input_tokens") or 0) for row in eligible),
            "cached_input_tokens": sum(int(row.get("cached_input_tokens") or 0) for row in eligible),
            "output_tokens": sum(int(row.get("output_tokens") or 0) for row in eligible),
            "reasoning_tokens": sum(int(row.get("reasoning_tokens") or 0) for row in eligible),
            "estimated_cost_total": round(total_estimated, 8) if calibrated_estimates else None,
            "actual_cost_total": round(sum(actual_values), 8) if actual_values else None,
            "average_cost_per_product_success": round(total_estimated / product_successes, 8)
            if product_successes and calibrated_estimates else None,
            "average_actual_cost_per_product_success": round(sum(actual_values) / product_successes, 8)
            if product_successes and actual_values else None,
            "currency": currency,
            "calibrated_cost_sample_count": sum(bool(row.get("base_price_calibrated")) for row in eligible),
            "uncalibrated_cost_sample_count": sum(not bool(row.get("base_price_calibrated")) for row in eligible),
            "relative_multiplier_only_count": sum(row.get("cost_basis") == "relative_multiplier_only" for row in eligible),
            "costs_by_currency": {name: {kind: round(amount, 8) for kind, amount in values.items()}
                                  for name, values in sorted(costs_by_currency.items())},
        }

    @staticmethod
    def _percentiles(values: list[float | int]) -> dict[str, Any]:
        return {"p50": nearest_rank(values, .50), "p90": nearest_rank(values, .90),
                "p95": nearest_rank(values, .95), "samples": len(values)}
