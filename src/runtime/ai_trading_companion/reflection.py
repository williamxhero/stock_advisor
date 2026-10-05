"""Runtime-owned ReflectionSpec/v1 for evidence-bound post-outcome review.

Reflection evaluates an immutable judgment snapshot against a later outcome. A
negative outcome alone never proves a reasoning or judgment error; a lesson is
only a candidate until independent evidence supports it.
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

CONTRACT = "ReflectionSpec/v1"
VERSION = 1
REPLAY_CONTRACT = "ReflectionReplay/v1"
ERROR_TYPES = frozenset({
    "judgment_error", "reasoning_error", "evidence_error", "timing_error",
    "risk_estimation_error", "random_outcome", "inconclusive",
})
STATUSES = frozenset({"qualified", "partial", "inconclusive", "failed"})


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: Any, field: str, limit: int = 2_000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise ValueError(f"{field} must be a bounded non-empty string")
    return value.strip()


def _refs(value: Any, field: str, *, required: bool = False) -> list[str]:
    if value is None:
        value = []
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{field} must be a list of non-empty references")
    refs = sorted(set(value))
    if required and not refs:
        raise ValueError(f"{field} requires independent evidence references")
    return refs


def build_input(checkpoint: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    snapshot = checkpoint.get("snapshot_json")
    if isinstance(snapshot, str):
        snapshot = json.loads(snapshot)
    if not isinstance(snapshot, dict) or not checkpoint.get("snapshot_id"):
        raise ValueError("Reflection requires an immutable judgment snapshot")
    if not checkpoint.get("cycle_id") or not checkpoint.get("checkpoint_id"):
        raise ValueError("Reflection requires cycle and checkpoint identity")
    value = {
        "contract": CONTRACT,
        "version": VERSION,
        "judgment_snapshot": {
            "snapshot_id": str(checkpoint["snapshot_id"]),
            "sha256": sha256({"snapshot": snapshot, "text": checkpoint.get("judgment_text", "")}),
            "as_of": str(checkpoint.get("judgment_as_of") or snapshot.get("reference_at") or ""),
        },
        "outcome": {
            "checkpoint_id": str(checkpoint["checkpoint_id"]),
            "horizon": str(checkpoint.get("horizon") or ""),
            "as_of": str(result.get("as_of") or ""),
            "verification_status": str(result.get("verification_status") or "unknown"),
            "sha256": sha256(result),
        },
        "provenance": {"cycle_id": str(checkpoint["cycle_id"])},
        "permissions": {"write_permissions": []},
    }
    validate_input(value)
    return value


def validate_input(value: dict[str, Any]) -> None:
    required = {"contract", "version", "judgment_snapshot", "outcome", "provenance", "permissions"}
    if not isinstance(value, dict) or value.get("contract") != CONTRACT:
        raise ValueError("unsupported Reflection input")
    if set(value) != required or value.get("version") != VERSION:
        raise ValueError("invalid Reflection input fields")
    snapshot = value["judgment_snapshot"]
    if not isinstance(snapshot, dict) or set(snapshot) != {"snapshot_id", "sha256", "as_of"}:
        raise ValueError("Reflection judgment snapshot identity is invalid")
    for key in ("snapshot_id", "sha256", "as_of"):
        _text(snapshot.get(key), f"judgment_snapshot.{key}")
    outcome = value["outcome"]
    if not isinstance(outcome, dict) or set(outcome) != {"checkpoint_id", "horizon", "as_of", "verification_status", "sha256"}:
        raise ValueError("Reflection outcome identity is invalid")
    for key in outcome:
        _text(outcome[key], f"outcome.{key}")
    provenance = value["provenance"]
    if not isinstance(provenance, dict) or set(provenance) != {"cycle_id"}:
        raise ValueError("Reflection provenance is invalid")
    _text(provenance["cycle_id"], "provenance.cycle_id")
    if value["permissions"] != {"write_permissions": []}:
        raise ValueError("Reflection must be read-only")


def build_output(
    input_contract: dict[str, Any], *, diagnosis: str, reason: str,
    evidence_refs: list[str] | None = None, lesson_candidate: dict[str, Any] | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    validate_input(input_contract)
    if diagnosis not in ERROR_TYPES:
        raise ValueError("unsupported Reflection diagnosis")
    refs = _refs(evidence_refs, "diagnostic_evidence_refs", required=diagnosis not in {"random_outcome", "inconclusive"})
    if diagnosis in {"random_outcome", "inconclusive"} and lesson_candidate is not None:
        raise ValueError("an inconclusive reflection cannot promote a lesson candidate")
    candidate = None
    if lesson_candidate is not None:
        if not isinstance(lesson_candidate, dict) or set(lesson_candidate) != {"title", "hypothesis", "evidence_refs", "state"}:
            raise ValueError("lesson candidate fields are invalid")
        candidate = {
            "title": _text(lesson_candidate["title"], "lesson_candidate.title"),
            "hypothesis": _text(lesson_candidate["hypothesis"], "lesson_candidate.hypothesis"),
            "evidence_refs": _refs(lesson_candidate["evidence_refs"], "lesson_candidate.evidence_refs", required=True),
            "state": "candidate",
        }
    value = {
        "contract": CONTRACT, "version": VERSION,
        "status": status or ("qualified" if diagnosis not in {"random_outcome", "inconclusive"} else "inconclusive"),
        "diagnosis": diagnosis, "reason": _text(reason, "reason"),
        "diagnostic_evidence_refs": refs, "judgment_snapshot": copy.deepcopy(input_contract["judgment_snapshot"]),
        "outcome": copy.deepcopy(input_contract["outcome"]), "lesson_candidate": candidate,
        "provenance": {"input_sha256": sha256(input_contract), "cycle_id": input_contract["provenance"]["cycle_id"]},
        "permissions": {"write_permissions": []},
    }
    validate_output(value)
    return value


def validate_output(value: dict[str, Any]) -> None:
    required = {"contract", "version", "status", "diagnosis", "reason", "diagnostic_evidence_refs", "judgment_snapshot", "outcome", "lesson_candidate", "provenance", "permissions"}
    if not isinstance(value, dict) or value.get("contract") != CONTRACT or set(value) != required:
        raise ValueError("invalid Reflection output fields")
    if value.get("version") != VERSION or value.get("status") not in STATUSES or value.get("diagnosis") not in ERROR_TYPES:
        raise ValueError("invalid Reflection output identity")
    _text(value["reason"], "reason")
    _refs(value["diagnostic_evidence_refs"], "diagnostic_evidence_refs")
    for key in ("judgment_snapshot", "outcome"):
        if not isinstance(value[key], dict):
            raise ValueError(f"Reflection {key} is required")
    if value["lesson_candidate"] is not None:
        candidate = value["lesson_candidate"]
        if not isinstance(candidate, dict) or candidate.get("state") != "candidate":
            raise ValueError("invalid lesson candidate")
        _refs(candidate.get("evidence_refs"), "lesson_candidate.evidence_refs", required=True)
    if not isinstance(value["provenance"], dict) or not value["provenance"].get("input_sha256"):
        raise ValueError("Reflection output provenance is required")
    if value["permissions"] != {"write_permissions": []}:
        raise ValueError("Reflection output must be read-only")


def from_outcome(checkpoint: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Conservatively classify an outcome without treating loss as proof of error."""
    input_contract = build_input(checkpoint, result)
    refs = _refs(result.get("diagnostic_evidence_refs"), "diagnostic_evidence_refs")
    requested = str(result.get("diagnosis") or result.get("error_type") or "")
    if requested in ERROR_TYPES and requested not in {"random_outcome", "inconclusive"} and not refs:
        requested = "inconclusive"
    if requested not in ERROR_TYPES:
        requested = "inconclusive" if str(result.get("verification_status")) in {"unknown", "unverified"} else "random_outcome"
    reason = str(result.get("diagnostic_reason") or (
        "结果偏离判断，但没有独立证据证明推理、证据、时机或风险估计错误；保留为随机或不可归因结果。"
        if requested == "random_outcome" else "当前结果不足以区分判断、推理、证据、时机和风险原因。"
    ))
    candidate = result.get("lesson_candidate") if requested not in {"random_outcome", "inconclusive"} and refs else None
    if isinstance(candidate, dict):
        candidate = {**candidate, "state": "candidate"}
    return build_output(input_contract, diagnosis=requested, reason=reason, evidence_refs=refs, lesson_candidate=candidate)


