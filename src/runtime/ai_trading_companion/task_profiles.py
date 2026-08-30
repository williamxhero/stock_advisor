"""Deterministic, versioned profiles for manual formal analysis."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .trading_calendar import XshgTradingCalendar


_SHANGHAI = ZoneInfo("Asia/Shanghai")


class AnalysisClarificationRequired(ValueError):
    """The structured intent does not safely identify a formal-analysis profile."""


class ManualAnalysisProfileResolver:
    """Resolve a manual request without borrowing a scheduled occurrence.

    The returned mapping is deliberately small and JSON-safe: it is persisted on
    the cycle, so subsequent stages use the exact profile selected at request
    time even if profile definitions later evolve.
    """

    VERSION = 1

    _PROFILES = {
        "pre_market_opportunity": {
            "task_key": "daily.opportunity.0900",
            "evidence_family": "previous_close",
            "stage_strategy": "pre_market_baseline",
            "h0_window_minutes": 15,
            "m1_publish_window_minutes": 25,
        },
        "intraday_execution": {
            "task_key": "daily.execution.0945",
            "evidence_family": "intraday_snapshot",
            "stage_strategy": "intraday_incremental",
            "h0_window_minutes": 20,
            "m1_publish_window_minutes": 30,
        },
        "lunch_break_analysis": {
            "task_key": "daily.execution.1030",
            "evidence_family": "morning_close",
            "stage_strategy": "lunch_break_reconciliation",
            "h0_window_minutes": 20,
            "m1_publish_window_minutes": 35,
        },
        "post_close_review": {
            "task_key": "daily.review.1520",
            "evidence_family": "completed_close",
            "stage_strategy": "post_close_review",
            "h0_window_minutes": 20,
            "m1_publish_window_minutes": 35,
        },
        "non_trading_research": {
            "task_key": "periodic.monthly",
            "evidence_family": "latest_completed_close",
            "stage_strategy": "non_trading_research",
            "h0_window_minutes": 60,
            "m1_publish_window_minutes": 120,
        },
    }

    def __init__(self, calendar: Any | None = None) -> None:
        self.calendar = calendar or XshgTradingCalendar()

    def resolve(self, requested_at: str, analysis: dict[str, Any]) -> dict[str, Any]:
        self._require_analysis(analysis)
        requested = self._aware(requested_at).astimezone(_SHANGHAI)
        profile_id = self._profile_id(requested, str(analysis["time_scope"]).strip())
        definition = self._PROFILES[profile_id]
        return {
            "profile_id": profile_id,
            "version": self.VERSION,
            "task_key": definition["task_key"],
            "evidence_family": definition["evidence_family"],
            "stage_strategy": definition["stage_strategy"],
            "delivery_window": {
                "h0_window_minutes": definition["h0_window_minutes"],
                "m1_publish_window_minutes": definition["m1_publish_window_minutes"],
            },
            "requested_at": requested.isoformat(),
            "analysis": {
                "subject": str(analysis["subject"]).strip(),
                "time_scope": str(analysis["time_scope"]).strip(),
                "goal": str(analysis["goal"]).strip(),
            },
        }

    def delivery_deadlines(self, profile: dict[str, Any], ready_at: str) -> dict[str, str]:
        """Create a manual delivery window relative to actual M0 readiness."""
        window = profile.get("delivery_window") or {}
        try:
            h0_minutes = int(window["h0_window_minutes"])
            publish_minutes = int(window["m1_publish_window_minutes"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("manual profile delivery_window is invalid") from exc
        if h0_minutes < 1 or publish_minutes <= h0_minutes:
            raise ValueError("manual profile delivery window is invalid")
        ready = self._aware(ready_at)
        return {
            "h0_auto_submit_at": (ready + timedelta(minutes=h0_minutes)).isoformat(),
            "m1_publish_deadline": (ready + timedelta(minutes=publish_minutes)).isoformat(),
        }

    def _profile_id(self, requested: datetime, time_scope: str) -> str:
        if not self.calendar.is_trading_day(requested.date()):
            actual = "non_trading_research"
        else:
            clock = requested.timetz().replace(tzinfo=None)
            if clock < time(9, 30):
                actual = "pre_market_opportunity"
            elif clock < time(11, 30):
                actual = "intraday_execution"
            elif clock < time(13, 0):
                actual = "lunch_break_analysis"
            elif clock < time(15, 0):
                actual = "intraday_execution"
            else:
                actual = "post_close_review"
        accepted_scopes = {
            "pre_market_opportunity": {"current_session", "pre_market"},
            "intraday_execution": {"current_session", "intraday"},
            "lunch_break_analysis": {"current_session", "lunch_break"},
            "post_close_review": {"current_session", "post_close"},
            "non_trading_research": {"current_session", "non_trading_period", "next_trading_session", "weekend"},
        }
        if time_scope not in accepted_scopes[actual]:
            raise AnalysisClarificationRequired(
                f"analysis.time_scope '{time_scope}' does not match the current market session"
            )
        return actual

    @staticmethod
    def _require_analysis(analysis: dict[str, Any]) -> None:
        if not isinstance(analysis, dict):
            raise AnalysisClarificationRequired("analysis details are required")
        for field in ("subject", "time_scope", "goal"):
            if not str(analysis.get(field) or "").strip():
                raise AnalysisClarificationRequired(f"analysis.{field} requires clarification")

    @staticmethod
    def _aware(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("requested_at must be timezone-aware")
        return parsed
