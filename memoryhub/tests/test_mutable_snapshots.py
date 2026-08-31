from __future__ import annotations

from pathlib import Path

import pytest

from trading_memory_hub import MemoryHub, SecretRejected


def web_episode(body: str) -> dict[str, object]:
    return {
        "memory_space_id": "partner-main", "source_system": "wag",
        "source_event_id": "read-1", "content_hash": "auto",
        "episode_type": "external_evidence", "body": body,
        "occurred_at": "2026-08-01T00:00:00Z",
        "known_at": "2026-08-31T01:00:00Z",
        "submitted_at": "2026-08-31T01:00:01Z",
        "authority": "mutable_source_snapshot", "protocol_version": "memoryhub/v1",
        "metadata": {"url": "https://example.test/article", "title": "旧网页"},
    }


def test_mutable_web_body_is_frozen_at_real_known_time(tmp_path: Path) -> None:
    hub = MemoryHub(tmp_path / "ledger.sqlite3")
    body = "网页当时写着：机器人订单增长。"
    receipt = hub.append(web_episode(body))
    before = hub.begin_snapshot("partner-main", as_of="2026-08-30T23:59:59Z", stage="chat")
    after = hub.begin_snapshot("partner-main", as_of="2026-08-31T02:00:00Z", stage="chat")

    assert hub.search(before.snapshot_id, "机器人") == []
    assert hub.expand(after.snapshot_id, receipt.episode_id)["body"] == body


def test_secret_guard_blocks_snapshot_before_ledger_write(tmp_path: Path) -> None:
    hub = MemoryHub(tmp_path / "ledger.sqlite3")

    with pytest.raises(SecretRejected):
        hub.append(web_episode("password=correct-horse-battery-staple"))

    assert hub.health()["ledger"]["episodes"] == 0

