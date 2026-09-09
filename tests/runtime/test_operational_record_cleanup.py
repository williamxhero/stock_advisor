from __future__ import annotations

from pathlib import Path

import pytest

from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.store import CompanionStore


TEST_PROVENANCE = {
    "contract": "companion-test-provenance/v1",
    "source": "repair_probe",
    "run_id": "repair-2026-09-09",
}


def _cleanup_command(cycle_id: str, command_id: str = "cleanup-1") -> dict:
    return {
        "contract": "companion-user-command/v1",
        "command_id": command_id,
        "cycle_id": cycle_id,
        "type": "clear_operational_records",
        "confirmed": True,
        "categories": ["fault_report", "test_utterance"],
    }


def test_explicit_cleanup_tombstones_only_faults_and_structured_test_utterances(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "companion.sqlite3")
    engine = CompanionEngine(store)
    cycle = store.ensure_daily_conversation("2026-09-09")
    normal = store.stage_message(
        cycle["cycle_id"], "这是正常消息，即使提到测试和故障也必须保留。", "conversation",
        message_id="normal-user",
    )
    synthetic = store.stage_message(
        cycle["cycle_id"], "合成测试输入", "conversation",
        message_id="test-user", provenance=TEST_PROVENANCE,
    )
    batch_id, _ = store.commit_staged_messages(cycle["cycle_id"], "conversation")
    stream = engine.chat_stream_started(cycle["cycle_id"], [batch_id], "ai_chat")
    engine.chat_stream_delta(cycle["cycle_id"], stream["stream_id"], "已经显示的前缀。")
    engine.chat_stream_failed(cycle["cycle_id"], stream["stream_id"], "network")
    engine.chat_ready(
        cycle["cycle_id"], "正常完整回答。",
        reply_to_batch_id=batch_id, reply_to_batch_ids=[batch_id],
    )
    normal_ai = next(
        artifact for artifact in store.artifacts(cycle["cycle_id"])
        if artifact["kind"] == "ai_chat" and artifact["body_markdown"] == "正常完整回答。"
    )
    test_ai = store.append_artifact(
        cycle["cycle_id"], "ai_chat", "model", "合成测试输出", cycle["as_of"],
        {"provenance": TEST_PROVENANCE},
    )
    formal_judgment = store.append_artifact(
        cycle["cycle_id"], "m1", "model", "必须保留的正式判断", cycle["as_of"], {},
    )

    command = _cleanup_command(cycle["cycle_id"])
    receipt = engine.command(command)
    replay = CompanionEngine(store).command(command)

    assert receipt == replay
    assert receipt["contract"] == "companion-operational-record-cleanup-result/v1"
    assert receipt["deleted"] == {"fault_report": 1, "test_utterance": 2}
    assert receipt["skipped"]["already_removed"] == 0
    projection = engine.command({
        "contract": "companion-user-command/v1",
        "command_id": "projection-after-cleanup",
        "cycle_id": cycle["cycle_id"],
        "type": "request_projection",
    })
    assert [item["message_id"] for item in projection["user_messages"]] == [normal["message_id"]]
    assert [item["text"] for item in projection["ai_messages"]] == [
        "正常完整回答。", "必须保留的正式判断",
    ]
    assert projection["fault_episodes"] == []
    assert projection["removed_operational_record_ids"] == sorted([
        synthetic["message_id"],
        test_ai["artifact_id"],
        next(
            artifact["artifact_id"] for artifact in store.artifacts(cycle["cycle_id"])
            if artifact["kind"] == "system_fault"
        ),
    ])

    # Tombstones remove only user-visible narrative projection. Audit facts remain.
    assert any(item["artifact_id"] == test_ai["artifact_id"] for item in store.artifacts(cycle["cycle_id"]))
    assert any(item["kind"] == "system_fault" for item in store.artifacts(cycle["cycle_id"]))
    assert store.stream_message(stream["stream_id"])["text"] == "已经显示的前缀。"
    assert store.stream_message(stream["stream_id"])["state"] == "failed"
    assert store.receipt(command["command_id"], command) == receipt
    assert any(event["event_type"] == "operational_records.cleared" for event in store.pending_events())

    protected = engine.command({
        **_cleanup_command(cycle["cycle_id"], "cleanup-protected"),
        "record_ids": [
            normal["message_id"], normal_ai["artifact_id"], formal_judgment["artifact_id"],
            stream["stream_id"], "missing",
        ],
    })
    assert protected["deleted"] == {"fault_report": 0, "test_utterance": 0}
    assert protected["rejected"] == {
        "normal_user_message": 1,
        "normal_ai_message": 1,
        "formal_judgment": 1,
        "visible_stream_prefix": 1,
        "unknown_record": 1,
    }


def test_cleanup_category_selection_and_repeated_new_request_are_deterministic(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "companion.sqlite3")
    engine = CompanionEngine(store)
    cycle = store.ensure_daily_conversation("2026-09-09")
    test_message = store.stage_message(
        cycle["cycle_id"], "测试输入", "conversation",
        message_id="test-only", provenance=TEST_PROVENANCE,
    )
    batch_id, _ = store.commit_staged_messages(cycle["cycle_id"], "conversation")
    stream = engine.chat_stream_started(cycle["cycle_id"], [batch_id], "ai_chat")
    engine.chat_stream_failed(cycle["cycle_id"], stream["stream_id"], "network")

    faults_only = engine.command({
        **_cleanup_command(cycle["cycle_id"], "faults-only"),
        "categories": ["fault_report"],
    })
    assert faults_only["deleted"] == {"fault_report": 1, "test_utterance": 0}
    assert [item["message_id"] for item in engine.command({
        "contract": "companion-user-command/v1",
        "command_id": "projection-test-stays",
        "cycle_id": cycle["cycle_id"],
        "type": "request_projection",
    })["user_messages"]] == [test_message["message_id"]]

    tests_only = engine.command({
        **_cleanup_command(cycle["cycle_id"], "tests-only"),
        "categories": ["test_utterance"],
    })
    assert tests_only["deleted"] == {"fault_report": 0, "test_utterance": 1}
    repeated = engine.command(_cleanup_command(cycle["cycle_id"], "cleanup-new-request"))
    assert repeated["deleted"] == {"fault_report": 0, "test_utterance": 0}
    assert repeated["skipped"]["already_removed"] == 2


def test_cleanup_requires_explicit_confirmation_and_allowlisted_classes(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "companion.sqlite3")
    engine = CompanionEngine(store)
    cycle = store.ensure_daily_conversation("2026-09-09")

    with pytest.raises(ValueError, match="confirmed"):
        engine.command({**_cleanup_command(cycle["cycle_id"]), "confirmed": False})
    with pytest.raises(ValueError, match="categories"):
        engine.command({**_cleanup_command(cycle["cycle_id"]), "categories": ["normal_user_message"]})
    with pytest.raises(ValueError, match="provenance"):
        store.stage_message(
            cycle["cycle_id"], "伪测试", "conversation",
            provenance={"source": "repair_probe", "run_id": "missing-contract"},
        )
