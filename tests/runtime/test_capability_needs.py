from __future__ import annotations

from pathlib import Path

from ai_trading_companion.store import CompanionStore


def test_capability_need_deduplicates_contract_and_persists_merged_traces(tmp_path: Path) -> None:
    database = tmp_path / "companion.sqlite3"
    store = CompanionStore(database)
    store.initialize()
    first = store.submit_capability_need({
        "contract": "ai-trading-capability-need/v1", "capability": "cn_equity_quote_batch",
        "output_contract": {"finality": "official_close", "fields": ["price", "quote_at"]},
        "urgency": "normal", "examples": [{"symbols": ["600000"]}],
        "failure_trace": {"error_code": "tool_not_found"}, "source_hints": ["public quote endpoint"],
    })
    second = CompanionStore(database).submit_capability_need({
        "contract": "ai-trading-capability-need/v1", "capability": "cn_equity_quote_batch",
        "output_contract": {"fields": ["price", "quote_at"], "finality": "official_close"},
        "urgency": "high", "examples": [{"symbols": ["000001"]}],
        "failure_trace": {"error_code": "tool_quote_trading_date_mismatch"}, "source_hints": ["backup quote endpoint"],
    })

    assert first["need_id"] == second["need_id"]
    assert second["state"] == "queued"
    assert second["urgency"] == "high"
    assert len(second["examples"]) == 2
    assert len(second["failure_traces"]) == 2
    assert set(second["source_hints"]) == {"public quote endpoint", "backup quote endpoint"}
    assert CompanionStore(database).list_capability_needs()[0]["need_id"] == first["need_id"]


def test_capability_need_rejects_unversioned_or_invalid_external_request(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "companion.sqlite3")
    store.initialize()

    for request in ({}, {"contract": "ai-trading-capability-need/v1", "capability": "bad/name", "output_contract": {}}):
        try:
            store.submit_capability_need(request)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid capability need was accepted")
