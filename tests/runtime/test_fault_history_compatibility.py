from __future__ import annotations

from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.store import CompanionStore


PRODUCTION_CYCLE_ID = "ae19d3de-a0c2-480e-9021-7878251f500e"
USER_TEXT = "\u8bf7\u6838\u9a8c\u4eca\u665a\u7684\u98ce\u9669\u3002"
CHAT_FAULT_TEXT = "\u804a\u5929\u56de\u590d \u9047\u5230\u6280\u672f\u6545\u969c\uff0c\u672a\u80fd\u5b8c\u6210\u3002"
RESEARCH_FAULT_TEXT = "\u516c\u5f00\u8865\u67e5 \u7684\u5173\u952e\u4e8b\u5b9e\u4ecd\u672a\u8fbe\u5230\u53ef\u6838\u9a8c\u6807\u51c6\u3002"
RECOVERY_TEXT = "\u8fd9\u662f\u540e\u6765\u6210\u529f\u8865\u51fa\u7684\u5b8c\u6574\u56de\u7b54\u3002"


def _legacy_midnight_cycle(
    database: Path,
    *,
    cycle_id: str,
    recover: bool,
) -> tuple[CompanionStore, CompanionEngine, dict, str]:
    store = CompanionStore(database)
    with patch(
        "ai_trading_companion.store.uuid.uuid4",
        return_value=UUID(cycle_id),
    ):
        cycle = store.create_cycle(
            "conversation.daily",
            "2026-09-09T00:00:00+08:00",
            "2026-09-08T16:00:00Z",
            kind="daily_conversation",
            work_start_at="2026-09-09T00:00:00+08:00",
        )
    with store.connection() as connection:
        connection.execute(
            "UPDATE companion_cycle SET state='open' WHERE cycle_id=?",
            (cycle["cycle_id"],),
        )
    cycle = store.get_cycle(cycle["cycle_id"])
    store.stage_message(
        cycle["cycle_id"], USER_TEXT, "conversation", message_id="user-midnight",
    )
    batch_id, _ = store.commit_staged_messages(cycle["cycle_id"], "conversation")

    def append_fault(
        event_type: str,
        body: str,
        *,
        stream_id: str | None = None,
        reason_category: str,
    ) -> None:
        metadata = {"reason_category": reason_category}
        payload: dict = {"cycle": cycle}
        if stream_id is not None:
            metadata["stream_id"] = stream_id
            payload["stream"] = store.stream_message(stream_id)
        artifact = store.append_artifact(
            cycle["cycle_id"], "system_fault", "system", body, cycle["as_of"], metadata,
        )
        payload.update({
            "source_artifact_id": artifact["artifact_id"],
            "reason": body,
            "diagnostic_code": reason_category,
        })
        store.queue_event(cycle["cycle_id"], event_type, payload)

    for _ in range(2):
        stream = store.start_stream_message(cycle["cycle_id"], [batch_id], "ai_chat")
        store.finish_stream_message(stream["stream_id"], error="legacy failure")
        append_fault(
            "chat.stream.failed",
            CHAT_FAULT_TEXT,
            stream_id=stream["stream_id"],
            reason_category="llm_runtime_error",
        )
    for _ in range(3):
        append_fault(
            "chat_research.failed",
            RESEARCH_FAULT_TEXT,
            reason_category="evidence_insufficient",
        )

    engine = CompanionEngine(store)
    if recover:
        engine.chat_ready(
            cycle["cycle_id"],
            RECOVERY_TEXT,
            reply_to_batch_id=batch_id,
            reply_to_batch_ids=[batch_id],
        )
    return store, engine, cycle, batch_id


def test_production_shaped_midnight_history_is_hidden_after_its_complete_reply(tmp_path: Path) -> None:
    store, engine, cycle, _ = _legacy_midnight_cycle(
        tmp_path / "resolved.sqlite3",
        cycle_id=PRODUCTION_CYCLE_ID,
        recover=True,
    )

    projection = engine.command({
        "contract": "companion-user-command/v1",
        "command_id": "historical-resolved-projection",
        "cycle_id": cycle["cycle_id"],
        "type": "request_projection",
    })

    assert cycle["cycle_id"] == PRODUCTION_CYCLE_ID
    assert projection["fault_episodes"] == []
    assert [item["text"] for item in projection["ai_messages"]] == [RECOVERY_TEXT]
    assert [item["text"] for item in projection["user_messages"]] == [USER_TEXT]
    assert len([
        artifact for artifact in store.artifacts(cycle["cycle_id"])
        if artifact["kind"] == "system_fault"
    ]) == 5
    assert store.removed_operational_record_ids(cycle["cycle_id"]) == []
    with store.connection() as connection:
        episodes = [
            dict(row) for row in connection.execute(
                "SELECT * FROM companion_fault_episode WHERE cycle_id=?",
                (cycle["cycle_id"],),
            )
        ]
    assert len(episodes) == 1
    assert episodes[0]["attempt_count"] == 5
    assert episodes[0]["state"] == "resolved"


def test_unresolved_legacy_history_conservatively_keeps_only_latest_fault_state(tmp_path: Path) -> None:
    store, engine, cycle, batch_id = _legacy_midnight_cycle(
        tmp_path / "active.sqlite3",
        cycle_id="11111111-2222-4333-8444-555555555555",
        recover=False,
    )

    first = engine.command({
        "contract": "companion-user-command/v1",
        "command_id": "historical-active-projection-1",
        "cycle_id": cycle["cycle_id"],
        "type": "request_projection",
    })
    restarted = CompanionEngine(store).command({
        "contract": "companion-user-command/v1",
        "command_id": "historical-active-projection-2",
        "cycle_id": cycle["cycle_id"],
        "type": "request_projection",
    })

    assert len(first["fault_episodes"]) == 1
    assert first["fault_episodes"] == restarted["fault_episodes"]
    fault = first["fault_episodes"][0]
    assert fault["scope_kind"] == "batch"
    assert fault["scope_key"] == batch_id
    assert fault["attempt_count"] == 5
    assert fault["text"] == RESEARCH_FAULT_TEXT
    assert store.pending_message_batches(cycle["cycle_id"], "conversation")[0]["state"] == "pending"


def test_unstructured_unknown_legacy_faults_do_not_merge_by_matching_text(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "conservative.sqlite3")
    cycle = store.ensure_daily_conversation("2026-09-09")
    for _ in range(2):
        store.append_artifact(
            cycle["cycle_id"], "system_fault", "system", "same unstructured legacy text",
            cycle["as_of"], {},
        )
    engine = CompanionEngine(store)

    projection = engine.command({
        "contract": "companion-user-command/v1",
        "command_id": "unknown-legacy-projection",
        "cycle_id": cycle["cycle_id"],
        "type": "request_projection",
    })

    assert len(projection["fault_episodes"]) == 2
    assert len({item["episode_id"] for item in projection["fault_episodes"]}) == 2
