from __future__ import annotations

import copy
import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.__main__ import _run_finrobot_calculations
from ai_trading_companion.finrobot_adapter import (
    CODE_VERSION,
    CONTRACT,
    FORMULAS,
    RESULT_CONTRACT,
    build_analysis_skill,
    build_input,
    compute,
    frozen_replay,
    install_qualification,
    validate_explanation,
    validate_input,
    validate_output,
)
from ai_trading_companion.store import CompanionStore


ROOT = Path(__file__).resolve().parents[2]
CUTOFF = "2026-10-06T01:45:00Z"


def period(kind: str = "ttm", start: str = "2025-01-01", end: str = "2025-12-31") -> dict[str, str]:
    return {"kind": kind, "start": start, "end": end}


def fact(
    value: str | None, *, unit: str = "currency:CNY", name: str = "revenue",
    instrument: str = "000001", source: str = "markethub", source_ref: str | None = None,
    fact_period: dict[str, str] | None = None, as_of: str = CUTOFF,
) -> dict[str, object]:
    return {
        "value": value, "unit": unit, "period": fact_period or period(), "instrument": instrument,
        "source": source, "source_ref": source_ref or f"markethub:financials:{name}",
        "as_of": as_of, "known_at": as_of,
    }


def growth_input() -> dict[str, object]:
    return build_input(
        "growth",
        {
            "current": fact("120", name="revenue-current", fact_period=period("annual", "2025-01-01", "2025-12-31")),
            "previous": fact("100", name="revenue-previous", fact_period=period("annual", "2024-01-01", "2024-12-31")),
        },
        as_of=CUTOFF, instrument="000001", cycle_id="cycle-1",
    )


def valuation_input() -> dict[str, object]:
    return build_input(
        "valuation",
        {
            "price": fact("12.00", unit="currency:CNY/share", name="price", fact_period=period("instant", "2026-10-06", "2026-10-06")),
            "eps": fact("2.00", unit="currency:CNY/share", name="eps"),
        },
        as_of=CUTOFF, instrument="000001",
    )


def test_formula_registry_is_versioned_and_complete() -> None:
    assert set(FORMULAS) == {"growth", "valuation", "peer_comparison", "earnings_quality", "sensitivity"}
    assert all(formula.version == "v1" and formula.required_fields for formula in FORMULAS.values())
    assert all(formula.expression and formula.decimal_places >= 0 for formula in FORMULAS.values())


def test_growth_is_decimal_and_traceable() -> None:
    result = compute(growth_input())
    assert result["contract"] == RESULT_CONTRACT
    assert result["state"] == "COMPUTED"
    assert result["value"] == "0.200000"
    assert result["formula"] == {"id": "growth", "version": "v1", "expression": "(current - previous) / previous"}
    assert result["precision"] == {"decimal_places": 6, "rounding": "ROUND_HALF_EVEN", "working_digits": 80}
    assert result["provenance"]["code_version"] == CODE_VERSION
    assert set(result["provenance"]["input_sources"]) == {"current", "previous"}


def test_valuation_uses_declared_units_and_periods() -> None:
    result = compute(valuation_input())
    assert result["value"] == "6.000000"
    assert result["unit"] == "multiple"


@pytest.mark.parametrize("formula_id", ["valuation", "peer_comparison", "earnings_quality", "sensitivity"])
def test_missing_required_inputs_are_not_computable(formula_id: str) -> None:
    values: dict[str, object] = {}
    result = compute(build_input(formula_id, values, as_of=CUTOFF, instrument="000001"))
    assert result["state"] == "NOT_COMPUTABLE"
    assert result["value"] is None
    assert result["reason"] == "missing_required_inputs"
    assert result["missing_fields"] == list(FORMULAS[formula_id].required_fields)


def test_missing_value_is_not_guessed() -> None:
    values = {"current": fact(None, name="revenue-current", fact_period=period("annual", "2025-01-01", "2025-12-31")),
              "previous": fact("100", name="revenue-previous", fact_period=period("annual", "2024-01-01", "2024-12-31"))}
    result = compute(build_input("growth", values, as_of=CUTOFF, instrument="000001"))
    assert result["state"] == "NOT_COMPUTABLE"
    assert result["missing_fields"] == ["current"]


