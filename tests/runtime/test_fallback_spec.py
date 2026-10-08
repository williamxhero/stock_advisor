import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from ai_trading_companion import fallback_spec
from ai_trading_companion.broker_client import canonical_packet_hash
from ai_trading_companion.cycle_replay import freeze_cycle, replay_cycle
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.store import CompanionStore


_AXES = (
    "delivery_speed",
    "qualification_probability",
    "research_quality",
    "judgment_outcome",
    "safety_reliability",
)


def test_install_qualification_reports_deterministic_independent_axes():
    first = fallback_spec.install_qualification()
    second = fallback_spec.install_qualification()

    assert first == second
    assert first["contract"] == "FallbackSpecInstallQualification/v1"
    assert first["qualified"] is True
    assert set(first["evaluation_vector"]) == set(_AXES)
    assert all(
        first["evaluation_vector"][axis]["status"] in {"pass", "fail", "not_measured"}
        for axis in _AXES
    )
    assert all(
        first["evaluation_vector"][axis].get("reason")
        for axis in _AXES
        if first["evaluation_vector"][axis]["status"] == "not_measured"
    )
    assert all(
        first["evaluation_vector"][axis]["status"] == "not_measured"
        for axis in _AXES[:-1]
    )
    assert first["evaluation_vector"]["safety_reliability"]["status"] in {"pass", "fail"}
    assert "aggregate_score" not in first


def test_fallback_receipt_keeps_failed_computation_unavailable_and_non_mutating():
    receipt = fallback_spec.build_receipt(
        "Adapter", "financial_calculation", status="failed",
        as_of="2026-09-21T01:45:00Z", source_contract="FinRobotAdapter/v1",
        source_version="1", input_sha256="a" * 64, deterministic=True,
        attempts=("attempt-1", "attempt-2"), cycle_id="cycle-1",
    )
    frozen = json.loads(json.dumps(receipt))

    assert fallback_spec.validate_receipt(receipt) == receipt
    assert receipt["state"] == "NOT_COMPUTABLE"
    assert receipt["substitute_value"] is None
    assert receipt["continuation"] == "blocked"
    assert receipt["boundaries"]["llm_numeric_substitution"] is False
    assert receipt["qualification"]["state"] == "not_evaluated"
    assert receipt == frozen


def test_fallback_receipt_schema_and_tampering_are_checked():
    receipt = fallback_spec.build_receipt(
        "MemoryHub", "read_episode", status="unavailable",
        as_of="2026-09-21T01:45:00Z", source_contract="MemoryHub/v1",
        source_version="1", input_sha256="b" * 64,
    )
    schema_path = Path(__file__).resolve().parents[2] / "resources" / "contracts" / "fallback-spec-v1.schema.json"
    Draft202012Validator(json.loads(schema_path.read_text(encoding="utf-8"))).validate(receipt)
    tampered = json.loads(json.dumps(receipt))
    tampered["boundaries"]["local_memory_fallback"] = True
    with pytest.raises(ValueError, match="deterministic policy"):
        fallback_spec.validate_receipt(tampered)


def test_adapter_replacement_cannot_change_contract_or_expand_permissions():
    requested = {
        "adapter_id": "primary", "mode": "read", "input_contract": "Input/v1",
        "output_contract": "Output/v1", "capabilities": ["market.read"],
        "permissions": {
            "network_permissions": ["market.read"], "state_permissions": ["cycle.read"],
            "write_permissions": [],
        },
    }
    permitted = {**requested, "adapter_id": "replacement", "permissions": {
        "network_permissions": [], "state_permissions": ["cycle.read"], "write_permissions": [],
    }}
    over_privileged = {**permitted, "permissions": {
        "network_permissions": [], "state_permissions": ["cycle.read"],
        "write_permissions": ["runtime.write"],
    }}
    changed_contract = {**permitted, "output_contract": "Output/v2"}

    assert fallback_spec.replacement_allowed(requested, permitted)
    assert not fallback_spec.replacement_allowed(requested, over_privileged)
    assert not fallback_spec.replacement_allowed(requested, changed_contract)


def test_persisted_cycle_replay_preserves_retry_evidence_and_original_artifacts(tmp_path):
    store = CompanionStore(tmp_path / "fallback-cycle.sqlite3")
    cycle = CompanionEngine(store).start_cycle(
        "daily.execution.0945", "2026-09-21T09:45:00+08:00", "2026-09-21T01:45:00Z",
        schedule_revision=7,
    )
    packet = {"frozen_public_evidence": [], "business_context": {"positions": []}}
    snapshot = store.create_evidence_snapshot(cycle["cycle_id"], {
        "schema_version": 3, "as_of": cycle["as_of"], "spoken_summary": "Observed facts",
        "sources": [{"evidence_ref": "ev-1", "excerpt": "Frozen evidence"}],
        "coverage": [], "critical_gaps": [], "conflicts": [], "high_impact_events": [],
    }, as_of=cycle["as_of"], source_watermarks={"market": "revision-7"})
    failed = store.begin_attempt(cycle["cycle_id"], "m1_judgment", cycle["as_of"],
                                 input_sha256=canonical_packet_hash(packet), input_packet=packet,
                                 model="frozen-model", runner_fingerprint="prompt-v7")
    store.finish_attempt(failed["attempt_id"], "failed", verifier={"passed": False})
    retried = store.begin_attempt(cycle["cycle_id"], "m1_judgment", cycle["as_of"],
                                  input_sha256=canonical_packet_hash(packet), input_packet=packet,
                                  model="frozen-model", runner_fingerprint="prompt-v7")
    store.finish_attempt(retried["attempt_id"], "succeeded", output={"direction": "wait"},
                         verifier={"passed": True})
    original_artifact = store.append_artifact(
        cycle["cycle_id"], "m1", "model", "Original judgment", cycle["as_of"]
    )

    frozen = freeze_cycle(store, cycle["cycle_id"])
    untouched = copy.deepcopy(frozen)
    first = replay_cycle(frozen)
    second = replay_cycle(copy.deepcopy(frozen))

    assert first == second
    assert frozen == untouched == freeze_cycle(store, cycle["cycle_id"])
    assert json.loads(frozen["source"]["attempts"][0]["input_packet_json"]) == packet
    assert [attempt["status"] for attempt in first["source"]["attempts"]] == ["failed", "succeeded"]
    assert first["source"]["evidence_snapshots"] == [snapshot]
    assert first["source"]["cycle"]["schedule"]["revision"] == 7
    assert first["source"]["attempts"][0]["runner_fingerprint"] == "prompt-v7"
    assert [attempt["qualified"] for attempt in first["qualification"]["attempts"]] == [False, True]
    assert first["source"]["artifacts"][0]["artifact_id"] == original_artifact["artifact_id"]
    assert first["source"]["artifacts"][0]["body_markdown"] == "Original judgment"
    assert store.latest_artifact(cycle["cycle_id"], "m1")["body_markdown"] == "Original judgment"
