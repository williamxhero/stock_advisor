from ai_trading_companion.store import CompanionStore


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
