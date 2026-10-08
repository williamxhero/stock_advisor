from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from ai_trading_companion import regression_spec
from ai_trading_companion.regression_gate import (
    AXES,
    CASE_IDS,
    CONTRACT,
    FROZEN_CASES,
    REGISTRY_CONTRACT,
    REGISTRY_SHA256,
    AggregateScoreError,
    RegressionGateError,
    case_registry,
    evaluate_case,
    frozen_replay,
    get_case,
    install_qualification,
    run_regression_gate,
    runtime_qualification,
    sha256,
    validate_case,
    validate_registry,
    validate_replay,
    validate_verdict,
)


ROOT = Path(__file__).resolve().parents[2]


def test_registry_is_versioned_and_covers_all_required_boundaries():
    registry = validate_registry()
    assert registry["contract"] == REGISTRY_CONTRACT
    assert registry["spec_contract"] == CONTRACT
    assert registry["spec_version"] == 1
    assert [case["case_id"] for case in registry["cases"]] == list(CASE_IDS)
    assert {case["category"] for case in registry["cases"]} >= {
        "isolation", "stage_boundary", "temporal_integrity", "evidence_quality",
        "message_boundary", "adapter", "skill", "memoryhub", "markethub",
    }


def test_registry_digest_and_case_digests_are_stable():
    first = validate_registry()
    second = validate_registry()
    assert first == second
    assert first["sha256"] == REGISTRY_SHA256
    assert all(len(case["sha256"]) == 64 for case in first["cases"])


def test_case_registry_returns_defensive_copies():
    exposed = case_registry()
    exposed[0]["fixture"]["boundary"]["fact_owner"] = "provider"
    assert case_registry()[0]["fixture"]["boundary"]["fact_owner"] == "runtime"


def test_registry_rejects_case_digest_tampering():
    broken = validate_registry()
    broken["cases"][0]["title"] = "rewritten"
    with pytest.raises(RegressionGateError, match="case digest"):
        validate_registry(broken)


def test_registry_rejects_duplicate_case_ids():
    broken = validate_registry()
    broken["cases"].append(copy.deepcopy(broken["cases"][0]))
    broken["sha256"] = sha256({key: value for key, value in broken.items() if key != "sha256"})
    with pytest.raises(RegressionGateError, match="unique"):
        validate_registry(broken)


@pytest.mark.parametrize("case", FROZEN_CASES, ids=lambda case: case.case_id)
def test_each_frozen_case_requires_all_independent_axes(case):
    result = evaluate_case(case)
    assert set(result["axes"]) == set(AXES)
    assert result["non_regression"]["passed"] is True
    assert all(result["axes"][axis]["passed"] for axis in AXES)
    assert all(result["axes"][axis]["checks"] for axis in AXES)


def test_default_gate_qualifies_without_provider_or_external_dependency():
    verdict = run_regression_gate()
    assert verdict["passed"] is True
    assert verdict["failure_cases"] == []
    assert [item["case_id"] for item in verdict["case_results"]] == list(CASE_IDS)


def test_gate_is_deterministic_across_runs():
    assert run_regression_gate() == run_regression_gate()


def test_gate_preserves_per_case_safety_and_quality_evidence():
    verdict = run_regression_gate()
    for result in verdict["case_results"]:
        assert result["axes"]["safety"]["checks"]
        assert result["axes"]["quality"]["checks"]
        assert result["non_regression"] == {"passed": True, "reasons": []}


def test_deliberately_degraded_safety_case_fails_gate():
    verdict = run_regression_gate({"m1_blind": {"safety": {"passed": False}}})
    assert verdict["passed"] is False
    result = verdict["case_results"][0]
    assert result["case_id"] == "m1_blind"
    assert "m1_blind" in verdict["failure_cases"]
    assert "safety_regressed_from_qualified_baseline" in result["non_regression"]["reasons"]


def test_deliberately_degraded_quality_case_fails_gate():
    verdict = run_regression_gate({"m0_directionless": {"quality": {"checks": {"m0_retains_unknowns": False}}}})
    assert verdict["passed"] is False
    result = next(item for item in verdict["case_results"] if item["case_id"] == "m0_directionless")
    assert result["axes"]["quality"]["passed"] is False
    assert "failed_assertion:m0_retains_unknowns" in result["axes"]["quality"]["reasons"]


def test_deliberately_degraded_recovery_case_fails_gate():
    verdict = run_regression_gate({"adapter_failure_recovery": {"recovery": False}})
    assert verdict["passed"] is False
    result = next(item for item in verdict["case_results"] if item["case_id"] == "adapter_failure_recovery")
    assert result["axes"]["recovery"]["passed"] is False


def test_explicit_missing_candidate_observation_fails_instead_of_defaulting_to_success():
    candidate = {"cases": {"m1_blind": {"safety": True, "quality": True, "recovery": True}}}
    verdict = run_regression_gate(candidate)
    assert verdict["passed"] is False
    assert set(verdict["failure_cases"]) == set(CASE_IDS) - {"m1_blind"}


def test_baseline_degradation_is_not_a_valid_non_regression_reference():
    baseline = {"m1_blind": {"safety": False}}
    verdict = run_regression_gate(baseline=baseline)
    result = verdict["case_results"][0]
    assert verdict["passed"] is False
    assert "baseline_case_not_qualified" in result["non_regression"]["reasons"]


def test_aggregate_score_is_rejected_from_verdict():
    broken = run_regression_gate()
    broken["aggregate_score"] = 1.0
    with pytest.raises(AggregateScoreError, match="aggregate score"):
        validate_verdict(broken)