def frozen_replay(input_contract: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
    validate_input(input_contract)
    validate_output(output)
    if output["provenance"].get("input_sha256") != sha256(input_contract):
        raise ValueError("Reflection replay provenance mismatch")
    return {
        "contract": REPLAY_CONTRACT, "version": VERSION,
        "source_input_sha256": sha256(input_contract), "source_output_sha256": sha256(output),
        "qualification": {"valid": True, "diagnosis": output["diagnosis"], "lesson_candidate": output["lesson_candidate"] is not None},
        "evaluation_vector": {
            "delivery_speed": {"state": "not_measured_in_frozen_replay"},
            "qualification_probability": {"state": "not_estimated_in_frozen_replay"},
            "research_quality": {"diagnosis": output["diagnosis"], "evidence_bound": bool(output["diagnostic_evidence_refs"])},
            "judgment_outcome": {"state": output["outcome"]["verification_status"]},
            "safety_reliability": {"snapshot_immutable": True, "read_only": output["permissions"]["write_permissions"] == []},
        },
    }


def install_qualification() -> dict[str, Any]:
    checkpoint = {"cycle_id": "reflection-install", "checkpoint_id": "checkpoint-install", "snapshot_id": "snapshot-install", "snapshot_json": {"direction": "bullish", "original_claims": ["fixture"]}, "judgment_as_of": "2026-01-01T00:00:00Z", "judgment_text": "fixture judgment", "horizon": "T+1"}
    result = {"as_of": "2026-01-02T00:00:00Z", "verification_status": "incorrect", "summary": "fixture outcome"}
    output = from_outcome(checkpoint, result)
    first = frozen_replay(build_input(checkpoint, result), output)
    second = frozen_replay(copy.deepcopy(build_input(checkpoint, result)), copy.deepcopy(output))
    return {"contract": "ReflectionInstallQualification/v1", "qualified": first == second and output["diagnosis"] in {"random_outcome", "inconclusive"}, "replay_sha256": sha256(first), "evaluation_vector": first["evaluation_vector"]}


if __name__ == "__main__":
    print(canonical_json(install_qualification()))
