from __future__ import annotations

from pathlib import Path

from trading_memory_hub import MemoryHub


def add(hub: MemoryHub, event_id: str, body: str, known_at: str, **metadata: object) -> str:
    receipt = hub.append(
        {
            "memory_space_id": "partner-main",
            "source_system": "stock-advisor",
            "source_event_id": event_id,
            "content_hash": f"sha256:{event_id}",
            "episode_type": metadata.pop("episode_type", "evidence"),
            "body": body,
            "occurred_at": known_at,
            "known_at": known_at,
            "submitted_at": known_at,
            "authority": "recorded_observation",
            "protocol_version": "memoryhub/v1",
            "metadata": metadata,
        }
    )
    return receipt.episode_id


def test_snapshot_freezes_watermark_and_blocks_future_knowledge(tmp_path: Path) -> None:
    hub = MemoryHub(tmp_path / "ledger.sqlite3")
    first = add(hub, "old", "机器人订单增长", "2026-08-31T01:00:00Z")
    snapshot = hub.begin_snapshot(
        "partner-main", as_of="2026-08-31T02:00:00Z", stage="chat"
    )
    add(hub, "late-write", "机器人新增合同", "2026-08-31T01:30:00Z")
    add(hub, "future", "机器人未来公告", "2026-08-31T03:00:00Z")

    cards = hub.search(snapshot.snapshot_id, "机器人")

    assert [card["episode_id"] for card in cards] == [first]
    assert hub.expand(snapshot.snapshot_id, first)["body"] == "机器人订单增长"


def test_m1_cannot_see_current_h0_but_m2_can_and_related_keeps_corrections(tmp_path: Path) -> None:
    hub = MemoryHub(tmp_path / "ledger.sqlite3")
    public = add(hub, "public", "公开核验的风险", "2026-08-31T01:00:00Z")
    h0 = add(
        hub, "h0", "我的 H0 判断", "2026-08-31T01:10:00Z",
        episode_type="user_message", cycle_id="cycle-1", stage="h0", actor="human",
    )
    correction = hub.append(
        {
            "memory_space_id": "partner-main",
            "source_system": "stock-advisor",
            "source_event_id": "public-correction",
            "content_hash": "sha256:public-correction",
            "episode_type": "correction",
            "body": "更正后的公开风险",
            "occurred_at": "2026-08-31T01:20:00Z",
            "known_at": "2026-08-31T01:20:00Z",
            "submitted_at": "2026-08-31T01:20:00Z",
            "authority": "recorded_observation",
            "protocol_version": "memoryhub/v1",
            "corrects_episode_id": public,
        }
    )
    m1 = hub.begin_snapshot(
        "partner-main", as_of="2026-08-31T02:00:00Z", stage="m1_research", cycle_id="cycle-1"
    )
    m2 = hub.begin_snapshot(
        "partner-main", as_of="2026-08-31T02:00:00Z", stage="m2_synthesis", cycle_id="cycle-1"
    )

    assert h0 not in {card["episode_id"] for card in hub.search(m1.snapshot_id, "判断")}
    assert h0 in {card["episode_id"] for card in hub.search(m2.snapshot_id, "判断")}
    assert correction.episode_id in {
        card["episode_id"] for card in hub.related(m1.snapshot_id, public)
    }
