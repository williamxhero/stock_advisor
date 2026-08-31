from __future__ import annotations

from pathlib import Path

import pytest

from trading_memory_hub import EpisodeConflict, MemoryHub


def episode(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "memory_space_id": "partner-main",
        "source_system": "stock-advisor",
        "source_event_id": "message-1",
        "content_hash": "sha256:one",
        "episode_type": "user_message",
        "body": "我更重视长期复利。",
        "occurred_at": "2026-08-31T01:00:00Z",
        "known_at": "2026-08-31T01:00:00Z",
        "submitted_at": "2026-08-31T01:00:01Z",
        "authority": "user_private_fact",
        "protocol_version": "memoryhub/v1",
    }
    value.update(overrides)
    return value


def test_append_is_idempotent_and_rejects_immutable_conflicts(tmp_path: Path) -> None:
    hub = MemoryHub(tmp_path / "ledger.sqlite3")

    first = hub.append(episode())
    replay = hub.append(episode())

    assert replay.episode_id == first.episode_id
    assert replay.sequence == 1
    with pytest.raises(EpisodeConflict):
        hub.append(episode(content_hash="sha256:changed", body="被改写的内容"))


def test_correction_appends_a_new_episode_without_rewriting_history(tmp_path: Path) -> None:
    hub = MemoryHub(tmp_path / "ledger.sqlite3")
    original = hub.append(episode())

    correction = hub.append(
        episode(
            source_event_id="message-2",
            content_hash="sha256:two",
            body="更正：我更重视风险调整后的长期复利。",
            corrects_episode_id=original.episode_id,
        )
    )

    assert correction.episode_id != original.episode_id
    assert correction.sequence == 2
    assert hub.health()["ledger"]["state"] == "ready"
    assert hub.health()["ledger"]["episodes"] == 2

