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


def test_memory_port_projects_only_formal_timeline_messages() -> None:
    memory = InMemoryMemoryAdapter()
    for event_id, episode_type, body in (
        ("user-1", "user_message", "正式问题"),
        ("candidate-1", "ai_candidate", "候选"),
        ("ai-1", "ai_message", "正式回答"),
    ):
        memory.append(
            {
                "memory_space_id": "acceptance", "source_system": "stock-advisor",
                "source_event_id": event_id, "content_hash": "auto", "episode_type": episode_type,
                "body": body, "occurred_at": "2026-09-01T00:00:00Z",
                "known_at": "2026-09-01T00:00:00Z", "submitted_at": "2026-09-01T00:00:00Z",
                "authority": "recorded_observation", "protocol_version": "memoryhub/v1",
            }
        )

    assert [item["body"] for item in memory.timeline("acceptance")] == ["正式问题", "正式回答"]
