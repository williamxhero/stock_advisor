from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from ai_trading_companion.analysis_skill import AnalysisSkill
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.skill_registry import (
    CONTRACT,
    SkillRegistryCatalog,
    build_registry,
    dependents,
    frozen_replay,
    install_qualification,
    resolve_skill,
    sha256,
    validate_registry,
)
from ai_trading_companion.store import CompanionStore


def entry(
    skill_id: str,
    *,
    provider: str | None = None,
    capabilities: tuple[str, ...] = ("sentiment",),
    dependencies: tuple[str, ...] = (),
    fallback: str | None = None,
    enabled: bool = True,
    health: str = "ready",
    critical: bool = False,
) -> dict[str, object]:
    return {
        "skill_id": skill_id,
        "available_versions": ["v1", "v2"],
        "current_version": "v2",
        "provider": provider or skill_id,
        "capabilities": list(capabilities),
        "enabled": enabled,
        "dependencies": list(dependencies),
        "fallback": fallback,
        "cost_tier": "low",
        "latency_class": "fast",
        "critical": critical,
        "applies_to": ["m1_research"],
        "health": health,
    }


def test_registry_manifest_is_versioned_read_only_and_schema_valid() -> None:
    manifest = build_registry("registry/v2", [entry("sentiment")], source="runtime-config", as_of="2026-10-05T01:00:00Z")
    assert manifest["contract"] == CONTRACT
    assert manifest["permissions"] == {"write_permissions": []}
    root = Path(__file__).parents[2]
    schema = json.loads((root / "resources/contracts/skill-registry-spec-v1.schema.json").read_text(encoding="utf-8"))
    assert list(Draft202012Validator(schema).iter_errors(manifest)) == []
    assert validate_registry(copy.deepcopy(manifest)) == manifest


def test_registry_normalizes_order_and_reports_dependency_impact() -> None:
    manifest = build_registry("registry/v1", [
        entry("m1", dependencies=("sentiment",), critical=True),
        entry("sentiment"),
    ])
    assert [item["skill_id"] for item in manifest["skills"]] == ["m1", "sentiment"]
    assert dependents(manifest, "sentiment") == ["m1"]
    receipt = resolve_skill(manifest, "m1", scope="m1_research")
    assert receipt["status"] == "ready"
    assert receipt["selected_provider"] == "m1"
    assert receipt["impacted_skills"] == []
    assert receipt["dependencies"][0]["requested_skill"] == "sentiment"


def test_disabled_or_unhealthy_skill_uses_compatible_fallback_and_records_path() -> None:
    manifest = build_registry("registry/v1", [
        entry("sentiment", provider="fingpt", fallback="sentiment_fallback", health="unavailable"),
        entry("sentiment_fallback", provider="llm", capabilities=("sentiment",)),
    ])
    receipt = resolve_skill(manifest, "sentiment", scope="m1_research")
    assert receipt["status"] == "ready"
    assert receipt["selected_provider"] == "llm"
    assert receipt["fallback_path"] == ["sentiment", "sentiment_fallback"]
    assert receipt["fallback_candidates"] == [
        {"skill_id": "sentiment", "current_version": "v2", "provider": "fingpt", "enabled": True, "health": "unavailable"},
        {"skill_id": "sentiment_fallback", "current_version": "v2", "provider": "llm", "enabled": True, "health": "ready"},
    ]
    assert receipt["selected_enabled"] is True
    assert receipt["reason"] == "fallback:unavailable"


def test_exhausted_fallbacks_and_nested_dependency_failures_keep_impact_paths() -> None:
    manifest = build_registry("registry/v1", [
        entry("primary", fallback="backup", health="unavailable"),
        entry("backup", health="unavailable"),
        entry("m1", dependencies=("primary",), critical=True),
    ])
    receipt = resolve_skill(manifest, "primary", scope="m1_research")
    assert receipt["selected_skill"] is None
    assert receipt["fallback_path"] == ["primary", "backup"]
    assert dependents(manifest, "backup") == ["m1", "primary"]
    nested = resolve_skill(manifest, "m1", scope="m1_research")
    assert nested["status"] == "blocked"
    assert nested["fallback_path"] == []
    assert nested["impacted_skills"] == ["m1", "primary"]
    dependency = next(item for item in nested["dependencies"] if item["requested_skill"] == "primary")
    assert dependency["fallback_path"] == ["primary", "backup"]
    assert dependency["requested_enabled"] is True
    assert dependency["selected_enabled"] is None


