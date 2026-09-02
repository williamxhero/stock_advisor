from ai_trading_companion.store import CompanionStore
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion import __main__ as runtime


def test_history_page_uses_scheduled_date_and_deduplicates_retry(tmp_path):
    store = CompanionStore(tmp_path / "companion.sqlite3")
    store.initialize()
    first = store.create_cycle("daily.opportunity.0900", "2026-08-26T09:00:00+08:00", "2026-08-26T01:00:00Z")
    retry = store.create_cycle("daily.opportunity.0900", "2026-08-26T09:00:00+08:00", "2026-08-26T01:00:00Z")
    store.append_artifact(retry["cycle_id"], "m0", "ai", "# 26号", "2026-08-26T01:00:00Z")

    page = store.history_page(limit=10)

    assert len(page["items"]) == 1
    assert page["items"][0]["cycle_id"] == retry["cycle_id"]
    assert page["items"][0]["scheduled_for"].startswith("2026-08-26")


def test_client_event_log_has_monotonic_sequence(tmp_path):
    store = CompanionStore(tmp_path / "companion.sqlite3")
    store.initialize()
    cycle = store.create_cycle("daily.opportunity.0900", "2026-08-26T09:00:00+08:00", "2026-08-26T01:00:00Z")
    store.queue_event(cycle["cycle_id"], "m0.ready", {"cycle": cycle})
    store.queue_portfolio_event("portfolio.snapshot", {"positions": []})

    events = store.client_events(0)

    assert [event["sequence"] for event in events] == sorted(event["sequence"] for event in events)
    assert len(events) == 2


def test_today_snapshot_exposes_the_runtime_xshg_trading_day_decision(tmp_path, monkeypatch):
    class WeekendCalendar:
        def is_trading_day(self, value):
            return False

    class Registry:
        calendar = WeekendCalendar()

    store = CompanionStore(tmp_path / "companion.sqlite3")
    monkeypatch.setattr(runtime, "_schedule_registry", lambda _: Registry())

    snapshot = runtime._gateway_snapshot(
        CompanionEngine(store), store, None, "today", {"date": "2026-08-29"}
    )

    assert snapshot["scheduled_date"] == "2026-08-29"
    assert snapshot["is_trading_day"] is False


def test_internal_acceptance_cycles_are_absent_from_today_and_history(tmp_path, monkeypatch):
    class TradingCalendar:
        def is_trading_day(self, value):
            return True

    class Registry:
        calendar = TradingCalendar()

    store = CompanionStore(tmp_path / "companion.sqlite3")
    store.initialize()
    hidden, _ = store.create_manual_analysis_cycle(
        request_id="acceptance-run",
        task_key="daily.execution.0945",
        requested_at="2026-09-02T06:42:59Z",
        source={"kind": "manual_acceptance", "note": "formal intraday analysis validation"},
        task_profile_id="intraday_market_and_portfolio",
        task_profile_version=1,
    )
    visible, _ = store.create_manual_analysis_cycle(
        request_id="user-run",
        task_key="daily.execution.0945",
        requested_at="2026-09-02T06:43:59Z",
        source={"kind": "user_request", "message_id": "analysis"},
        task_profile_id="intraday_market_and_portfolio",
        task_profile_version=1,
    )
    monkeypatch.setattr(runtime, "_schedule_registry", lambda _: Registry())

    today = runtime._gateway_snapshot(
        CompanionEngine(store), store, None, "today", {"date": "2026-09-02"}
    )
    history = runtime._gateway_snapshot(
        CompanionEngine(store), store, None, "history", {"limit": "31"}
    )

    today_ids = {item["cycle"]["cycle_id"] for item in today["projections"]}
    history_ids = {item["cycle_id"] for item in history["items"]}
    assert visible["cycle_id"] in today_ids
    assert visible["cycle_id"] in history_ids
    assert hidden["cycle_id"] not in today_ids
    assert hidden["cycle_id"] not in history_ids
