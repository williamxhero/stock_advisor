from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from ai_trading_companion.agent_contract import attach_input
from ai_trading_companion.agent_role import attach_role_inputs
from ai_trading_companion.debate import (
    CONTRACT,
    build_input,
    build_output,
    failure,
    from_stage,
    frozen_replay,
    install_qualification,
    validate_output,
)


PACKET = {
    "cycle_id": "cycle-debate-1",
    "stage": "m1_research",
    "as_of": "2026-09-21T01:00:00Z",
    "evidence_snapshot": {"snapshot_id": "snapshot-debate-1", "as_of": "2026-09-21T01:00:00Z"},
    "memories": [],
    "sha256": "packet-debate-1",
}


def _packet() -> dict:
    value = {**PACKET}
    value["agent_contract"] = attach_input(value, capability="research:m1_research")["agent_contract"]
    return attach_role_inputs(value, stage="m1_research")


def test_debate_requires_claim_and_evidence_for_every_argument() -> None:
    packet = _packet()
    output = from_stage(
        packet,
        {"propositions": [{"id": "claim-1", "text": "承接改善", "evidence_refs": ["ev-1"]}]},
        {"passed": True}, attempt_id="attempt-1",
    )
    assert output["contract"] == CONTRACT
    assert output["arguments"][0]["claim_id"] == "claim-1"
    assert output["arguments"][0]["evidence_ids"] == ["ev-1"]
    assert output["coordination"]["state"] == "ready"

    insufficient = from_stage(packet, {"propositions": [{"id": "claim-no-evidence", "text": "未知"}]}, {"passed": True}, attempt_id="attempt-2")
    assert insufficient["status"] == "evidence_insufficient"
    assert insufficient["coordination"]["issues"][0]["state"] == "evidence_insufficient"


def test_counterargument_must_target_a_specific_claim_and_conflict_is_structured() -> None:
    packet = _packet()
    output = from_stage(
        packet,
        {
            "propositions": [{"id": "claim-1", "text": "承接改善", "evidence_refs": ["ev-1"]}],
            "counterevidence": [{"target_claim_id": "claim-1", "text": "量价不一致", "evidence_refs": ["ev-2"], "status": "refutes"}],
            "conflicts": [{"claim_id": "claim-1", "competing_evidence_refs": ["ev-1", "ev-2"], "materiality": "high"}],
        },
        {"passed": True}, attempt_id="attempt-3",
    )
    assert output["status"] == "conflict_unresolved"
    assert output["counterarguments"][0]["target_claim_id"] == "claim-1"
    assert output["conflicts"][0]["state"] == "unresolved"
    with pytest.raises(ValueError, match="known claim_id"):
        invalid = copy.deepcopy(output)
        invalid["counterarguments"][0]["target_claim_id"] = "claim-missing"
        validate_output(invalid)


def test_validation_matches_schema_for_unknown_and_unstructured_fields() -> None:
    packet = _packet()
    output = from_stage(
        packet,
        {"propositions": [{"id": "claim-1", "text": "承接改善", "evidence_refs": ["ev-1"]}]},
        {"passed": True}, attempt_id="attempt-strict",
    )
    unknown = copy.deepcopy(output)
    unknown["unexpected"] = True
    with pytest.raises(ValueError, match="unsupported fields"):
        validate_output(unknown)

    malformed = copy.deepcopy(output)
    malformed["arguments"][0]["role"] = 42
    with pytest.raises(ValueError, match="argument.role"):
        validate_output(malformed)

    malformed = copy.deepcopy(output)
    malformed["coordination"]["issues"] = ["not-structured"]
    with pytest.raises(ValueError, match="coordination.issues"):
        validate_output(malformed)

    malformed = copy.deepcopy(output)
    malformed["provenance"]["input_sha256"] = None
    with pytest.raises(ValueError, match="input provenance"):
        validate_output(malformed)


def test_timeout_and_failure_are_coordinator_states_without_write_permissions() -> None:
    packet = _packet()
    timed_out = failure(packet, stage="m1_research", status="timed_out", attempt_id="attempt-timeout", reason="deadline")
    assert timed_out["coordination"]["state"] == "timed_out"
    assert timed_out["permissions"]["write_permissions"] == []
    assert timed_out["visibility"] == "internal"


def test_frozen_replay_preserves_sources_and_evaluation_axes() -> None:
    packet = _packet()
    output = from_stage(packet, {"propositions": [{"id": "claim-1", "text": "承接改善", "evidence_refs": ["ev-1"]}]}, {"passed": True}, attempt_id="attempt-replay")
    source_input = build_input(packet, stage="m1_research")
    first = frozen_replay(source_input, output)
    second = frozen_replay(copy.deepcopy(source_input), copy.deepcopy(output))
    assert first == second
    assert set(first["evaluation_vector"]) == {"delivery_speed", "qualification_probability", "research_quality", "judgment_outcome", "safety_reliability"}
    assert output["provenance"]["source_output_sha256"]


def test_schema_install_contract_and_source_independent_replay() -> None:
    root = Path(__file__).parents[2]
    schema = json.loads((root / "resources/contracts/debate-spec-v1.schema.json").read_text(encoding="utf-8"))
    input_schema = json.loads((root / "resources/contracts/debate-input-v1.schema.json").read_text(encoding="utf-8"))
    packet = _packet()
    source_input = build_input(packet, stage="m1_research")
    output = from_stage(packet, {"propositions": [{"id": "claim-1", "text": "fixture", "evidence_refs": ["ev-1"]}]}, {"passed": True}, attempt_id="install")
    assert not list(Draft202012Validator(input_schema).iter_errors(source_input))
    assert not list(Draft202012Validator(schema).iter_errors(output))
    qualification = install_qualification()
    assert qualification["qualified"] is True
    assert set(qualification["evaluation_vector"]) == {"delivery_speed", "qualification_probability", "research_quality", "judgment_outcome", "safety_reliability"}
    script = (root / "scripts/verify-install.ps1").read_text(encoding="utf-8")
    assert "resources\\contracts\\debate-spec-v1.schema.json" in script
    assert "runtime\\ai_trading_companion\\debate.py" in script
