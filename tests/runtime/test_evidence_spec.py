from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from ai_trading_companion.acquisition import AcquisitionBoundary
from ai_trading_companion.evidence_spec import qualify, validate
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
