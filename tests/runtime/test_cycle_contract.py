from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from ai_trading_companion.cycle_contract import SPEC_VERSION
from ai_trading_companion.cycle_contract import memory_boundary
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.store import CompanionStore
from ai_trading_companion.packet_builder import RuntimePacketBuilder


def make_engine() -> tuple[CompanionStore, CompanionEngine, str]:
    temporary = TemporaryDirectory()
    store = CompanionStore(Path(temporary.name) / "companion.sqlite3")
    engine = CompanionEngine(store)
    # Keep the temporary directory alive through the test via the store.
    store._test_temporary = temporary  # type: ignore[attr-defined]
    return store, engine, temporary.name


def test_cycle_contract_freezes_identity_schedule_and_state_events() -> None:
    store, engine, _ = make_engine()
    cycle = engine.start_cycle(
        "daily.execution.0945",
        "2026-09-21T09:45:00+08:00",
        "2026-09-21T01:45:00Z",
        schedule_id="daily-execution",
        schedule_revision=7,
        schedule_snapshot={"trigger": {"type": "trading_day_fixed", "time": "09:45"}},
    )

    assert cycle["cycle_spec_version"] == SPEC_VERSION
    contract = json.loads(cycle["cycle_contract_json"])
    assert contract["cycle_id"] == cycle["cycle_id"]
    assert contract["schedule_revision"] == 7
    assert cycle["cycle_contract_hash"]
    projection = engine.decision_cycle_contract(cycle["cycle_id"])
    assert projection["cycle_spec_version"] == SPEC_VERSION
    assert projection["cycle_contract_hash"] == cycle["cycle_contract_hash"]
    assert projection["cycle_provenance"] == contract

    repeated = engine.start_cycle(
        cycle["task_key"], cycle["scheduled_for"], "2026-09-21T02:00:00Z",
        schedule_id="changed", schedule_revision=8,
    )
    assert repeated["cycle_id"] == cycle["cycle_id"]
    assert engine.decision_cycle_contract(repeated["cycle_id"])["cycle_provenance"] == contract

    started = store.transition(cycle["cycle_id"], "researching_m0")
    events = store.cycle_events(cycle["cycle_id"])
    assert [event["event_type"] for event in events] == [
        "cycle.created", "cycle.state.researching_m0",
    ]
    assert events[-1]["from_state"] == "queued"
    assert events[-1]["to_state"] == started["state"]
    assert events[-1]["payload"]["revision"] == started["revision"]


def test_m1_durable_attempt_boundary_rejects_h0_raw_and_derived_signals() -> None:
    store, engine, _ = make_engine()
    cycle = engine.start_cycle(
        "daily.execution.0945", "2026-09-21T09:45:00+08:00", "2026-09-21T01:45:00Z"
    )

    with pytest.raises(ValueError, match="exposes H0"):
        store.begin_attempt(
            cycle["cycle_id"], "m1_judgment", cycle["as_of"],
            input_packet={"frozen_public_evidence": [], "h0_text": "用户原话"},
        )
    with pytest.raises(ValueError, match="exposes H0"):
        store.begin_attempt(
            cycle["cycle_id"], "m1_research", cycle["as_of"],
            input_packet={"public_evidence": [], "derived_signals": ["follow_h0"]},
        )

    attempt = store.begin_attempt(
        cycle["cycle_id"], "m1_judgment", cycle["as_of"],
        input_packet={"frozen_public_evidence": [], "business_context": {"portfolio": None}},
    )
    assert attempt["attempt_id"]


@pytest.mark.parametrize("stage", ["m1_research", "m1_judgment"])
@pytest.mark.parametrize("key", ["h0_propositions", "h0_actions", "cognition_receipt", "human_judgment"])
def test_m1_attempt_rejects_derived_input_before_persistence(stage: str, key: str) -> None:
    store, engine, _ = make_engine()
    cycle = engine.start_cycle(
        "daily.execution.0945", "2026-09-21T09:45:00+08:00", "2026-09-21T01:45:00Z"
    )
    with pytest.raises(ValueError, match="H0"):
        store.begin_attempt(cycle["cycle_id"], stage, cycle["as_of"], input_packet={"context": {key: "signal"}})
    assert store.attempts(cycle["cycle_id"]) == []


