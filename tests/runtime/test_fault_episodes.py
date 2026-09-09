from __future__ import annotations

import json
from pathlib import Path

from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.store import CompanionStore


def _event_payloads(store: CompanionStore, event_type: str) -> list[dict]:
    return [
        json.loads(event["payload_json"])
        for event in store.pending_events()
        if event["event_type"] == event_type
    ]


def test_one_batch_keeps_one_fault_episode_until_a_complete_reply_resolves_it(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "companion.sqlite3")
    engine = CompanionEngine(store)
    cycle = store.ensure_daily_conversation("2026-09-09")
    store.stage_message(cycle["cycle_id"], "请继续核验", "conversation", message_id="user-1")
    batch_id, _ = store.commit_staged_messages(cycle["cycle_id"], "conversation")

    first = engine.chat_stream_started(cycle["cycle_id"], [batch_id], "ai_chat")
    engine.chat_stream_failed(cycle["cycle_id"], first["stream_id"], "broker_unavailable")
    engine.background_failed(cycle["cycle_id"], "chat_research", "evidence_insufficient")
    second = engine.chat_stream_started(cycle["cycle_id"], [batch_id], "ai_chat")
    engine.chat_stream_failed(cycle["cycle_id"], second["stream_id"], "network")

    failures = [
        *_event_payloads(store, "chat.stream.failed"),
        *_event_payloads(store, "chat_research.failed"),
    ]
    episode_ids = {
        episode["episode_id"]
        for payload in failures
        for episode in payload["fault_episodes"]
    }
    assert len(episode_ids) == 1
    projection = engine.command({
        "contract": "companion-user-command/v1",
        "command_id": "projection-before-recovery",
        "cycle_id": cycle["cycle_id"],
        "type": "request_projection",
    })
    assert len(projection["fault_episodes"]) == 1
    assert projection["fault_episodes"][0]["attempt_count"] == 3
    assert projection["fault_episodes"][0]["state"] == "active"
    assert store.pending_message_batches(cycle["cycle_id"], "conversation")[0]["state"] == "pending"

    engine.chat_ready(
        cycle["cycle_id"],
        "已经核验完成。",
        reply_to_batch_id=batch_id,
        reply_to_batch_ids=[batch_id],
    )

    recovered = engine.command({
        "contract": "companion-user-command/v1",
        "command_id": "projection-after-recovery",
        "cycle_id": cycle["cycle_id"],
        "type": "request_projection",
    })
    assert recovered["fault_episodes"] == []
    assert [item["text"] for item in recovered["ai_messages"]] == ["已经核验完成。"]
    assert len([item for item in store.artifacts(cycle["cycle_id"]) if item["kind"] == "system_fault"]) == 3
    ready = _event_payloads(store, "chat.ready")[-1]
    assert ready["resolved_fault_episode_ids"] == sorted(episode_ids)

    late = engine.chat_stream_started(cycle["cycle_id"], [batch_id], "ai_chat")
    engine.chat_stream_failed(cycle["cycle_id"], late["stream_id"], "network")
    final_projection = engine.command({
        "contract": "companion-user-command/v1",
        "command_id": "projection-after-late-failure",
        "cycle_id": cycle["cycle_id"],
        "type": "request_projection",
    })
    assert final_projection["fault_episodes"] == []
    assert _event_payloads(store, "chat.stream.failed")[-1]["fault_episodes"][0]["state"] == "resolved"


def test_identical_fault_text_for_different_batches_keeps_separate_episodes(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "companion.sqlite3")
    engine = CompanionEngine(store)
    cycle = store.ensure_daily_conversation("2026-09-09")
    batch_ids: list[str] = []
    for index in range(2):
        store.stage_message(
            cycle["cycle_id"], f"第 {index + 1} 个问题", "conversation",
            message_id=f"user-{index + 1}",
        )
        batch_id, _ = store.commit_staged_messages(cycle["cycle_id"], "conversation")
        batch_ids.append(batch_id)
        stream = engine.chat_stream_started(cycle["cycle_id"], [batch_id], "ai_chat")
        engine.chat_stream_failed(cycle["cycle_id"], stream["stream_id"], "network")

    projection = engine.command({
        "contract": "companion-user-command/v1",
        "command_id": "separate-batches",
        "cycle_id": cycle["cycle_id"],
        "type": "request_projection",
    })

    assert len(projection["fault_episodes"]) == 2
    assert {episode["scope_key"] for episode in projection["fault_episodes"]} == set(batch_ids)
    assert len({episode["episode_id"] for episode in projection["fault_episodes"]}) == 2
