from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from ai_trading_companion.reflection import (
    CONTRACT,
    build_input,
    build_output,
    from_outcome,
    frozen_replay,
    install_qualification,
    validate_output,
)


def checkpoint() -> dict[str, object]:
    return {
        "cycle_id": "cycle-reflection", "checkpoint_id": "checkpoint-1",
        "snapshot_id": "snapshot-1", "snapshot_json": {"direction": "bullish", "original_claims": ["claim"]},
        "judgment_as_of": "2026-09-20T01:00:00Z", "judgment_text": "判断原文", "horizon": "T+1",
    }


def outcome(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {"as_of": "2026-09-21T01:00:00Z", "verification_status": "incorrect", "summary": "结果偏离"}
    value.update(changes)
    return value


def test_reflection_binds_immutable_snapshot_and_outcome_and_schema() -> None:
    input_contract = build_input(checkpoint(), outcome())
    output = from_outcome(checkpoint(), outcome())
    root = Path(__file__).parents[2]
    schema = json.loads((root / "resources/contracts/reflection-spec-v1.schema.json").read_text(encoding="utf-8"))
    assert output["contract"] == CONTRACT
    assert output["diagnosis"] == "random_outcome"
    assert output["lesson_candidate"] is None
    assert list(Draft202012Validator(schema).iter_errors(output)) == []
    replay = frozen_replay(input_contract, output)
    assert replay["qualification"]["valid"] is True
    assert replay["evaluation_vector"]["safety_reliability"]["snapshot_immutable"] is True


def test_bad_outcome_does_not_become_reasoning_error_without_diagnostic_evidence() -> None:
    output = from_outcome(checkpoint(), outcome(diagnosis="reasoning_error"))
    assert output["diagnosis"] == "inconclusive"
    assert output["diagnostic_evidence_refs"] == []
    assert output["lesson_candidate"] is None


def test_supported_error_requires_evidence_and_can_create_lesson_candidate() -> None:
    result = outcome(
        diagnosis="timing_error", diagnostic_reason="触发条件在窗口结束后才满足。",
        diagnostic_evidence_refs=["evidence:timing-1"],
        lesson_candidate={"title": "保留时间窗口", "hypothesis": "窗口结束后不把迟到信号归入原判断", "evidence_refs": ["evidence:timing-1"]},
    )
    output = from_outcome(checkpoint(), result)
    assert output["diagnosis"] == "timing_error"
    assert output["lesson_candidate"]["state"] == "candidate"
    validate_output(output)

    with pytest.raises(ValueError, match="independent evidence"):
        build_output(build_input(checkpoint(), outcome()), diagnosis="evidence_error", reason="没有证据", evidence_refs=[])


def test_reflection_replay_and_install_qualification_are_deterministic() -> None:
    input_contract = build_input(checkpoint(), outcome())
    output = from_outcome(checkpoint(), outcome())
    assert frozen_replay(input_contract, output) == frozen_replay(copy.deepcopy(input_contract), copy.deepcopy(output))
    qualification = install_qualification()
    assert qualification["contract"] == "ReflectionInstallQualification/v1"
    assert qualification["qualified"] is True
