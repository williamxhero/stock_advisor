"""Task-semantic, frozen Evidence v3 contracts."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .trading_calendar import XshgTradingCalendar


_SHANGHAI = ZoneInfo("Asia/Shanghai")


class EvidenceContractFactory:
    """Hide market-calendar and freshness policy behind one small packet seam."""

    def __init__(self, calendar: Any | None = None) -> None:
        self.calendar = calendar or XshgTradingCalendar()

    def build(
        self, *, task_key: str, stage: str, as_of: str,
        task_profile: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        frozen = self._aware(as_of)
        requirements = self._requirements(task_key, stage, frozen, task_profile)
        contract = {
            "version": 3,
            "as_of": frozen.isoformat().replace("+00:00", "Z"),
            "requirements": requirements,
        }
        if task_profile is not None:
            contract["task_profile"] = {
                "profile_id": str(task_profile["profile_id"]),
                "version": int(task_profile["version"]),
                "evidence_family": str(task_profile["evidence_family"]),
                "stage_strategy": str(task_profile["stage_strategy"]),
            }
        contract["contract_hash"] = self.contract_hash(contract)
        return contract

    def _requirements(
        self, task_key: str, stage: str, as_of: datetime,
        task_profile: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if task_profile is not None and stage == "m0_research":
            return self._manual_requirements(as_of, str(task_profile["evidence_family"]))
        if task_key == "daily.opportunity.0900" and stage == "m0_research":
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
        if task_key == "daily.review.1520" and stage == "m0_research":
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

    def _manual_requirements(self, as_of: datetime, evidence_family: str) -> list[dict[str, Any]]:
        if evidence_family == "intraday_snapshot":
            market_at = as_of
            events_start = as_of
        elif evidence_family == "morning_close":
            local = as_of.astimezone(_SHANGHAI)
            market_at = datetime.combine(local.date(), time(11, 30), _SHANGHAI).astimezone(ZoneInfo("UTC"))
            events_start = market_at
        else:
            market_at = self._latest_completed_close(as_of)
            events_start = market_at
        market_text = self._iso(market_at)
        return [
            {
                "key": "current_market_state", "blocking": True,
                "allowed_coverage": ["covered"],
                "window": {"start": market_text, "end": market_text, "mode": "exact"},
            },
            {
                "key": "material_events_and_counterevidence", "blocking": True,
                "allowed_coverage": ["covered", "checked_no_change"],
                "window": {"start": self._iso(events_start), "end": self._iso(as_of), "mode": "after_start_to_end"},
                "negative_query_terms": ["鍏憡", "鏀跨瓥", "椋庨櫓"],
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

    @staticmethod
    def contract_hash(contract: dict[str, Any]) -> str:
        payload = {key: value for key, value in contract.items() if key != "contract_hash"}
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
