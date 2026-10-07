from __future__ import annotations

from pathlib import Path
import threading

import pytest

from ai_trading_companion.memory_port import HttpMemoryAdapter, InMemoryMemoryAdapter
from ai_trading_companion.memory_retrieval import MemoryIsolationError, build_profile
from trading_memory_hub.server import make_server


@pytest.fixture(params=["in_memory", "http"])
def memory(request, tmp_path: Path):
    if request.param == "in_memory":
        yield InMemoryMemoryAdapter()
        return
    server = make_server("127.0.0.1", 0, tmp_path / "memory.sqlite3", source_adapters={})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield HttpMemoryAdapter(f"http://127.0.0.1:{server.server_port}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
from ai_trading_companion.memory_type import typed_episode


def episode(event: str, semantic_type: str, *, known_at: str = "2026-09-01T00:00:00Z", **overrides: object) -> dict:
    value = {
        "memory_space_id": "retrieval-test", "source_system": "stock-advisor",
        "source_event_id": event, "content_hash": "auto", "episode_type": "note",
        "body": "600519 risk", "occurred_at": known_at, "known_at": known_at,
        "submitted_at": known_at, "authority": "recorded_observation",
        "protocol_version": "memoryhub/v1",
    }
    value.update(overrides)
    return typed_episode(value, semantic_type=semantic_type)


def snapshot(memory, stage: str = "chat") -> dict:
    return memory.begin_snapshot({
        "memory_space_id": "retrieval-test", "as_of": "2026-10-01T00:00:00Z",
        "stage": stage, "cycle_id": "cycle-test",
    })


def test_runtime_bundle_ranks_stable_preference_above_stale_observation(memory) -> None:
    stale = memory.append(episode("stale", "observation", body="600519 risk " * 10))
    stable = memory.append(episode("stable", "preference", authority="user_private_fact"))
    frozen = snapshot(memory)
    bundle = memory.retrieve_bundle(frozen["snapshot_id"], "600519 risk", limit=1)

    assert [item["episode_id"] for item in bundle["results"]] == [stable["episode_id"]]
    assert bundle["retrieval"]["contract"] == "MemoryRetrievalSpec/v1"
    assert bundle["retrieval"]["status"] == "qualified"
    assert set(bundle["retrieval"]["candidate_episode_ids"]) == {stale["episode_id"], stable["episode_id"]}


@pytest.mark.parametrize("stage", ["m1_research", "m1_judgment"])
def test_blind_retrieval_blocks_transitive_h0_lineage_across_cycles_on_every_read(memory, stage: str) -> None:
    h0 = memory.append(episode("h0", "judgment", episode_type="h0", body="SECRET-DIRECTION",
                               metadata={"stage": "h0", "cycle_id": "older-cycle"}))
    action = memory.append(episode("action", "user_fact", metadata={
        "memory_retrieval": build_profile(parent_episode_ids=[h0["episode_id"]]),
    }))
    lesson = memory.append(episode("lesson", "lesson", metadata={
        "memory_retrieval": build_profile(parent_episode_ids=[action["episode_id"]]),
    }))
    safe = memory.append(episode("safe", "evidence"))
    frozen = snapshot(memory, stage)

    bundle = memory.retrieve_bundle(frozen["snapshot_id"], "600519 risk")
    assert [item["episode_id"] for item in bundle["results"]] == [safe["episode_id"]]
    assert "SECRET-DIRECTION" not in str(bundle)
    assert [item["episode_id"] for item in memory.search(frozen["snapshot_id"], "risk")] == [safe["episode_id"]]
    for receipt in (h0, action, lesson):
        with pytest.raises(MemoryIsolationError):
            memory.expand(frozen["snapshot_id"], receipt["episode_id"])
        with pytest.raises(MemoryIsolationError):
            memory.related(frozen["snapshot_id"], receipt["episode_id"])


def test_reliability_outcome_and_market_matching_are_independent_ranking_dimensions(memory) -> None:
    evidence = memory.append(episode("evidence", "evidence", body="support"))
    outcome = memory.append(episode("outcome", "outcome", body="outcome"))
    good = memory.append(episode("verified", "lesson", metadata={"memory_retrieval": build_profile(
        reliability="verified", outcome_support="supported", lesson_state="verified",
        instruments=["600519"], market_states=["range"],
        evidence_episode_ids=[evidence["episode_id"]], outcome_episode_ids=[outcome["episode_id"]],
    )}))
    memory.append(episode("error", "lesson", body="600519 risk " * 20, metadata={"memory_retrieval": build_profile(
        reliability="conflicted", outcome_support="contradicted", lesson_state="error",
        instruments=["000001"], market_states=["bull"], outcome_episode_ids=[outcome["episode_id"]],
    )}))
    frozen = snapshot(memory)
    bundle = memory.retrieve_bundle(frozen["snapshot_id"], "600519 risk", context={"market_state": "range"})
    first = bundle["results"][0]
    assert first["episode_id"] == good["episode_id"]
    assert first["retrieval"]["half_life_days"] == 365
    assert first["retrieval"]["dimensions"]["reliability"] == 1
    assert first["retrieval"]["dimensions"]["outcome_support"] == 1
    assert first["retrieval"]["dimensions"]["market_state_match"] == 1
    assert first["retrieval"]["provenance"]["source_event_id"] == "verified"
    assert memory.expand(frozen["snapshot_id"], good["episode_id"])["metadata"]["memory_retrieval"]["lesson_state"] == "verified"