def test_registry_fails_closed_for_invalid_graphs_and_incompatible_fallbacks() -> None:
    with pytest.raises(ValueError, match="unknown dependency"):
        build_registry("registry/v1", [entry("m1", dependencies=("missing",))])
    with pytest.raises(ValueError, match="cycle"):
        build_registry("registry/v1", [entry("a", dependencies=("b",)), entry("b", dependencies=("a",))])
    with pytest.raises(ValueError, match="fallback cycle"):
        build_registry("registry/v1", [
            entry("a", fallback="b"), entry("b", fallback="a"),
        ])
    with pytest.raises(ValueError, match="resolution graph cycle"):
        build_registry("registry/v1", [
            entry("a", dependencies=("b",)), entry("b", fallback="a"),
        ])
    with pytest.raises(ValueError, match="same capabilities"):
        build_registry("registry/v1", [
            entry("primary", fallback="other", capabilities=("sentiment",)),
            entry("other", capabilities=("growth",)),
        ])


def test_critical_unavailable_skill_blocks_and_optional_skill_degrades() -> None:
    manifest = build_registry("registry/v1", [
        entry("critical", critical=True, enabled=False),
        entry("optional", enabled=False),
    ])
    assert resolve_skill(manifest, "critical", scope="m1_research")["status"] == "blocked"
    assert resolve_skill(manifest, "optional", scope="m1_research")["status"] == "degraded"
    assert resolve_skill(manifest, "optional", scope="m1_research")["selected_skill"] is None


def test_catalog_rejects_duplicate_and_exports_immutable_copy() -> None:
    catalog = SkillRegistryCatalog("runtime/v1")
    first = entry("sentiment")
    catalog.register(first)
    first["enabled"] = False
    with pytest.raises(ValueError, match="duplicate"):
        catalog.register(entry("sentiment"))
    manifest = catalog.manifest()
    assert manifest["skills"][0]["enabled"] is True
    assert catalog.manifest() == manifest


def test_frozen_replay_binds_registry_and_resolution_hash() -> None:
    manifest = build_registry("registry/v1", [entry("sentiment")])
    receipt = resolve_skill(manifest, "sentiment", scope="m1_research")
    first = frozen_replay(manifest, receipt)
    second = frozen_replay(copy.deepcopy(manifest), copy.deepcopy(receipt))
    assert first == second
    assert first["registry_sha256"] == sha256(manifest)
    changed = copy.deepcopy(receipt)
    changed["selected_provider"] = "other"
    with pytest.raises(ValueError, match="does not match"):
        frozen_replay(manifest, changed)


def test_engine_registered_execution_binds_selected_provider_and_version(tmp_path: Path) -> None:
    manifest = build_registry("runtime/v1", [
        entry("sentiment", provider="fingpt", capabilities=("sentiment",)),
        entry("sentiment_fallback", provider="llm", capabilities=("sentiment",)),
    ])
    engine = CompanionEngine(CompanionStore(tmp_path / "companion.sqlite3"), skill_registry_spec=manifest)
    engine.register_analysis_skill(AnalysisSkill("fingpt", "v2", ("sentiment",), ("text",), "probabilistic", lambda inputs: {"provider": "fingpt", "text": inputs["text"]}))
    engine.register_analysis_skill(AnalysisSkill("llm", "v2", ("sentiment",), ("text",), "probabilistic", lambda inputs: {"provider": "llm", "text": inputs["text"]}))
    resolved = engine.execute_registered_analysis_skill("sentiment", {"text": "公告"}, as_of="2026-10-05T01:00:00Z", scope="m1_research")
    assert resolved["resolution"]["selected_provider"] == "fingpt"
    assert resolved["result"]["data"]["provider"] == "fingpt"
    assert resolved["result"]["skill_version"] == "v2"


def test_engine_rejects_provider_replacement_with_changed_input_contract(tmp_path: Path) -> None:
    manifest = build_registry("runtime/v1", [entry("sentiment", provider="fingpt")])
    engine = CompanionEngine(CompanionStore(tmp_path / "companion.sqlite3"), skill_registry_spec=manifest)
    engine.register_analysis_skill(AnalysisSkill("fingpt", "v2", ("sentiment",), ("text",), "probabilistic", lambda inputs: {}))
    replacement = entry("sentiment", provider="llm")
    with pytest.raises(ValueError, match="AnalysisSkill contract"):
        engine.replace_analysis_skill(
            AnalysisSkill("llm", "v2", ("sentiment",), ("headline",), "probabilistic", lambda inputs: {}),
            registry_entry=replacement,
        )
    assert engine.analysis_skills.resolve("fingpt").required_inputs == ("text",)
    assert engine.skill_registry_spec["skills"][0]["provider"] == "fingpt"


