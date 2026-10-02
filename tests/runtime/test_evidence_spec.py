from __future__ import annotations

import copy
import hashlib
import json
import uuid
from pathlib import Path

import pytest

from ai_trading_companion.acquisition import AcquisitionBoundary
from ai_trading_companion.evidence_spec import fingerprint, frozen_replay, install_qualification, qualify, validate
from ai_trading_companion.evidence_qualification import POLICY_VERSION, qualify_record
from ai_trading_companion.memory_evidence import MemoryEvidenceRegistrar
from ai_trading_companion.memory_port import InMemoryMemoryAdapter
from ai_trading_companion.store import CompanionStore
from trading_memory_hub import MemoryHub


def _record(kind: str = "news_disclosure", *, truth: str = "verified", propagation: str = "unknown") -> dict:
    boundary = AcquisitionBoundary("replay-attempt")
    observation, _ = boundary.observe("web_read", {"requirement_key": "events"}, {"results": [{
        "url": "https://example.test/event", "title": "公开材料", "excerpt_text": "2026-09-20 发生了可核验事件",
        "fact_as_of": "2026-09-20T01:00:00Z", "factual_status": truth,
        "market_propagation": propagation,
        "propagation_impact": {"breadth": "theme", "evidence_refs": ["prop-1"]},
        "evidence_kind": kind,
    }]}, True)
    return observation["evidence_items"][0]["evidence_spec"]


def test_record_keeps_two_independent_truth_dimensions_and_provenance() -> None:
    record = _record(propagation="observed")

    validate(record)
    assert record["occurred_at"] == "2026-09-20T01:00:00Z"
    assert record["known_at"]
    assert record["provenance"]["observation_id"].startswith("obs_")
    assert record["truth_status"] == "verified"
    assert record["market_propagation"] == {
        "status": "observed", "impact": {"breadth": "theme", "evidence_refs": ["prop-1"]},
    }


