from __future__ import annotations

from pathlib import Path

from trading_memory_hub import MemoryHub


def append(hub: MemoryHub, event: str, body: str, **metadata: object) -> str:
    return hub.append(
        {
            "memory_space_id": "partner-main", "source_system": "test",
            "source_event_id": event, "content_hash": "auto",
            "episode_type": "evidence", "body": body,
            "occurred_at": "2026-08-31T01:00:00Z", "known_at": "2026-08-31T01:00:00Z",
            "submitted_at": "2026-08-31T01:00:00Z", "authority": "recorded_observation",
            "protocol_version": "memoryhub/v1", "metadata": metadata,
        }
    ).episode_id


def test_derivation_failure_never_rolls_back_episode(tmp_path: Path) -> None:
    hub = MemoryHub(tmp_path / "ledger.sqlite3")
    episode_id = append(hub, "event-1", "机器人订单增长，但现金流风险上升。")

    def failing(_: str) -> dict[str, object]:
        raise RuntimeError("ollama unavailable")

    assert hub.derive_pending(failing, extractor_version="gemma-test/v1") == 0
    snapshot = hub.begin_snapshot("partner-main", as_of="2026-08-31T02:00:00Z", stage="chat")
    assert hub.expand(snapshot.snapshot_id, episode_id)["body"].startswith("机器人订单")
    assert hub.health()["derivation"]["failed"] == 1


def test_derived_memory_keeps_exact_span_and_extractor_version(tmp_path: Path) -> None:
    hub = MemoryHub(tmp_path / "ledger.sqlite3")
    episode_id = append(hub, "event-1", "机器人订单增长，但现金流风险上升。")

    def extract(_: str) -> dict[str, object]:
        return {
            "summary": "订单增长同时伴随现金流风险。",
            "entities": ["机器人", "现金流"],
            "propositions": [{"text": "现金流风险上升", "span": "现金流风险上升"}],
            "relations": [{"subject": "订单", "predicate": "伴随", "object": "现金流风险"}],
        }

    assert hub.derive_pending(extract, extractor_version="gemma-test/v1") == 1
    derived = hub.derived_memory(episode_id)

    assert derived["extractor_version"] == "gemma-test/v1"
    assert derived["propositions"][0]["span"] == "现金流风险上升"


def test_only_deterministic_event_links_reduce_independent_source_count(tmp_path: Path) -> None:
    hub = MemoryHub(tmp_path / "ledger.sqlite3")
    original = append(hub, "a", "公告原文", original_url="https://example.test/notice/1")
    repost = append(hub, "b", "转载文本", original_url="https://example.test/notice/1")
    similar = append(hub, "c", "标题相似的另一件事")

    assert hub.link_events(original, repost)["status"] == "confirmed"
    assert hub.link_events(original, similar)["status"] == "candidate"
    assert hub.independent_source_count([original, repost, similar]) == 2
