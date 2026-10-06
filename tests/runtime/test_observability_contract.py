from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker, RefResolver

from ai_trading_companion.observability_contract import (
    AggregateScoreError,
    CONTRACT,
    EVALUATION_DIMENSIONS,
    ObservabilityContractError,
    build_evaluation_vector,
    build_event,
    compute_wait_workload,
    event_type_for_runtime_event,
    frozen_replay,
    sha256,
    validate_evaluation_vector,
    validate_event,
    validate_replay,
    ObservationEvent,
)
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.store import CompanionStore

ROOT = Path(__file__).resolve().parents[2]
T0 = "2026-10-06T01:00:00Z"
T1 = "2026-10-06T01:01:00Z"
T2 = "2026-10-06T01:02:00Z"


def event(kind="actual_started", **kwargs):
    occurred_at = kwargs.pop("occurred_at", T0)
    known_at = kwargs.pop("known_at", occurred_at)
    recorded_at = kwargs.pop("recorded_at", known_at)
    return build_event(
        event_id=kwargs.pop("event_id", f"event-{kind}"), event_type=kind,
        cycle_id=kwargs.pop("cycle_id", "cycle-1"), task_key="daily.execution.0945",
        stage=kwargs.pop("stage", "m0"), occurred_at=occurred_at,
        known_at=known_at, recorded_at=recorded_at,
        source=kwargs.pop("source", {"source": "runtime", "component": "test", "writer": "runtime"}),
        attribution=kwargs.pop("attribution", None), observations=kwargs.pop("observations", None),
        **kwargs,
    )


def test_contract_is_versioned():
    assert event()["contract"] == CONTRACT
    assert event()["contract_version"] == 1


def test_each_lifecycle_state_is_independent():
    assert {event(kind, event_id=kind)["event_type"] for kind in ("plan_started", "actual_started", "qualified_delivery", "failed")} == {
        "plan_started", "actual_started", "qualified_delivery", "failed"
    }


def test_required_identity_fields_are_preserved():
    value = event(stage_run_id="run-1", attempt_id="attempt-1")
    assert value["cycle_id"] == "cycle-1"
    assert value["stage_run_id"] == "run-1"
    assert value["attempt_id"] == "attempt-1"


def test_timestamp_order_is_enforced():
    with pytest.raises(ObservabilityContractError, match="known_at"):
        event(known_at="2026-10-06T00:59:00Z")
    with pytest.raises(ObservabilityContractError, match="recorded_at"):
        event(known_at=T1, recorded_at=T0)


def test_timezone_is_required():
    with pytest.raises(ObservabilityContractError, match="ISO-8601"):
        event(occurred_at="2026-10-06T01:00:00")


def test_unknown_event_fields_are_rejected():
    value = event()
    value["new_field"] = True
    with pytest.raises(ObservabilityContractError, match="exact"):
        validate_event(value)


def test_runtime_only_writer_is_enforced():
    with pytest.raises(PermissionError, match="Runtime-owned"):
        event(source={"source": "evaluation", "component": "test", "writer": "evaluation"})


def test_digest_is_deterministic_and_tamper_evident():
    first = event()
    second = event()
    assert first == second
    broken = copy.deepcopy(first)
    broken["status"] = "changed"
    with pytest.raises(ObservabilityContractError, match="digest"):
        validate_event(broken)


def test_plan_does_not_start_user_wait():
    value = event("plan_started", event_id="plan")
    assert value["attribution"]["user_wait"]["started_at"] is None


def test_actual_start_begins_wait():
    value = event("actual_started")
    assert value["attribution"]["user_wait"]["started_at"] == T0


def test_qualified_delivery_closes_wait():
    value = event("qualified_delivery", occurred_at=T1, attribution={"user_wait": {"started_at": T0, "ended_at": T1}})
    assert value["attribution"]["user_wait"]["ended_at"] == T1


def test_workload_and_wait_are_separate():
    value = event("actual_started", attribution={"user_wait": {"started_at": T0}, "workload": {"duration_ms": 5000}})
    assert value["attribution"]["user_wait"]["duration_ms"] is None
    assert value["attribution"]["workload"]["duration_ms"] == 5000


def test_upstream_reuse_retains_source_and_age_timestamps():
    value = event(attribution={"upstream": {"source_cycle_id": "prefetch-1", "source_task_key": "daily.opportunity.0900", "source_stage": "m0", "known_at": T0, "reused_at": T1}})
    assert value["attribution"]["upstream"]["source_cycle_id"] == "prefetch-1"
    assert value["attribution"]["upstream"]["reused_at"] == T1


def test_upstream_known_time_cannot_be_after_reuse():
    with pytest.raises(ObservabilityContractError, match="known_at"):
        event(attribution={"upstream": {"known_at": T2, "reused_at": T1}})


