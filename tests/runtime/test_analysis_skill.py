from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from ai_trading_companion.analysis_skill import (
    AnalysisSkill,
    SkillRegistry,
    frozen_replay,
    install_qualification,
    validate_output,
)
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.store import CompanionStore


def test_registry_declares_capabilities_executes_validated_output_and_health() -> None:
    registry = SkillRegistry()
    registry.register(AnalysisSkill(
        "financial_growth", "fixture/v1", ("growth",), ("series",), "deterministic",
        lambda inputs: {"growth": inputs["series"][-1] - inputs["series"][0]},
        healthcheck=lambda: {"state": "ready", "latency_class": "low"},
    ))
    result = registry.execute("financial_growth", {"series": [1, 2, 4]}, as_of="2026-09-20T01:00:00Z", cycle_id="cycle-1")
    assert result["contract"] == "AnalysisSkillResult/v1"
    assert result["data"] == {"growth": 3}
    assert registry.healthcheck("financial_growth")["state"] == "ready"
    assert registry.manifest()[0]["mode"] == "deterministic"


def test_skill_rejects_missing_inputs_private_state_and_duplicate_registration() -> None:
    registry = SkillRegistry()
    skill = AnalysisSkill("market_structure", "fixture/v1", ("structure",), ("bars",), "probabilistic", lambda inputs: {"state": "range"})
    registry.register(skill)
    with pytest.raises(ValueError, match="inputs missing"):
        registry.execute("market_structure", {}, as_of="2026-09-20T01:00:00Z")
    with pytest.raises(ValueError, match="protected field"):
        registry.execute("market_structure", {"bars": [], "portfolio": {}}, as_of="2026-09-20T01:00:00Z")
    with pytest.raises(ValueError, match="already registered"):
        registry.register(skill)


def test_skill_failure_is_structured_and_final_judgment_is_forbidden() -> None:
    registry = SkillRegistry()
    registry.register(AnalysisSkill("bad", "fixture/v1", ("x",), (), "probabilistic", lambda _: (_ for _ in ()).throw(RuntimeError("boom"))))
    result = registry.execute("bad", {}, as_of="2026-09-20T01:00:00Z")
    assert result["status"] == "failed"
    assert result["data"]["error"] == "RuntimeError"
    with pytest.raises(ValueError, match="protected field"):
        validate_output({
            "contract": "AnalysisSkillResult/v1", "version": 1, "skill_id": "x", "skill_version": "v1",
            "mode": "deterministic", "status": "succeeded", "data": {"final_judgment": "buy"},
            "provenance": {"input_sha256": "x"}, "permissions": {"write_permissions": []},
        })


@pytest.mark.parametrize("inputs", [{}, {"price": 10}])
def test_deterministic_skill_returns_not_computable_instead_of_guessing(inputs: dict) -> None:
    registry = SkillRegistry()
    registry.register(AnalysisSkill(
        "valuation", "v1", ("valuation",), ("price",), "deterministic",
        lambda _: (_ for _ in ()).throw(ArithmeticError("sensitive provider detail")),
    ))
    result = registry.execute("valuation", inputs, as_of="2026-09-20T01:00:00Z")
    assert result["status"] == "failed"
    assert result["data"]["state"] == "NOT_COMPUTABLE"
    assert result["data"]["value"] is None
    receipt = result["provenance"]["fallback"]
    assert receipt["continuation"] == "blocked"
    assert "sensitive provider detail" not in json.dumps(result)


def test_engine_real_runtime_seam_uses_skill_registry_without_owning_business_state(tmp_path: Path) -> None:
    engine = CompanionEngine(CompanionStore(tmp_path / "companion.sqlite3"))
    engine.register_analysis_skill(AnalysisSkill("valuation", "fixture/v1", ("valuation",), ("price",), "deterministic", lambda inputs: {"fair_value": inputs["price"] * 2}))
    result = engine.execute_analysis_skill("valuation", {"price": 10}, as_of="2026-09-20T01:00:00Z", cycle_id="cycle-1")
    assert result["data"]["fair_value"] == 20
    assert result["permissions"]["write_permissions"] == []


def test_replay_schema_and_install_qualification_are_deterministic() -> None:
    registry = SkillRegistry()
    registry.register(AnalysisSkill("x", "v1", ("x",), (), "deterministic", lambda _: {"value": 1}))
    result = registry.execute("x", {}, as_of="2026-09-20T01:00:00Z")
    request = {
        "contract": "AnalysisSkillSpec/v1", "version": 1, "skill_id": "x", "skill_version": "v1",
        "capabilities": ["x"], "required_inputs": [], "mode": "deterministic", "inputs": {},
        "provenance": {"as_of": "2026-09-20T01:00:00Z", "cycle_id": None}, "permissions": {"write_permissions": []},
    }
    replay = frozen_replay(request, result)
    assert replay == frozen_replay(copy.deepcopy(request), copy.deepcopy(result))
    root = Path(__file__).parents[2]
    schema = json.loads((root / "resources/contracts/analysis-skill-spec-v1.schema.json").read_text(encoding="utf-8"))
    assert list(Draft202012Validator(schema).iter_errors(request)) == []
    qualification = install_qualification()
    assert qualification["contract"] == "AnalysisSkillInstallQualification/v1"
    assert qualification["qualified"] is True
