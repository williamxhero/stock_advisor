from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
import json
from pathlib import Path
from typing import Any, Callable, Protocol
from zoneinfo import ZoneInfo

from .engine import CompanionEngine, iso, parse


SHANGHAI = ZoneInfo("Asia/Shanghai")
CATCH_UP_WINDOW = timedelta(minutes=15)
RESEARCH_STALE_AFTER = timedelta(minutes=12)


@dataclass(frozen=True)
class DailySchedule:
    task_key: str
    at: time
    lead_time: timedelta = timedelta(0)


@dataclass(frozen=True)
class PeriodicSchedule:
    task_key: str
    at: time
    day: int
    months: frozenset[int] | None = None


class TradingCalendar(Protocol):
    def is_trading_day(self, value: Any) -> bool: ...


DAILY_SCHEDULES = (
    DailySchedule("daily.opportunity.0900", time(9, 0), timedelta(minutes=30)),
    DailySchedule("daily.execution.0945", time(9, 45)),
    DailySchedule("daily.execution.1030", time(10, 30)),
    DailySchedule("daily.execution.1430", time(14, 30)),
    DailySchedule("daily.review.1520", time(15, 20)),
)


PERIODIC_SCHEDULES = (
    PeriodicSchedule("periodic.monthly", time(19, 0), 1),
    PeriodicSchedule("periodic.quarterly", time(19, 30), 2, frozenset({1, 4, 7, 10})),
    PeriodicSchedule("periodic.annual", time(20, 0), 3, frozenset({1})),
)


def load_schedules(resources_root: Path) -> tuple[tuple[DailySchedule, ...], tuple[PeriodicSchedule, ...]]:
    payload = json.loads((Path(resources_root) / "schedules" / "tasks.json").read_text(encoding="utf-8"))
    daily = tuple(
        DailySchedule(item["task_key"], time.fromisoformat(item["at"]), timedelta(minutes=int(item.get("lead_minutes", 0))))
        for item in payload["daily"]
    )
    periodic = tuple(
        PeriodicSchedule(
            item["task_key"], time.fromisoformat(item["at"]), int(item["day"]),
            None if item.get("months") == "*" else frozenset(int(month) for month in item["months"]),
        )
        for item in payload["periodic"]
    )
    return daily, periodic


def run_daily_schedule(
    engine: CompanionEngine,
    store: Any,
    at: datetime,
    execute_cycle: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    schedules: tuple[DailySchedule, ...] = DAILY_SCHEDULES,
    trading_calendar: TradingCalendar | None = None,
) -> list[dict[str, Any]]:
    """Create each due intraday cycle exactly once and make missed work explicit."""
    if at.tzinfo is None:
        raise ValueError("scheduler time must be timezone-aware")
    local_at = at.astimezone(SHANGHAI)
    if local_at.weekday() >= 5 or (trading_calendar is not None and not trading_calendar.is_trading_day(local_at.date())):
        return []

    results: list[dict[str, Any]] = []
    for item in schedules:
        scheduled = datetime.combine(local_at.date(), item.at, SHANGHAI)
        if local_at < scheduled - item.lead_time:
            continue
        scheduled_for = scheduled.isoformat(timespec="seconds")
        cycle = store.find_cycle(item.task_key, scheduled_for)
        late = local_at > scheduled + CATCH_UP_WINDOW

        if cycle is not None:
            if cycle["state"] == "queued":
                if late:
                    cycle = engine.mark_missed(cycle["cycle_id"], "启动已超过 15 分钟补偿窗口")
                    results.append(_result(item.task_key, scheduled_for, "missed", cycle))
                    continue
            elif cycle["state"] in {"researching", "researching_m0"}:
                stale = at.astimezone(SHANGHAI) >= parse(cycle["updated_at"]).astimezone(SHANGHAI) + RESEARCH_STALE_AFTER
                if not stale:
                    continue
                if late:
                    cycle = engine.research_failed(cycle["cycle_id"], "研究进程中断且已超过补偿窗口")
                    results.append(_result(item.task_key, scheduled_for, "failed", cycle))
                    continue
                cycle = engine.recover_research(cycle["cycle_id"], "检测到中断的研究进程，自动恢复")
            else:
                continue
        else:
            cycle = engine.start_cycle(item.task_key, scheduled_for, iso(at))
            if late:
                cycle = engine.mark_missed(cycle["cycle_id"], "服务恢复时已超过 15 分钟补偿窗口")
                results.append(_result(item.task_key, scheduled_for, "missed", cycle))
                continue

        try:
            cycle = execute_cycle(cycle)
            results.append(_result(item.task_key, scheduled_for, "started", cycle))
        except Exception as exc:
            current = store.get_cycle(cycle["cycle_id"])
            if current["state"] != "failed":
                current = engine.research_failed(cycle["cycle_id"], str(exc))
            results.append(_result(item.task_key, scheduled_for, "failed", current, str(exc)))
    return results


def run_periodic_schedule(
    engine: CompanionEngine,
    store: Any,
    at: datetime,
    execute_cycle: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    schedules: tuple[PeriodicSchedule, ...] = PERIODIC_SCHEDULES,
) -> list[dict[str, Any]]:
    if at.tzinfo is None:
        raise ValueError("scheduler time must be timezone-aware")
    local_at = at.astimezone(SHANGHAI)
    results: list[dict[str, Any]] = []
    for item in schedules:
        if local_at.day != item.day or (item.months is not None and local_at.month not in item.months):
            continue
        scheduled = datetime.combine(local_at.date(), item.at, SHANGHAI)
        if local_at < scheduled:
            continue
        scheduled_for = scheduled.isoformat(timespec="seconds")
        cycle = store.find_cycle(item.task_key, scheduled_for)
        late = local_at > scheduled + CATCH_UP_WINDOW
        if cycle is not None:
            if cycle["state"] == "queued" and late:
                cycle = engine.mark_missed(cycle["cycle_id"], "启动已超过 15 分钟补偿窗口")
                results.append(_result(item.task_key, scheduled_for, "missed", cycle))
            continue
        cycle = engine.start_cycle(item.task_key, scheduled_for, iso(at))
        if late:
            cycle = engine.mark_missed(cycle["cycle_id"], "服务恢复时已超过 15 分钟补偿窗口")
            results.append(_result(item.task_key, scheduled_for, "missed", cycle))
            continue
        try:
            cycle = execute_cycle(cycle)
            results.append(_result(item.task_key, scheduled_for, "started", cycle))
        except Exception as exc:
            current = store.get_cycle(cycle["cycle_id"])
            if current["state"] != "failed":
                current = engine.research_failed(cycle["cycle_id"], str(exc))
            results.append(_result(item.task_key, scheduled_for, "failed", current, str(exc)))
    return results


def _result(
    task_key: str,
    scheduled_for: str,
    action: str,
    cycle: dict[str, Any],
    error: str | None = None,
) -> dict[str, Any]:
    result = {
        "task_key": task_key,
        "scheduled_for": scheduled_for,
        "action": action,
        "cycle_id": cycle["cycle_id"],
        "state": cycle["state"],
    }
    if error:
        result["error"] = error[-2000:]
    return result