def test_nested_aggregate_score_is_rejected_from_verdict():
    broken = run_regression_gate()
    broken["case_results"][0]["axes"]["quality"]["scores"] = {"quality": 1}
    with pytest.raises(AggregateScoreError, match="aggregate score"):
        validate_verdict(broken)


def test_verdict_digest_and_vector_cannot_hide_case_degradation():
    broken = run_regression_gate()
    broken["case_results"][0]["axes"]["safety"]["passed"] = False
    broken["sha256"] = sha256({key: value for key, value in broken.items() if key != "sha256"})
    with pytest.raises(RegressionGateError, match="case result digest|evaluation vector"):
        validate_verdict(broken)


def test_frozen_replay_is_deterministic_and_non_mutating():
    verdict = run_regression_gate()
    original = copy.deepcopy(verdict)
    first = frozen_replay(verdict)
    second = frozen_replay(copy.deepcopy(verdict))
    assert first == second
    assert verdict == original
    assert validate_replay(first) == first
    assert first["qualification"]["per_case_axes"] is True


def test_replay_rejects_source_digest_tampering():
    broken = frozen_replay()
    broken["source_sha256"] = "0" * 64
    with pytest.raises(RegressionGateError, match="source digest"):
        validate_replay(broken)


def test_replay_rejects_aggregate_score_even_if_digest_is_recomputed():
    broken = frozen_replay()
    broken["source"]["verdict"]["evaluation_vector"]["aggregate_score"] = 1
    broken["source_sha256"] = sha256(broken["source"])
    broken["sha256"] = sha256({key: value for key, value in broken.items() if key != "sha256"})
    with pytest.raises(AggregateScoreError):
        validate_replay(broken)


def test_install_qualification_is_deterministic_and_versioned():
    first = install_qualification()
    second = install_qualification()
    assert first == second
    assert first["qualified"] is True
    assert first["contract"] == "RegressionSpecInstallQualification/v1"
    assert first["registry_sha256"] == REGISTRY_SHA256
    assert len(first["verdict_sha256"]) == 64 and len(first["replay_sha256"]) == 64


def test_runtime_qualification_is_fail_closed_and_read_only():
    qualification = runtime_qualification()
    assert qualification["qualified"] is True
    assert qualification["provenance"]["source"] == "runtime-regression-gate"
    for result in qualification["case_results"]:
        assert result["provenance"]["source"] == "runtime-regression-gate"


def test_m1_fixture_keeps_h0_out_of_input_and_quantresearch_read_only():
    case = get_case("m1_blind")
    assert case.fixture["m1_input"]["h0"] is None
    assert case.fixture["m1_input"]["h0_derived"] is None
    assert case.fixture["boundary"]["quantresearch"] == {"access": "read_only", "write_permissions": []}


def test_m0_fixture_is_directionless_and_append_only():
    case = get_case("m0_directionless")
    assert case.fixture["m0_output"]["direction"] is None
    assert case.fixture["m0_output"]["action"] is None
    assert case.fixture["boundary"]["stages"]["m0"] == "observation"


def test_temporal_fixture_is_bounded_by_cutoff():
    case = get_case("time_travel")
    cutoff = case.fixture["cutoff"]
    assert all(row["as_of"] <= cutoff and row["known_at"] <= cutoff for row in case.fixture["facts"])


def test_missing_and_conflicting_fixture_preserves_unknowns():
    case = get_case("missing_conflicting_data")
    assert case.fixture["evidence"]["critical_gaps"]
    assert case.fixture["evidence"]["conflicts"][0]["resolution"] == "unresolved_equal_tier"


def test_message_fixture_requires_correction_reference_without_rewriting():
    case = get_case("message_immutability")
    assert case.fixture["original"]["state"] == "published"
    assert case.fixture["original"]["message_id"] in case.fixture["correction"]["references"]


@pytest.mark.parametrize("case_id", ["adapter_failure_recovery", "skill_failure_recovery", "memoryhub_failure_recovery", "markethub_failure_recovery"])
def test_dependency_failure_cases_have_failed_and_recovered_attempts(case_id):
    attempts = get_case(case_id).fixture["attempts"]
    assert attempts[0]["state"] in {"failed", "unavailable"}
    assert attempts[1]["state"] == "recovered"


def test_schema_accepts_registry_verdict_replay_and_install_outputs():
    schema = json.loads((ROOT / "resources/contracts/regression-spec-v1.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    values = [validate_registry(), run_regression_gate(), frozen_replay(), install_qualification()]
    for value in values:
        assert list(validator.iter_errors(value)) == [], value


def delayed_probe_success(data):
    import time
    # Recovery qualification must tolerate startup contention within the
    # production budget; dedicated adapter timeout tests still own latency.
    time.sleep(1.1)
    return {"value": int(data["value"]) + 1}


@pytest.mark.slow
def test_adapter_recovery_probe_tolerates_fixture_startup_contention(monkeypatch):
    from ai_trading_companion import regression_probes

    monkeypatch.setattr(regression_probes, "_probe_execute_success", delayed_probe_success)
    result = regression_probes.run_probes(get_case("adapter_failure_recovery"))
    assert all(result["axes"][axis]["passed"] for axis in ("safety", "quality", "recovery")), result


def test_compatibility_spec_module_exposes_gate_contract():
    assert regression_spec.CONTRACT == CONTRACT
    assert regression_spec.run_regression_gate() == run_regression_gate()
