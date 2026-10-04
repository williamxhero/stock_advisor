from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.memory_port import InMemoryMemoryAdapter
from ai_trading_companion.memory_type import (
    CONTRACT,
    SEMANTIC_TYPES,
    build_envelope,
    frozen_replay,
    install_qualification,
    typed_episode,
    validate,
)
from ai_trading_companion.store import CompanionStore
from trading_memory_hub import MemoryHub, SourceIntegrityError



def episode(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "memory_space_id": "partner-main",
        "source_system": "stock-advisor",
        "source_event_id": "memory-type-1",
        "content_hash": "auto",
        "episode_type": "personal_fact",
        "body": "用户长期重视风险调整后的复利。",
        "occurred_at": "2026-09-20T01:00:00Z",
        "known_at": "2026-09-20T01:01:00Z",
        "submitted_at": "2026-09-20T01:02:00Z",
        "authority": "user_private_fact",
        "protocol_version": "memoryhub/v1",
    }
    value.update(overrides)
    return value


def test_memory_type_envelope_covers_semantic_types_and_schema() -> None:
    root = Path(__file__).parents[2]
    schema = json.loads((root / "resources/contracts/memory-type-spec-v1.schema.json").read_text(encoding="utf-8"))
    for semantic_type in SEMANTIC_TYPES:
        value = build_envelope(episode(), semantic_type=semantic_type)
        validate(value)
        assert list(Draft202012Validator(schema).iter_errors(value)) == []
        assert value["contract"] == CONTRACT


def test_typed_episode_preserves_authority_times_and_correction_binding(tmp_path: Path) -> None:
    hub = MemoryHub(tmp_path / "ledger.sqlite3")
    original = typed_episode(episode(), semantic_type="user_fact")
    first = hub.append(original)
    correction = typed_episode(
        episode(
            source_event_id="memory-type-correction",
            body="更正：用户重视回撤控制。",
            episode_type="correction",
            authority="user_private_fact",
            corrects_episode_id=first.episode_id,
        ),
        semantic_type="correction",
    )
    second = hub.append(correction)

    exported = hub.export_space("partner-main")
    assert exported["episodes"][0]["body"] == original["body"]
    assert exported["episodes"][1]["metadata"]["memory_type"]["correction_of"] == first.episode_id
    assert exported["episodes"][1]["metadata"]["memory_type"]["temporal"]["submitted_at"] == "2026-09-20T01:02:00Z"
    assert second.episode_id != first.episode_id


def test_memoryhub_rejects_typed_identity_drift_but_keeps_legacy_episodes_compatible(tmp_path: Path) -> None:
    hub = MemoryHub(tmp_path / "ledger.sqlite3")
    value = typed_episode(episode(), semantic_type="user_fact")
    tampered = copy.deepcopy(value)
    tampered["metadata"]["memory_type"]["authority"] = "published_ai_message"
    with pytest.raises(SourceIntegrityError, match="authority"):
        hub.append(tampered)

    legacy = episode(source_event_id="legacy-1", metadata={"legacy": True})
    receipt = hub.append(legacy)
    assert receipt.episode_id


def test_projection_is_rebuildable_and_not_authoritative(tmp_path: Path) -> None:
    hub = MemoryHub(tmp_path / "ledger.sqlite3")
    value = typed_episode(episode(), semantic_type="observation")
    receipt = hub.append(value)

    def extract(_: str) -> dict[str, object]:
        return {"summary": "派生摘要", "propositions": []}

    assert hub.derive_pending(extract, extractor_version="memory-type-test/v1") == 1
    assert hub.derived_memory(receipt.episode_id)["summary"] == "派生摘要"
    stored = hub.export_space("partner-main")["episodes"][0]
    assert stored["metadata"]["memory_type"]["semantic_type"] == "observation"
    assert "memory_type" not in hub.derived_memory(receipt.episode_id)


def test_runtime_message_path_persists_memory_type_envelope(tmp_path: Path) -> None:
    memory = InMemoryMemoryAdapter()
    engine = CompanionEngine(CompanionStore(tmp_path / "companion.sqlite3"), memory=memory)
    cycle = engine.start_cycle("daily.execution.0945", "2026-09-20T01:00:00Z", "2026-09-20T01:00:00Z")
    engine.record_submitted_messages(cycle["cycle_id"], [{
        "message_id": "message-memory-type",
        "body_text": "我更重视回撤控制。",
        "occurred_at": "2026-09-20T01:00:00Z",
        "known_at": "2026-09-20T01:01:00Z",
        "submitted_at": "2026-09-20T01:02:00Z",
        "staged_at": "2026-09-20T01:01:00Z",
        "provenance_json": "{}",
        "batch_id": "batch-1",
        "phase": "chat",
    }])

    envelope = memory._episodes[0]["metadata"]["memory_type"]
    assert envelope["semantic_type"] == "message"
    assert envelope["source"]["source_event_id"] == "message-memory-type"
    assert envelope["temporal"]["submitted_at"] == "2026-09-20T01:02:00Z"


def test_memory_type_replay_and_install_qualification_are_deterministic() -> None:
    value = build_envelope(episode(), semantic_type="preference")
    first = frozen_replay(value)
    second = frozen_replay(copy.deepcopy(value))
    assert first == second
    qualification = install_qualification()
    assert qualification["contract"] == "MemoryTypeInstallQualification/v1"
    assert qualification["qualified"] is True
    assert set(qualification["evaluation_vector"]) == {
        "delivery_speed", "qualification_probability", "research_quality",
        "judgment_outcome", "safety_reliability",
    }
