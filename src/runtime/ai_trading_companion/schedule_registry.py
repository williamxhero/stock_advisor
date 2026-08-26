"""Validated, caller-neutral registry for future research schedules.

The desktop UI is the only caller in the first release.  Keeping the registry
separate from Exchange makes a later, explicitly approved caller possible
without giving an LLM write access to SQLite or to schedule files.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import json
from typing import Any, Protocol
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
WORKFLOWS = {"companion_judgment", "periodic_review"}
TRIGGERS = {"trading_day_fixed", "market_relative", "calendar_periodic", "once"}


class TradingCalendar(Protocol):
    def is_trading_day(self, value: date) -> bool: ...


@dataclass(frozen=True)
class ScheduledTemplate:
    schedule_id: str
    task_key: str
    revision: int
    workflow_key: str
    target: datetime
    lead_minutes: int


def _local(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(SHANGHAI) if parsed.tzinfo else parsed.replace(tzinfo=SHANGHAI)


def _clock(value: Any, field: str = "time") -> time:
    try:
        return time.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"{field} must be HH:MM") from exc


def _day(value: Any, field: str = "date") -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD") from exc


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return a normalised immutable revision payload or raise ValueError."""
    name = str(config.get("name") or "").strip()
    workflow = str(config.get("workflow_key") or "")
    trigger = dict(config.get("trigger") or {})
    kind = str(trigger.get("type") or "")
    if not name:
        raise ValueError("任务名称不能为空")
    if len(name) > 80:
        raise ValueError("任务名称不能超过 80 个字符")
    if workflow not in WORKFLOWS:
        raise ValueError("任务内容必须是伴生研判或定期复盘")
    if kind not in TRIGGERS:
        raise ValueError("不支持的触发类型")
    if workflow == "companion_judgment" and kind not in {"trading_day_fixed", "market_relative", "once"}:
        raise ValueError("伴生研判只支持交易日固定时间、开收盘相对时间或单次任务")
    if workflow == "periodic_review" and kind not in {"calendar_periodic", "once"}:
        raise ValueError("定期复盘只支持日历周期或单次任务")
    normalized = {
        "name": name,
        "workflow_key": workflow,
        "trigger": {"type": kind},
        "effective_from": str(config.get("effective_from") or ""),
        "effective_until": str(config.get("effective_until") or ""),
        "note": str(config.get("note") or "").strip(),
    }
    if normalized["effective_from"]:
        _day(normalized["effective_from"], "effective_from")
    if normalized["effective_until"]:
        _day(normalized["effective_until"], "effective_until")
    if normalized["effective_from"] and normalized["effective_until"] and normalized["effective_from"] > normalized["effective_until"]:
        raise ValueError("结束日期不能早于开始日期")
    if kind == "trading_day_fixed":
        normalized["trigger"].update(time=_clock(trigger.get("time")).isoformat(timespec="minutes"), lead_minutes=max(0, int(trigger.get("lead_minutes", 0))))
    elif kind == "market_relative":
        anchor = str(trigger.get("anchor") or "")
        offset = int(trigger.get("offset_minutes", 0))
        if anchor not in {"open", "close"}:
            raise ValueError("相对时间必须选择开盘或收盘")
        if not -240 <= offset <= 240:
            raise ValueError("相对时间必须在开收盘前后 240 分钟内")
        normalized["trigger"].update(anchor=anchor, offset_minutes=offset, lead_minutes=max(0, int(trigger.get("lead_minutes", 0))))
    elif kind == "calendar_periodic":
        months = trigger.get("months", "*")
        if months != "*":
            months = sorted({int(month) for month in months})
            if not months or any(month < 1 or month > 12 for month in months):
                raise ValueError("月份必须在 1 至 12 之间")
        day_value = trigger.get("day")
        if day_value != "last":
            day_value = int(day_value)
            if not 1 <= day_value <= 28:
                raise ValueError("固定日期只能是 1 至 28，或选择每月最后一天")
        normalized["trigger"].update(months=months, day=day_value, time=_clock(trigger.get("time")).isoformat(timespec="minutes"))
    else:
        normalized["trigger"].update(date=_day(trigger.get("date")).isoformat(), time=_clock(trigger.get("time")).isoformat(timespec="minutes"))
    return normalized


def _target_for_day(config: dict[str, Any], value: date) -> datetime | None:
    trigger = config["trigger"]
    kind = trigger["type"]
    if kind == "trading_day_fixed":
        return datetime.combine(value, _clock(trigger["time"]), SHANGHAI)
    if kind == "market_relative":
        anchor = time(9, 30) if trigger["anchor"] == "open" else time(15, 0)
        return datetime.combine(value, anchor, SHANGHAI) + timedelta(minutes=int(trigger["offset_minutes"]))
    if kind == "calendar_periodic":
        months = trigger["months"]
        if months != "*" and value.month not in months:
            return None
        if trigger["day"] == "last":
            next_month = value.replace(day=28) + timedelta(days=4)
            wanted = (next_month - timedelta(days=next_month.day)).day
        else:
            wanted = int(trigger["day"])
        return datetime.combine(value, _clock(trigger["time"]), SHANGHAI) if value.day == wanted else None
    if value.isoformat() != trigger["date"]:
        return None
    return datetime.combine(value, _clock(trigger["time"]), SHANGHAI)


def policy_key(config: dict[str, Any], target: datetime) -> str:
    if config["workflow_key"] == "periodic_review":
        return "periodic.monthly"
    minute = target.hour * 60 + target.minute
    if minute < 9 * 60 + 30:
        return "daily.opportunity.0900"
    if minute < 10 * 60 + 30:
        return "daily.execution.0945"
    if minute < 14 * 60 + 30:
        return "daily.execution.1030"
    if minute < 15 * 60:
        return "daily.execution.1430"
    return "daily.review.1520"


