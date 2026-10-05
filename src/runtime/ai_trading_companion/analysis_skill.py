"""Runtime-owned AnalysisSkillSpec/v1.

Skills are replaceable capability adapters. They can calculate or interpret a
bounded input, but cannot own task state, portfolio facts, long-term memory,
or the final judgment.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable

CONTRACT = "AnalysisSkillSpec/v1"
VERSION = 1
RESULT_CONTRACT = "AnalysisSkillResult/v1"
REPLAY_CONTRACT = "AnalysisSkillReplay/v1"
MODES = frozenset({"deterministic", "probabilistic"})
STATUSES = frozenset({"succeeded", "partial", "blocked", "failed", "unknown"})
_FORBIDDEN = frozenset({
    "task_state", "portfolio", "positions", "memoryhub", "memory", "final_judgment",
    "judgment", "orders", "schedule", "production_strategy", "exchange_write",
})


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _bounded(value: Any, field: str, *, limit: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise ValueError(f"{field} must be a bounded non-empty string")
    return value.strip()


def _walk_forbidden(value: Any, path: str = "skill") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).casefold() in _FORBIDDEN:
                raise ValueError(f"AnalysisSkill forbids protected field at {path}.{key}")
            _walk_forbidden(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk_forbidden(child, f"{path}[{index}]")


def validate_input(value: dict[str, Any]) -> None:
    required = {"contract", "version", "skill_id", "skill_version", "capabilities", "required_inputs", "mode", "inputs", "provenance", "permissions"}
    if not isinstance(value, dict) or value.get("contract") != CONTRACT or set(value) != required:
        raise ValueError("invalid AnalysisSkill input fields")
    if value["version"] != VERSION or value["mode"] not in MODES:
        raise ValueError("invalid AnalysisSkill identity")
    _bounded(value["skill_id"], "skill_id")
    _bounded(value["skill_version"], "skill_version")
    if not isinstance(value["capabilities"], list) or not value["capabilities"] or any(not isinstance(item, str) or not item for item in value["capabilities"]):
        raise ValueError("AnalysisSkill capabilities are required")
    if not isinstance(value["required_inputs"], list) or any(not isinstance(item, str) or not item for item in value["required_inputs"]):
        raise ValueError("AnalysisSkill required_inputs are invalid")
    if not isinstance(value["inputs"], dict):
        raise ValueError("AnalysisSkill inputs must be an object")
    if not isinstance(value["provenance"], dict) or not value["provenance"].get("as_of"):
        raise ValueError("AnalysisSkill provenance.as_of is required")
    if value["permissions"] != {"write_permissions": []}:
        raise ValueError("AnalysisSkill is read-only")
    _walk_forbidden(value)


def validate_output(value: dict[str, Any]) -> None:
    required = {"contract", "version", "skill_id", "skill_version", "mode", "status", "data", "provenance", "permissions"}
    if not isinstance(value, dict) or value.get("contract") != RESULT_CONTRACT or set(value) != required:
        raise ValueError("invalid AnalysisSkill output fields")
    if value["version"] != VERSION or value["mode"] not in MODES or value["status"] not in STATUSES:
        raise ValueError("invalid AnalysisSkill output identity")
    _bounded(value["skill_id"], "skill_id")
    _bounded(value["skill_version"], "skill_version")
    if not isinstance(value["data"], dict):
        raise ValueError("AnalysisSkill data must be an object")
    if not isinstance(value["provenance"], dict) or not value["provenance"].get("input_sha256"):
        raise ValueError("AnalysisSkill output provenance is required")
    if value["permissions"] != {"write_permissions": []}:
        raise ValueError("AnalysisSkill output is read-only")
    _walk_forbidden(value)


@dataclass(frozen=True)
class AnalysisSkill:
    skill_id: str
    skill_version: str
    capabilities: tuple[str, ...]
    required_inputs: tuple[str, ...]
    mode: str
    execute: Callable[[dict[str, Any]], dict[str, Any]]
    validate: Callable[[dict[str, Any]], None] | None = None
    healthcheck: Callable[[], dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError("unsupported AnalysisSkill mode")
        if not self.skill_id or not self.skill_version or not self.capabilities:
            raise ValueError("AnalysisSkill identity and capabilities are required")


class SkillRegistry:
    """In-memory registry of capability implementations; no business state ownership."""

    def __init__(self) -> None:
        self._skills: dict[str, AnalysisSkill] = {}

    def register(self, skill: AnalysisSkill) -> None:
        if skill.skill_id in self._skills:
            raise ValueError("analysis skill already registered")
        self._skills[skill.skill_id] = skill

    def resolve(self, skill_id: str) -> AnalysisSkill:
        try:
            return self._skills[skill_id]
        except KeyError as exc:
            raise ValueError("analysis skill is not registered") from exc

    def execute(self, skill_id: str, inputs: dict[str, Any], *, as_of: str, cycle_id: str | None = None) -> dict[str, Any]:
        skill = self.resolve(skill_id)
        if not isinstance(inputs, dict):
            raise ValueError("analysis skill inputs must be an object")
        missing = [name for name in skill.required_inputs if name not in inputs]
        if missing:
            raise ValueError("analysis skill inputs missing: " + ", ".join(missing))
        request = {
            "contract": CONTRACT, "version": VERSION, "skill_id": skill.skill_id,
            "skill_version": skill.skill_version, "capabilities": list(skill.capabilities),
            "required_inputs": list(skill.required_inputs), "mode": skill.mode,
            "inputs": copy.deepcopy(inputs), "provenance": {"as_of": as_of, "cycle_id": cycle_id},
            "permissions": {"write_permissions": []},
        }
        validate_input(request)
        if skill.validate is not None:
            skill.validate(copy.deepcopy(inputs))
        try:
            data = skill.execute(copy.deepcopy(inputs))
            status = "succeeded"
        except Exception as exc:
            data, status = {"error": type(exc).__name__}, "failed"
        if not isinstance(data, dict):
            data, status = {"error": "skill_result_must_be_object"}, "failed"
        result = {
            "contract": RESULT_CONTRACT, "version": VERSION,
            "skill_id": skill.skill_id, "skill_version": skill.skill_version,
            "mode": skill.mode, "status": status, "data": data,
            "provenance": {"input_sha256": sha256(request), "as_of": as_of, "cycle_id": cycle_id},
            "permissions": {"write_permissions": []},
        }
        validate_output(result)
        return result

    def healthcheck(self, skill_id: str) -> dict[str, Any]:
        skill = self.resolve(skill_id)
        result = skill.healthcheck() if skill.healthcheck is not None else {"state": "ready"}
        if not isinstance(result, dict) or result.get("state") not in {"ready", "degraded", "unavailable"}:
            raise ValueError("invalid analysis skill healthcheck")
        return {"skill_id": skill_id, "skill_version": skill.skill_version, **result}

    def manifest(self) -> list[dict[str, Any]]:
        return [{"skill_id": skill.skill_id, "skill_version": skill.skill_version, "capabilities": list(skill.capabilities), "mode": skill.mode} for skill in self._skills.values()]


def frozen_replay(request: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
    validate_input(request)
    validate_output(output)
    if output["provenance"].get("input_sha256") != sha256(request):
        raise ValueError("AnalysisSkill replay provenance mismatch")
    return {
        "contract": REPLAY_CONTRACT, "version": VERSION,
        "source_input_sha256": sha256(request), "source_output_sha256": sha256(output),
        "qualification": {"valid": True, "status": output["status"], "read_only": output["permissions"]["write_permissions"] == []},
        "evaluation_vector": {
            "delivery_speed": {"state": "not_measured_in_frozen_replay"},
            "qualification_probability": {"state": "not_estimated_in_frozen_replay"},
            "research_quality": {"capabilities": request["capabilities"], "mode": request["mode"]},
            "judgment_outcome": {"state": "not_a_final_judgment"},
            "safety_reliability": {"read_only": True, "protected_fields_rejected": True},
        },
    }


def install_qualification() -> dict[str, Any]:
    registry = SkillRegistry()
    registry.register(AnalysisSkill("financial_growth", "fixture/v1", ("growth",), ("series",), "deterministic", lambda data: {"growth": data["series"][-1] - data["series"][0]}))
    first = registry.execute("financial_growth", {"series": [1, 2, 4]}, as_of="2026-01-01T00:00:00Z")
    request = {
        "contract": CONTRACT, "version": VERSION, "skill_id": "financial_growth", "skill_version": "fixture/v1",
        "capabilities": ["growth"], "required_inputs": ["series"], "mode": "deterministic",
        "inputs": {"series": [1, 2, 4]}, "provenance": {"as_of": "2026-01-01T00:00:00Z", "cycle_id": None},
        "permissions": {"write_permissions": []},
    }
    replay = frozen_replay(request, first)
    return {"contract": "AnalysisSkillInstallQualification/v1", "qualified": replay["qualification"]["valid"], "replay_sha256": sha256(replay), "evaluation_vector": replay["evaluation_vector"]}


if __name__ == "__main__":
    print(canonical_json(install_qualification()))
