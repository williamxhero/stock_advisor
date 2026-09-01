from pathlib import Path

from ai_trading_companion.memory_commands import handle_memory_command
from ai_trading_companion.memory_port import InMemoryMemoryAdapter


def test_export_then_confirmed_clear_is_visible_and_idempotent(tmp_path: Path) -> None:
    memory = InMemoryMemoryAdapter()
    memory.append(
        {
            "memory_space_id": "private", "source_system": "stock-advisor",
            "source_event_id": "m1", "content_hash": "auto", "episode_type": "user_message",
            "body": "我的私人判断", "occurred_at": "2026-09-01T00:00:00Z",
            "known_at": "2026-09-01T00:00:00Z", "submitted_at": "2026-09-01T00:00:00Z",
            "authority": "user_private_fact", "protocol_version": "memoryhub/v1",
        }
    )
    exported = handle_memory_command(memory, "private", {"command_id": "export-1", "type": "memory.export"}, tmp_path)

    cancelled = handle_memory_command(memory, "private", {"command_id": "clear-cancel", "type": "memory.clear", "confirmed": False}, tmp_path)
    cleared = handle_memory_command(
        memory, "private",
        {"command_id": "clear-1", "type": "memory.clear", "confirmed": True, "confirmation_token": exported["confirmation_token"]},
        tmp_path,
    )
    replay = handle_memory_command(
        memory, "private",
        {"command_id": "clear-1", "type": "memory.clear", "confirmed": True, "confirmation_token": exported["confirmation_token"]},
        tmp_path,
    )

    assert Path(exported["machine_export_path"]).exists()
    assert "我的私人判断" in Path(exported["human_export_path"]).read_text(encoding="utf-8")
    assert cancelled["cleared"] is False
    assert cleared == replay
    assert memory.timeline("private") == []