def test_wait_workload_derivation_uses_actual_to_delivery_only():
    result = compute_wait_workload([event("plan_started", event_id="p"), event("actual_started", event_id="a", occurred_at=T0), event("qualified_delivery", event_id="d", occurred_at=T2, attribution={"user_wait": {"started_at": T0, "ended_at": T2}})])
    assert result["user_wait"]["duration_ms"] == 120000
    assert result["workload"]["direct_event_count"] == 3


def test_prefetch_is_workload_not_wait():
    result = compute_wait_workload([event("actual_started", event_id="a"), event("qualified_delivery", event_id="d", occurred_at=T1, attribution={"user_wait": {"started_at": T0, "ended_at": T1}, "upstream": {"source_cycle_id": "prefetch"}, "workload": {"duration_ms": 90000}})])
    assert result["user_wait"]["duration_ms"] == 60000
    assert result["workload"]["upstream_duration_ms"] == 90000


def test_retry_and_failure_are_not_success():
    failed = event("failed", event_id="failed", observations={"retry": {"retryable": True}, "workload": {"retry_count": 2}})
    assert failed["event_type"] == "failed"
    assert failed["observations"]["workload"]["retry_count"] == 2


def test_runtime_event_mapping_covers_lifecycle():
    assert event_type_for_runtime_event("cycle.created") == "plan_started"
    assert event_type_for_runtime_event("m0.started") == "actual_started"
    assert event_type_for_runtime_event("m1.ready") == "qualified_delivery"
    assert event_type_for_runtime_event("research.failed") == "failed"
    assert event_type_for_runtime_event("unrelated.event") is None


def test_evaluation_vector_keeps_all_dimensions():
    vector = build_evaluation_vector({dimension: {"value": None, "status": "unknown"} for dimension in EVALUATION_DIMENSIONS})
    assert set(vector["dimensions"]) == set(EVALUATION_DIMENSIONS)
    assert "score" not in vector


@pytest.mark.parametrize("key", ["score", "aggregate_score", "composite", "overall", "weighted_average"])
def test_aggregate_score_is_rejected_at_any_nesting(key):
    dimensions = {dimension: {"value": None} for dimension in EVALUATION_DIMENSIONS}
    dimensions["research_quality"][key] = 1
    with pytest.raises(AggregateScoreError):
        build_evaluation_vector(dimensions)


def test_vector_unknown_or_missing_dimension_is_rejected():
    dimensions = {dimension: {"value": None} for dimension in EVALUATION_DIMENSIONS[:-1]}
    with pytest.raises(ObservabilityContractError, match="missing"):
        build_evaluation_vector(dimensions)
    complete = {dimension: {"value": None} for dimension in EVALUATION_DIMENSIONS}
    complete["unexpected"] = {"value": 1}
    with pytest.raises(ObservabilityContractError, match="unknown"):
        build_evaluation_vector(complete)


def test_replay_and_evaluation_contracts_have_explicit_versions():
    vector = build_evaluation_vector({dimension: {"value": None} for dimension in EVALUATION_DIMENSIONS})
    replay = frozen_replay(event())
    assert vector["contract_version"] == 1
    assert replay["contract_version"] == 1
    vector["contract_version"] = True
    with pytest.raises(ObservabilityContractError):
        validate_evaluation_vector(vector)
    replay["contract_version"] = True
    with pytest.raises(ObservabilityContractError):
        validate_replay(replay)


def test_private_content_and_unbounded_payloads_are_rejected():
    with pytest.raises(ObservabilityContractError, match="private"):
        event(observations={"thinking": "do not persist"})
    with pytest.raises(ObservabilityContractError, match="size"):
        event(observations={"large": "x" * 8193})


def test_wait_duration_and_causal_order_are_enforced():
    with pytest.raises(ObservabilityContractError, match="duration"):
        event("qualified_delivery", occurred_at=T1, attribution={"user_wait": {"started_at": T0, "ended_at": T1, "duration_ms": 1}})
    with pytest.raises(ObservabilityContractError, match="precede"):
        compute_wait_workload([event("actual_started"), event("qualified_delivery", occurred_at="2026-10-06T00:59:00Z", attribution={"user_wait": {"started_at": "2026-10-06T01:00:00Z", "ended_at": "2026-10-06T00:59:00Z"}})])


def test_observation_event_facade_is_defensive():
    value = event()
    facade = ObservationEvent(value)
    with pytest.raises(TypeError):
        facade.value["status"] = "tampered"
    value["status"] = "changed"
    assert facade.to_dict()["status"] == "actual_started"


def test_replay_is_deterministic_and_read_only():
    source = event()
    original = copy.deepcopy(source)
    assert frozen_replay(source) == frozen_replay(copy.deepcopy(source))
    assert source == original


def test_replay_digest_tampering_is_rejected():
    replay = frozen_replay(event())
    replay["source"]["status"] = "tampered"
    with pytest.raises(ObservabilityContractError):
        validate_replay(replay)


def test_json_schema_accepts_valid_event():
    schema = json.loads((ROOT / "resources/contracts/observability-spec-v1.schema.json").read_text(encoding="utf-8"))
    errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(event()))
    assert errors == []


