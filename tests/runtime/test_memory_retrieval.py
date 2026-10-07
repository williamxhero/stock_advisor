from __future__ import annotations

import copy
from pathlib import Path
import threading
from urllib.request import urlopen

import pytest

from ai_trading_companion.memory_port import HttpMemoryAdapter, InMemoryMemoryAdapter, MemoryUnavailable
from ai_trading_companion.memory_retrieval import (
    MemoryIsolationError, build_input, build_profile, frozen_replay, validate_input,
)
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
    assert bundle["retrieval"]["candidate_episode_ids"] == [safe["episode_id"]]
    assert bundle["retrieval"]["excluded"] == []
    assert bundle["retrieval"]["candidate_window_saturated"] is None
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


@pytest.mark.parametrize("state,half_life", [("candidate", 30), ("verified", 365), ("error", 7)])
def test_lessons_decay_by_semantic_state_without_promoting_memory(memory, state: str, half_life: int) -> None:
    evidence = memory.append(episode("evidence", "evidence", body="support"))
    outcome = memory.append(episode("outcome", "outcome", body="outcome"))
    profile = build_profile(
        lesson_state=state, reliability="verified" if state == "verified" else "unverified",
        outcome_support="supported" if state == "verified" else "unknown",
        evidence_episode_ids=[evidence["episode_id"]], outcome_episode_ids=[outcome["episode_id"]],
    )
    lesson = memory.append(episode("lesson", "lesson", metadata={"memory_retrieval": profile}))
    before = memory.export_space("retrieval-test")
    bundle = memory.retrieve_bundle(snapshot(memory)["snapshot_id"], "risk")
    assert bundle["results"][0]["episode_id"] == lesson["episode_id"]
    assert bundle["results"][0]["retrieval"]["half_life_days"] == half_life
    assert memory.export_space("retrieval-test") == before


@pytest.mark.parametrize("parent_kind", ["missing", "other_space", "future"])
def test_unavailable_lineage_cannot_be_ranked_or_expanded(memory, parent_kind: str) -> None:
    parent_id = "missing-episode"
    if parent_kind != "missing":
        overrides = {"memory_space_id": "other-space"} if parent_kind == "other_space" else {"known_at": "2026-11-01T00:00:00Z"}
        parent_id = memory.append(episode("parent", "evidence", **overrides))["episode_id"]
    candidate = memory.append(episode("candidate", "lesson", metadata={
        "memory_retrieval": build_profile(parent_episode_ids=[parent_id]),
    }))
    frozen = snapshot(memory)
    bundle = memory.retrieve_bundle(frozen["snapshot_id"], "risk")
    assert bundle["results"] == []
    with pytest.raises(MemoryIsolationError):
        memory.expand(frozen["snapshot_id"], candidate["episode_id"])


def test_frozen_watermark_and_submitted_time_prevent_backfilled_context(memory) -> None:
    memory.append(episode("late-submission", "preference", submitted_at="2026-10-02T00:00:00Z"))
    safe = memory.append(episode("safe", "preference"))
    frozen = snapshot(memory)
    memory.append(episode("backfill", "preference"))
    bundle = memory.retrieve_bundle(frozen["snapshot_id"], "risk")
    assert [item["episode_id"] for item in bundle["results"]] == [safe["episode_id"]]
    assert bundle["retrieval"]["status"] == "degraded"


@pytest.mark.parametrize("metadata", [
    {"stage": "m2_synthesis"}, {"source_message_id": "h0-message"},
    {"h0_derived": True}, {"h0_artifact_id": "frozen-h0"},
])
def test_blind_personal_facts_do_not_launder_directional_provenance(memory, metadata: dict) -> None:
    record = memory.append(episode("fact", "user_fact", metadata=metadata, authority="user_private_fact"))
    frozen = snapshot(memory, "m1_judgment")
    assert memory.retrieve_bundle(frozen["snapshot_id"], "risk")["results"] == []
    with pytest.raises(MemoryIsolationError):
        memory.expand(frozen["snapshot_id"], record["episode_id"])


def test_blind_related_results_cannot_bypass_lineage_filter(memory) -> None:
    safe = memory.append(episode("safe", "evidence"))
    memory.append(episode("unsafe", "user_fact", metadata={
        "related_episode_ids": [safe["episode_id"]], "h0_derived": True,
    }))
    frozen = snapshot(memory, "m1_research")
    assert memory.related(frozen["snapshot_id"], safe["episode_id"]) == []


@pytest.mark.parametrize("profile", [
    {"reliability": "verified"}, {"outcome_support": "supported"},
    {"lesson_state": "verified"}, {"reliability": "invented"},
    {"parent_episode_ids": [""]},
])
def test_profile_cannot_fabricate_reliability_or_lesson_maturity(profile: dict) -> None:
    with pytest.raises(ValueError):
        build_profile(**profile)


