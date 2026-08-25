"""Fail-closed local XSHG trading calendar with explicit local overrides."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path


class TradingCalendarUnavailable(RuntimeError):
    pass


class XshgTradingCalendar:
    def __init__(self, overrides_path: Path | None = None) -> None:
        self.overrides_path = Path(overrides_path) if overrides_path else None

    def is_trading_day(self, value: date) -> bool:
        overrides = self._overrides()
        key = value.isoformat()
        if key in overrides.get("open", []):
            return True
        if key in overrides.get("closed", []):
            return False
        try:
            import exchange_calendars as xcals
            import pandas as pd
        except ImportError as exc:
            raise TradingCalendarUnavailable(
                "本地交易日历不可用；请修复发布版 Python 依赖或配置本地覆盖文件。"
            ) from exc
        return bool(xcals.get_calendar("XSHG").is_session(pd.Timestamp(value)))

    def _overrides(self) -> dict[str, list[str]]:
        if self.overrides_path is None or not self.overrides_path.exists():
            return {"open": [], "closed": []}
        data = json.loads(self.overrides_path.read_text(encoding="utf-8"))
        return {
            "open": [str(item) for item in data.get("open", [])],
            "closed": [str(item) for item in data.get("closed", [])],
        }
