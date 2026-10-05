from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.mandate_spec import (
    build_mandate,
    frozen_replay,
    install_qualification,
    resolve_cycle_mandates,
    validate_mandate,
    validate_mandate_set,
)
from ai_trading_companion.memory_port import InMemoryMemoryAdapter
from ai_trading_companion.packet_builder import RuntimePacketBuilder
from ai_trading_companion.store import CompanionStore


def test_m1_visibility_is_runtime_forced_and_configuration_cannot_override() -> None:
    mandate = build_mandate("daily.execution.0945", "m1_judgment", as_of="2026-10-05T01:45:00Z")
    assert mandate["visibility"]["h0_visible"] is False
    assert mandate["permissions"] == {"write_permissions": []}
    assert mandate["quantresearch_permission"]["access"] == "read_only"
    with pytest.raises(ValueError, match="cannot override"):
        build_mandate(
            "daily.execution.0945", "m1_judgment", as_of="2026-10-05T01:45:00Z",
            config={"visibility": {"version": 1, "h0_visible": True}},
        )


def test_mandate_schema_and_digest_are_strict() -> None:
    root = Path(__file__).parents[2]
    schema = json.loads((root / "resources/contracts/mandate-spec-v1.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    m1 = build_mandate("daily.execution.0945", "m1_research", as_of="2026-10-05T01:45:00Z")
    m0 = build_mandate("daily.execution.0945", "m0_research", as_of="2026-10-05T01:45:00Z")
    assert not list(validator.iter_errors(m1))
    assert not list(validator.iter_errors(m0))
    broken = dict(m1)
    broken["visibility"] = {**m1["visibility"], "h0_visible": True}
    assert list(validator.iter_errors(broken))
    broken = dict(m1)
    broken["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="sha256 mismatch"):
        validate_mandate(broken)


def test_mandate_set_is_complete_and_frozen_replay_is_deterministic() -> None:
    first = resolve_cycle_mandates(
        "daily.execution.0945", as_of="2026-10-05T01:45:00Z", memory_space_id="formal",
    )
    second = resolve_cycle_mandates(
        "daily.execution.0945", as_of="2026-10-05T01:45:00Z", memory_space_id="formal",
    )
    assert first == second
    assert validate_mandate_set(first) == first
    replay = frozen_replay(first["mandates"]["m1_judgment"])
    assert replay == frozen_replay(first["mandates"]["m1_judgment"])
    assert replay["qualification"]["m1_h0_blind"] is True
    assert install_qualification()["qualified"] is True


class _MemoryProbe:
    def __init__(self) -> None:
        self.snapshot_request = None
        self.limit = None

    def begin_snapshot(self, request):
        self.snapshot_request = request
        return {"snapshot_id": "snapshot-1"}

    def retrieve_bundle(self, snapshot_id, query, *, limit):
        self.limit = limit
        return {"results": [
            {"episode_type": "lesson", "memory_id": "lesson-1"},
            {"episode_type": "message", "memory_id": "message-1"},
        ]}


def test_runtime_cycle_persists_mandate_and_packet_enforces_memory_scope(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "companion.sqlite3")
    engine = CompanionEngine(store, memory=InMemoryMemoryAdapter(), memory_space_id="engine-space")
    config = {
        "memory_scope": {
            "version": 1, "memory_space_id": "frozen-space",
            "allowed_stages": [
                "m0_research", "m0_compose", "m1_research", "m1_judgment", "m2",
                "chat_research", "chat", "reflection", "workflow_feedback", "outcome_research",
            ], "allowed_kinds": ["lesson"],
            "max_results": 1, "as_of_bounded": True,
        }
    }
    cycle = engine.start_cycle(
        "daily.execution.0945", "2026-10-05T09:45:00+08:00",
        "2026-10-05T01:45:00Z", mandate_config=config,
    )
    provenance = json.loads(cycle["cycle_provenance_json"])
    mandate_set = validate_mandate_set(provenance["mandates"])
    assert mandate_set["mandates"]["m0_research"]["memory_scope"]["memory_space_id"] == "frozen-space"
    duplicate = engine.start_cycle(
        "daily.execution.0945", "2026-10-05T09:45:00+08:00",
        "2026-10-05T01:45:00Z",
        mandate_config={"risk_level": {"version": 1, "value": "low"}},
    )
    assert duplicate["cycle_id"] == cycle["cycle_id"]
    assert json.loads(duplicate["cycle_provenance_json"])["mandates"] == provenance["mandates"]

    probe = _MemoryProbe()
    packet = RuntimePacketBuilder(
        Path(__file__).parents[2] / "resources", store, memory=probe,
        memory_space_id="ignored-by-frozen-mandate",
    ).build(cycle, "m0_research")
    assert probe.snapshot_request["memory_space_id"] == "frozen-space"
    assert probe.limit == 1
    assert [row["episode_type"] for row in packet["public_research_scope"]["selected_memory"]] == ["lesson"]
    assert packet["mandate"]["sha256"] == packet["mandate_reference"]["sha256"]


def test_manual_cycle_freezes_same_mandate_contract(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "companion.sqlite3")
    engine = CompanionEngine(store, memory=InMemoryMemoryAdapter(), memory_space_id="manual-space")
    result = engine.request_formal_analysis({
        "request_id": "mandate-request-1",
        "requested_at": "2026-10-05T01:45:00Z",
        "source": {"conversation_cycle_id": "conversation-1"},
        "task_key": "daily.review.1520",
        "task_profile": {"profile_id": "profile-1", "version": 1},
    })
    cycle = store.get_cycle(result["receipt"]["cycle_id"])
    provenance = json.loads(cycle["cycle_provenance_json"])
    assert validate_mandate_set(provenance["mandates"])["mandates"]["m1_judgment"]["visibility"]["h0_visible"] is False
    replay = engine.request_formal_analysis({
        "request_id": "mandate-request-1",
        "requested_at": "2026-10-05T01:45:00Z",
        "source": {"conversation_cycle_id": "conversation-1"},
        "task_key": "daily.review.1520",
        "task_profile": {"profile_id": "profile-1", "version": 1},
    })
    assert replay["receipt"]["state"] == "reused"
    assert replay["receipt"]["cycle_id"] == cycle["cycle_id"]
