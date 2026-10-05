"""Runtime-owned, versioned mandate boundaries for companion orchestration."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any

CONTRACT = "MandateSpec/v1"
VERSION = 1
SKILL_SET_VERSION = 1
MEMORY_SCOPE_VERSION = 1
QUANTRESEARCH_PERMISSION_VERSION = 1
RISK_LEVEL_VERSION = 1
VISIBILITY_VERSION = 1

_STAGES = frozenset({
    "m0_research", "m0_compose", "m1_research", "m1_judgment", "m2",
    "chat_research", "chat", "reflection", "workflow_feedback", "outcome_research",
})
_RISK_LEVELS = frozenset({"low", "medium", "high", "critical"})
_SKILL_ID = re.compile(r"^[A-Za-z0-9_.:-]+$")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _is_m1(stage: str) -> bool:
    return stage in {"m1", "m1_research", "m1_judgment"} or stage.startswith("m1_")


def _skill_set(value: Any, field: str) -> dict[str, Any]:
    if isinstance(value, list):
        value = {"version": SKILL_SET_VERSION, "items": value}
    if not isinstance(value, dict) or value.get("version") != SKILL_SET_VERSION:
        raise ValueError(f"{field} must use MandateSkillSet/v1")
    items = value.get("items")
    if not isinstance(items, list):
        raise ValueError(f"{field}.items must be a list")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, str):
            item = {"skill_id": item, "version": "*", "scope": "*"}
        if not isinstance(item, dict):
            raise ValueError(f"{field}.items must contain objects")
        allowed = {"skill_id", "version", "scope"}
        if set(item) - allowed:
            raise ValueError(f"{field} contains unknown fields")
        skill_id = str(item.get("skill_id") or "")
        version = str(item.get("version") or "")
        scope = str(item.get("scope") or "*")
        if not _SKILL_ID.fullmatch(skill_id) or not version.strip() or not scope.strip():
            raise ValueError(f"{field} contains an invalid skill declaration")
        if skill_id in seen:
            raise ValueError(f"{field} contains duplicate skill: {skill_id}")
        seen.add(skill_id)
        normalized.append({"skill_id": skill_id, "version": version, "scope": scope})
    return {"version": SKILL_SET_VERSION, "items": normalized}


def _memory_scope(value: Any, *, stage: str) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("version") != MEMORY_SCOPE_VERSION:
        raise ValueError("memory_scope must use MandateMemoryScope/v1")
    allowed = {"version", "memory_space_id", "allowed_stages", "allowed_kinds", "max_results", "as_of_bounded"}
    if set(value) - allowed:
        raise ValueError("memory_scope contains unknown fields")
    memory_space_id = value.get("memory_space_id")
    if not isinstance(memory_space_id, str) or not memory_space_id.strip():
        raise ValueError("memory_scope.memory_space_id is required")
    stages = value.get("allowed_stages")
    kinds = value.get("allowed_kinds")
    maximum = value.get("max_results")
    if not isinstance(stages, list) or not stages or any(str(item) not in _STAGES for item in stages):
        raise ValueError("memory_scope.allowed_stages contains an unsupported stage")
    if stage not in stages:
        raise ValueError("memory_scope does not permit the mandate stage")
    if not isinstance(kinds, list) or any(not isinstance(item, str) or not item.strip() for item in kinds):
        raise ValueError("memory_scope.allowed_kinds must be a list of strings")
    if isinstance(maximum, bool) or not isinstance(maximum, int) or not 0 < maximum <= 80:
        raise ValueError("memory_scope.max_results must be an integer from 1 through 80")
    if value.get("as_of_bounded") is not True:
        raise ValueError("memory_scope must be as-of bounded")
    return {
        "version": MEMORY_SCOPE_VERSION,
        "memory_space_id": str(value.get("memory_space_id") or "runtime-default"),
        "allowed_stages": list(dict.fromkeys(str(item) for item in stages)),
        "allowed_kinds": list(dict.fromkeys(kinds)),
        "max_results": maximum,
        "as_of_bounded": True,
    }


def _quantresearch(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("version") != QUANTRESEARCH_PERMISSION_VERSION:
        raise ValueError("quantresearch_permission must use QuantResearchPermission/v1")
    allowed = {"version", "enabled", "access", "scope", "write_permissions"}
    if set(value) - allowed:
        raise ValueError("quantresearch_permission contains unknown fields")
    if not isinstance(value.get("enabled"), bool) or value.get("access") != "read_only":
        raise ValueError("QuantResearch permission must be explicitly read_only")
    scope = value.get("scope")
    writes = value.get("write_permissions")
    if not isinstance(scope, list) or any(not isinstance(item, str) or not item.strip() for item in scope):
        raise ValueError("quantresearch_permission.scope must be a list of strings")
    if writes != []:
        raise ValueError("QuantResearch permission cannot contain write permissions")
    return {"version": 1, "enabled": value["enabled"], "access": "read_only",
            "scope": list(dict.fromkeys(scope)), "write_permissions": []}


def _risk(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("version") != RISK_LEVEL_VERSION:
        raise ValueError("risk_level must use MandateRiskLevel/v1")
    if set(value) - {"version", "value"} or value.get("value") not in _RISK_LEVELS:
        raise ValueError("risk_level.value is unsupported")
    return {"version": RISK_LEVEL_VERSION, "value": value["value"]}


def _visibility(value: Any, *, stage: str) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("version") != VISIBILITY_VERSION:
        raise ValueError("visibility must use MandateVisibility/v1")
    allowed = {"version", "h0_visible", "human_directional_signal_visible", "published_chat_after_cutoff_visible"}
    if set(value) - allowed:
        raise ValueError("visibility contains unknown fields")
    if not isinstance(value.get("h0_visible"), bool):
        raise ValueError("visibility.h0_visible must be boolean")
    if _is_m1(stage) and value["h0_visible"] is not False:
        raise ValueError("M1 mandate permanently requires h0_visible=false")
    for key in ("human_directional_signal_visible", "published_chat_after_cutoff_visible"):
        if value.get(key) is not False:
            raise ValueError(f"visibility.{key} must be false")
    return {"version": VISIBILITY_VERSION, "h0_visible": False if _is_m1(stage) else value["h0_visible"],
            "human_directional_signal_visible": False,
            "published_chat_after_cutoff_visible": False}


def validate_mandate(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("contract") != CONTRACT:
        raise ValueError("unsupported MandateSpec")
    required = {"contract", "version", "task_key", "stage", "goal", "constraints",
                "required_skills", "optional_skills", "memory_scope", "quantresearch_permission",
                "risk_level", "visibility", "provenance", "permissions", "sha256"}
    missing = sorted(required - set(value))
    if missing:
        raise ValueError("MandateSpec missing: " + ", ".join(missing))
    allowed = required
    if set(value) - allowed:
        raise ValueError("MandateSpec contains unknown fields")
    if value.get("version") != VERSION or not str(value.get("task_key") or "").strip() or value.get("stage") not in _STAGES:
        raise ValueError("invalid MandateSpec identity")
    if not isinstance(value.get("goal"), str) or not value["goal"].strip():
        raise ValueError("MandateSpec goal is required")
    if not isinstance(value.get("constraints"), list) or any(not isinstance(item, str) for item in value["constraints"]):
        raise ValueError("MandateSpec constraints must be strings")
    provenance = value["provenance"]
    if (
        not isinstance(provenance, dict)
        or provenance.get("contract") != "MandateProvenance/v1"
        or provenance.get("source") != "runtime"
        or not isinstance(provenance.get("as_of"), str)
        or not provenance["as_of"].strip()
    ):
        raise ValueError("MandateSpec provenance is incomplete")
    required_skills = _skill_set(value["required_skills"], "required_skills")
    optional_skills = _skill_set(value["optional_skills"], "optional_skills")
    required_ids = {item["skill_id"] for item in required_skills["items"]}
    optional_ids = {item["skill_id"] for item in optional_skills["items"]}
    if required_ids & optional_ids:
        raise ValueError("a skill cannot be both required and optional")
    memory_scope = _memory_scope(value["memory_scope"], stage=value["stage"])
    quantresearch = _quantresearch(value["quantresearch_permission"])
    risk = _risk(value["risk_level"])
    visibility = _visibility(value["visibility"], stage=value["stage"])
    if value["permissions"] != {"write_permissions": []}:
        raise ValueError("MandateSpec is read-only")
    provenance = value["provenance"]
    if not isinstance(provenance, dict) or provenance.get("contract") != "MandateProvenance/v1" or not str(provenance.get("as_of") or ""):
        raise ValueError("MandateSpec provenance is incomplete")
    expected = {key: item for key, item in value.items() if key != "sha256"}
    if value["sha256"] != sha256(expected):
        raise ValueError("MandateSpec sha256 mismatch")
    value["required_skills"] = required_skills
    value["optional_skills"] = optional_skills
    value["memory_scope"] = memory_scope
    value["quantresearch_permission"] = quantresearch
    value["risk_level"] = risk
    value["visibility"] = visibility
    return value


def build_mandate(
    task_key: str,
    stage: str,
    *,
    as_of: str,
    task_profile: dict[str, Any] | None = None,
    required_skills: list[Any] | dict[str, Any] | None = None,
    optional_skills: list[Any] | dict[str, Any] | None = None,
    memory_scope: dict[str, Any] | None = None,
    quantresearch_enabled: bool | None = None,
    risk_level: str | None = None,
    visibility: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    memory_space_id: str = "runtime-default",
) -> dict[str, Any]:
    """Build one immutable mandate; task configuration is data, not orchestration."""
    if stage not in _STAGES:
        raise ValueError(f"unsupported mandate stage: {stage}")
    config = copy.deepcopy(config or {})
    if not isinstance(config, dict):
        raise ValueError("mandate configuration must be an object")
    # Explicitly reject an attempted M1 override instead of silently accepting it.
    configured_visibility = config.get("visibility") if "visibility" in config else visibility
    if _is_m1(stage) and isinstance(configured_visibility, dict) and configured_visibility.get("h0_visible") is True:
        raise ValueError("task configuration cannot override M1 h0_visible=false")
    profile = task_profile or {}
    default_risk = "high" if str(task_key).startswith("daily.execution") else ("medium" if str(task_key).startswith("daily") else "low")
    if not isinstance(memory_space_id, str) or not memory_space_id.strip():
        raise ValueError("memory_space_id is required")
    default_memory = {
        "version": MEMORY_SCOPE_VERSION, "memory_space_id": memory_space_id.strip(),
        "allowed_stages": [stage], "allowed_kinds": [], "max_results": 80, "as_of_bounded": True,
    }
    supplied_memory = config.get("memory_scope", memory_scope) or default_memory
    supplied_quant = config.get("quantresearch_permission")
    if supplied_quant is None:
        supplied_quant = {
            "version": QUANTRESEARCH_PERMISSION_VERSION,
            "enabled": bool(quantresearch_enabled if quantresearch_enabled is not None else stage in {"m0_research", "m1_research", "m1_judgment"}),
            "access": "read_only", "scope": ["quantresearch_readonly"], "write_permissions": [],
        }
    supplied_risk = config.get("risk_level", risk_level) or {"version": RISK_LEVEL_VERSION, "value": default_risk}
    supplied_visibility = configured_visibility or {
        "version": VISIBILITY_VERSION, "h0_visible": not _is_m1(stage),
        "human_directional_signal_visible": False, "published_chat_after_cutoff_visible": False,
    }
    required_value = config.get("required_skills", required_skills if required_skills is not None else [])
    optional_value = config.get("optional_skills", optional_skills if optional_skills is not None else [])
    value: dict[str, Any] = {
        "contract": CONTRACT, "version": VERSION, "task_key": str(task_key), "stage": stage,
        "goal": f"Execute the runtime-owned {stage} mandate for {task_key}.",
        "constraints": ["runtime_owned", "as_of_bounded", "structured_artifact_only", "read_only"],
        "required_skills": required_value, "optional_skills": optional_value,
        "memory_scope": supplied_memory, "quantresearch_permission": supplied_quant,
        "risk_level": supplied_risk, "visibility": supplied_visibility,
        "provenance": {
            "contract": "MandateProvenance/v1", "source": "runtime", "as_of": str(as_of),
            "task_profile_id": profile.get("profile_id"), "task_profile_version": profile.get("version"),
        },
        "permissions": {"write_permissions": []},
    }
    # Normalize before hashing; this also enforces the permanent M1 boundary.
    value["required_skills"] = _skill_set(value["required_skills"], "required_skills")
    value["optional_skills"] = _skill_set(value["optional_skills"], "optional_skills")
    value["memory_scope"] = _memory_scope(value["memory_scope"], stage=stage)
    value["quantresearch_permission"] = _quantresearch(value["quantresearch_permission"])
    value["risk_level"] = _risk(value["risk_level"])
    value["visibility"] = _visibility(value["visibility"], stage=stage)
    value["sha256"] = sha256(value)
    return validate_mandate(value)


def validate_mandate_set(value: dict[str, Any]) -> dict[str, Any]:
    required = {"contract", "version", "mandates", "sha256"}
    allowed = required | {"skill_resolutions"}
    if not isinstance(value, dict) or not required.issubset(value) or set(value) - allowed:
        raise ValueError("invalid MandateSet fields")
    if value.get("contract") != "MandateSet/v1" or value.get("version") != 1:
        raise ValueError("unsupported MandateSet contract")
    mandates = value.get("mandates")
    if not isinstance(mandates, dict) or set(mandates) != set(_STAGES):
        raise ValueError("MandateSet must contain every supported stage")
    normalized = {stage: validate_mandate(copy.deepcopy(mandates[stage])) for stage in sorted(mandates)}
    payload = {key: item for key, item in value.items() if key != "sha256"}
    payload["mandates"] = normalized
    expected = sha256(payload)
    if value.get("sha256") != expected:
        raise ValueError("MandateSet sha256 mismatch")
    return {**payload, "sha256": expected}


def resolve_skill_declarations(
    mandate: dict[str, Any], registry: dict[str, Any], *,
    health: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Bind frozen declarations to SkillRegistry receipts without granting writes."""
    from .skill_registry import resolve_skill

    frozen = validate_mandate(copy.deepcopy(mandate))
    entries = {str(item["skill_id"]): item for item in registry.get("skills", [])} if isinstance(registry, dict) else {}
    receipts: list[dict[str, Any]] = []
    for kind in ("required_skills", "optional_skills"):
        required = kind == "required_skills"
        for declaration in frozen[kind]["items"]:
            skill_id = declaration["skill_id"]
            entry = entries.get(skill_id)
            if entry is None:
                raise ValueError(f"{kind} declares unknown SkillRegistry skill: {skill_id}")
            declared_version = declaration["version"]
            available = set(entry.get("available_versions") or [])
            if declared_version != "*" and declared_version not in available:
                raise ValueError(f"{skill_id} does not provide declared version {declared_version}")
            receipt = resolve_skill(registry, skill_id, scope=declaration["scope"], health=health)
            if declared_version != "*" and receipt["requested_version"] != declared_version:
                raise ValueError(f"{skill_id} declaration is not the registry current version")
            if required and receipt["selected_skill"] is None:
                raise ValueError(f"required skill is unavailable: {skill_id}")
            receipts.append({"kind": "required" if required else "optional", "declaration": declaration, "resolution": receipt})
    return receipts


