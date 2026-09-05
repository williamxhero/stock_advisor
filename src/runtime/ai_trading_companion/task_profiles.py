"""Deterministic, versioned profiles for manual formal analysis."""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
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

    VERSION = 4

    _PROFILES = {
        "pre_market_opportunity": {
            "task_key": "daily.opportunity.0900",
            "display_name": "盘前市场机会研判",
            "evidence_family": "previous_close",
            "stage_strategy": "pre_market_baseline",
            "h0_window_minutes": 15,
            "m1_publish_window_minutes": 25,
        },
        "intraday_execution": {
            "task_key": "daily.execution.0945",
            "display_name": "盘中市场环境研判",
            "evidence_family": "intraday_snapshot",
            "stage_strategy": "intraday_incremental",
            "h0_window_minutes": 20,
            "m1_publish_window_minutes": 30,
        },
        "lunch_break_analysis": {
            "task_key": "daily.execution.1030",
            "display_name": "午间市场环境研判",
            "evidence_family": "morning_close",
            "stage_strategy": "lunch_break_reconciliation",
            "h0_window_minutes": 20,
            "m1_publish_window_minutes": 35,
        },
        "post_close_review": {
            "task_key": "daily.review.1520",
            "display_name": "收盘市场复盘",
            "evidence_family": "completed_close",
            "stage_strategy": "post_close_review",
            "h0_window_minutes": 20,
            "m1_publish_window_minutes": 35,
        },
        "non_trading_outlook": {
            "task_key": "manual.non_trading_outlook",
            "display_name": "非交易日市场环境总结与下一交易日预判",
            "evidence_family": "latest_completed_close",
            "stage_strategy": "non_trading_research",
            "h0_window_minutes": 60,
            "m1_publish_window_minutes": 120,
        },
        "weekend_review": {
            "task_key": "manual.non_trading_outlook",
            "display_name": "周末整周市场与持仓复盘",
            "evidence_family": "completed_trading_week",
            "stage_strategy": "weekend_review",
            "h0_window_minutes": 60,
            "m1_publish_window_minutes": 120,
        },
    }

    def __init__(self, calendar: Any | None = None) -> None:
        self.calendar = calendar or XshgTradingCalendar()

    def resolve(self, requested_at: str, analysis: dict[str, Any]) -> dict[str, Any]:
        self._require_analysis(analysis)
        requested = self._aware(requested_at).astimezone(_SHANGHAI)
        requested_time_scope = str(analysis["time_scope"]).strip()
        time_scope = self._normalize_time_scope(requested_time_scope, requested)
        profile_id = self._profile_id(requested, time_scope)
        definition = self._PROFILES[profile_id]
        return {
            "profile_id": profile_id,
            "version": self.VERSION,
            "task_key": definition["task_key"],
            "display_name": definition["display_name"],
            "evidence_family": definition["evidence_family"],
            "stage_strategy": definition["stage_strategy"],
            "delivery_window": {
                "h0_window_minutes": definition["h0_window_minutes"],
                "m1_publish_window_minutes": definition["m1_publish_window_minutes"],
            },
            "requested_at": requested.isoformat(),
            "analysis": {
                "subject": str(analysis["subject"]).strip(),
                "time_scope": time_scope,
                "requested_time_scope": requested_time_scope,
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
        if time_scope == "post_close":
            # An explicit completed-close request is anchored to the latest
            # finished trading session, independent of the wall-clock session
            # in which the user asks for it.
            actual = "post_close_review"
        elif time_scope == "weekend" and not self.calendar.is_trading_day(requested.date()):
            actual = "weekend_review"
        elif not self.calendar.is_trading_day(requested.date()):
            actual = "non_trading_outlook"
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
            "pre_market_opportunity": {"current_session", "pre_market", "next_trading_session"},
            "intraday_execution": {"current_session", "intraday"},
            "lunch_break_analysis": {"current_session", "lunch_break"},
            "post_close_review": {"current_session", "post_close"},
            "non_trading_outlook": {"current_session", "non_trading_period", "next_trading_session", "weekend"},
            "weekend_review": {"weekend"},
        }
        if time_scope not in accepted_scopes[actual]:
            raise AnalysisClarificationRequired(
                f"analysis.time_scope '{time_scope}' does not match the current market session"
            )
        return actual

    def _normalize_time_scope(self, time_scope: str, requested: datetime) -> str:
        """Map unambiguous user-facing session phrases to canonical scopes."""
        folded = time_scope.casefold()
        dated_session = re.search(
            r"(?P<year>\d{4})" + "\u5e74" + r"(?P<month>\d{1,2})" + "\u6708"
            + r"(?P<day>\d{1,2})" + "\u65e5"
            + r"(?:\s*(?P<hour>\d{1,2}):(?P<minute>\d{2}))?",
            time_scope,
        )
        if dated_session:
            try:
                target = date(
                    int(dated_session.group("year")),
                    int(dated_session.group("month")),
                    int(dated_session.group("day")),
                )
            except ValueError:
                target = None
            explicit_hour = dated_session.group("hour")
            explicit_minute = dated_session.group("minute")
            explicit_clock = (
                time(int(explicit_hour), int(explicit_minute))
                if explicit_hour is not None and explicit_minute is not None
                and 0 <= int(explicit_hour) <= 23 and 0 <= int(explicit_minute) <= 59
                else None
            )
            is_close_endpoint = "\u6536\u76d8" in time_scope or (
                explicit_clock is not None and explicit_clock >= time(15, 0)
            )
            latest = requested.date()
            if requested.timetz().replace(tzinfo=None) < time(15, 0):
                latest -= timedelta(days=1)
            while not self.calendar.is_trading_day(latest):
                latest -= timedelta(days=1)
            if (
                target is not None
                and is_close_endpoint
                and target <= latest
                and self.calendar.is_trading_day(target)
            ):
                return "post_close"
        same_day_close = (
            "\u6536\u76d8" in time_scope
            and any(anchor in time_scope for anchor in ("\u4eca\u5929", "\u4eca\u65e5", "\u5f53\u5929"))
        )
        if (
            same_day_close
            and self.calendar.is_trading_day(requested.date())
            and requested.timetz().replace(tzinfo=None) >= time(15, 0)
        ):
            return "post_close"
        completed_close_terms = (
            "已收盘交易日", "最近交易日收盘", "最近一个交易日收盘", "今日收盘", "当天收盘", "盘后",
            "收盘至下一交易日", "收盘到下一交易日",
        )
        completed_close_english = (
            "latest completed trading day", "most recent completed trading day",
            "latest completed close", "post-close", "post close",
            "close through next trading session", "close to next trading session",
        )
        close_to_next_session = (
            "收盘" in time_scope and any(term in time_scope for term in ("下一交易日", "下一个交易日"))
        ) or ("close" in folded and "next trading" in folded)
        chinese_completed_close_anchor = bool(re.search(
            r"(?:(?:\d{4}年)?\d{1,2}月\d{1,2}日|最近(?:一个)?交易日|上一交易日|前一交易日|昨日|已)[^。；，]{0,10}收盘",
            time_scope,
        ))
        chinese_present_endpoint = any(term in time_scope for term in (
            "至当前", "到当前", "截至当前", "延伸到当前",
            "至现在", "到现在", "截至现在", "延伸到现在",
        ))
        english_completed_close_anchor = bool(re.search(
            r"(?:latest|recent|previous|yesterday|\d{1,2}[/-]\d{1,2}|"
            r"january|february|march|april|may|june|july|august|september|october|november|december)"
            r"[^.]{0,24}\bclose\b",
            folded,
        ))
        english_present_endpoint = bool(re.search(
            r"\b(?:through|to|until|as of)\s+(?:the\s+)?(?:current|present|now)\b",
            folded,
        ))
        close_to_present = (
            chinese_completed_close_anchor and chinese_present_endpoint
        ) or (
            english_completed_close_anchor and english_present_endpoint
        )
        if close_to_next_session or close_to_present or any(term in time_scope for term in completed_close_terms) or any(
            term in folded for term in completed_close_english
        ):
            return "post_close"
        is_weekend_to_next_session = (
            ("周末" in time_scope and ("周一" in time_scope or "下一交易日" in time_scope))
            or ("weekend" in folded and ("monday" in folded or "next trading session" in folded))
        )
        return "next_trading_session" if is_weekend_to_next_session else time_scope

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
