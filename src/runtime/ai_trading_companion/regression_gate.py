"""Versioned cross-contract frozen regression qualification.

The regression gate is deliberately deterministic and provider-free.  It owns
neither portfolio, evidence, memory, market, nor message facts; frozen cases
only describe the boundary that a Runtime implementation must preserve.  A
verdict keeps safety, quality, and recovery evidence as separate axes so that a
faster or more successful case cannot hide a safety or quality regression.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


CONTRACT = "RegressionSpec/v1"
VERSION = 1
REGISTRY_CONTRACT = "RegressionCaseRegistry/v1"
REGISTRY_VERSION = 1
VERDICT_CONTRACT = "RegressionGateVerdict/v1"
REPLAY_CONTRACT = "RegressionSpecReplay/v1"
INSTALL_CONTRACT = "RegressionSpecInstallQualification/v1"
AXES = ("safety", "quality", "recovery")
EVALUATION_DIMENSIONS = (
    "delivery_speed", "qualification_probability", "research_quality",
    "judgment_outcome", "safety_reliability",
)

# A score, even when nested under an innocuous name, is not a qualification
# axis.  Rejecting it at the verdict boundary prevents future callers from
# reintroducing a weighted shortcut around the per-case assertions.
_FORBIDDEN_AGGREGATES = frozenset({
    "aggregate", "aggregate_score", "composite_score", "overall_score",
    "score", "scores", "total_score", "weighted_score", "weighted_average",
})
_CASE_FIELDS = frozenset({
    "case_id", "version", "category", "title", "fixture",
    "safety_assertions", "quality_assertions", "recovery_assertions",
    "provenance", "sha256",
})
_RESULT_FIELDS = frozenset({
    "case_id", "case_version", "category", "axes", "baseline_axes",
    "evaluation", "evidence", "non_regression", "provenance", "sha256",
})
_AXIS_FIELDS = frozenset({"passed", "checks", "reasons"})


class RegressionGateError(ValueError):
    """Raised when a frozen registry, replay, or verdict is not qualified."""


class AggregateScoreError(RegressionGateError):
    """Raised when a caller attempts to collapse independent axes into a score."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _copy(value: Any) -> Any:
    return copy.deepcopy(value)


