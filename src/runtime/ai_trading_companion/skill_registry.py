"""Versioned, deterministic registry metadata and capability selection."""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

CONTRACT = "SkillRegistrySpec/v1"
VERSION = 1
RESOLUTION_CONTRACT = "SkillRegistryResolution/v1"
REPLAY_CONTRACT = "SkillRegistryReplay/v1"
_COST_TIERS = frozenset({"low", "medium", "high"})
_LATENCY_CLASSES = frozenset({"fast", "standard", "slow"})
_HEALTH_STATES = frozenset({"ready", "degraded", "unavailable"})


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: Any, field: str, *, limit: int = 160) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise ValueError(f"{field} must be a bounded non-empty string")
    return value.strip()


def validate_registry(value: dict[str, Any]) -> dict[str, Any]:
    required = {"contract", "version", "registry_version", "skills", "provenance", "permissions"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("invalid SkillRegistry fields")
    if value["contract"] != CONTRACT or value["version"] != VERSION:
        raise ValueError("unsupported SkillRegistry contract")
    registry_version = _text(value["registry_version"], "registry_version")
    provenance = value["provenance"]
    if not isinstance(provenance, dict) or set(provenance) != {"source", "as_of"}:
        raise ValueError("invalid SkillRegistry provenance")
    _text(provenance["source"], "provenance.source")
    _text(provenance["as_of"], "provenance.as_of")
    if value["permissions"] != {"write_permissions": []}:
        raise ValueError("SkillRegistry must be read-only")
    if not isinstance(value["skills"], list):
        raise ValueError("SkillRegistry skills must be a list")

    normalized: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    fields = {
        "skill_id", "available_versions", "current_version", "provider", "capabilities",
        "enabled", "dependencies", "fallback", "cost_tier", "latency_class", "critical", "applies_to", "health",
    }
    for raw in value["skills"]:
        if not isinstance(raw, dict) or set(raw) != fields:
            raise ValueError("invalid SkillRegistry entry fields")
        skill_id = _text(raw["skill_id"], "skill_id")
        if skill_id in by_id:
            raise ValueError(f"duplicate SkillRegistry skill_id: {skill_id}")
        versions = raw["available_versions"]
        if not isinstance(versions, list) or not versions:
            raise ValueError(f"{skill_id} must declare available_versions")
        versions = sorted({_text(item, f"{skill_id}.available_versions") for item in versions})
        current = _text(raw["current_version"], f"{skill_id}.current_version")
        if current not in versions:
            raise ValueError(f"{skill_id}.current_version must be available")
        capabilities = raw["capabilities"]
        if not isinstance(capabilities, list) or not capabilities:
            raise ValueError(f"{skill_id} must declare capabilities")
        capabilities = sorted({_text(item, f"{skill_id}.capabilities") for item in capabilities})
        dependencies = raw["dependencies"]
        if not isinstance(dependencies, list):
            raise ValueError(f"{skill_id}.dependencies must be a list")
        dependencies = sorted({_text(item, f"{skill_id}.dependencies") for item in dependencies})
        if skill_id in dependencies:
            raise ValueError(f"{skill_id} cannot depend on itself")
        applies_to = raw["applies_to"]
        if not isinstance(applies_to, list) or not applies_to:
            raise ValueError(f"{skill_id} must declare applies_to")
        applies_to = sorted({_text(item, f"{skill_id}.applies_to") for item in applies_to})
        if type(raw["enabled"]) is not bool or type(raw["critical"]) is not bool:
            raise ValueError(f"{skill_id}.enabled and critical must be booleans")
        if raw["cost_tier"] not in _COST_TIERS or raw["latency_class"] not in _LATENCY_CLASSES:
            raise ValueError(f"{skill_id} has invalid cost or latency class")
        if raw["health"] not in _HEALTH_STATES:
            raise ValueError(f"{skill_id} has invalid health state")
        fallback = raw["fallback"]
        if fallback is not None:
            fallback = _text(fallback, f"{skill_id}.fallback")
            if fallback == skill_id:
                raise ValueError(f"{skill_id} cannot fall back to itself")
        entry = {
            "skill_id": skill_id, "available_versions": versions, "current_version": current,
            "provider": _text(raw["provider"], f"{skill_id}.provider"),
            "capabilities": capabilities, "enabled": raw["enabled"],
            "dependencies": dependencies, "fallback": fallback,
            "cost_tier": raw["cost_tier"], "latency_class": raw["latency_class"],
            "critical": raw["critical"], "applies_to": applies_to, "health": raw["health"],
        }
        by_id[skill_id] = entry
        normalized.append(entry)

    normalized.sort(key=lambda entry: entry["skill_id"])
    for entry in normalized:
        for dependency in entry["dependencies"]:
            if dependency not in by_id:
                raise ValueError(f"{entry['skill_id']} has unknown dependency: {dependency}")
        fallback = entry["fallback"]
        if fallback is not None:
            if fallback not in by_id:
                raise ValueError(f"{entry['skill_id']} has unknown fallback: {fallback}")
            if set(entry["capabilities"]) != set(by_id[fallback]["capabilities"]):
                raise ValueError(f"{entry['skill_id']} fallback must provide the same capabilities")

    _assert_acyclic(by_id, "dependencies")
    _assert_acyclic(by_id, "fallback", single=True)
    _assert_resolution_graph_acyclic(by_id)
    return {
        "contract": CONTRACT, "version": VERSION, "registry_version": registry_version,
        "skills": normalized, "provenance": copy.deepcopy(provenance),
        "permissions": {"write_permissions": []},
    }


def _assert_resolution_graph_acyclic(by_id: dict[str, dict[str, Any]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(skill_id: str) -> None:
        if skill_id in visiting:
            raise ValueError("SkillRegistry resolution graph cycle detected")
        if skill_id in visited:
            return
        visiting.add(skill_id)
        entry = by_id[skill_id]
        targets = [*entry["dependencies"]]
        if entry["fallback"] is not None:
            targets.append(entry["fallback"])
        for target in targets:
            visit(target)
        visiting.remove(skill_id)
        visited.add(skill_id)

    for skill_id in sorted(by_id):
        visit(skill_id)


def _assert_acyclic(by_id: dict[str, dict[str, Any]], relation: str, *, single: bool = False) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(skill_id: str) -> None:
        if skill_id in visiting:
            raise ValueError(f"SkillRegistry {relation} cycle detected")
        if skill_id in visited:
            return
        visiting.add(skill_id)
        targets = by_id[skill_id][relation]
        targets = [] if targets is None else [targets] if single else targets
        for target in targets:
            visit(target)
        visiting.remove(skill_id)
        visited.add(skill_id)

    for skill_id in sorted(by_id):
        visit(skill_id)


def build_registry(
    registry_version: str,
    skills: list[dict[str, Any]],
    *,
    source: str = "runtime",
    as_of: str = "unspecified",
) -> dict[str, Any]:
    return validate_registry({
        "contract": CONTRACT, "version": VERSION, "registry_version": registry_version,
        "skills": copy.deepcopy(skills),
        "provenance": {"source": source, "as_of": as_of},
        "permissions": {"write_permissions": []},
    })


def dependents(registry: dict[str, Any], skill_id: str, *, transitive: bool = True) -> list[str]:
    manifest = validate_registry(registry)
    known = {entry["skill_id"] for entry in manifest["skills"]}
    if skill_id not in known:
        raise ValueError(f"unknown SkillRegistry skill: {skill_id}")
    reverse: dict[str, set[str]] = {entry["skill_id"]: set() for entry in manifest["skills"]}
    for entry in manifest["skills"]:
        for dependency in entry["dependencies"]:
            reverse[dependency].add(entry["skill_id"])
        if entry["fallback"] is not None:
            reverse[entry["fallback"]].add(entry["skill_id"])
    direct = reverse[skill_id]
    result = set(direct)
    if transitive:
        frontier = list(direct)
        while frontier:
            current = frontier.pop()
            for candidate in reverse[current]:
                if candidate not in result:
                    result.add(candidate)
                    frontier.append(candidate)
    return sorted(result)


def resolve_skill(
    registry: dict[str, Any], skill_id: str, *, scope: str,
    health: dict[str, str] | None = None,
) -> dict[str, Any]:
    manifest = validate_registry(registry)
    entries = {entry["skill_id"]: entry for entry in manifest["skills"]}
    requested = _text(skill_id, "skill_id")
    scope = _text(scope, "scope")
    if health is not None and not isinstance(health, dict):
        raise ValueError("SkillRegistry health must be a mapping")
    health = dict(health or {})
    if requested not in entries:
        raise ValueError(f"unknown SkillRegistry skill: {requested}")
    if set(health) - set(entries):
        raise ValueError("SkillRegistry health contains unknown skills")
    if any(state not in _HEALTH_STATES for state in health.values()):
        raise ValueError("invalid SkillRegistry health state")

    def select(current: str, chain: list[str]) -> tuple[str | None, str | None, list[dict[str, Any]]]:
        entry = entries[current]
        if current in chain:
            return None, "fallback_cycle", []
        reason = None
        if not entry["enabled"]:
            reason = "disabled"
        elif scope not in entry["applies_to"] and "*" not in entry["applies_to"]:
            reason = "out_of_scope"
        elif health.get(current, entry["health"]) == "unavailable":
            reason = "unavailable"
        dependency_receipts = []
        if reason is None:
            for dependency in entry["dependencies"]:
                selected, dependency_reason, nested = select(dependency, chain + [current])
                dependency_receipts.extend(nested)
                dependency_entry = entries[dependency]
                selected_dependency = entries[selected] if selected else None
                dependency_receipts.append({
                    "requested_skill": dependency,
                    "requested_version": dependency_entry["current_version"],
                    "requested_enabled": dependency_entry["enabled"],
                    "selected_skill": selected,
                    "selected_version": selected_dependency["current_version"] if selected_dependency else None,
                    "selected_provider": selected_dependency["provider"] if selected_dependency else None,
                    "selected_enabled": selected_dependency["enabled"] if selected_dependency else None,
                    "status": "ready" if selected else "blocked",
                    "reason": dependency_reason,
                    "fallback_path": _fallback_path(entries, dependency, selected),
                    "fallback_candidates": _candidate_audit(
                        entries, _fallback_path(entries, dependency, selected), health,
                    ),
                })
                if selected is None:
                    reason = "dependency_unavailable"
                    break
        if reason is not None and entry["fallback"] is not None:
            selected, fallback_reason, nested = select(entry["fallback"], chain + [current])
            dependency_receipts.extend(nested)
            if selected is not None:
                return selected, f"fallback:{reason}", dependency_receipts
            return None, fallback_reason or reason, dependency_receipts
        return (current, None, dependency_receipts) if reason is None else (None, reason, dependency_receipts)

    selected, reason, dependency_receipts = select(requested, [])
    selected_entry = entries[selected] if selected else None
    health_snapshot = {key: health[key] for key in sorted(health)}
    fallback_path = _fallback_path(entries, requested, selected)
    result_status = "ready" if selected else ("blocked" if entries[requested]["critical"] else "degraded")
    receipt = {
        "contract": RESOLUTION_CONTRACT, "version": VERSION,
        "registry_version": manifest["registry_version"], "registry_sha256": sha256(manifest),
        "requested_skill": requested, "requested_version": entries[requested]["current_version"],
        "requested_enabled": entries[requested]["enabled"], "requested_critical": entries[requested]["critical"],
        "selected_skill": selected,
        "selected_version": selected_entry["current_version"] if selected_entry else None,
        "selected_provider": selected_entry["provider"] if selected_entry else None,
        "selected_enabled": selected_entry["enabled"] if selected_entry else None,
        "selected_cost_tier": selected_entry["cost_tier"] if selected_entry else None,
        "selected_latency_class": selected_entry["latency_class"] if selected_entry else None,
        "selected_critical": selected_entry["critical"] if selected_entry else None,
        "status": result_status, "reason": reason,
        "health_snapshot": health_snapshot,
        "fallback_path": fallback_path,
        "fallback_candidates": _candidate_audit(entries, fallback_path, health),
        "dependencies": sorted(dependency_receipts, key=lambda item: (item["requested_skill"], item["selected_skill"] or "")),
        "impacted_skills": sorted({
            *dependents(manifest, requested),
            *(
                impacted
                for item in dependency_receipts
                if item["selected_skill"] is None
                for candidate in [item["requested_skill"], *item["fallback_path"]]
                for impacted in dependents(manifest, candidate)
            ),
        }),
        "scope": scope,
    }
    return receipt


def _fallback_path(entries: dict[str, dict[str, Any]], requested: str, selected: str | None) -> list[str]:
    if selected == requested or entries[requested]["fallback"] is None:
        return []
    path = [requested]
    current = requested
    seen = {requested}
    while current != selected:
        fallback = entries[current]["fallback"]
        if fallback is None or fallback in seen:
            break
        path.append(fallback)
        seen.add(fallback)
        current = fallback
    return path


def _candidate_audit(
    entries: dict[str, dict[str, Any]], path: list[str], health: dict[str, str],
) -> list[dict[str, Any]]:
    return [
        {
            "skill_id": skill_id,
            "current_version": entries[skill_id]["current_version"],
            "provider": entries[skill_id]["provider"],
            "enabled": entries[skill_id]["enabled"],
            "health": health.get(skill_id, entries[skill_id]["health"]),
        }
        for skill_id in path
    ]


def frozen_replay(registry: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    manifest = validate_registry(registry)
    if not isinstance(receipt, dict) or receipt.get("contract") != RESOLUTION_CONTRACT:
        raise ValueError("invalid SkillRegistry resolution receipt")
    if receipt.get("registry_sha256") != sha256(manifest):
        raise ValueError("SkillRegistry replay registry hash mismatch")
    expected = resolve_skill(
        manifest, receipt["requested_skill"], scope=receipt["scope"],
        health=receipt.get("health_snapshot", {}),
    )
    if expected != receipt:
        raise ValueError("SkillRegistry replay receipt does not match frozen inputs")
    return {
        "contract": REPLAY_CONTRACT, "version": VERSION,
        "registry_sha256": sha256(manifest), "resolution_sha256": sha256(receipt),
        "qualification": {"valid": True, "status": receipt["status"], "selected_skill": receipt["selected_skill"]},
        "evaluation_vector": {
            "delivery_speed": {"state": "not_measured_in_frozen_replay"},
            "qualification_probability": {"state": "not_estimated_in_frozen_replay"},
            "research_quality": {"skill": receipt["selected_skill"], "version": receipt["selected_version"]},
            "judgment_outcome": {"state": "not_a_final_judgment"},
            "safety_reliability": {"registry_hash_bound": True, "read_only": True, "fallback_path_recorded": bool(receipt["fallback_path"])},
        },
    }


class SkillRegistryCatalog:
    """Mutable registration facade whose exported manifest is immutable-by-copy."""

    def __init__(self, registry_version: str = "runtime/v1") -> None:
        self.registry_version = _text(registry_version, "registry_version")
        self._entries: dict[str, dict[str, Any]] = {}

    def register(self, entry: dict[str, Any]) -> None:
        skill_id = _text(entry.get("skill_id"), "skill_id")
        if skill_id in self._entries:
            raise ValueError(f"duplicate SkillRegistry skill_id: {skill_id}")
        candidate = {**self._entries, skill_id: copy.deepcopy(entry)}
        build_registry(self.registry_version, list(candidate.values()))
        self._entries = candidate

    def replace(self, entry: dict[str, Any]) -> None:
        skill_id = _text(entry.get("skill_id"), "skill_id")
        if skill_id not in self._entries:
            raise ValueError(f"cannot replace unregistered SkillRegistry skill_id: {skill_id}")
        candidate = {**self._entries, skill_id: copy.deepcopy(entry)}
        build_registry(self.registry_version, list(candidate.values()))
        self._entries = candidate

    def resolve(self, skill_id: str, *, scope: str, health: dict[str, str] | None = None) -> dict[str, Any]:
        return resolve_skill(self.manifest(), skill_id, scope=scope, health=health)

    def manifest(self) -> dict[str, Any]:
        return build_registry(self.registry_version, list(self._entries.values()))


# The explicit names keep control-plane metadata distinct from the executable
# AnalysisSkill registry while making the contract easy to discover.
SkillRegistry = SkillRegistryCatalog
SkillRegistrySpec = SkillRegistryCatalog


def install_qualification() -> dict[str, Any]:
    manifest = build_registry("install/v1", [{
        "skill_id": "financial_growth", "available_versions": ["fixture/v1"], "current_version": "fixture/v1",
        "provider": "fixture", "capabilities": ["growth"], "enabled": True,
        "dependencies": [], "fallback": None, "cost_tier": "low", "latency_class": "fast",
        "critical": True, "applies_to": ["m1_research"], "health": "ready",
    }])
    receipt = resolve_skill(manifest, "financial_growth", scope="m1_research", health={"financial_growth": "ready"})
    replay = frozen_replay(manifest, receipt)
    return {"contract": "SkillRegistryInstallQualification/v1", "qualified": receipt["status"] == "ready" and replay["qualification"]["valid"], "replay_sha256": sha256(replay), "evaluation_vector": replay["evaluation_vector"]}


if __name__ == "__main__":
    print(canonical_json(install_qualification()))
