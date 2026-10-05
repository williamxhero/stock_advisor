"""Runtime-owned FinRobotAdapterSpec/v1 deterministic financial computations.

Only structured, sourced inputs enter the operators. Providers may explain a
receipt, never supply missing facts or replace a registered calculation. Decimal
strings preserve precision across JSON, installations and frozen replay.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from types import MappingProxyType
from typing import Any

CONTRACT = "FinRobotAdapterSpec/v1"
VERSION = 1
RESULT_CONTRACT = "FinRobotAdapterResult/v1"
REPLAY_CONTRACT = "FinRobotAdapterReplay/v1"
CODE_VERSION = "finrobot-deterministic/v1"
STAGES = frozenset({"m0_research", "m0_compose", "m1_research", "m1_judgment", "m2", "chat_research", "chat", "reflection", "outcome_research"})
_PERMISSIONS = {"write_permissions": []}
_QUANTRESEARCH = {"access": "read_only", "write_permissions": []}
_DECIMAL = re.compile(r"^-?(?:0|[1-9][0-9]{0,23})(?:\.[0-9]{1,12})?$")
_CURRENCY = re.compile(r"^currency:[A-Z]{3}(?:/share)?$")


@dataclass(frozen=True)
class Formula:
    formula_id: str
    version: str
    expression: str
    required_fields: tuple[str, ...]
    output_unit: str
    decimal_places: int


FORMULAS = MappingProxyType({item.formula_id: item for item in (
    Formula("growth", "v1", "(current - previous) / previous", ("current", "previous"), "ratio", 6),
    Formula("valuation", "v1", "price / eps", ("price", "eps"), "multiple", 6),
    Formula("peer_comparison", "v1", "company_pe / peer_pe - 1", ("company_pe", "peer_pe"), "ratio", 6),
    Formula("earnings_quality", "v1", "operating_cash_flow / net_income", ("operating_cash_flow", "net_income"), "ratio", 6),
    Formula("sensitivity", "v1", "eps * (1 + eps_shock) * pe * (1 + pe_shock)", ("eps", "pe", "eps_shock", "pe_shock"), "currency_per_share", 4),
)})


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _exact(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"FinRobot {name} fields are not exact")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 300 or value != value.strip():
        raise ValueError(f"FinRobot {name} must be bounded text")
    return value


def _timestamp(value: Any) -> datetime:
    text = _text(value, "timestamp")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("FinRobot timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("FinRobot timestamp must be timezone-aware")
    return parsed


def _number(value: Any) -> Decimal:
    # Decimal strings are preferred, while JSON integer/float inputs remain
    # accepted for callers that cannot preserve a decimal lexical form.  The
    # conversion goes through str(value), never through binary arithmetic.
    if isinstance(value, bool):
        raise ValueError("FinRobot value must be a decimal")
    if isinstance(value, (int, float, Decimal)):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            raise ValueError("FinRobot value must be finite")
        if isinstance(value, Decimal) and not value.is_finite():
            raise ValueError("FinRobot value must be finite")
        return Decimal(str(value))
    if not isinstance(value, str) or not _DECIMAL.fullmatch(value):
        raise ValueError("FinRobot value must be a bounded decimal")
    return Decimal(value)


def _period(value: Any, cutoff: datetime) -> dict[str, Any]:
    _exact(value, {"kind", "start", "end"}, "period")
    if value["kind"] not in {"annual", "quarter", "ttm", "instant", "scenario"}:
        raise ValueError("FinRobot period kind is unsupported")
    try:
        start, end = date.fromisoformat(value["start"]), date.fromisoformat(value["end"])
    except (TypeError, ValueError) as exc:
        raise ValueError("FinRobot period dates are invalid") from exc
    if start > end or end > cutoff.date():
        raise ValueError("FinRobot period is inverted or future-dated")
    days = (end - start).days + 1
    kind = value["kind"]
    if (kind in {"instant", "scenario"} and days != 1
            or kind in {"annual", "ttm"} and days not in {365, 366}
            or kind == "quarter" and not 89 <= days <= 92):
        raise ValueError("FinRobot period duration does not match kind")
    return value


def _fact(value: Any, cutoff: datetime, *, scenario: bool) -> dict[str, Any]:
    _exact(value, {"value", "unit", "period", "instrument", "source", "source_ref", "as_of", "known_at"}, "fact")
    if value["value"] is not None:
        _number(value["value"])
    _text(value["instrument"], "instrument")
    _text(value["source_ref"], "source_ref")
    if value["source"] != ("runtime_scenario" if scenario else "markethub"):
        raise ValueError("FinRobot facts must be MarketHub-owned; LLM-fabricated inputs are forbidden")
    prefix = "runtime:scenario:" if scenario else "markethub:"
    if not value["source_ref"].startswith(prefix):
        raise ValueError("FinRobot source reference is untraceable")
    unit = value["unit"]
    if not isinstance(unit, str) or unit not in {"ratio", "multiple"} and not _CURRENCY.fullmatch(unit):
        raise ValueError("FinRobot unit is unsupported")
    as_of, known_at = _timestamp(value["as_of"]), _timestamp(value["known_at"])
    if as_of > known_at or known_at > cutoff:
        raise ValueError("FinRobot input is unavailable at the frozen cutoff")
    _period(value["period"], as_of)
    if scenario and (unit != "ratio" or value["period"]["kind"] != "scenario"):
        raise ValueError("FinRobot scenario inputs require ratio units and scenario periods")
    return value


def _formula(value: Any) -> Formula:
    _exact(value, {"id", "version"}, "formula")
    formula = FORMULAS.get(value["id"]) if isinstance(value["id"], str) else None
    if formula is None or value["version"] != formula.version:
        raise ValueError("FinRobot formula version is unsupported")
    return formula


def build_input(
    formula_id: str, facts: dict[str, Any], *, as_of: str, instrument: str,
    stage: str = "m0_compose", cycle_id: str | None = None, request_id: str | None = None,
    formula_version: str = "v1",
) -> dict[str, Any]:
    value = {
        "contract": CONTRACT, "version": VERSION, "formula": {"id": formula_id, "version": formula_version},
        "facts": copy.deepcopy(facts), "instrument": instrument, "stage": stage,
        "permissions": copy.deepcopy(_PERMISSIONS), "quantresearch": copy.deepcopy(_QUANTRESEARCH),
        "provenance": {"source": "runtime", "as_of": as_of, "cycle_id": cycle_id, "request_id": request_id},
    }
    value["sha256"] = sha256(value)
    return validate_input(value)


def validate_input(value: dict[str, Any]) -> dict[str, Any]:
    _exact(value, {"contract", "version", "formula", "facts", "instrument", "stage", "permissions", "quantresearch", "provenance", "sha256"}, "input")
    if value["contract"] != CONTRACT or type(value["version"]) is not int or value["version"] != VERSION or value["stage"] not in STAGES:
        raise ValueError("FinRobot input identity is unsupported")
    formula = _formula(value["formula"])
    _text(value["instrument"], "instrument")
    if value["permissions"] != _PERMISSIONS or value["quantresearch"] != _QUANTRESEARCH:
        raise ValueError("FinRobot permissions must be read-only")
    provenance = _exact(value["provenance"], {"source", "as_of", "cycle_id", "request_id"}, "provenance")
    if provenance["source"] != "runtime":
        raise ValueError("FinRobot provenance must be Runtime-owned")
    cutoff = _timestamp(provenance["as_of"])
    for key in ("cycle_id", "request_id"):
        if provenance[key] is not None:
            _text(provenance[key], key)
    facts = value["facts"]
    if not isinstance(facts, dict) or set(facts) - set(formula.required_fields):
        raise ValueError("FinRobot facts contain unknown fields (LLM substitution is forbidden)")
    for key, fact in facts.items():
        _fact(fact, cutoff, scenario=formula.formula_id == "sensitivity" and key in {"eps_shock", "pe_shock"})
        if fact["instrument"] != value["instrument"] and not (formula.formula_id == "peer_comparison" and key == "peer_pe"):
            raise ValueError("FinRobot fact instrument mismatch")
    if value["sha256"] != sha256({key: item for key, item in value.items() if key != "sha256"}):
        raise ValueError("FinRobot input digest mismatch")
    return value


def _compatibility(formula: Formula, facts: dict[str, Any]) -> str | None:
    units = {key: fact["unit"] for key, fact in facts.items()}
    periods = {key: fact["period"] for key, fact in facts.items()}
    name = formula.formula_id
    if name in {"growth", "earnings_quality"}:
        if len(set(units.values())) != 1 or not _CURRENCY.fullmatch(next(iter(units.values()))) or next(iter(units.values())).endswith("/share"):
            return "incompatible_units"
        if any(period["kind"] not in {"annual", "quarter", "ttm"} for period in periods.values()):
            return "incompatible_periods"
        if name == "earnings_quality" and periods["operating_cash_flow"] != periods["net_income"]:
            return "incompatible_periods"
        if name == "growth":
            current, previous = periods["current"], periods["previous"]
            if current["kind"] != previous["kind"] or previous["end"] >= current["start"]:
                return "incompatible_periods"
            # v1 growth compares adjacent, non-overlapping periods, not arbitrary
            # historical points that could silently change the growth horizon.
            if (date.fromisoformat(current["start"]) - date.fromisoformat(previous["end"])).days != 1:
                return "incompatible_periods"
    elif name == "valuation":
        if not _CURRENCY.fullmatch(units["price"]) or not units["price"].endswith("/share") or units["price"] != units["eps"]:
            return "incompatible_units"
        if periods["price"]["kind"] != "instant" or periods["eps"]["kind"] != "ttm":
            return "incompatible_periods"
    elif name == "peer_comparison":
        if set(units.values()) != {"multiple"}:
            return "incompatible_units"
        if periods["company_pe"] != periods["peer_pe"] or periods["company_pe"]["kind"] != "instant":
            return "incompatible_periods"
    elif name == "sensitivity":
        if not _CURRENCY.fullmatch(units["eps"]) or not units["eps"].endswith("/share") or units["pe"] != "multiple":
            return "incompatible_units"
        if periods["eps"]["kind"] != "ttm" or periods["pe"]["kind"] != "instant" or periods["eps_shock"] != periods["pe_shock"]:
            return "incompatible_periods"
    return None


def compute(input_contract: dict[str, Any]) -> dict[str, Any]:
    frozen = copy.deepcopy(validate_input(input_contract))
    formula = _formula(frozen["formula"])
    facts = frozen["facts"]
    missing = [key for key in formula.required_fields if key not in facts or facts[key]["value"] is None]
    reason = "missing_required_inputs" if missing else _compatibility(formula, facts)
    result_value = None
    if reason is None:
        numbers = {key: _number(fact["value"]) for key, fact in facts.items()}
        denominator = {"growth": "previous", "valuation": "eps", "peer_comparison": "peer_pe", "earnings_quality": "net_income"}.get(formula.formula_id)
        if denominator and numbers[denominator] <= 0:
            reason = "nonpositive_denominator"
        elif formula.formula_id == "valuation" and numbers["price"] <= 0:
            reason = "nonpositive_price"
        elif formula.formula_id == "peer_comparison" and numbers["company_pe"] <= 0:
            reason = "nonpositive_multiple"
        elif formula.formula_id == "sensitivity" and (numbers["eps"] <= 0 or numbers["pe"] <= 0 or numbers["eps_shock"] < -1 or numbers["pe_shock"] < -1):
            reason = "invalid_scenario_domain"
        else:
            with localcontext() as context:
                context.prec = 80
                context.rounding = ROUND_HALF_EVEN
                if formula.formula_id == "growth":
                    result = (numbers["current"] - numbers["previous"]) / numbers["previous"]
                elif formula.formula_id == "valuation":
                    result = numbers["price"] / numbers["eps"]
                elif formula.formula_id == "peer_comparison":
                    result = numbers["company_pe"] / numbers["peer_pe"] - 1
                elif formula.formula_id == "earnings_quality":
                    result = numbers["operating_cash_flow"] / numbers["net_income"]
                else:
                    result = numbers["eps"] * (1 + numbers["eps_shock"]) * numbers["pe"] * (1 + numbers["pe_shock"])
                try:
                    rounded = result.quantize(Decimal(1).scaleb(-formula.decimal_places))
                    result_value = format(abs(rounded) if rounded == 0 else rounded, "f")
                except InvalidOperation:
                    reason = "precision_overflow"
    unit = facts.get("eps", {}).get("unit") if formula.output_unit == "currency_per_share" else formula.output_unit
    receipt = {
        "contract": RESULT_CONTRACT, "version": VERSION, "spec_contract": CONTRACT,
        "state": "NOT_COMPUTABLE" if reason else "COMPUTED", "value": result_value,
        "unit": unit, "reason": reason, "missing_fields": missing, "input": frozen,
        "formula": {"id": formula.formula_id, "version": formula.version, "expression": formula.expression},
        "precision": {"decimal_places": formula.decimal_places, "rounding": "ROUND_HALF_EVEN", "working_digits": 80},
        "permissions": copy.deepcopy(_PERMISSIONS), "quantresearch": copy.deepcopy(_QUANTRESEARCH),
        "provenance": {"source": "runtime", "as_of": frozen["provenance"]["as_of"],
                       "code_version": CODE_VERSION, "input_sha256": frozen["sha256"],
                       "input_sources": {key: {"source_ref": fact["source_ref"], "as_of": fact["as_of"], "known_at": fact["known_at"], "sha256": sha256(fact)} for key, fact in facts.items()}},
    }
    receipt["sha256"] = sha256(receipt)
    return receipt


def validate_output(value: dict[str, Any], *, input_contract: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("input"), dict):
        raise ValueError("FinRobot result must contain traceable input")
    expected = compute(input_contract if input_contract is not None else value["input"])
    if value != expected:
        raise ValueError("FinRobot result differs from deterministic operator (LLM substitution is forbidden)")
    return value


def validate_explanation(value: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    validate_output(receipt)
    _exact(value, {"output_kind", "summary", "result_sha256"}, "explanation")
    if value["output_kind"] != "interpretation" or value["result_sha256"] != receipt["sha256"]:
        raise ValueError("FinRobot LLM may only explain a bound result")
    _text(value["summary"], "summary")
    return value


def qualify_output(value: dict[str, Any]) -> dict[str, Any]:
    validate_output(value)
    return {"passed": True, "deterministic": True, "read_only": True, "state": value["state"]}


def build_analysis_skill() -> Any:
    """Return the Runtime AnalysisSkill registration for this contract."""
    from .analysis_skill import AnalysisSkill

    def execute(data: dict[str, Any]) -> dict[str, Any]:
        required = {"formula_id", "facts", "instrument", "as_of"}
        if not isinstance(data, dict) or set(data) != required:
            raise ValueError("FinRobot skill input fields are not exact")
        request = build_input(
            data["formula_id"], data["facts"], as_of=data["as_of"],
            instrument=data["instrument"], stage="m0_research",
        )
        return compute(request)

    def validate_skill(data: dict[str, Any]) -> None:
        if not isinstance(data, dict) or set(data) != {"formula_id", "facts", "instrument", "as_of"}:
            raise ValueError("FinRobot skill input fields are not exact")

    return AnalysisSkill(
        "deterministic_finance_v1", "FinRobotAdapterSpec/v1",
        ("growth", "valuation", "peer_comparison", "earnings_quality", "sensitivity"),
        ("formula_id", "facts", "instrument", "as_of"), "deterministic", execute, validate_skill,
    )


def frozen_replay(input_contract: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
    source_input, source_output = copy.deepcopy(input_contract), copy.deepcopy(output)
    validate_output(source_output, input_contract=source_input)
    replay = {
        "contract": REPLAY_CONTRACT, "version": VERSION,
        "source_input": source_input, "source_output": source_output,
        "source_input_sha256": source_input["sha256"], "source_output_sha256": source_output["sha256"],
        "qualification": qualify_output(source_output),
        "evaluation_vector": {
            "delivery_speed": {"status": "not_measured_in_frozen_replay"},
            "qualification_probability": {"status": "not_estimated_in_frozen_replay"},
            "research_quality": {"status": "not_measured_in_frozen_replay"},
            "judgment_outcome": {"status": "adapter_not_a_judgment"},
            "safety_reliability": {"status": "pass", "deterministic": True, "write_permissions": []},
        },
    }
    replay["sha256"] = sha256(replay)
    return replay


def validate_replay(value: dict[str, Any]) -> dict[str, Any]:
    required = {
        "contract", "version", "source_input", "source_output", "source_input_sha256",
        "source_output_sha256", "qualification", "evaluation_vector", "sha256",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("FinRobot replay fields are not exact")
    if value["contract"] != REPLAY_CONTRACT or value["version"] != VERSION:
        raise ValueError("unsupported FinRobot replay identity")
    source_input = validate_input(value["source_input"])
    source_output = validate_output(value["source_output"], input_contract=source_input)
    if value["source_input_sha256"] != source_input["sha256"] or value["source_output_sha256"] != source_output["sha256"]:
        raise ValueError("FinRobot replay provenance mismatch")
    if value["sha256"] != sha256({key: item for key, item in value.items() if key != "sha256"}):
        raise ValueError("FinRobot replay digest mismatch")
    qualification = value["qualification"]
    if not isinstance(qualification, dict) or qualification.get("passed") is not True or qualification.get("deterministic") is not True:
        raise ValueError("FinRobot replay qualification mismatch")
    if not isinstance(value["evaluation_vector"], dict) or set(value["evaluation_vector"]) != {
        "delivery_speed", "qualification_probability", "research_quality", "judgment_outcome", "safety_reliability",
    }:
        raise ValueError("FinRobot replay evaluation vector is incomplete")
    return value


# Descriptive aliases mirror the other versioned Runtime contracts.
build_output = compute
validate = validate_output
replay = frozen_replay


def install_qualification() -> dict[str, Any]:
    cutoff = "2026-10-06T01:45:00Z"
    fact = {"value": "120", "unit": "currency:CNY", "instrument": "000001",
            "source": "markethub", "source_ref": "markethub:install:revenue", "as_of": cutoff, "known_at": cutoff,
            "period": {"kind": "annual", "start": "2025-01-01", "end": "2025-12-31"}}
    previous = copy.deepcopy(fact)
    previous.update(value="100", period={"kind": "annual", "start": "2024-01-01", "end": "2024-12-31"})
    value = build_input("growth", {"current": fact, "previous": previous}, as_of=cutoff, instrument="000001", cycle_id="finrobot-install")
    result = compute(value)
    replay = frozen_replay(value, result)
    missing = build_input("valuation", {}, as_of=cutoff, instrument="000001")
    missing_replay = frozen_replay(missing, compute(missing))
    checks = {"computed": result["value"] == "0.200000", "missing_not_guessed": missing_replay["source_output"]["state"] == "NOT_COMPUTABLE",
              "frozen_replay": replay == frozen_replay(value, result), "read_only": result["permissions"] == _PERMISSIONS}
    return {"contract": "FinRobotAdapterInstallQualification/v1", "qualified": all(checks.values()), "checks": checks,
            "replay_sha256": replay["sha256"], "missing_replay_sha256": missing_replay["sha256"], "evaluation_vector": replay["evaluation_vector"]}


if __name__ == "__main__":
    print(canonical_json(install_qualification()))
