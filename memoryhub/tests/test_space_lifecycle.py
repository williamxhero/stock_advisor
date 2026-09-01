from pathlib import Path

from trading_memory_hub import MemoryHub, MemoryHubError


def add(hub: MemoryHub, space: str, event_id: str, body: str, *, correction: str | None = None) -> str:
    return hub.append(
        {
            "memory_space_id": space, "source_system": "stock-advisor",
            "source_event_id": event_id, "content_hash": "auto", "episode_type": "user_message",
            "body": body, "occurred_at": "2026-09-01T00:00:00Z",
            "known_at": "2026-09-01T00:00:00Z", "submitted_at": "2026-09-01T00:00:00Z",
            "authority": "user_private_fact", "protocol_version": "memoryhub/v1",
            "corrects_episode_id": correction,
        }
    ).episode_id


def test_export_is_human_readable_and_machine_rebuildable(tmp_path: Path) -> None:
    hub = MemoryHub(tmp_path / "ledger.sqlite3")
    first = add(hub, "private", "m1", "原始判断")
    add(hub, "private", "m2", "更正判断", correction=first)

    exported = hub.export_space("private")

    assert "原始判断" in exported["human_markdown"]
    assert [item["source_event_id"] for item in exported["episodes"]] == ["m1", "m2"]
    assert exported["episodes"][1]["corrects_episode_id"] == first
    assert exported["export_sha256"].startswith("sha256:")


def test_clear_requires_fresh_confirmation_and_is_idempotent(tmp_path: Path) -> None:
    hub = MemoryHub(tmp_path / "ledger.sqlite3")
    add(hub, "private", "m1", "私人消息")
    add(hub, "other", "other-1", "其他空间")
    exported = hub.export_space("private")
    confirmation = hub.prepare_clear("private", exported["export_sha256"])
    stale_confirmation = hub.prepare_clear("private", exported["export_sha256"])

    receipt = hub.clear_space("private", confirmation["confirmation_token"])
    replay = hub.clear_space("private", confirmation["confirmation_token"])

    assert receipt == replay
    assert receipt["deleted_episodes"] == 1
    assert hub.timeline("private") == []
    assert [item["body"] for item in hub.timeline("other")] == ["其他空间"]
    add(hub, "private", "m1", "私人消息")
    try:
        hub.clear_space("private", stale_confirmation["confirmation_token"])
    except MemoryHubError:
        pass
    else:
        raise AssertionError("a confirmation from before a completed clear must be invalidated")
    try:
        hub.prepare_clear("private", "sha256:stale")
    except MemoryHubError:
        pass
    else:
        raise AssertionError("stale export must not authorize clear")