@pytest.mark.parametrize("field,value", [
    ("version", 2), ("version", True), ("policy_version", "unknown/v1"),
    ("permissions", {"write_permissions": ["memoryhub"]}), ("limit", 0),
])
def test_typed_input_rejects_version_and_permission_drift(field: str, value: object) -> None:
    memory = InMemoryMemoryAdapter()
    request = build_input(snapshot(memory), "risk")
    request[field] = value
    with pytest.raises(ValueError):
        validate_input(request)


@pytest.mark.parametrize("stage", ["chat", "m1_judgment"])
def test_actual_retrieval_input_replays_twice_offline_without_rewriting_history(memory, stage: str) -> None:
    safe = memory.append(episode("safe", "evidence"))
    h0 = memory.append(episode("h0", "judgment", episode_type="h0", metadata={"stage": "h0"}))
    memory.append(episode("derived", "lesson", metadata={
        "memory_retrieval": build_profile(parent_episode_ids=[h0["episode_id"]]),
    }))
    frozen = snapshot(memory, stage)
    before = memory.export_space("retrieval-test")
    archive = memory.freeze_retrieval(frozen["snapshot_id"], "risk")
    first = frozen_replay(archive)
    second = frozen_replay(copy.deepcopy(archive))
    assert first == second
    assert first["output"] == archive["payload"]["original_output"]
    assert safe["episode_id"] in {item["episode_id"] for item in first["output"]["results"]}
    assert first["output"]["retrieval"]["snapshot"] == frozen
    assert set(first["evaluation_vector"]) == {
        "delivery_speed", "qualification_probability", "research_quality", "judgment_outcome", "safety_reliability",
    }
    assert first["evaluation_vector"]["judgment_outcome"]["state"] == "not_measured_in_frozen_replay"
    assert memory.export_space("retrieval-test") == before
    tampered = copy.deepcopy(archive)
    tampered["payload"]["originals"][safe["episode_id"]]["body"] = "rewritten history"
    with pytest.raises(ValueError, match="integrity"):
        frozen_replay(tampered)


def test_rejected_evidence_cannot_support_a_reliable_descendant(memory) -> None:
    rejected = memory.append(episode("rejected", "evidence", metadata={
        "memory_retrieval": build_profile(reliability="rejected"),
    }))
    child = memory.append(episode("child", "lesson", metadata={
        "memory_retrieval": build_profile(reliability="verified", evidence_episode_ids=[rejected["episode_id"]]),
    }))
    frozen = snapshot(memory)
    assert memory.retrieve_bundle(frozen["snapshot_id"], "risk")["results"] == []
    with pytest.raises(MemoryIsolationError, match="rejected_reliability"):
        memory.expand(frozen["snapshot_id"], child["episode_id"])


def test_observation_two_day_half_life_uses_occurrence_not_ingestion_time(memory) -> None:
    receipt = memory.append(episode("observation", "observation", occurred_at="2026-09-29",
                                    known_at="2026-09-30T00:00:00Z"))
    bundle = memory.retrieve_bundle(snapshot(memory)["snapshot_id"], "risk")
    assert bundle["results"][0]["episode_id"] == receipt["episode_id"]
    assert bundle["results"][0]["retrieval"]["dimensions"]["decay"] == 0.5
    assert bundle["results"][0]["retrieval"]["half_life_days"] == 2


def test_fractional_second_future_knowledge_cannot_enter_frozen_context(memory) -> None:
    future = memory.append(episode("fractional-future", "preference", known_at="2026-10-01T00:00:00.500Z"))
    frozen = snapshot(memory)
    assert memory.retrieve_bundle(frozen["snapshot_id"], "risk")["results"] == []
    with pytest.raises(MemoryIsolationError):
        memory.expand(frozen["snapshot_id"], future["episode_id"])


def test_transport_failure_retry_and_new_adapter_keep_original_snapshot(tmp_path: Path) -> None:
    server = make_server("127.0.0.1", 0, tmp_path / "memory.sqlite3", source_adapters={})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        memory = HttpMemoryAdapter(base_url)
        safe = memory.append(episode("safe", "evidence"))
        frozen = snapshot(memory, "m1_judgment")
        before = memory.export_space("retrieval-test")
        failures = 1

        def flaky_opener(request, **kwargs):
            nonlocal failures
            if request.full_url.endswith("/expand") and failures:
                failures -= 1
                raise OSError("synthetic transport outage")
            return urlopen(request, **kwargs)

        flaky = HttpMemoryAdapter(base_url, opener=flaky_opener)
        with pytest.raises(MemoryUnavailable, match="transport outage"):
            flaky.retrieve_bundle(frozen["snapshot_id"], "risk")
        retry = flaky.retrieve_bundle(frozen["snapshot_id"], "risk")
        restarted = HttpMemoryAdapter(base_url).retrieve_bundle(frozen["snapshot_id"], "risk")
        assert retry["results"] == restarted["results"]
        assert retry["results"][0]["episode_id"] == safe["episode_id"]
        assert retry["snapshot"] == restarted["snapshot"] == frozen
        assert memory.export_space("retrieval-test") == before
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
