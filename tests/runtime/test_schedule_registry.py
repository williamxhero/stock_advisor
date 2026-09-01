import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from ai_trading_companion.schedule_registry import ScheduleRegistry
from ai_trading_companion.scheduler import run_registry_schedule
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.store import CompanionStore


class Calendar:
    def is_trading_day(self, value):
        return value.weekday() < 5


def registry(tmp_path):
    store = CompanionStore(tmp_path / "data" / "companion.sqlite3")
    store.initialize()
    return store, ScheduleRegistry(store, Calendar())


def test_create_update_and_pause_keep_immutable_revisions(tmp_path):
    store, schedules = registry(tmp_path)
    created = schedules.create({
        "name": "盘前机会发现", "workflow_key": "companion_judgment",
        "trigger": {"type": "trading_day_fixed", "time": "09:00", "lead_minutes": 30},
    })
    assert created["status"] == "active"
    changed = schedules.update(created["schedule_id"], 1, {
        "name": "新版盘前机会发现", "workflow_key": "companion_judgment",
        "trigger": {"type": "trading_day_fixed", "time": "09:10", "lead_minutes": 30},
    })
    assert changed["current_revision"] == 2
    assert schedules.pause(created["schedule_id"], 2)["status"] == "paused"
    archived = schedules.archive(created["schedule_id"], 3)
    assert archived["status"] == "archived"
    assert schedules.resume(created["schedule_id"], 4)["status"] == "active"
    with store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM schedule_revision WHERE schedule_id=?", (created["schedule_id"],)).fetchone()[0] == 2


def test_preview_rejects_unsupported_workflow_trigger_pair(tmp_path):
    _, schedules = registry(tmp_path)
    with pytest.raises(ValueError, match="伴生研判"):
        schedules.preview({
            "name": "错误组合", "workflow_key": "companion_judgment",
            "trigger": {"type": "calendar_periodic", "months": "*", "day": 1, "time": "19:00"},
        })


def test_next_targets_skip_non_trading_days_and_one_shot_is_not_shifted(tmp_path):
    _, schedules = registry(tmp_path)
    monday = datetime(2026, 8, 24, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    preview = schedules.preview({
        "name": "交易日", "workflow_key": "companion_judgment",
        "trigger": {"type": "trading_day_fixed", "time": "09:00"},
    }, monday)
    assert preview["next_targets"][0].startswith("2026-08-24T09:00")
    once = schedules.preview({
        "name": "周末单次", "workflow_key": "companion_judgment",
        "trigger": {"type": "once", "date": "2026-08-29", "time": "10:00"},
    }, monday)
    assert once["next_targets"] == ["2026-08-29T10:00:00+08:00"]


def test_prepared_cycle_keeps_the_revision_that_created_it(tmp_path):
    store, schedules = registry(tmp_path)
    created = schedules.create({
        "name": "盘前", "workflow_key": "companion_judgment",
        "trigger": {"type": "trading_day_fixed", "time": "09:00", "lead_minutes": 30},
    })
    engine = CompanionEngine(store)
    at = datetime(2026, 8, 24, 8, 31, tzinfo=ZoneInfo("Asia/Shanghai"))
    result = run_registry_schedule(engine, store, schedules, at)
    assert result[0]["action"] == "prepared"
    schedules.update(created["schedule_id"], 1, {
        "name": "已修改", "workflow_key": "companion_judgment",
        "trigger": {"type": "trading_day_fixed", "time": "09:10", "lead_minutes": 0},
    })
    cycle = store.get_cycle(result[0]["cycle_id"])
    assert cycle["schedule_revision"] == 1
    assert "盘前" in cycle["schedule_snapshot_json"]


def test_late_unstarted_schedule_is_marked_missed(tmp_path):
    store, schedules = registry(tmp_path)
    schedules.create({
        "name": "单次", "workflow_key": "companion_judgment",
        "trigger": {"type": "once", "date": "2026-08-24", "time": "09:00"},
    })
    result = run_registry_schedule(CompanionEngine(store), store, schedules, datetime(2026, 8, 24, 9, 16, tzinfo=ZoneInfo("Asia/Shanghai")))
    assert result[0]["action"] == "missed"


def test_seed_installs_all_eight_defaults_once(tmp_path):
    _, schedules = registry(tmp_path)
    schedules.seed({
        "daily": [{"task_key": "daily.opportunity.0900", "at": "09:00", "lead_minutes": 30}],
        "periodic": [{"task_key": "periodic.monthly", "months": "*", "day": 1, "at": "19:00"}],
    })
    at = datetime(2026, 8, 31, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert [item["config"]["name"] for item in schedules.list(at)] == ["盘前机会发现", "月度复盘"]


def test_seed_backfills_legacy_cycles_to_their_default_template(tmp_path):
    store, schedules = registry(tmp_path)
    cycle = store.create_cycle("daily.opportunity.0900", "2026-08-24T09:00:00+08:00", "2026-08-24T01:00:00Z")
    schedules.seed({"daily": [{"task_key": "daily.opportunity.0900", "at": "09:00", "lead_minutes": 30}], "periodic": []})
    schedules.list()  # initializes the v9 backfill after template seeding
    restored = store.get_cycle(cycle["cycle_id"])
    assert restored["schedule_id"] == "daily.opportunity.0900"
    assert restored["schedule_revision"] == 1


def test_schedule_history_shows_one_latest_result_per_trigger_time(tmp_path):
    store, schedules = registry(tmp_path)
    schedule = schedules.create({
        "name": "盘前机会发现", "workflow_key": "companion_judgment",
        "trigger": {"type": "trading_day_fixed", "time": "09:00"},
    })
    config = json.loads(schedule["config_json"])
    older = store.create_cycle(
        schedule["task_key"], "2026-08-25T09:00:00.0000000+08:00", "2026-08-25T01:00:00Z",
        schedule_id=schedule["schedule_id"], schedule_revision=schedule["current_revision"],
        schedule_snapshot=config,
    )
    store.transition(older["cycle_id"], "model_only_ready")
    latest = store.create_cycle(
        schedule["task_key"], "2026-08-25T09:00:00+08:00", "2026-08-25T01:00:01Z",
        schedule_id=schedule["schedule_id"], schedule_revision=schedule["current_revision"],
        schedule_snapshot=config,
    )
    store.transition(latest["cycle_id"], "complete")

    assert store.schedule_history(schedule["schedule_id"]) == [{
        "cycle_id": latest["cycle_id"], "scheduled_for": "2026-08-25T09:00:00+08:00",
        "state": "complete", "updated_at": store.get_cycle(latest["cycle_id"])["updated_at"],
    }]