def test_json_schemas_accept_vector_and_replay():
    event_schema = json.loads((ROOT / "resources/contracts/observability-spec-v1.schema.json").read_text(encoding="utf-8"))
    vector_schema = json.loads((ROOT / "resources/contracts/observability-evaluation-v1.schema.json").read_text(encoding="utf-8"))
    replay_schema = json.loads((ROOT / "resources/contracts/observability-replay-v1.schema.json").read_text(encoding="utf-8"))
    source = event()
    vector = build_evaluation_vector({dimension: {"value": None} for dimension in EVALUATION_DIMENSIONS})
    replay = frozen_replay(source)
    assert list(Draft202012Validator(vector_schema).iter_errors(vector)) == []
    resolver = RefResolver.from_schema(replay_schema, store={
        "observability-spec-v1.schema.json": event_schema,
        "./observability-spec-v1.schema.json": event_schema,
    })
    assert list(Draft202012Validator(replay_schema, resolver=resolver, format_checker=FormatChecker()).iter_errors(replay)) == []


def test_store_projects_runtime_event_and_is_idempotent(tmp_path):
    store = CompanionStore(tmp_path / "runtime.sqlite3")
    cycle = store.create_cycle("daily.execution.0945", T0, T0)
    payload = {"observability_event_id": "stable-1", "cycle": cycle, "occurred_at": T0}
    first = store.record_observation_event(cycle["cycle_id"], "m0.started", payload)
    second = store.record_observation_event(cycle["cycle_id"], "m0.started", payload)
    assert first == second
    assert len(store.observation_events(cycle["cycle_id"])) == 1


def test_store_rejects_conflicting_event_id(tmp_path):
    store = CompanionStore(tmp_path / "runtime.sqlite3")
    cycle = store.create_cycle("daily.execution.0945", T0, T0)
    store.record_observation_event(cycle["cycle_id"], "m0.started", {"observability_event_id": "stable-1", "cycle": cycle, "occurred_at": T0})
    with pytest.raises(ValueError, match="conflict"):
        store.record_observation_event(cycle["cycle_id"], "m0.started", {"observability_event_id": "stable-1", "cycle": cycle, "occurred_at": T1})


def test_store_observation_rows_are_append_only(tmp_path):
    store = CompanionStore(tmp_path / "runtime.sqlite3")
    cycle = store.create_cycle("daily.execution.0945", T0, T0)
    value = store.record_observation_event(cycle["cycle_id"], "m0.started", {"cycle": cycle, "occurred_at": T0})
    with store.connection() as connection:
        with pytest.raises(Exception, match="immutable"):
            connection.execute("UPDATE observability_event SET event_json='tampered' WHERE event_id=?", (value["event_id"],))
        with pytest.raises(Exception, match="immutable"):
            connection.execute("DELETE FROM observability_event WHERE event_id=?", (value["event_id"],))


def test_stage_and_attempt_facts_do_not_start_user_wait(tmp_path):
    store = CompanionStore(tmp_path / "runtime.sqlite3")
    cycle = store.create_cycle("daily.execution.0945", T0, T0)
    stage = store.start_stage(cycle["cycle_id"], "m0", as_of=T0)
    attempt = store.begin_attempt(cycle["cycle_id"], "m0_research", T0, "packet-hash")
    events = store.observation_events(cycle["cycle_id"])
    stage_event = next(item for item in events if item["event_id"] == store.stage_events(cycle["cycle_id"], "m0")[0]["event_id"])
    attempt_event = next(item for item in events if item["event_id"] == attempt["attempt_id"])
    assert stage_event["attribution"]["user_wait"]["started_at"] is None
    assert attempt_event["attribution"]["user_wait"]["started_at"] is None
    assert stage_event["attribution"]["workload"]["kind"] == "stage"
    assert attempt_event["attribution"]["workload"]["kind"] == "attempt"


def test_engine_lifecycle_emission_projects_runtime_event(tmp_path):
    store = CompanionStore(tmp_path / "runtime.sqlite3")
    engine = CompanionEngine(store)
    cycle = engine.start_cycle("daily.execution.0945", T1, as_of=T0)
    projected = store.observation_events(cycle["cycle_id"])
    assert [item["event_type"] for item in projected] == ["plan_started"]
    assert projected[0]["source"]["writer"] == "runtime"


def test_store_preserves_read_only_quantresearch_and_isolation_metadata(tmp_path):
    store = CompanionStore(tmp_path / "runtime.sqlite3")
    cycle = store.create_cycle("daily.execution.0945", T0, T0)
    value = store.record_observation_event(cycle["cycle_id"], "m1.ready", {"cycle": cycle, "observations": {"quantresearch": {"access": "read_only", "write_permissions": []}, "isolation": {"m0": True, "h0": False, "m1": True, "m2": True}}})
    assert value["observations"]["quantresearch"] == {
        "access": "read_only", "write_permissions": [],
    }
    assert value["observations"]["isolation"] == {
        "m0": True, "h0": False, "m1": True, "m2": True,
    }
    assert value["source"]["source"] == "runtime"