def session_name(target: datetime, workflow_key: str) -> str:
    if workflow_key == "periodic_review":
        return "定期复盘"
    minute = target.hour * 60 + target.minute
    if minute < 9 * 60 + 30: return "盘前"
    if minute < 11 * 60 + 30: return "盘中"
    if minute < 13 * 60: return "午间"
    if minute < 15 * 60: return "盘中"
    return "盘后"


class ScheduleRegistry:
    def __init__(self, store: Any, calendar: TradingCalendar) -> None:
        self.store = store
        self.calendar = calendar

    def seed(self, payload: dict[str, Any]) -> None:
        names = {
            "daily.opportunity.0900": "盘前机会发现", "daily.execution.0945": "开盘执行研判",
            "daily.execution.1030": "盘中研判", "daily.execution.1430": "尾盘执行研判",
            "daily.review.1520": "收盘复盘", "periodic.monthly": "月度复盘",
            "periodic.quarterly": "季度复盘", "periodic.annual": "年度复盘",
        }
        for item in payload.get("daily", []):
            task_key = str(item["task_key"])
            self.store.seed_schedule(task_key, task_key, {
                "name": names.get(task_key, task_key), "workflow_key": "companion_judgment",
                "trigger": {"type": "trading_day_fixed", "time": item["at"], "lead_minutes": item.get("lead_minutes", 0)},
            })
        for item in payload.get("periodic", []):
            task_key = str(item["task_key"])
            self.store.seed_schedule(task_key, task_key, {
                "name": names.get(task_key, task_key), "workflow_key": "periodic_review",
                "trigger": {"type": "calendar_periodic", "months": item.get("months", "*"), "day": item["day"], "time": item["at"]},
            })

    def validate_or_repair(self) -> bool:
        """Return whether a checksummed local repair was required."""
        try:
            for row in self.store.list_schedules():
                validate_config(json.loads(row["config_json"]))
            return False
        except (ValueError, json.JSONDecodeError, TypeError):
            if not self.store.repair_schedule_registry():
                raise RuntimeError("任务配置损坏，且没有可验证的本地快照可自动恢复")
            for row in self.store.list_schedules():
                validate_config(json.loads(row["config_json"]))
            return True
    def list(self, at: datetime | None = None, *, include_inactive: bool = True) -> list[dict[str, Any]]:
        current = (at or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
        output = []
        for row in self.store.list_schedules(include_inactive=include_inactive):
            config = json.loads(row["config_json"])
            next_targets = self.next_targets(config, current, 3)
            output.append({**row, "config": config, "next_targets": [x.isoformat(timespec="seconds") for x in next_targets],
                           "session": session_name(next_targets[0], config["workflow_key"]) if next_targets else None})
        return sorted(output, key=lambda row: (row["status"] != "active", row["next_targets"][0] if row["next_targets"] else "9999", row["schedule_id"]))

    def preview(self, config: dict[str, Any], at: datetime | None = None) -> dict[str, Any]:
        normalized = validate_config(config)
        current = (at or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
        targets = self.next_targets(normalized, current, 3)
        return {"config": normalized, "next_targets": [x.isoformat(timespec="seconds") for x in targets],
                "session": session_name(targets[0], normalized["workflow_key"]) if targets else None,
                "summary": self._summary(normalized, targets[0] if targets else None)}

    def create(self, config: dict[str, Any]) -> dict[str, Any]:
        normalized = validate_config(config)
        return self.store.create_schedule(normalized)

    def update(self, schedule_id: str, expected_version: int, config: dict[str, Any]) -> dict[str, Any]:
        return self.store.update_schedule(schedule_id, expected_version, validate_config(config))

    def pause(self, schedule_id: str, expected_version: int) -> dict[str, Any]:
        return self.store.set_schedule_status(schedule_id, expected_version, "paused")

    def resume(self, schedule_id: str, expected_version: int) -> dict[str, Any]:
        return self.store.set_schedule_status(schedule_id, expected_version, "active")

    def archive(self, schedule_id: str, expected_version: int) -> dict[str, Any]:
        """Logical deletion: no new cycles, immutable history remains visible."""
        return self.store.set_schedule_status(schedule_id, expected_version, "archived")

    def next_targets(self, config: dict[str, Any], current: datetime, count: int) -> list[datetime]:
        start = current.date()
        if config.get("effective_from"):
            start = max(start, _day(config["effective_from"], "effective_from"))
        end = _day(config["effective_until"], "effective_until") if config.get("effective_until") else None
        found: list[datetime] = []
        for offset in range(0, 1100):
            value = start + timedelta(days=offset)
            if end and value > end: break
            target = _target_for_day(config, value)
            if target is None or target <= current: continue
            if config["trigger"]["type"] in {"trading_day_fixed", "market_relative"} and not self.calendar.is_trading_day(value): continue
            found.append(target)
            if len(found) == count: break
        return found

    @staticmethod
    def _summary(config: dict[str, Any], next_target: datetime | None) -> str:
        target = config["trigger"]
        if target["type"] == "trading_day_fixed": rule = f"每个 A 股交易日 {target['time']}"
        elif target["type"] == "market_relative": rule = f"{('开盘' if target['anchor'] == 'open' else '收盘')} {'前' if target['offset_minutes'] < 0 else '后'} {abs(target['offset_minutes'])} 分钟"
        elif target["type"] == "calendar_periodic": rule = f"日历周期 {target['time']}"
        else: rule = f"单次 {target['date']} {target['time']}"
        suffix = f"；下一次：{next_target.strftime('%Y-%m-%d %H:%M')}" if next_target else "；当前生效范围内没有下一次执行"
        return f"{config['name']}将在{rule}触发{suffix}"
