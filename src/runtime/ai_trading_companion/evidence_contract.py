"""Task-semantic, frozen Evidence v3 contracts."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .trading_calendar import XshgTradingCalendar


_SHANGHAI = ZoneInfo("Asia/Shanghai")


class EvidenceContractFactory:
    """Hide market-calendar and freshness policy behind one small packet seam."""

    def __init__(self, calendar: Any | None = None) -> None:
        self.calendar = calendar or XshgTradingCalendar()

    def build(self, *, task_key: str, stage: str, as_of: str) -> dict[str, Any]:
        frozen = self._aware(as_of)
        requirements = self._requirements(task_key, stage, frozen)
        return {
            "version": 3,
            "as_of": frozen.isoformat().replace("+00:00", "Z"),
            "requirements": requirements,
        }

    def _requirements(self, task_key: str, stage: str, as_of: datetime) -> list[dict[str, Any]]:
        if task_key == "daily.opportunity.0900" and stage in {"m0_research", "m1_research"}:
            close = self._latest_completed_close(as_of)
            close_text = self._iso(close)
            return [
                {
                    "key": "current_market_state", "blocking": True,
                    "allowed_coverage": ["covered"],
                    "window": {"start": close_text, "end": close_text, "mode": "exact"},
                },
                {
                    "key": "material_events_and_counterevidence", "blocking": True,
                    "allowed_coverage": ["covered", "checked_no_change"],
                    "window": {"start": close_text, "end": self._iso(as_of), "mode": "after_start_to_end"},
                    "negative_query_terms": ["公告", "政策", "风险"],
                },
            ]
        if task_key == "daily.review.1520" and stage in {"m0_research", "m1_research"}:
            close = self._latest_completed_close(as_of)
            close_text = self._iso(close)
            prior_close_text = self._iso(self._latest_completed_close(close - timedelta(seconds=1)))
            return [
                {
                    "key": "current_market_state", "blocking": True,
                    "allowed_coverage": ["covered"],
                    "window": {"start": close_text, "end": close_text, "mode": "exact"},
                },
                {
                    "key": "material_events_and_counterevidence", "blocking": True,
                    "allowed_coverage": ["covered", "checked_no_change"],
                    "window": {"start": prior_close_text, "end": self._iso(as_of), "mode": "after_start_to_end"},
                    "negative_query_terms": ["公告", "政策", "风险"],
                },
            ]
        return [
            {
                "key": "current_market_state", "blocking": True,
                "allowed_coverage": ["covered"],
                "window": {"start": self._iso(as_of), "end": self._iso(as_of), "mode": "exact"},
            },
            {
                "key": "material_events_and_counterevidence", "blocking": True,
                "allowed_coverage": ["covered", "checked_no_change"],
                "window": {"start": self._iso(as_of), "end": self._iso(as_of), "mode": "after_start_to_end"},
                "negative_query_terms": ["公告", "政策", "风险"],
            },
        ]

    def _latest_completed_close(self, as_of: datetime) -> datetime:
        local = as_of.astimezone(_SHANGHAI)
        candidate = local.date()
        if local.timetz().replace(tzinfo=None) < time(15, 0):
            candidate -= timedelta(days=1)
        while not self.calendar.is_trading_day(candidate):
            candidate -= timedelta(days=1)
        return datetime.combine(candidate, time(15, 0), tzinfo=_SHANGHAI).astimezone(ZoneInfo("UTC"))

    @staticmethod
    def _aware(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("evidence contract as_of must include timezone")
        return parsed.astimezone(ZoneInfo("UTC"))

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z")