def test_ai_reasoning_can_be_retained_but_cannot_be_external_fact() -> None:
    record = _record("ai_reasoning")

    assert qualify(record)["state"] == "rejected"
    assert "ai_is_not_external_evidence" in qualify(record)["reasons"]
    forged = copy.deepcopy(record)
    forged["external_fact"] = True
    forged["record_id"] = hashlib.sha256(
        __import__("json").dumps({k: v for k, v in forged.items() if k != "record_id"}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with pytest.raises(ValueError, match="external fact"):
        validate(forged)


@pytest.mark.parametrize("provenance", [{"generated_by": "ai"}, {"origin": "ai"}])
def test_acquisition_cannot_promote_ai_origin_to_market_fact(provenance: dict) -> None:
    boundary = AcquisitionBoundary("ai-origin")
    observation, _ = boundary.observe("market_snapshot", {}, {"results": [{
        "url": "https://example.test/generated", "excerpt_text": "模型生成的行情描述",
        "fact_as_of": "2026-09-20T01:00:00Z", "factual_status": "verified",
        "evidence_kind": "market_fact", **provenance,
    }]}, True)
    record = observation["evidence_items"][0]["evidence_spec"]
    assert record["kind"] == "ai_reasoning"
    assert record["external_fact"] is False
    assert record["provenance"]["origin"] == "ai"
    assert qualify(record)["state"] == "rejected"


def test_memoryhub_receipt_preserves_the_versioned_record() -> None:
    memory = InMemoryMemoryAdapter()
    record = _record()
    registrar = MemoryEvidenceRegistrar(memory, clock=lambda: "2026-09-20T02:00:00Z")

    registered = registrar.register_web_snapshot(
        memory_space_id="replay", source_event_id="event-1", url=record["source"]["url"],
        title=record["source"]["title"], body=record["content"], occurred_at=record["occurred_at"],
        evidence_spec=record,
    )

    episode = memory.export_space("replay")["episodes"][0]
    stored = episode["metadata"]["evidence_spec"]
    assert stored["contract"] == "EvidenceSpec/v1"
    assert stored["known_at"] == registered.known_at
    assert stored["market_propagation"]["status"] == "unknown"
    assert registered.context["memory_episode_id"] == episode["episode_id"]


def test_memoryhub_rejects_modified_evidence_before_recording() -> None:
    memory = InMemoryMemoryAdapter()
    record = _record()
    record["truth_status"] = "refuted"
    registrar = MemoryEvidenceRegistrar(memory, clock=lambda: "2026-09-20T02:00:00Z")

    with pytest.raises(ValueError, match="integrity mismatch"):
        registrar.register_web_snapshot(
            memory_space_id="replay", source_event_id="tampered-event",
            url=record["source"]["url"], title=record["source"]["title"],
            body=record["content"], occurred_at=record["occurred_at"], evidence_spec=record,
        )

    assert memory.export_space("replay")["episodes"] == []


@pytest.mark.parametrize("field,value", [
    ("body", "different material"),
    ("url", "https://example.test/different"),
])
def test_memoryhub_rejects_snapshot_that_does_not_match_evidence(field: str, value: str) -> None:
    memory = InMemoryMemoryAdapter()
    record = _record()
    arguments = {
        "memory_space_id": "replay", "source_event_id": "mismatched-event",
        "url": record["source"]["url"], "title": record["source"]["title"],
        "body": record["content"], "occurred_at": record["occurred_at"], "evidence_spec": record,
    }
    arguments[field] = value
    registrar = MemoryEvidenceRegistrar(memory, clock=lambda: "2026-09-20T02:00:00Z")

    with pytest.raises(ValueError, match="snapshot does not match evidence"):
        registrar.register_web_snapshot(**arguments)

    assert memory.export_space("replay")["episodes"] == []


def test_legacy_memoryhub_snapshot_without_evidence_spec_remains_accepted(tmp_path: Path) -> None:
    memory = MemoryHub(tmp_path / "memory.sqlite3")
    receipt = memory.append({
        "memory_space_id": "legacy", "source_system": "wag",
        "source_event_id": "event-legacy", "content_hash": "auto",
        "episode_type": "external_evidence", "body": "旧路径证据",
        "occurred_at": "2026-09-19T01:00:00Z",
        "known_at": "2026-09-20T02:00:00Z",
        "submitted_at": "2026-09-20T02:00:00Z",
        "authority": "mutable_source_snapshot", "protocol_version": "memoryhub/v1",
        "metadata": {"url": "https://example.test/legacy", "title": "旧路径", "object_reference": None},
    })

    assert receipt.episode_id
    assert memory.export_space("legacy")["episodes"][0]["metadata"] == {
        "object_reference": None,
        "title": "旧路径",
        "url": "https://example.test/legacy",
    }


def test_frozen_replay_rebuilds_the_same_qualification_without_mutating_history() -> None:
    record = _record(propagation="observed")
    frozen = json.loads(json.dumps(record, ensure_ascii=False, sort_keys=True))

    assert qualify(frozen) == qualify(record)
    expired = copy.deepcopy(frozen)
    expired["expires_at"] = "2099-01-01T00:00:00Z"
    expired["record_id"] = hashlib.sha256(
        json.dumps({k: v for k, v in expired.items() if k != "record_id"}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert qualify(expired, as_of="2099-01-01T00:00:00Z")["state"] == "expired"
    assert record["market_propagation"]["status"] == "observed"


def test_frozen_replay_keeps_input_version_qualification_and_original_artifact() -> None:
    record = _record(propagation="observed")
    artifact = {"judgment": "条件成立", "published_at": "2026-09-20T03:00:00Z"}
    first = frozen_replay(record, as_of="2099-01-01T00:00:00Z", original_artifact=artifact)
    second = frozen_replay(copy.deepcopy(record), as_of="2099-01-01T00:00:00Z", original_artifact=artifact)
    assert first == second
    assert first["evidence_contract"] == "EvidenceSpec/v1"
    assert first["qualification"]["state"] == "qualified"
    assert first["original_artifact"] == artifact
    assert record["record_id"] == first["evidence"]["record_id"]


def test_install_qualification_is_deterministic_and_keeps_evaluation_axes_separate() -> None:
    first = install_qualification()
    second = install_qualification()
    assert first == second
    assert first["qualified"] is True
    assert first["replay"]["evidence_contract"] == "EvidenceSpec/v1"
    assert first["replay"]["original_artifact"]["artifact_id"] == "install-evidence-artifact"
    assert set(first["evaluation_vector"]) == {
        "delivery_speed", "qualification_probability", "research_quality",
        "judgment_outcome", "safety_reliability",
    }
    smoke = first["source_unavailable_smoke"]
    assert smoke["status"] == "failed"
    assert smoke["available"] is False and smoke["qualified"] is False
    assert smoke["verifier_passed"] is False and smoke["evidence_items"] == []
    assert smoke["backend_calls"] == [{"operation": "web_read", "url": "https://example.test/unavailable"}]
    assert all(smoke["measurements"].values())
    assert first["evaluation_vector"]["judgment_outcome"]["status"] == "not_measured"
    assert first["evaluation_vector"]["safety_reliability"]["measurements"]["unavailable_source_safe"] is True
    for axis, value in first["evaluation_vector"].items():
        assert isinstance(value, dict)
        assert value["status"] in {"pass", "fail", "not_measured"}
        assert isinstance(value["measurements"], dict)
        assert isinstance(value["measurements"]["measured"], bool)
        if value["status"] != "pass":
            assert value["reason"]


def test_replay_uses_production_weak_source_policy_and_binds_cutoff() -> None:
    record = _record("market_fact")
    record["source"]["reference"]["screenshot_only"] = True
    record["record_id"] = fingerprint({k: v for k, v in record.items() if k != "record_id"})
    replay = frozen_replay(record, as_of="2099-01-01T00:00:00Z")
    assert replay["qualification"] == qualify_record(record, as_of="2099-01-01T00:00:00Z")
    assert replay["qualification"]["state"] == "degraded"
    assert replay["qualification"]["permitted_use"] == "context_only"
    assert replay["qualification_inputs"]["qualification_policy_version"] == POLICY_VERSION
    assert replay["input_sha256"] == fingerprint(replay["qualification_inputs"])
    assert replay["input_sha256"] != frozen_replay(record, as_of="2099-01-02T00:00:00Z")["input_sha256"]


def test_replay_receipt_persists_every_production_qualification_input() -> None:
    record = _record("news_disclosure")
    replay = frozen_replay(
        record,
        as_of="2026-09-20T08:00:00Z",
        source_refs=("source-b", "source-a", "source-a"),
        source_conflict_refs=("conflict-2", "conflict-1"),
        memory_receipt={"episode_id": "episode-1", "content_hash": "hash-1"},
        allow_post_cutoff_known_at=True,
    )

    inputs = replay["qualification_inputs"]
    assert inputs["as_of"] == "2026-09-20T08:00:00Z"
    assert inputs["qualification_policy_version"] == POLICY_VERSION
    assert inputs["source_refs"] == ["source-a", "source-b"]
    assert inputs["source_conflict_refs"] == ["conflict-1", "conflict-2"]
    assert inputs["memory_receipt"] == {"episode_id": "episode-1", "content_hash": "hash-1"}
    assert inputs["allow_post_cutoff_known_at"] is True
    assert replay["input_sha256"] == fingerprint(inputs)
    assert replay["qualification"]["input_record_refs"][0]["source_refs"] == ["source-a", "source-b"]
    assert replay["qualification"]["qualification_policy_version"] == POLICY_VERSION
    assert replay["qualification"]["as_of"] == "2026-09-20T08:00:00Z"
    assert replay["qualification"]["source_conflict_refs"] == ["conflict-1", "conflict-2"]
    assert replay["qualification"]["input_record_refs"][0]["memory_receipt"] == inputs["memory_receipt"]


def test_frozen_replay_preserves_production_permission_boundaries() -> None:
    ai_record = _record("ai_reasoning")
    screenshot_record = _record("market_fact")
    screenshot_record["source"]["reference"]["screenshot_only"] = True
    screenshot_record["record_id"] = fingerprint({
        key: value for key, value in screenshot_record.items() if key != "record_id"
    })

    ai_replay = frozen_replay(ai_record, as_of="2099-01-01T00:00:00Z")
    screenshot_replay = frozen_replay(screenshot_record, as_of="2099-01-01T00:00:00Z")

    assert ai_replay["qualification"]["state"] == "rejected"
    assert ai_replay["qualification"]["permitted_use"] == "reasoning_only"
    assert "ai_is_not_external_evidence" in ai_replay["qualification"]["reasons"]
    assert screenshot_replay["qualification"]["state"] == "degraded"
    assert screenshot_replay["qualification"]["permitted_use"] == "context_only"
    assert "weak_source_screenshot_only" in screenshot_replay["qualification"]["reasons"]


def test_replay_rejects_tampered_input_without_rewriting_original() -> None:
    record = _record()
    original_id = record["record_id"]
    tampered = copy.deepcopy(record)
    tampered["content"] = "被篡改"
    with pytest.raises(ValueError, match="integrity mismatch"):
        frozen_replay(tampered)
    assert record["record_id"] == original_id


def test_runtime_ledger_keeps_record_fields_and_emits_exchange_contract(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "companion.sqlite3")
    cycle = store.ensure_daily_conversation("2026-09-20")
    store.record_evidence(cycle, "m0_research", {"as_of": "2026-09-20T01:00:00Z", "sources": [{
        "url": "https://example.test/fact", "title": "事实", "fact_as_of": "2026-09-20T01:00:00Z",
        "excerpt": "2026-09-20 可核验事实",
    }]})

    row = store.evidence_for_day("2026-09-20", "9999-01-01T00:00:00Z")[0]
    assert row["evidence_kind"] == "news_disclosure"
    assert row["coverage_state"] == "observed"
    assert row["occurred_at"] == "2026-09-20T01:00:00Z"
    assert row["known_at"]
    assert json.loads(row["provenance_json"])["origin"] == "external_source"
    events = store.pending_events()
    assert any(event["event_type"] == "evidence.recorded" for event in events)


def test_runtime_ledger_retries_are_idempotent_and_retain_versioned_provenance(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "companion.sqlite3")
    cycle = store.ensure_daily_conversation("2026-09-20")
    evidence = {"as_of": "2026-09-20T01:00:00Z", "sources": [{
        "evidence_ref": "fact-1", "url": "https://example.test/fact", "title": "事实",
        "fact_as_of": "2026-09-20T01:00:00Z", "excerpt": "2026-09-20 可核验事实",
    }]}

    first = store.record_evidence(cycle, "m0_research", evidence)
    second = store.record_evidence(cycle, "m0_research", evidence)

    rows = store.evidence_for_day("2026-09-20", "9999-01-01T00:00:00Z")
    assert len(rows) == 1
    spec = json.loads(rows[0]["evidence_spec_json"])
    assert spec["contract"] == "EvidenceSpec/v1"
    assert spec["record_id"]
    assert spec["provenance"]["evidence_ref"] == "fact-1"
    assert json.loads(rows[0]["qualification_spec_json"])["input_record_refs"][0]["record_id"] == spec["record_id"]
    assert len(first) == 1
    assert second == []


def test_runtime_ledger_rejects_provenance_reference_drift(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "companion.sqlite3")
    cycle = store.ensure_daily_conversation("2026-09-20")
    observation, _ = AcquisitionBoundary("provenance-drift").observe("web_read", {}, {"results": [{
        "url": "https://example.test/fact", "title": "Fact", "excerpt_text": "stable fact",
        "fact_as_of": "2026-09-20T01:00:00Z",
    }]}, True)
    item = observation["evidence_items"][0]
    item["evidence_spec"]["provenance"]["evidence_ref"] = "different-ref"
    item["evidence_spec"]["record_id"] = fingerprint({
        key: value for key, value in item["evidence_spec"].items() if key != "record_id"
    })
    evidence = {"sources": [{"evidence_ref": item["evidence_ref"]}]}
    with pytest.raises(ValueError, match="provenance reference"):
        store.record_evidence(cycle, "m0_research", evidence, [observation])


def test_runtime_ledger_rejects_missing_provenance_reference_for_bound_source(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "companion.sqlite3")
    cycle = store.ensure_daily_conversation("2026-09-20")
    observation, _ = AcquisitionBoundary("missing-provenance-ref").observe(
        "web_read", {}, {"results": [{
            "url": "https://example.test/fact", "title": "Fact",
            "excerpt_text": "stable fact", "fact_as_of": "2026-09-20T01:00:00Z",
        }]}, True,
    )
    item = observation["evidence_items"][0]
    item["evidence_spec"]["provenance"]["evidence_ref"] = ""
    item["evidence_spec"]["record_id"] = fingerprint({
        key: value for key, value in item["evidence_spec"].items() if key != "record_id"
    })
    evidence = {"sources": [{"evidence_ref": item["evidence_ref"]}]}
    with pytest.raises(ValueError, match="provenance reference"):
        store.record_evidence(cycle, "m0_research", evidence, [observation])


def test_runtime_ledger_preserves_distinct_observed_versions_with_identical_content(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "companion.sqlite3")
    cycle = store.ensure_daily_conversation("2026-09-20")
    boundary = AcquisitionBoundary("versioned-attempt")
    observations = []
    for occurred_at in ("2026-09-20T01:00:00Z", "2026-09-20T02:00:00Z"):
        observation, _ = boundary.observe("web_read", {}, {"results": [{
            "url": "https://example.test/fact", "title": "Fact", "excerpt_text": "unchanged content",
            "fact_as_of": occurred_at, "factual_status": "verified",
        }]}, True)
        observations.append(observation)
    evidence = {"as_of": "2099-01-01T00:00:00Z", "sources": [
        {"evidence_ref": observation["evidence_items"][0]["evidence_ref"]}
        for observation in observations
    ]}

    assert len(store.record_evidence(cycle, "m0_research", evidence, observations)) == 2
    assert store.record_evidence(cycle, "m0_research", evidence, observations) == []
    rows = store.evidence_for_day("2026-09-20", "2099-01-01T00:00:00Z")
    assert {row["occurred_at"] for row in rows} == {
        "2026-09-20T01:00:00Z", "2026-09-20T02:00:00Z",
    }
    assert {json.loads(row["evidence_spec_json"])["record_id"] for row in rows} == {
        observation["evidence_items"][0]["evidence_spec"]["record_id"] for observation in observations
    }


def test_runtime_ledger_upgrade_reuses_existing_acquisition_identity(tmp_path: Path) -> None:
    path = tmp_path / "companion.sqlite3"
    store = CompanionStore(path)
    cycle = store.ensure_daily_conversation("2026-09-20")
    observation, _ = AcquisitionBoundary("old-attempt").observe("web_read", {}, {"results": [{
        "url": "https://example.test/fact", "title": "Fact", "excerpt_text": "original fact",
        "fact_as_of": "2026-09-20T01:00:00Z",
    }]}, True)
    evidence = {"as_of": "2099-01-01T00:00:00Z", "sources": [{
        "evidence_ref": observation["evidence_items"][0]["evidence_ref"],
    }]}
    store.record_evidence(cycle, "m0_research", evidence, [observation])
    row = store.evidence_for_day("2026-09-20", "2099-01-01T00:00:00Z")[0]
    old_id = str(uuid.uuid5(uuid.NAMESPACE_URL,
        f"2026-09-20|{row['source_url']}|{row['content_sha256']}"))
    # Reconstruct the pre-upgrade persisted identity and uniqueness index.
    with store.connection() as connection:
        connection.execute("DELETE FROM evidence_cycle_use")
        connection.execute("UPDATE evidence_ledger_entry SET evidence_id=?", (old_id,))
        connection.execute("CREATE UNIQUE INDEX ux_evidence_content ON evidence_ledger_entry(trading_date,source_url,content_sha256)")
    event_count = len(store.pending_events())

    upgraded = CompanionStore(path)
    upgraded.initialize()
    assert upgraded.record_evidence(cycle, "m0_research", evidence, [observation]) == []
    assert [entry["evidence_id"] for entry in upgraded.evidence_for_day(
        "2026-09-20", "2099-01-01T00:00:00Z"
    )] == [old_id]
    assert len(upgraded.pending_events()) == event_count
