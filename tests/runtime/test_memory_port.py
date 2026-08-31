from __future__ import annotations

from ai_trading_companion.memory_port import InMemoryMemoryAdapter


def test_memory_port_exposes_protocol_without_storage_details() -> None:
    port = InMemoryMemoryAdapter()
    receipt = port.append(
        {
            "memory_space_id": "acceptance",
            "source_system": "test",
            "source_event_id": "event-1",
            "content_hash": "sha256:test",
            "episode_type": "user_message",
            "body": "冻结消息",
            "occurred_at": "2026-08-31T01:00:00Z",
            "known_at": "2026-08-31T01:00:00Z",
            "submitted_at": "2026-08-31T01:00:01Z",
            "authority": "user_private_fact",
            "protocol_version": "memoryhub/v1",
        }
    )

    assert receipt["sequence"] == 1
    assert port.health()["protocol_version"] == "memoryhub/v1"