def test_zero_denominator_is_not_computable() -> None:
    values = {"current": fact("100", name="revenue-current", fact_period=period("annual", "2025-01-01", "2025-12-31")),
              "previous": fact("0", name="revenue-previous", fact_period=period("annual", "2024-01-01", "2024-12-31"))}
    result = compute(build_input("growth", values, as_of=CUTOFF, instrument="000001"))
    assert result["state"] == "NOT_COMPUTABLE"
    assert result["reason"] == "nonpositive_denominator"


def test_untraceable_source_reference_is_rejected() -> None:
    values = {"current": fact("120", source_ref="model:guess", name="revenue-current", fact_period=period("annual", "2025-01-01", "2025-12-31")),
              "previous": fact("100", name="revenue-previous", fact_period=period("annual", "2024-01-01", "2024-12-31"))}
    with pytest.raises(ValueError, match="source reference"):
        build_input("growth", values, as_of=CUTOFF, instrument="000001")


def test_llm_fabricated_input_source_is_rejected() -> None:
    values = {"current": fact("120", source="llm", source_ref="llm:answer", name="revenue-current", fact_period=period("annual", "2025-01-01", "2025-12-31")),
              "previous": fact("100", name="revenue-previous", fact_period=period("annual", "2024-01-01", "2024-12-31"))}
    with pytest.raises(ValueError, match="MarketHub-owned|fabricated"):
        build_input("growth", values, as_of=CUTOFF, instrument="000001")


def test_runtime_scenario_requires_explicit_scenario_source_and_period() -> None:
    scenario = fact("0.10", unit="ratio", source="runtime_scenario", source_ref="runtime:scenario:eps", name="eps-shock",
                    fact_period=period("scenario", "2026-10-06", "2026-10-06"))
    assert scenario["source"] == "runtime_scenario"
    values = {"eps": fact("2", unit="currency:CNY/share", name="eps"), "pe": fact("10", unit="multiple", name="pe", fact_period=period("instant", "2026-10-06", "2026-10-06")), "eps_shock": scenario,
              "pe_shock": copy.deepcopy(scenario)}
    values["pe_shock"]["source_ref"] = "runtime:scenario:pe"
    result = compute(build_input("sensitivity", values, as_of=CUTOFF, instrument="000001"))
    assert result["value"] == "24.2000"


def test_scenario_from_llm_is_rejected() -> None:
    with pytest.raises(ValueError, match="MarketHub-owned|fabricated"):
        build_input("sensitivity", {"eps_shock": fact("0.1", source="llm", source_ref="llm:shock", name="eps-shock",
            fact_period=period("scenario", "2026-10-06", "2026-10-06"))}, as_of=CUTOFF, instrument="000001")


@pytest.mark.parametrize("field", ["unit", "period"])
def test_incompatible_units_or_periods_are_not_computable(field: str) -> None:
    values = {"current": fact("120", name="revenue-current", fact_period=period("annual", "2025-01-01", "2025-12-31")),
              "previous": fact("100", name="revenue-previous", fact_period=period("annual", "2024-01-01", "2024-12-31"))}
    if field == "unit":
        values["previous"]["unit"] = "currency:USD"
    else:
        values["previous"]["period"] = period("quarter", "2024-10-01", "2024-12-31")
    result = compute(build_input("growth", values, as_of=CUTOFF, instrument="000001"))
    assert result["state"] == "NOT_COMPUTABLE"
    assert result["reason"] == f"incompatible_{field}s"


def test_future_dated_input_is_rejected() -> None:
    future = fact("100", name="future", as_of="2026-10-07T00:00:00Z", fact_period=period("ttm", "2025-10-07", "2026-10-06"))
    with pytest.raises(ValueError, match="unavailable"):
        build_input("valuation", {"price": future}, as_of=CUTOFF, instrument="000001")