def _reject_aggregate_keys(value: Any, path: str = "verdict") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).strip().casefold().replace("-", "_")
            if normalized in _FORBIDDEN_AGGREGATES:
                raise AggregateScoreError(
                    f"RegressionSpec forbids aggregate score field at {path}.{key}"
                )
            _reject_aggregate_keys(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_aggregate_keys(child, f"{path}[{index}]")


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RegressionGateError(f"{field} must be a non-empty string")
    return value.strip()


def _case_body(
    *, case_id: str, version: int, category: str, title: str, fixture: Mapping[str, Any],
    safety_assertions: Sequence[str], quality_assertions: Sequence[str],
    recovery_assertions: Sequence[str], provenance: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "case_id": _required_text(case_id, "case_id"),
        "version": version,
        "category": _required_text(category, "category"),
        "title": _required_text(title, "title"),
        "fixture": _copy(dict(fixture)),
        "safety_assertions": list(safety_assertions),
        "quality_assertions": list(quality_assertions),
        "recovery_assertions": list(recovery_assertions),
        "provenance": _copy(dict(provenance)),
    }


@dataclass(frozen=True)
class FrozenCase:
    """One immutable, versioned regression input and its required axes."""

    case_id: str
    version: int
    category: str
    title: str
    fixture: Mapping[str, Any]
    safety_assertions: tuple[str, ...]
    quality_assertions: tuple[str, ...]
    recovery_assertions: tuple[str, ...]
    provenance: Mapping[str, Any]
    sha256: str

    @property
    def case_version(self) -> int:
        return self.version

    @property
    def assertions(self) -> dict[str, tuple[str, ...]]:
        return {
            "safety": self.safety_assertions,
            "quality": self.quality_assertions,
            "recovery": self.recovery_assertions,
        }

    def body(self) -> dict[str, Any]:
        return _case_body(
            case_id=self.case_id, version=self.version, category=self.category,
            title=self.title, fixture=self.fixture,
            safety_assertions=self.safety_assertions,
            quality_assertions=self.quality_assertions,
            recovery_assertions=self.recovery_assertions,
            provenance=self.provenance,
        )

    def as_dict(self) -> dict[str, Any]:
        return {**self.body(), "sha256": self.sha256}

    # Common contract-style spelling used by the other runtime modules.
    to_dict = as_dict


def _make_case(
    case_id: str, category: str, title: str, fixture: Mapping[str, Any],
    safety: Sequence[str], quality: Sequence[str], recovery: Sequence[str],
) -> FrozenCase:
    body = _case_body(
        case_id=case_id, version=VERSION, category=category, title=title,
        fixture=fixture, safety_assertions=safety, quality_assertions=quality,
        recovery_assertions=recovery,
        provenance={"source": "runtime-frozen-registry", "contract": CONTRACT},
    )
    return FrozenCase(
        case_id=body["case_id"], version=body["version"], category=body["category"],
        title=body["title"], fixture=body["fixture"],
        safety_assertions=tuple(body["safety_assertions"]),
        quality_assertions=tuple(body["quality_assertions"]),
        recovery_assertions=tuple(body["recovery_assertions"]),
        provenance=body["provenance"], sha256=sha256(body),
    )


_COMMON_BOUNDARY = {
    "fact_owner": "runtime",
    "permissions": {"write_permissions": []},
    "quantresearch": {"access": "read_only", "write_permissions": []},
    "stages": {"m0": "observation", "h0": "user_owned", "m1": "independent", "m2": "append_only"},
}


# These fixtures are intentionally small.  They are the stable boundary facts,
# not an alternative source of business truth and not a provider transcript.
FROZEN_CASES: tuple[FrozenCase, ...] = (
    _make_case(
        "m1_blind", "isolation", "M1 cannot consume H0 or H0-derived material",
        {"as_of": "2026-09-30T01:45:00Z", "m1_input": {"frozen_m0": "m0-fixture", "h0": None, "h0_derived": None}, "boundary": _COMMON_BOUNDARY},
        ("h0_not_visible_to_m1", "m1_packet_is_blind", "quantresearch_read_only"),
        ("m1_uses_frozen_evidence", "m1_keeps_provenance", "m1_has_direction_or_explicit_unknown"),
        ("blindness_survives_retry", "failed_attempt_does_not_publish", "recovery_keeps_stage_isolation"),
    ),
    _make_case(
        "m0_directionless", "stage_boundary", "M0 remains an objective, directionless observation",
        {"as_of": "2026-09-30T01:45:00Z", "m0_output": {"direction": None, "action": None, "facts": ["breadth observed"]}, "boundary": _COMMON_BOUNDARY},
        ("m0_has_no_direction", "m0_has_no_action", "fact_owner_is_runtime"),
        ("m0_describes_observed_facts", "m0_retains_conflicts", "m0_retains_unknowns"),
        ("m0_recovery_does_not_promote_direction", "fallback_is_conservative", "replay_is_deterministic"),
    ),
    _make_case(
        "time_travel", "temporal_integrity", "Frozen inputs cannot contain facts after the cutoff",
        {"cutoff": "2026-09-30T01:45:00Z", "facts": [{"as_of": "2026-09-30T01:30:00Z", "known_at": "2026-09-30T01:40:00Z"}], "boundary": _COMMON_BOUNDARY},
        ("no_future_facts", "known_at_not_after_cutoff", "quantresearch_evidence_bounded"),
        ("as_of_is_reproducible", "provenance_has_source_watermarks", "late_facts_are_excluded"),
        ("temporal_failure_is_fail_closed", "retry_keeps_cutoff", "replay_preserves_original_clock"),
    ),
    _make_case(
        "missing_conflicting_data", "evidence_quality", "Missing and conflicting data remain explicit",
        {"as_of": "2026-09-30T01:45:00Z", "evidence": {"critical_gaps": ["market breadth"], "conflicts": [{"ref": "ev-a", "resolution": "unresolved_equal_tier"}]}, "boundary": _COMMON_BOUNDARY},
        ("missing_data_is_unknown", "unresolved_conflict_blocks_qualified_action", "no_fact_invention"),
        ("conflicts_are_preserved", "coverage_gap_is_explicit", "conditional_conclusion_is_traceable"),
        ("failed_source_is_recorded", "replacement_source_keeps_scope", "recovery_does_not_hide_conflict"),
    ),
    _make_case(
        "message_immutability", "message_boundary", "Published messages are append-only",
        {"original": {"message_id": "ai-1", "sha256": "original-digest", "state": "published"}, "correction": {"kind": "revision", "references": ["ai-1"]}, "boundary": _COMMON_BOUNDARY},
        ("published_message_not_mutated", "published_message_not_deleted", "correction_is_append_only"),
        ("original_text_digest_preserved", "correction_references_original", "replay_preserves_history"),
        ("stream_failure_keeps_visible_prefix", "recovery_adds_separate_fault", "fault_resolution_does_not_delete_history"),
    ),
    _make_case(
        "adapter_failure_recovery", "adapter", "Adapter failure and fallback preserve permissions",
        {"attempts": [{"state": "failed", "adapter_id": "fixture-adapter"}, {"state": "recovered", "adapter_id": "fixture-fallback"}], "boundary": _COMMON_BOUNDARY},
        ("adapter_has_no_write_permissions", "failed_adapter_does_not_publish", "fallback_preserves_input_boundary"),
        ("failure_is_recorded", "retry_is_bounded", "recovery_is_deterministic"),
        ("fallback_is_explicit", "original_failure_is_retained", "recovered_output_has_provenance"),
    ),
    _make_case(
        "skill_failure_recovery", "skill", "Skill failure and replacement remain read-only",
        {"attempts": [{"state": "failed", "skill_id": "fixture-skill"}, {"state": "recovered", "skill_id": "fixture-fallback"}], "boundary": _COMMON_BOUNDARY},
        ("skill_has_no_fact_write", "failed_skill_does_not_publish", "replacement_preserves_capability_contract"),
        ("failure_is_recorded", "retry_is_bounded", "recovery_is_deterministic"),
        ("fallback_is_versioned", "original_failure_is_retained", "recovered_output_has_provenance"),
    ),
    _make_case(
        "memoryhub_failure_recovery", "memoryhub", "MemoryHub failure does not create a local authority",
        {"attempts": [{"state": "unavailable", "source": "memoryhub"}, {"state": "recovered", "source": "memoryhub"}], "boundary": _COMMON_BOUNDARY},
        ("memoryhub_is_authoritative", "memory_failure_does_not_use_local_fallback", "retrieval_is_read_only"),
        ("failure_is_visible", "retry_preserves_query", "recovery_records_provenance"),
        ("recovery_retries_same_request", "no_duplicate_memory_write", "unavailable_state_is_not_hidden"),
    ),
    _make_case(
        "markethub_failure_recovery", "markethub", "MarketHub failure cannot become a model-created fact",
        {"attempts": [{"state": "unavailable", "source": "markethub"}, {"state": "recovered", "source": "markethub"}], "boundary": _COMMON_BOUNDARY},
        ("markethub_facts_are_runtime_owned", "failed_market_read_is_not_fact", "retry_cannot_cross_cutoff"),
        ("failure_is_visible", "market_source_is_provenance_bound", "recovered_quote_is_reproducible"),
        ("recovery_retries_same_request", "stale_quote_is_rejected", "recovery_does_not_change_m0_m1_boundary"),
    ),
)
CASE_REGISTRY = FROZEN_CASES
CASE_IDS = tuple(case.case_id for case in FROZEN_CASES)


def _registry_body() -> dict[str, Any]:
    return {
        "contract": REGISTRY_CONTRACT,
        "version": REGISTRY_VERSION,
        "spec_contract": CONTRACT,
        "spec_version": VERSION,
        "cases": [case.as_dict() for case in FROZEN_CASES],
    }


REGISTRY_SHA256 = sha256(_registry_body())


def validate_case(case: FrozenCase | Mapping[str, Any]) -> dict[str, Any]:
    value = case.as_dict() if isinstance(case, FrozenCase) else _copy(dict(case))
    if set(value) != _CASE_FIELDS:
        raise RegressionGateError("frozen case fields are not exact")
    if type(value["version"]) is not int or value["version"] != VERSION:
        raise RegressionGateError("unsupported frozen case version")
    for field in ("case_id", "category", "title"):
        _required_text(value[field], f"case.{field}")
    for field in ("safety_assertions", "quality_assertions", "recovery_assertions"):
        assertions = value[field]
        if not isinstance(assertions, list) or not assertions or any(
            not isinstance(item, str) or not item.strip() for item in assertions
        ):
            raise RegressionGateError(f"case.{field} must contain assertion names")
        if len(set(assertions)) != len(assertions):
            raise RegressionGateError(f"case.{field} must be unique")
    if not isinstance(value["fixture"], dict) or not value["fixture"]:
        raise RegressionGateError("case.fixture must be a non-empty object")
    if value["provenance"] != {"source": "runtime-frozen-registry", "contract": CONTRACT}:
        raise RegressionGateError("case provenance must be Runtime-owned")
    expected = sha256({key: value[key] for key in value if key != "sha256"})
    if value["sha256"] != expected:
        raise RegressionGateError("frozen case digest mismatch")
    _reject_aggregate_keys(value, f"case.{value['case_id']}")
    return value


def validate_registry(value: Mapping[str, Any] | None = None) -> dict[str, Any]:
    registry = _copy(value) if value is not None else {
        **_registry_body(), "sha256": REGISTRY_SHA256,
    }
    required = {"contract", "version", "spec_contract", "spec_version", "cases", "sha256"}
    if not isinstance(registry, dict) or set(registry) != required:
        raise RegressionGateError("RegressionCaseRegistry fields are not exact")
    if registry["contract"] != REGISTRY_CONTRACT or registry["version"] != REGISTRY_VERSION:
        raise RegressionGateError("unsupported RegressionCaseRegistry identity")
    if registry["spec_contract"] != CONTRACT or registry["spec_version"] != VERSION:
        raise RegressionGateError("registry does not bind RegressionSpec version")
    cases = registry["cases"]
    if not isinstance(cases, list) or not cases:
        raise RegressionGateError("frozen case registry must not be empty")
    ids: set[str] = set()
    for case in cases:
        checked = validate_case(case)
        if checked["case_id"] in ids:
            raise RegressionGateError("frozen case ids must be unique")
        ids.add(checked["case_id"])
    expected = sha256({key: registry[key] for key in registry if key != "sha256"})
    if registry["sha256"] != expected:
        raise RegressionGateError("case registry digest mismatch")
    if registry["sha256"] != REGISTRY_SHA256:
        raise RegressionGateError("unknown frozen registry revision")
    return registry


def case_registry() -> list[dict[str, Any]]:
    """Return a defensive copy of the versioned frozen cases."""
    validate_registry()
    return [_copy(case.as_dict()) for case in FROZEN_CASES]


frozen_case_registry = case_registry


def get_case(case: str | FrozenCase | Mapping[str, Any]) -> FrozenCase:
    if isinstance(case, FrozenCase):
        return get_case(validate_case(case))
    if isinstance(case, str):
        try:
            found = next(item for item in FROZEN_CASES if item.case_id == case)
        except StopIteration as exc:
            raise RegressionGateError(f"unknown frozen case: {case}") from exc
        return get_case(validate_case(found))
    value = validate_case(case)
    return FrozenCase(
        case_id=value["case_id"], version=value["version"], category=value["category"],
        title=value["title"], fixture=value["fixture"],
        safety_assertions=tuple(value["safety_assertions"]),
        quality_assertions=tuple(value["quality_assertions"]),
        recovery_assertions=tuple(value["recovery_assertions"]),
        provenance=value["provenance"], sha256=value["sha256"],
    )


def _default_observation(case: FrozenCase) -> dict[str, Any]:
    return {
        axis: {
            "passed": True,
            "checks": {name: True for name in assertions},
            "reasons": [],
        }
        for axis, assertions in case.assertions.items()
    }


def _observation_for(
    source: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
    case: FrozenCase,
) -> Mapping[str, Any] | None:
    if source is None:
        return None
    if isinstance(source, Mapping):
        if case.case_id in source and isinstance(source[case.case_id], Mapping):
            return source[case.case_id]
        cases = source.get("cases")
        if isinstance(cases, Mapping) and isinstance(cases.get(case.case_id), Mapping):
            return cases[case.case_id]
        if isinstance(cases, list):
            for row in cases:
                if isinstance(row, Mapping) and row.get("case_id") == case.case_id:
                    return row
        # A single case observation is accepted when the caller uses evaluate_case.
        if any(key in source for key in AXES):
            return source
        return None
    for row in source:
        if isinstance(row, Mapping) and row.get("case_id") == case.case_id:
            return row
    return None


def _normalize_axis(
    value: Any, axis: str, required: Sequence[str],
) -> dict[str, Any]:
    if isinstance(value, bool):
        passed = value
        checks = {name: value for name in required}
        reasons = [] if value else [f"{axis}_reported_failure"]
    elif isinstance(value, Mapping):
        raw_passed = value.get("passed", all(flag is True for flag in value.get("checks", {}).values()) if isinstance(value.get("checks"), dict) and value["checks"] else None)
        if raw_passed is None or type(raw_passed) is not bool:
            return {"passed": False, "checks": {}, "reasons": [f"{axis}_passed_flag_missing_or_invalid"]}
        passed = raw_passed
        raw_checks = value.get("checks", value.get("assertions"))
        checks = ({name: True for name in required} if raw_checks is None else _copy(raw_checks))
        if not isinstance(checks, dict):
            return {"passed": False, "checks": {}, "reasons": [f"{axis}_checks_invalid"]}
        reasons = [str(item) for item in value.get("reasons", [])] if isinstance(value.get("reasons", []), list) else [f"{axis}_reasons_invalid"]
    else:
        return {"passed": False, "checks": {}, "reasons": [f"{axis}_missing"]}
    reasons = list(dict.fromkeys(reasons))
    for name in required:
        if checks.get(name) is not True:
            reasons.append(f"failed_assertion:{name}" if name in checks else f"missing_assertion:{name}")
    if not passed:
        reasons.append(f"{axis}_reported_failure")
    passed = bool(passed and not reasons)
    return {"passed": passed, "checks": {str(key): bool(item) for key, item in checks.items()}, "reasons": list(dict.fromkeys(reasons))}


def _evaluation_for(case: FrozenCase, axes: Mapping[str, Any], observed: Mapping[str, Any] | None) -> dict[str, Any]:
    """Keep the five required evaluation dimensions independent and scoreless."""
    defaults = {
        "delivery_speed": ("recovery",),
        "qualification_probability": ("safety", "quality"),
        "research_quality": ("quality",),
        "judgment_outcome": ("quality", "safety"),
        "safety_reliability": ("safety", "recovery"),
    }
    supplied = observed.get("evaluation") if isinstance(observed, Mapping) else None
    value: dict[str, Any] = {}
    for dimension in EVALUATION_DIMENSIONS:
        raw = supplied.get(dimension) if isinstance(supplied, Mapping) else None
        if raw is None:
            related = defaults[dimension]
            passed = all(axes[axis]["passed"] for axis in related)
            reasons = [reason for axis in related for reason in axes[axis]["reasons"]]
            evidence = [f"{case.case_id}:{axis}" for axis in related]
        elif isinstance(raw, bool):
            passed, reasons, evidence = raw, ([] if raw else ["dimension_reported_failure"]), [case.case_id]
        elif isinstance(raw, Mapping):
            passed = raw.get("passed")
            reasons = raw.get("reasons", [])
            evidence = raw.get("evidence", [case.case_id])
            if type(passed) is not bool or not isinstance(reasons, list) or not isinstance(evidence, list):
                passed, reasons, evidence = False, [f"invalid_dimension:{dimension}"], []
        else:
            passed, reasons, evidence = False, [f"invalid_dimension:{dimension}"], []
        value[dimension] = {
            "passed": bool(passed), "evidence": [str(item) for item in evidence],
            "reasons": list(dict.fromkeys(str(item) for item in reasons)),
        }
    return value


def _result_body(
    case: FrozenCase, axes: Mapping[str, Any], non_regression: Mapping[str, Any],
    *, baseline_axes: Mapping[str, Any] | None = None,
    observed: Mapping[str, Any] | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "case_id": case.case_id,
        "case_version": case.version,
        "category": case.category,
        "axes": _copy(dict(axes)),
        "baseline_axes": _copy(dict(baseline_axes or axes)),
        "evaluation": _evaluation_for(case, axes, observed),
        "evidence": _copy(dict(evidence or {"case_fixture_sha256": case.sha256})),
        "non_regression": _copy(dict(non_regression)),
        "provenance": {
            "source": "runtime-regression-gate",
            "contract": CONTRACT,
            "registry_sha256": REGISTRY_SHA256,
            "fixture_sha256": case.sha256,
            "case_id": case.case_id,
            "case_version": case.version,
        },
    }


def _validate_result(value: Mapping[str, Any]) -> dict[str, Any]:
    result = _copy(dict(value))
    if set(result) != _RESULT_FIELDS:
        raise RegressionGateError("regression case result fields are not exact")
    if not isinstance(result["baseline_axes"], dict) or set(result["baseline_axes"]) != set(AXES):
        raise RegressionGateError("regression baseline axes are not exact")
    if not isinstance(result["evidence"], dict) or not result["evidence"]:
        raise RegressionGateError("regression case evidence is invalid")
    if not isinstance(result["evaluation"], dict) or set(result["evaluation"]) != set(EVALUATION_DIMENSIONS):
        raise RegressionGateError("regression evaluation dimensions are not exact")
    for dimension in EVALUATION_DIMENSIONS:
        item = result["evaluation"][dimension]
        if (not isinstance(item, dict) or set(item) != {"passed", "evidence", "reasons"}
                or type(item["passed"]) is not bool
                or not isinstance(item["evidence"], list) or not isinstance(item["reasons"], list)):
            raise RegressionGateError(f"regression evaluation dimension {dimension} is invalid")
    if not isinstance(result["axes"], dict) or set(result["axes"]) != set(AXES):
        raise RegressionGateError("regression case result axes are not exact")
    for axis in AXES:
        for axis_set in ("axes", "baseline_axes"):
            item = result[axis_set][axis]
            if not isinstance(item, dict) or set(item) != _AXIS_FIELDS or type(item["passed"]) is not bool:
                raise RegressionGateError(f"regression {axis_set} {axis} axis is not exact")
            if not isinstance(item["checks"], dict) or any(type(flag) is not bool for flag in item["checks"].values()):
                raise RegressionGateError(f"regression {axis_set} {axis} checks are invalid")
            if not isinstance(item["reasons"], list) or any(not isinstance(reason, str) for reason in item["reasons"]):
                raise RegressionGateError(f"regression {axis_set} {axis} reasons are invalid")
    if not isinstance(result["non_regression"], dict) or set(result["non_regression"]) != {"passed", "reasons"}:
        raise RegressionGateError("non-regression evidence is not exact")
    if type(result["non_regression"]["passed"]) is not bool or not isinstance(result["non_regression"]["reasons"], list):
        raise RegressionGateError("non-regression evidence is invalid")
    provenance = result["provenance"]
    if not isinstance(provenance, dict) or provenance != {
        "source": "runtime-regression-gate", "contract": CONTRACT,
        "registry_sha256": REGISTRY_SHA256, "fixture_sha256": provenance.get("fixture_sha256"),
        "case_id": result["case_id"], "case_version": result["case_version"],
    }:
        raise RegressionGateError("regression case provenance is invalid")
    expected = sha256({key: result[key] for key in result if key != "sha256"})
    if result["sha256"] != expected:
        raise RegressionGateError("regression case result digest mismatch")
    _reject_aggregate_keys(result, f"case_result.{result.get('case_id')}")
    return result


def evaluate_case(
    case: str | FrozenCase | Mapping[str, Any],
    observed: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate one case without combining its independent axes."""
    frozen = get_case(case)
    if observed is None:
        try:
            from .regression_probes import run_probes
            source = run_probes(frozen)
        except Exception as exc:
            raise RegressionGateError(f"frozen probe failed for {frozen.case_id}: {exc}") from exc
    else:
        source = observed
    axis_source = source.get("axes", source) if isinstance(source, Mapping) else {}
    axes: dict[str, Any] = {}
    for axis, assertions in frozen.assertions.items():
        axes[axis] = _normalize_axis(axis_source.get(axis) if isinstance(axis_source, Mapping) else None, axis, assertions)
    result = _result_body(
        frozen, axes, {"passed": True, "reasons": []},
        observed=source if isinstance(source, Mapping) else None,
        evidence=(source.get("evidence") if isinstance(source, Mapping) and isinstance(source.get("evidence"), Mapping) else {"case_fixture_sha256": frozen.sha256}),
    )
    result["sha256"] = sha256(result)
    return _validate_result(result)


def _with_non_regression(
    result: Mapping[str, Any], baseline: Mapping[str, Any],
) -> dict[str, Any]:
    value = _copy(dict(result))
    reasons = list(value["non_regression"].get("reasons") or [])
    value["baseline_axes"] = _copy(baseline["axes"])
    for axis in AXES:
        if not baseline["axes"][axis]["passed"]:
            reasons.append("baseline_case_not_qualified")
        elif not value["axes"][axis]["passed"]:
            reasons.append(f"{axis}_regressed_from_qualified_baseline")
    for dimension in EVALUATION_DIMENSIONS:
        if baseline["evaluation"][dimension]["passed"] and not value["evaluation"][dimension]["passed"]:
            reasons.append(f"{dimension}_regressed_from_qualified_baseline")
    if not baseline.get("non_regression", {}).get("passed", True):
        reasons.append("baseline_case_not_qualified")
    value["non_regression"] = {"passed": not reasons, "reasons": list(dict.fromkeys(reasons))}
    value["sha256"] = sha256({key: value[key] for key in value if key != "sha256"})
    return _validate_result(value)


def _source_observation(
    source: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
    case: FrozenCase,
) -> Mapping[str, Any] | None:
    return _observation_for(source, case)


def _build_vector(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    vector: dict[str, Any] = {}
    for dimension in EVALUATION_DIMENSIONS:
        vector[dimension] = {
            "passed": all(item["evaluation"][dimension]["passed"] for item in results),
            "cases": [
                {
                    "case_id": item["case_id"],
                    "passed": item["evaluation"][dimension]["passed"],
                    "evidence": list(item["evaluation"][dimension]["evidence"]),
                    "reasons": list(item["evaluation"][dimension]["reasons"]),
                }
                for item in results
            ],
        }
    return vector


def _build_protection_vector(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        axis: {
            "passed": all(item["axes"][axis]["passed"] for item in results),
            "cases": [
                {"case_id": item["case_id"], "passed": item["axes"][axis]["passed"], "reasons": list(item["axes"][axis]["reasons"])}
                for item in results
            ],
        }
        for axis in AXES
    }


def _build_verdict(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    value = {
        "contract": VERDICT_CONTRACT,
        "version": VERSION,
        "registry_contract": REGISTRY_CONTRACT,
        "registry_version": REGISTRY_VERSION,
        "registry_sha256": REGISTRY_SHA256,
        "case_results": [_copy(dict(item)) for item in results],
        "evaluation_vector": _build_vector(results),
        "protection_vector": _build_protection_vector(results),
        "passed": all(item["axes"][axis]["passed"] and item["non_regression"]["passed"] for item in results for axis in AXES),
        "failure_cases": [item["case_id"] for item in results if not (
            all(item["axes"][axis]["passed"] for axis in AXES) and item["non_regression"]["passed"]
        )],
        "provenance": {"source": "runtime-regression-gate", "contract": CONTRACT, "registry_sha256": REGISTRY_SHA256},
    }
    value["sha256"] = sha256(value)
    return validate_verdict(value)


def run_regression_gate(
    candidate: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
    *, baseline: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run every frozen case and return a per-case, per-axis verdict.

    ``candidate`` is an optional deterministic observation map used by tests,
    replay tooling, and Runtime qualification adapters.  A missing observation
    means the frozen positive fixture.  ``baseline`` is also frozen by default;
    if a supplied baseline is weaker, the gate fails rather than treating that
    weakness as an acceptable starting point.
    """
    validate_registry()
    results: list[dict[str, Any]] = []
    for case in FROZEN_CASES:
        baseline_observation = _source_observation(baseline, case)
        candidate_observation = _source_observation(candidate, case)
        # An omitted source means the frozen qualified baseline. Once a caller
        # supplies a source, an omitted case is explicit missing evidence and
        # must fail closed rather than silently becoming a positive fixture.
        if baseline is not None and baseline_observation is None:
            baseline_observation = {}
        if candidate is not None and candidate_observation is None:
            candidate_observation = {}
        baseline_result = evaluate_case(case, baseline_observation)
        candidate_result = evaluate_case(case, candidate_observation)
        results.append(_with_non_regression(candidate_result, baseline_result))
    return _build_verdict(results)


# A concise alias for callers that use the term qualification instead of gate.
qualify = run_regression_gate
run = run_regression_gate


def validate_verdict(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a verdict and enforce the no-aggregate-score rule."""
    _reject_aggregate_keys(value)
    verdict = _copy(dict(value))
    required = {
        "contract", "version", "registry_contract", "registry_version", "registry_sha256",
        "case_results", "evaluation_vector", "protection_vector", "passed", "failure_cases", "provenance", "sha256",
    }
    if set(verdict) != required:
        raise RegressionGateError("RegressionGateVerdict fields are not exact")
    if verdict["contract"] != VERDICT_CONTRACT or verdict["version"] != VERSION:
        raise RegressionGateError("unsupported RegressionGateVerdict identity")
    if verdict["registry_contract"] != REGISTRY_CONTRACT or verdict["registry_version"] != REGISTRY_VERSION or verdict["registry_sha256"] != REGISTRY_SHA256:
        raise RegressionGateError("verdict is bound to an unknown case registry")
    results = verdict["case_results"]
    if not isinstance(results, list) or [item.get("case_id") for item in results] != list(CASE_IDS):
        raise RegressionGateError("verdict must contain every frozen case in registry order")
    for item in results:
        _validate_result(item)
    vector = verdict["evaluation_vector"]
    if not isinstance(vector, dict) or set(vector) != set(EVALUATION_DIMENSIONS):
        raise RegressionGateError("evaluation vector dimensions are not exact")
    for dimension in EVALUATION_DIMENSIONS:
        entry = vector[dimension]
        if not isinstance(entry, dict) or set(entry) != {"passed", "cases"} or type(entry["passed"]) is not bool:
            raise RegressionGateError(f"evaluation vector {dimension} is not exact")
        expected_cases = [
            {"case_id": item["case_id"], "passed": item["evaluation"][dimension]["passed"],
             "evidence": item["evaluation"][dimension]["evidence"], "reasons": item["evaluation"][dimension]["reasons"]}
            for item in results
        ]
        if entry["cases"] != expected_cases or entry["passed"] != all(row["passed"] for row in expected_cases):
            raise RegressionGateError(f"evaluation vector {dimension} does not preserve case evidence")
    protection = verdict["protection_vector"]
    if not isinstance(protection, dict) or set(protection) != set(AXES):
        raise RegressionGateError("protection vector axes are not exact")
    for axis in AXES:
        entry = protection[axis]
        expected_cases = [{"case_id": item["case_id"], "passed": item["axes"][axis]["passed"], "reasons": item["axes"][axis]["reasons"]} for item in results]
        if not isinstance(entry, dict) or set(entry) != {"passed", "cases"} or entry["cases"] != expected_cases or entry["passed"] != all(row["passed"] for row in expected_cases):
            raise RegressionGateError(f"protection vector {axis} does not preserve case evidence")
    expected_failures = [item["case_id"] for item in results if not (
        all(item["axes"][axis]["passed"] for axis in AXES) and item["non_regression"]["passed"]
    )]
    if verdict["failure_cases"] != expected_failures:
        raise RegressionGateError("verdict failure cases do not preserve per-case degradation")
    if type(verdict["passed"]) is not bool or verdict["passed"] != (not expected_failures):
        raise RegressionGateError("verdict qualification does not match case evidence")
    if verdict["provenance"] != {
        "source": "runtime-regression-gate", "contract": CONTRACT, "registry_sha256": REGISTRY_SHA256,
    }:
        raise RegressionGateError("verdict provenance is invalid")
    expected = sha256({key: verdict[key] for key in verdict if key != "sha256"})
    if verdict["sha256"] != expected:
        raise RegressionGateError("regression verdict digest mismatch")
    return verdict


validate = validate_verdict


def frozen_replay(verdict: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Reconstruct the same qualification from frozen registry and verdict."""
    source_verdict = validate_verdict(verdict) if verdict is not None else run_regression_gate()
    source = {"registry": validate_registry(), "verdict": source_verdict}
    value = {
        "contract": REPLAY_CONTRACT,
        "version": VERSION,
        "registry_sha256": REGISTRY_SHA256,
        "source": source,
        "source_sha256": sha256(source),
        "qualification": {
            "valid": True,
            "deterministic": True,
            "cases_reconstructed": list(CASE_IDS),
            "per_case_axes": True,
            "safety_non_regression": source_verdict["protection_vector"]["safety"]["passed"],
            "quality_non_regression": source_verdict["protection_vector"]["quality"]["passed"],
            "recovery_paths_checked": source_verdict["protection_vector"]["recovery"]["passed"],
            "evaluation_dimensions": {
                dimension: source_verdict["evaluation_vector"][dimension]["passed"]
                for dimension in EVALUATION_DIMENSIONS
            },
            "aggregate_score_forbidden": True,
        },
    }
    value["sha256"] = sha256(value)
    return value


run_frozen_replay = frozen_replay
replay = frozen_replay


def validate_replay(value: Mapping[str, Any]) -> dict[str, Any]:
    _reject_aggregate_keys(value, "replay")
    replay_value = _copy(dict(value))
    required = {"contract", "version", "registry_sha256", "source", "source_sha256", "qualification", "sha256"}
    if set(replay_value) != required or replay_value["contract"] != REPLAY_CONTRACT or replay_value["version"] != VERSION:
        raise RegressionGateError("RegressionSpec replay fields are not exact")
    if replay_value["registry_sha256"] != REGISTRY_SHA256 or not isinstance(replay_value["source"], dict):
        raise RegressionGateError("RegressionSpec replay registry identity mismatch")
    validate_registry(replay_value["source"].get("registry"))
    source_verdict = validate_verdict(replay_value["source"].get("verdict"))
    if replay_value["source_sha256"] != sha256(replay_value["source"]):
        raise RegressionGateError("RegressionSpec replay source digest mismatch")
    qualification = replay_value["qualification"]
    required_qualification = {
        "valid", "deterministic", "cases_reconstructed", "per_case_axes",
        "safety_non_regression", "quality_non_regression", "recovery_paths_checked",
        "evaluation_dimensions", "aggregate_score_forbidden",
    }
    if not isinstance(qualification, dict) or set(qualification) != required_qualification:
        raise RegressionGateError("RegressionSpec replay qualification fields are not exact")
    if any(type(qualification[key]) is not bool or qualification[key] is not True for key in (
        "valid", "deterministic", "per_case_axes", "aggregate_score_forbidden",
    )):
        raise RegressionGateError("RegressionSpec replay qualification is invalid")
    if qualification["cases_reconstructed"] != list(CASE_IDS):
        raise RegressionGateError("RegressionSpec replay did not reconstruct all cases")
    if qualification["safety_non_regression"] != source_verdict["protection_vector"]["safety"]["passed"] or qualification["quality_non_regression"] != source_verdict["protection_vector"]["quality"]["passed"] or qualification["recovery_paths_checked"] != source_verdict["protection_vector"]["recovery"]["passed"]:
        raise RegressionGateError("RegressionSpec replay protection qualification does not match verdict")
    expected_dimensions = {dimension: source_verdict["evaluation_vector"][dimension]["passed"] for dimension in EVALUATION_DIMENSIONS}
    if qualification["evaluation_dimensions"] != expected_dimensions:
        raise RegressionGateError("RegressionSpec replay evaluation dimensions do not match verdict")
    if replay_value["sha256"] != sha256({key: replay_value[key] for key in replay_value if key != "sha256"}):
        raise RegressionGateError("RegressionSpec replay digest mismatch")
    return replay_value


def install_qualification() -> dict[str, Any]:
    """Return deterministic installation evidence without contacting providers."""
    verdict = run_regression_gate()
    replay_value = validate_replay(frozen_replay(verdict))
    value = {
        "contract": INSTALL_CONTRACT,
        "version": VERSION,
        "qualified": verdict["passed"],
        "registry_contract": REGISTRY_CONTRACT,
        "registry_version": REGISTRY_VERSION,
        "registry_sha256": REGISTRY_SHA256,
        "verdict_sha256": verdict["sha256"],
        "replay_sha256": replay_value["sha256"],
        "evaluation_vector": _copy(verdict["evaluation_vector"]),
        "protection_vector": _copy(verdict["protection_vector"]),
        "case_results": _copy(verdict["case_results"]),
        "provenance": {"source": "runtime-regression-gate", "contract": CONTRACT},
    }
    value["sha256"] = sha256(value)
    return value


def runtime_qualification() -> dict[str, Any]:
    """Fail closed before a Runtime can consume a changed provider or skill."""
    qualification = install_qualification()
    if qualification["qualified"] is not True:
        raise RegressionGateError(
            "runtime regression qualification failed: "
            + ", ".join(item["case_id"] for item in qualification["case_results"] if not item["non_regression"]["passed"])
        )
    return qualification


qualify_runtime = runtime_qualification


if __name__ == "__main__":
    print(canonical_json(install_qualification()))
