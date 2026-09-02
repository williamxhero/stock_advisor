"""Task-semantic, frozen Evidence v4 contracts."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .trading_calendar import XshgTradingCalendar


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_INTRADAY_MARKET_MAX_AGE = timedelta(minutes=15)
_INTRADAY_EVENT_ANCHORS = {
    "daily.execution.0945": time(9, 0),
    "daily.execution.1030": time(9, 45),
    "daily.execution.1430": time(10, 30),
}


class EvidenceContractFactory:
    """Hide market-calendar and freshness policy behind one small packet seam."""

    def __init__(self, calendar: Any | None = None) -> None:
        self.calendar = calendar or XshgTradingCalendar()

    def build(
        self, *, task_key: str, stage: str, as_of: str,
        task_profile: dict[str, Any] | None = None,
        internal_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        frozen = self._aware(as_of)
        requirements = self._requirements(task_key, stage, frozen, task_profile, internal_context or {})
        contract = {
            "version": 4,
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
        internal_context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if task_profile is not None and stage == "m0_research":
            return self._manual_requirements(as_of, str(task_profile["evidence_family"]))
        if task_key == "daily.opportunity.0900" and stage in {"m0_research", "m1_research"}:
            close = self._latest_completed_close(as_of)
            close_text = self._iso(close)
            return self._with_portfolio_requirements([
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
            ], market_window={"start": close_text, "end": close_text, "mode": "exact"},
               events_window={"start": close_text, "end": self._iso(as_of), "mode": "after_start_to_end"},
               internal_context=internal_context or {})
        if task_key == "daily.review.1520" and stage in {"m0_research", "m1_research"}:
            close = self._latest_completed_close(as_of)
            close_text = self._iso(close)
            prior_close_text = self._iso(self._latest_completed_close(close - timedelta(seconds=1)))
            context = internal_context or {}
            holdings = [str(value) for value in context.get("portfolio_entities") or [] if str(value)]
            return [
                {
                    "key": "indices_close", "blocking": True,
                    "allowed_coverage": ["covered"],
                    "window": {"start": close_text, "end": close_text, "mode": "exact"},
                    "evidence_terms": [["上证", "沪指"], ["深成指", "深证成指"], ["创业板"], ["涨", "跌", "%"]],
                    "minimum_numeric_facts": 3,
                },
                {
                    "key": "turnover_compare", "blocking": True,
                    "allowed_coverage": ["covered"],
                    "window": {"start": close_text, "end": close_text, "mode": "exact"},
                    "evidence_terms": [["成交额", "成交"], ["亿", "万亿"], ["昨日", "前一交易日", "上一交易日", "较前日", "较上日"]],
                    "minimum_numeric_facts": 2,
                },
                {
                    "key": "market_breadth", "blocking": True,
                    "allowed_coverage": ["covered"],
                    "window": {"start": close_text, "end": close_text, "mode": "exact"},
                    "evidence_terms": [["上涨"], ["下跌"], ["家", "只"]],
                    "minimum_numeric_facts": 2,
                },
                {
                    "key": "themes_and_capacity_cores", "blocking": True,
                    "allowed_coverage": ["covered"],
                    "window": {"start": close_text, "end": close_text, "mode": "exact"},
                    "evidence_terms": [["板块", "题材"], ["领涨", "涨幅居前", "强势"], ["领跌", "跌幅居前", "弱势"]],
                    "minimum_named_entities": 2,
                },
                {
                    "key": "events_and_counterevidence", "blocking": True,
                    "allowed_coverage": ["covered", "checked_no_change"],
                    "window": {"start": prior_close_text, "end": self._iso(as_of), "mode": "after_start_to_end"},
                    "negative_query_terms": ["公告", "政策", "风险"],
                },
                {
                    "key": "prior_judgment_changes", "blocking": True,
                    "allowed_coverage": ["covered", "checked_no_change"],
                    "evidence_class": "internal_runtime",
                    "internal_record_count": int(context.get("prior_judgment_count") or 0),
                },
                {
                    "key": "portfolio_market_state", "blocking": True,
                    "allowed_coverage": ["covered", "checked_no_change"],
                    "window": {"start": close_text, "end": close_text, "mode": "exact"},
                    "evidence_class": "public_if_present",
                    "required_entities": holdings,
                    "minimum_numeric_facts": 4 * len(holdings) if holdings else 0,
                },
                {
                    "key": "portfolio_events_and_counterevidence", "blocking": True,
                    "allowed_coverage": ["covered", "checked_no_change"],
                    "window": {"start": prior_close_text, "end": self._iso(as_of), "mode": "after_start_to_end"},
                    "evidence_class": "public_if_present",
                    "required_entities": holdings,
                    "negative_query_terms": ["公告", "停复牌", "财报", "风险"],
                },
                {
                    "key": "forum_and_sentiment", "blocking": False,
                    "allowed_coverage": ["covered", "checked_no_change"],
                    "window": {"start": prior_close_text, "end": self._iso(as_of), "mode": "after_start_to_end"},
                },
            ]
        if task_key in _INTRADAY_EVENT_ANCHORS and stage in {"m0_research", "m1_research"}:
            return self._scheduled_intraday_requirements(task_key, as_of, internal_context or {})
        market_window = {"start": self._iso(as_of), "end": self._iso(as_of), "mode": "exact"}
        return self._with_portfolio_requirements([
            {
                "key": "current_market_state", "blocking": True,
                "allowed_coverage": ["covered"],
                "window": market_window,
            },
            {
                "key": "material_events_and_counterevidence", "blocking": True,
                "allowed_coverage": ["covered", "checked_no_change"],
                "window": {"start": self._iso(as_of), "end": self._iso(as_of), "mode": "after_start_to_end"},
                "negative_query_terms": ["公告", "政策", "风险"],
            },
        ], market_window=market_window,
           events_window={"start": self._iso(as_of), "end": self._iso(as_of), "mode": "after_start_to_end"},
           internal_context=internal_context or {})

    def _scheduled_intraday_requirements(
        self, task_key: str, as_of: datetime, internal_context: dict[str, Any],
    ) -> list[dict[str, Any]]:
        local = as_of.astimezone(_SHANGHAI)
        event_start = datetime.combine(
            local.date(), _INTRADAY_EVENT_ANCHORS[task_key], _SHANGHAI,
        ).astimezone(ZoneInfo("UTC"))
        market_start = as_of - _INTRADAY_MARKET_MAX_AGE
        market_window = {
            "start": self._iso(market_start), "end": self._iso(as_of),
            "mode": "after_start_to_end",
        }
        events_window = {
            "start": self._iso(event_start), "end": self._iso(as_of),
            "mode": "after_start_to_end",
        }
        return self._with_portfolio_requirements([
            {
                "key": "current_market_state", "blocking": True,
                "allowed_coverage": ["covered"],
                "window": market_window,
            },
            {
                "key": "material_events_and_counterevidence", "blocking": True,
                "allowed_coverage": ["covered", "checked_no_change"],
                "window": events_window,
                "negative_query_terms": ["公告", "政策", "风险"],
            },
        ], market_window=market_window, events_window=events_window, internal_context=internal_context)

    @staticmethod
    def _with_portfolio_requirements(
        requirements: list[dict[str, Any]], *, market_window: dict[str, Any],
        events_window: dict[str, Any], internal_context: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Attach the deterministic blocking facts every formal analysis needs."""
        # Runtime always supplies this key from its authoritative portfolio store.
        # Keeping direct factory callers on their original shape preserves read-only
        # v3 artifact tests and prevents callers without a portfolio snapshot from
        # silently claiming an empty portfolio.
        if "portfolio_entities" not in internal_context:
            return requirements
        holdings = [str(value) for value in internal_context.get("portfolio_entities") or [] if str(value)]
        return [
            *requirements,
            {
                "key": "market_breadth", "blocking": True,
                "allowed_coverage": ["covered"], "window": market_window,
                "minimum_numeric_facts": 3,
            },
            {
                "key": "portfolio_market_state", "blocking": True,
                "allowed_coverage": ["covered"] if holdings else ["checked_no_change"],
                "window": market_window, "evidence_class": "public_if_present",
                "required_entities": holdings,
                "minimum_numeric_facts": 4 * len(holdings) if holdings else 0,
            },
            {
                "key": "portfolio_events_and_counterevidence", "blocking": True,
                "allowed_coverage": ["covered", "checked_no_change"],
                "window": events_window, "evidence_class": "public_if_present", "required_entities": holdings,
                "negative_query_terms": ["公告", "停复牌", "财报", "风险"],
            },
        ]

    def _manual_requirements(self, as_of: datetime, evidence_family: str) -> list[dict[str, Any]]:
        if evidence_family == "intraday_snapshot":
            market_window = {
                "start": self._iso(as_of - _INTRADAY_MARKET_MAX_AGE),
                "end": self._iso(as_of),
                "mode": "after_start_to_end",
            }
            events_start = self._manual_intraday_anchor(as_of)
        elif evidence_family == "morning_close":
            local = as_of.astimezone(_SHANGHAI)
            market_start = datetime.combine(local.date(), time(11, 15), _SHANGHAI).astimezone(ZoneInfo("UTC"))
            events_start = datetime.combine(local.date(), time(10, 30), _SHANGHAI).astimezone(ZoneInfo("UTC"))
            market_window = {
                "start": self._iso(market_start),
                "end": self._iso(as_of),
                "mode": "after_start_to_end",
            }
        else:
            market_at = self._latest_completed_close(as_of)
            events_start = market_at
            market_text = self._iso(market_at)
            market_window = {"start": market_text, "end": market_text, "mode": "exact"}
        return [
            {
                "key": "current_market_state", "blocking": True,
                "allowed_coverage": ["covered"],
                "window": market_window,
            },
            {
                "key": "material_events_and_counterevidence", "blocking": True,
                "allowed_coverage": ["covered", "checked_no_change"],
                "window": {"start": self._iso(events_start), "end": self._iso(as_of), "mode": "after_start_to_end"},
                "negative_query_terms": ["公告", "政策", "风险"],
            },
        ]

    @staticmethod
    def _manual_intraday_anchor(as_of: datetime) -> datetime:
        local = as_of.astimezone(_SHANGHAI)
        anchors = (time(9, 0), time(9, 45), time(10, 30), time(14, 30))
        selected = max((anchor for anchor in anchors if anchor <= local.time()), default=time(9, 0))
        return datetime.combine(local.date(), selected, _SHANGHAI).astimezone(ZoneInfo("UTC"))

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
