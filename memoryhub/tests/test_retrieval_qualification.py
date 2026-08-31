from pathlib import Path

from trading_memory_hub.core import MemoryHub


def _episode(event: str, body: str, known_at: str) -> dict[str, object]:
    return {
        "memory_space_id": "partner-main", "source_system": "test", "source_event_id": event,
        "content_hash": "auto", "episode_type": "evidence", "body": body,
        "occurred_at": known_at, "known_at": known_at, "submitted_at": known_at,
        "authority": "recorded_observation", "protocol_version": "memoryhub/v1",
    }


def test_bundle_versions_audit_is_private_and_frozen_replay_is_gated(tmp_path: Path) -> None:
    hub = MemoryHub(tmp_path / "memory.db")
    first = hub.append(_episode("e1", "机器人订单增长", "2026-08-01T00:00:00Z"))
    future = hub.append(_episode("e2", "机器人订单后来下滑", "2026-09-01T00:00:00Z"))
    snapshot = hub.begin_snapshot(
        "partner-main", as_of="2026-08-31T00:00:00Z", stage="chat", cycle_id="cycle-1"
    )

    bundle = hub.retrieve_bundle(snapshot.snapshot_id, "机器人", limit=20)
    assert bundle["versions"] == {
        "policy": "memory-policy/v1", "retriever": "sqlite-lexical/v1",
        "index": "source-search/v1", "extractor": "derived-memory/v1",
        "protocol": "memoryhub/v1",
    }
    assert bundle["snapshot"]["watermark"] == 2
    assert [row["episode_id"] for row in bundle["results"]] == [first.episode_id]
    assert bundle["audit_id"]
    audit = hub.retrieval_audit(bundle["audit_id"])
    assert audit["query"] == "机器人"
    assert audit["final_episode_ids"] == [first.episode_id]
    assert future.episode_id in audit["excluded"]["future_knowledge"]
    assert "hidden_reasoning" not in audit
    assert hub.timeline("partner-main") == []
    assert all(row["episode_type"] != "retrieval_audit" for row in hub.export_space("partner-main")["episodes"])

    frozen = hub.create_frozen_query_set("partner-main", [
        {"query": "机器人", "snapshot_id": snapshot.snapshot_id,
         "expected_episode_ids": [first.episode_id], "major_counterevidence_ids": []}
    ])
    report = hub.evaluate_candidate(frozen["query_set_id"], {
        "adapter": "graphiti", "retriever_version": "graphiti/v1",
        "runs": [{"episode_ids": [first.episode_id], "latency_ms": 3, "fault": None}],
    })
    assert set(report["metrics"]) == {
        "recall_misses", "false_associations", "major_counterevidence_misses",
        "future_leakage", "latency_ms", "faults",
    }
    assert hub.promote_candidate(report["report_id"])["active_retriever"] == "graphiti/v1"

    failed = hub.evaluate_candidate(frozen["query_set_id"], {
        "adapter": "mem0", "retriever_version": "mem0/v1",
        "runs": [{"episode_ids": [future.episode_id], "latency_ms": 1, "fault": None}],
    })
    assert failed["qualified"] is False
    try:
        hub.promote_candidate(failed["report_id"])
    except ValueError as error:
        assert "not qualified" in str(error)
    else:
        raise AssertionError("unqualified candidate was promoted")
    assert hub.replay_bundle(bundle["bundle_id"])["results"] == bundle["results"]
