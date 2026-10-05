from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from ai_trading_companion.audit_contract import (
    CONTRACT,
    build_output,
    expected_writer_identity,
    frozen_replay,
    install_qualification,
    sha256,
    validate_output,
    validate_replay,
)
from ai_trading_companion.store import CompanionStore


ROOT = Path(__file__).resolve().parents[2]
AS_OF = "2026-10-05T01:45:00Z"


def packet() -> dict:
    return {
        "schema_version": 3,
        "cycle_id": "audit-cycle",
        "stage": "m1_judgment",
        "task_key": "manual.non_trading_outlook",
        "as_of": AS_OF,
        "evidence": {
            "schema_version": 3,
            "as_of": AS_OF,
            "known_at": "2026-10-05T02:00:00Z",
            "sources": [{
                "evidence_ref": "ev-market",
                "source_identity": "market-feed",
                "excerpt": "成交量和广度数据",
                "fact_as_of": "2026-10-05T01:30:00Z",
                "known_at": "2026-10-05T02:00:00Z",
            }],
        },
        "evidence_snapshot": {
            "content_hash": sha256("snapshot"),
            "as_of": AS_OF,
            "included_sources": ["ev-market"],
        },
        "mandate": {"sha256": sha256("mandate")},
        "task_profile": {"version": 2},
    }


def output() -> dict:
    return {
        "propositions": [{
            "id": "p-market",
            "kind": "claim",
            "text": "市场广度仍然偏弱。",
            "evidence_refs": ["ev-market"],
            "counterevidence_refs": [],
        }],
        "risks": [{"id": "r-data", "text": "数据覆盖存在边界。", "evidence_refs": ["ev-market"]}],
    }


def record() -> dict:
    text = "维持观察，等待更多同一时点证据。"
    text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return build_output(
        cycle_id="audit-cycle", stage="m1_judgment", attempt_id="attempt-1",
        packet=packet(), output=output(), attempt={"model": "test-model", "broker_provider": "test-provider"},
        judgment={"state": "published", "text": text, "sha256": text_hash, "proposition_ids": ["p-market"]},
        writer_identity=expected_writer_identity(
            cycle_id="audit-cycle", stage="m1_judgment", attempt_id="attempt-1", component="companion-engine",
        ),
    )


def test_audit_answers_versions_capabilities_and_citations():
    value = record()
    assert value["contract"] == CONTRACT
    assert value["data_versions"] and value["code_versions"] and value["parameter_versions"]
    assert value["capabilities"]["models"][0]["id"] == "test-model"
    assert value["capabilities"]["providers"][0]["id"] == "test-provider"
    assert value["propositions"][0]["evidence_refs"] == ["ev-market"]
    assert value["risks"][0]["evidence_refs"] == ["ev-market"]
    schema = json.loads((ROOT / "resources/contracts/audit-spec-v1.schema.json").read_text(encoding="utf-8"))
    assert not list(Draft202012Validator(schema).iter_errors(value))


def test_provider_output_private_reasoning_is_rejected_before_recording():
    broken = output()
    broken["chain_of_thought"] = "private deliberation"
    with pytest.raises(ValueError, match="private reasoning"):
        build_output(cycle_id="audit-cycle", stage="m1_judgment", attempt_id="attempt-1", packet=packet(), output=broken)


def test_unknown_proposition_and_risk_citations_are_rejected():
    broken = copy.deepcopy(output())
    broken["propositions"][0]["evidence_refs"] = ["not-in-catalog"]
    with pytest.raises(ValueError, match="outside the catalog"):
        build_output(cycle_id="audit-cycle", stage="m1_judgment", attempt_id="attempt-1", packet=packet(), output=broken)
    broken = copy.deepcopy(output())
    broken["risks"][0]["evidence_refs"] = ["not-in-catalog"]
    with pytest.raises(ValueError, match="outside the catalog"):
        build_output(cycle_id="audit-cycle", stage="m1_judgment", attempt_id="attempt-1", packet=packet(), output=broken)


@pytest.mark.parametrize("payload", [
    {"chain_of_thought": "private"},
    {"nested": {"scratchpad": ["private"]}},
    {"text": "This is a chain of thought that must not be recorded."},
])
def test_private_reasoning_is_rejected(payload):
    broken = record()
    if "text" in payload:
        broken["judgment"]["text"] = payload["text"]
        broken["judgment"]["sha256"] = hashlib.sha256(payload["text"].encode("utf-8")).hexdigest()
    else:
        broken["provenance"]["private_reasoning"] = payload
    broken["sha256"] = sha256({key: value for key, value in broken.items() if key != "sha256"})
    with pytest.raises(ValueError, match="private reasoning"):
        validate_output(broken)


def test_non_executing_writer_and_identity_mismatch_are_rejected():
    value = record()
    broken = copy.deepcopy(value)
    broken["writer_identity"]["source"] = "evaluation"
    broken["sha256"] = sha256({key: item for key, item in broken.items() if key != "sha256"})
    with pytest.raises(ValueError, match="Runtime-owned|writer"):
        validate_output(broken)
    with pytest.raises(PermissionError, match="writer identity"):
        validate_output(value, expected_writer_identity={
            **value["writer_identity"], "attempt_id": "another-attempt",
        })


def test_runtime_storage_is_immutable_and_evaluator_can_only_reference(tmp_path):
    store = CompanionStore(tmp_path / "audit.sqlite3")
    cycle = store.create_cycle("manual.non_trading_outlook", AS_OF, AS_OF)
    value = record()
    value["cycle_id"] = cycle["cycle_id"]
    value["writer_identity"] = expected_writer_identity(
        cycle_id=cycle["cycle_id"], stage="m1_judgment", attempt_id="attempt-1", component="companion-engine",
    )
    value["provenance"]["cycle_id"] = cycle["cycle_id"]
    value["sha256"] = sha256({key: item for key, item in value.items() if key != "sha256"})
    stored = store.append_audit_record(
        value, writer_identity=value["writer_identity"],
    )
    assert stored["record_id"] == value["sha256"]
    assert store.reference_audit_record(value["sha256"])["record"] == value
    with store.connection() as connection:
        with pytest.raises(Exception, match="immutable"):
            connection.execute("UPDATE audit_record SET writer_source='evaluation' WHERE record_id=?", (value["sha256"],))
        with pytest.raises(Exception, match="immutable"):
            connection.execute("DELETE FROM audit_record WHERE record_id=?", (value["sha256"],))


def test_frozen_replay_and_installation_qualification_are_deterministic():
    value = record()
    first = frozen_replay(value)
    second = frozen_replay(copy.deepcopy(value))
    assert first == second
    assert validate_replay(first) == first
    assert install_qualification() == install_qualification()
    tampered = copy.deepcopy(first)
    tampered["source_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="digest"):
        validate_replay(tampered)
