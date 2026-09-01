import json
from pathlib import Path

from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.memory_port import InMemoryMemoryAdapter
from ai_trading_companion.store import CompanionStore


def test_submitted_chat_and_published_reply_replay_from_memoryhub_to_projection(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "companion.sqlite3")
    memory = InMemoryMemoryAdapter()
    engine = CompanionEngine(store, memory=memory, memory_space_id="acceptance")
    cycle = store.ensure_daily_conversation("2026-09-01")
    store.stage_message(cycle["cycle_id"], "请核验机器人风险", "conversation", message_id="user-1")
    batch_id, messages = store.commit_staged_messages(cycle["cycle_id"], "conversation")

    engine.record_submitted_messages(cycle["cycle_id"], messages)
    engine.chat_ready(
        cycle["cycle_id"], "我核验后认为风险仍在。",
        reply_to_batch_id=batch_id, reply_to_batch_ids=[batch_id],
    )
    projection = engine.command(
        {"command_id": "project", "cycle_id": cycle["cycle_id"], "type": "request_projection"}
    )

    assert [(item["message_id"], item["text"]) for item in projection["user_messages"]] == [
        ("user-1", "请核验机器人风险")
    ]
    assert [item["text"] for item in projection["ai_messages"]] == ["我核验后认为风险仍在。"]
    assert [item["episode_type"] for item in memory.timeline("acceptance")] == [
        "user_message", "ai_message",
    ]


def test_failed_stream_keeps_visible_prefix_as_incomplete_memory_message(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "companion.sqlite3")
    memory = InMemoryMemoryAdapter()
    engine = CompanionEngine(store, memory=memory, memory_space_id="acceptance")
    cycle = store.ensure_daily_conversation("2026-09-01")
    stream = engine.chat_stream_started(cycle["cycle_id"], ["batch-1"], "ai_chat")
    engine.chat_stream_delta(cycle["cycle_id"], stream["stream_id"], "已经核验一半。")

    delta = next(event for event in store.pending_events() if event["event_type"] == "chat.stream.delta")
    payload = json.loads(delta["payload_json"])
    assert payload["message"]["message_id"] == stream["stream_id"]
    assert payload["message"]["sealed_at"] == stream["created_at"]

    engine.chat_stream_failed(cycle["cycle_id"], stream["stream_id"], "network")

    item = memory.timeline("acceptance")[0]
    assert item["body"] == "已经核验一半。"
    assert item["metadata"]["message_id"] == stream["stream_id"]
    assert item["metadata"]["state"] == "incomplete"


def test_restart_recovers_visible_stream_prefix_without_changing_its_id(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "companion.sqlite3")
    memory = InMemoryMemoryAdapter()
    engine = CompanionEngine(store, memory=memory, memory_space_id="acceptance")
    cycle = store.ensure_daily_conversation("2026-09-01")
    stream = engine.chat_stream_started(cycle["cycle_id"], ["batch-1"], "ai_chat")
    engine.chat_stream_delta(cycle["cycle_id"], stream["stream_id"], "重启前已显示。")

    restarted = CompanionEngine(store, memory=memory, memory_space_id="acceptance")
    restarted.recover_interrupted_streams()

    item = memory.timeline("acceptance")[0]
    assert item["metadata"]["message_id"] == stream["stream_id"]
    assert item["body"] == "重启前已显示。"
    assert store.stream_message(stream["stream_id"])["state"] == "failed"