def test_real_research_packet_preserves_frozen_cycle_reference_on_retry() -> None:
    class EmptyMemory:
        def begin_snapshot(self, request):
            return {"snapshot_id": "test-snapshot"}

        def retrieve_bundle(self, snapshot_id, query, *, limit):
            return {"results": []}

    store, engine, _ = make_engine()
    cycle = engine.start_cycle(
        "daily.execution.0945", "2026-09-21T09:45:00+08:00", "2026-09-21T01:45:00Z",
        schedule_id="daily-execution", schedule_revision=7,
    )
    builder = RuntimePacketBuilder(Path(__file__).resolve().parents[2] / "resources", store, memory=EmptyMemory())
    packet = builder.build(cycle, "m0_research")
    reference = packet["cycle_reference"]
    assert reference["contract"] == "CompanionDecisionCycleSpec/v1"
    assert reference["sha256"] == cycle["cycle_contract_hash"]
    assert reference["input"]["schedule_revision"] == 7
    retry = store.retry_cycle(cycle["cycle_id"], "m0", "temporary failure")
    assert builder.build(retry, "m0_research")["cycle_reference"] == reference


def test_retry_recovery_and_rollback_are_append_only_and_replayable() -> None:
    store, engine, _ = make_engine()
    cycle = engine.start_cycle(
        "daily.execution.0945", "2026-09-21T09:45:00+08:00", "2026-09-21T01:45:00Z"
    )
    original = store.append_artifact(
        cycle["cycle_id"], "m0", "model", "客观观察", cycle["as_of"], {"direction_free": True}
    )

    retried = store.retry_cycle(cycle["cycle_id"], "m0", "provider timeout")
    assert retried["state"] == "m0_retry_wait"
    recovered = store.recover_cycle(cycle["cycle_id"], "m0", reason="provider restored")
    assert recovered["state"] == "queued"

    rerun = store.rollback_cycle(cycle["cycle_id"], "m0", "qualification failed")
    assert rerun["cycle_id"] != cycle["cycle_id"]
    assert store.latest_artifact(cycle["cycle_id"], "m0")["artifact_id"] == original["artifact_id"]
    assert store.latest_artifact(rerun["cycle_id"], "m0")["body_markdown"] == "客观观察"

    source_events = store.cycle_events(cycle["cycle_id"])
    assert [event["event_type"] for event in source_events] == [
        "cycle.created", "stage.retrying", "cycle.recovered", "stage.rolled_back",
    ]
    assert source_events[-1]["payload"]["rerun_cycle_id"] == rerun["cycle_id"]
    assert store.cycle_events(rerun["cycle_id"])[0]["event_type"] == "cycle.created"


def test_rollback_preserves_private_context_and_rejects_terminal_recovery() -> None:
    store, engine, _ = make_engine()
    cycle = engine.start_cycle(
        "daily.execution.0945", "2026-09-21T09:45:00+08:00", "2026-09-21T01:45:00Z"
    )
    store.transition(
        cycle["cycle_id"], "researching_m1",
        private_context_json=json.dumps({"positions": [{"code": "600487"}]}, ensure_ascii=False),
        private_context_sha256="private-hash",
        private_context_frozen_at="2026-09-21T01:50:00Z",
    )
    source = store.get_cycle(cycle["cycle_id"])
    rerun = store.rollback_cycle(cycle["cycle_id"], "m1", "repair probe")
    assert rerun["private_context_json"] == source["private_context_json"]
    assert rerun["private_context_sha256"] == source["private_context_sha256"]
    assert memory_boundary(rerun, "m1_judgment", "2026-09-21T02:10:00Z") == (
        cycle["cycle_id"], "2026-09-21T01:50:00Z"
    )

    terminal = store.transition(cycle["cycle_id"], "complete")
    with pytest.raises(ValueError, match="terminal cycle"):
        store.recover_cycle(terminal["cycle_id"], "m1")