def test_engine_replaces_provider_without_changing_capability_contract(tmp_path: Path) -> None:
    manifest = build_registry("runtime/v1", [entry("sentiment", provider="fingpt")])
    engine = CompanionEngine(CompanionStore(tmp_path / "companion.sqlite3"), skill_registry_spec=manifest)
    engine.register_analysis_skill(AnalysisSkill("fingpt", "v2", ("sentiment",), ("text",), "probabilistic", lambda inputs: {"provider": "fingpt"}))
    replacement = entry("sentiment", provider="llm")
    engine.replace_analysis_skill(
        AnalysisSkill("llm", "v2", ("sentiment",), ("text",), "probabilistic", lambda inputs: {"provider": "llm"}),
        registry_entry=replacement,
    )
    result = engine.execute_registered_analysis_skill(
        "sentiment", {"text": "公告"}, as_of="2026-10-05T01:00:00Z", scope="m1_research",
    )
    assert result["resolution"]["requested_skill"] == "sentiment"
    assert result["resolution"]["selected_provider"] == "llm"
    assert result["result"]["data"]["provider"] == "llm"
    with pytest.raises(ValueError, match="not registered"):
        engine.analysis_skills.resolve("fingpt")


def test_engine_uses_runtime_health_to_select_and_audit_fallback(tmp_path: Path) -> None:
    manifest = build_registry("runtime/v2", [
        entry("sentiment", provider="fingpt", capabilities=("sentiment",), fallback="sentiment_fallback"),
        entry("sentiment_fallback", provider="llm", capabilities=("sentiment",)),
    ])
    engine = CompanionEngine(CompanionStore(tmp_path / "companion.sqlite3"), skill_registry_spec=manifest)
    engine.register_analysis_skill(AnalysisSkill(
        "fingpt", "v2", ("sentiment",), ("text",), "probabilistic",
        lambda inputs: {"provider": "fingpt"}, healthcheck=lambda: {"state": "unavailable"},
    ))
    engine.register_analysis_skill(AnalysisSkill("llm", "v2", ("sentiment",), ("text",), "probabilistic", lambda inputs: {"provider": "llm"}))
    result = engine.execute_registered_analysis_skill(
        "sentiment", {"text": "公告"}, as_of="2026-10-05T01:00:00Z", scope="m1_research",
    )
    assert result["resolution"]["requested_enabled"] is True
    assert result["resolution"]["selected_provider"] == "llm"
    assert result["resolution"]["selected_version"] == "v2"
    assert result["resolution"]["fallback_path"] == ["sentiment", "sentiment_fallback"]
    assert result["result"]["data"]["provider"] == "llm"


def test_engine_persists_frozen_registry_resolution_for_cycle(tmp_path: Path) -> None:
    manifest = build_registry("runtime/v1", [entry("sentiment", provider="fingpt")])
    store = CompanionStore(tmp_path / "companion.sqlite3")
    engine = CompanionEngine(store, skill_registry_spec=manifest)
    engine.register_analysis_skill(AnalysisSkill("fingpt", "v2", ("sentiment",), ("text",), "probabilistic", lambda inputs: {"sentiment": "neutral"}))
    cycle = engine.start_cycle("daily.execution.0945", "2026-10-05T09:45:00+08:00", "2026-10-05T01:45:00Z")
    result = engine.execute_registered_analysis_skill(
        "sentiment", {"text": "公告"}, as_of=cycle["as_of"], scope="m1_research", cycle_id=cycle["cycle_id"],
    )
    artifact = result["audit_artifact"]
    assert artifact is not None
    stored = store.latest_artifact(cycle["cycle_id"], "skill_registry_resolution")
    frozen = json.loads(stored["body_markdown"])
    metadata = json.loads(stored["metadata_json"])
    assert frozen["registry"] == manifest
    assert frozen["resolution"] == result["resolution"]
    assert metadata["requested_enabled"] is True
    assert metadata["selected_enabled"] is True
    assert metadata["registry_sha256"] == sha256(manifest)
    engine.replace_analysis_skill(
        AnalysisSkill("llm", "v2", ("sentiment",), ("text",), "probabilistic", lambda inputs: {"sentiment": "positive"}),
        registry_entry=entry("sentiment", provider="llm"),
    )
    assert frozen_replay(frozen["registry"], frozen["resolution"])["qualification"]["valid"] is True


def test_installed_registry_qualification_is_deterministic() -> None:
    qualification = install_qualification()
    assert qualification["contract"] == "SkillRegistryInstallQualification/v1"
    assert qualification["qualified"] is True