def resolve_cycle_mandates(
    task_key: str,
    *,
    as_of: str,
    task_profile: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    memory_space_id: str = "runtime-default",
    skill_registry: dict[str, Any] | None = None,
    skill_health: dict[str, str] | None = None,
) -> dict[str, Any]:
    stages = ["m0_research", "m0_compose", "m1_research", "m1_judgment", "m2", "chat_research", "chat", "reflection", "workflow_feedback", "outcome_research"]
    mandates = {
        stage: build_mandate(
            task_key, stage, as_of=as_of, task_profile=task_profile, config=config,
            memory_space_id=memory_space_id,
        )
        for stage in stages
    }
    result: dict[str, Any] = {"contract": "MandateSet/v1", "version": 1, "mandates": mandates}
    if skill_registry is not None:
        result["skill_resolutions"] = {
            stage: resolve_skill_declarations(mandate, skill_registry, health=skill_health)
            for stage, mandate in mandates.items()
        }
    result["sha256"] = sha256({key: value for key, value in result.items() if key != "sha256"})
    return validate_mandate_set(result)


def mandate_for_stage(
    cycle: dict[str, Any], stage: str, *, memory_space_id: str = "runtime-default",
) -> dict[str, Any]:
    if stage not in _STAGES:
        raise ValueError(f"unsupported mandate stage: {stage}")
    try:
        provenance = json.loads(str(cycle.get("cycle_provenance_json") or "{}"))
    except json.JSONDecodeError:
        provenance = {}
    mandate_set = provenance.get("mandates") if isinstance(provenance, dict) else None
    if isinstance(mandate_set, dict):
        frozen_set = validate_mandate_set(copy.deepcopy(mandate_set))
        if stage in frozen_set["mandates"]:
            return copy.deepcopy(frozen_set["mandates"][stage])
    # Legacy cycles predate MandateSpec; new cycle creation always persists the
    # complete set, while this deterministic fallback keeps old packet fixtures readable.
    return build_mandate(
        str(cycle.get("task_key") or "unknown"), stage,
        as_of=str(cycle.get("as_of") or "unknown"), memory_space_id=memory_space_id,
    )


def frozen_replay(mandate: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(mandate)
    validate_mandate(value)
    expected = sha256({key: item for key, item in value.items() if key != "sha256"})
    if expected != value["sha256"]:
        raise ValueError("MandateSpec replay digest mismatch")
    return {"contract": "MandateSpecReplay/v1", "source": value, "source_sha256": value["sha256"], "qualification": {"valid": True, "read_only": value["permissions"] == {"write_permissions": []}, "m1_h0_blind": _is_m1(value["stage"]) and value["visibility"]["h0_visible"] is False}}


def install_qualification() -> dict[str, Any]:
    sample = build_mandate("daily.execution.0945", "m1_judgment", as_of="2026-10-05T01:45:00Z")
    replay = frozen_replay(sample)
    return {
        "contract": "MandateSpecInstallQualification/v1",
        "qualified": replay["qualification"]["valid"],
        "replay_sha256": sha256(replay),
        "evaluation_vector": {
            "schema": True, "frozen_replay": True, "m1_blind": True, "read_only": True,
        },
    }


if __name__ == "__main__":
    print(canonical_json(install_qualification()))