def test_formula_version_tampering_is_rejected() -> None:
    value = growth_input()
    value["formula"]["version"] = "v0"
    value["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="formula version"):
        validate_input(value)


def test_input_digest_tampering_is_rejected() -> None:
    value = growth_input()
    value["facts"]["current"]["value"] = "121"
    with pytest.raises(ValueError, match="digest"):
        validate_input(value)


def test_permissions_and_quantresearch_are_read_only() -> None:
    value = growth_input()
    value["permissions"] = {"write_permissions": ["portfolio"]}
    value["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="read-only"):
        validate_input(value)
    value = growth_input()
    value["quantresearch"] = {"access": "write", "write_permissions": ["strategy"]}
    value["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="read-only"):
        validate_input(value)


def test_llm_cannot_replace_deterministic_result() -> None:
    request = growth_input()
    result = compute(request)
    forged = copy.deepcopy(result)
    forged["value"] = "0.210000"
    with pytest.raises(ValueError, match="deterministic operator"):
        validate_output(forged, input_contract=request)


def test_explanation_must_bind_exact_receipt() -> None:
    result = compute(growth_input())
    valid = {"output_kind": "interpretation", "summary": "Growth is twenty percent.", "result_sha256": result["sha256"]}
    assert validate_explanation(valid, result) == valid
    invalid = {**valid, "result_sha256": "0" * 64}
    with pytest.raises(ValueError, match="bound result"):
        validate_explanation(invalid, result)


def test_frozen_replay_reconstructs_original_receipt() -> None:
    request = growth_input()
    result = compute(request)
    replay = frozen_replay(request, result)
    assert replay == frozen_replay(copy.deepcopy(request), copy.deepcopy(result))
    assert replay["source_input"]["sha256"] == request["sha256"]
    assert replay["source_output"]["sha256"] == result["sha256"]
    assert replay["qualification"]["deterministic"] is True
    assert set(replay["evaluation_vector"]) == {"delivery_speed", "qualification_probability", "research_quality", "judgment_outcome", "safety_reliability"}


def test_replay_rejects_forged_output() -> None:
    request = growth_input()
    result = compute(request)
    result["value"] = "0.3"
    with pytest.raises(ValueError, match="deterministic operator"):
        frozen_replay(request, result)


def test_analysis_skill_registry_is_read_only_and_versioned(tmp_path: Path) -> None:
    skill = build_analysis_skill()
    assert skill.skill_id == "deterministic_finance_v1"
    assert skill.skill_version == CONTRACT
    assert skill.mode == "deterministic"
    store = CompanionStore(tmp_path / "finrobot.sqlite3")
    engine = CompanionEngine(store)
    engine.register_analysis_skill(skill)
    execution = engine.execute_registered_analysis_skill(
        skill.skill_id,
        {"formula_id": "growth", "facts": growth_input()["facts"], "instrument": "000001", "as_of": CUTOFF},
        as_of=CUTOFF, scope="m0_research", cycle_id=None,
    )
    assert execution["result"]["status"] == "succeeded"
    assert execution["result"]["data"]["value"] == "0.200000"
    assert execution["result"]["permissions"] == {"write_permissions": []}


def test_runtime_path_persists_qualified_calculation_before_m0(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "runtime.sqlite3")
    engine = CompanionEngine(store)
    engine.register_analysis_skill(build_analysis_skill())
    cycle = engine.start_cycle("daily.execution.0945", CUTOFF, CUTOFF)
    receipts = _run_finrobot_calculations(
        engine, store, cycle,
        [{"formula_id": "growth", "facts": growth_input()["facts"], "instrument": "000001"}],
    )
    assert len(receipts) == 1
    assert receipts[0]["result"]["state"] == "COMPUTED"
    artifact = store.latest_artifact(cycle["cycle_id"], "derived_calculation")
    assert artifact is not None
    assert artifact["actor"] == "runtime"
    body = json.loads(artifact["body_markdown"])
    assert body["provenance"]["input_sources"]["current"]["source_ref"].startswith("markethub:")


def test_schema_accepts_input_result_and_replay() -> None:
    request = growth_input()
    result = compute(request)
    replay = frozen_replay(request, result)
    schema = json.loads((ROOT / "resources/contracts/finrobot-adapter-spec-v1.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    assert list(validator.iter_errors(request)) == []
    assert list(validator.iter_errors(result)) == []
    assert list(validator.iter_errors(replay)) == []


def test_installation_qualification_is_deterministic() -> None:
    assert install_qualification() == install_qualification()
    assert install_qualification()["qualified"] is True
