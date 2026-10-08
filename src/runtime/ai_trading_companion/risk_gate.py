"""Non-compensable Runtime qualification, independent of provider/agent reviews.

The frozen input is deliberately projected, not a copy of H0 or model context.
A receipt grants advice eligibility only, never fact writes or execution rights.
"""
from __future__ import annotations

import copy
import json
import math
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any

from .mandate_spec import sha256, validate_mandate
from .position_safety import (
    CANONICAL_RISK, assert_no_execution, build_input as position_input, build_output as position_output,
    freshness_problems, validate_input as validate_positions,
)

CONTRACT = "RiskGateSpec/v1"
RESULT_CONTRACT = "RiskGateResult/v1"
POLICY = "CanonicalRiskPolicy/v1"
SPEC = {"contract": CONTRACT, "version": 1}
STAGES = frozenset({"m0_compose", "m1_judgment", "m2", "chat", "reflection"})
_FACT_WRITES = frozenset({"memoryhub_write", "write_memory", "evidence_write", "write_evidence",
                          "production_strategy_write", "write_strategy", "schedule_write", "write_schedule"})
_LEVERAGE = re.compile(r"杠杆|融资(?:加仓|买入|交易)|配资|借钱(?:炒股|买入)|margin|leverag", re.I)
_NEGATED = re.compile(r"不(?:认可|建议|应|能|要|使用|因|代表|等于)|禁止|拒绝|避免|不得|不能|不可|no\b|not\b|avoid\b", re.I)
_ADVICE = re.compile(r"建议|可以|应当|应该|我会|认可|推荐|\b(?:recommend|should|allow|use)\b", re.I)
_ADD_RISK = re.compile(r"(?:建议|可以|应当|应该|我会|认可|推荐).{0,12}(?:加仓|买入|扩大敞口|新增风险)|"
                       r"(?:加仓|买入)吧|\b(?:recommend|should|allow).{0,20}(?:buy|add risk)\b", re.I)
_DIRECTION = re.compile(r"(?:我|建议|可以|应该).{0,8}(?:看多|看空|买入|卖出|加仓)|\b(?:bullish|bearish)\b", re.I)


def build_input(packet: dict[str, Any]) -> dict[str, Any]:
    stage, as_of = packet.get("stage"), packet.get("as_of")
    if stage not in STAGES:
        raise ValueError("unsupported risk gate stage")
    mandate = copy.deepcopy(packet.get("mandate"))
    validate_mandate(mandate)
    cutoff = datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))
    mandate_at = datetime.fromisoformat(mandate["provenance"]["as_of"].replace("Z", "+00:00"))
    if cutoff.tzinfo is None or mandate_at.tzinfo is None or mandate["stage"] != stage or mandate_at > cutoff:
        raise ValueError("risk gate mandate identity mismatch")
    positions = copy.deepcopy(packet.get("position_safety"))
    if positions is not None:
        validate_positions(positions)
        if positions["stage"] != stage or positions["as_of"] != as_of:
            raise ValueError("risk gate position identity mismatch")
    value = {
        **SPEC, "cycle_id": packet.get("cycle_id"), "stage": stage, "as_of": as_of,
        "mandate": mandate, "evidence": copy.deepcopy(packet.get("evidence") or {}),
        "evidence_snapshot": copy.deepcopy(packet.get("evidence_snapshot")),
        "position_safety": positions, "policy": {"contract": POLICY, **CANONICAL_RISK},
        "provenance": {"source": "runtime", "mandate_sha256": mandate["sha256"],
                       "evidence_sha256": sha256(packet.get("evidence") or {}),
                       "position_sha256": positions["sha256"] if positions else None},
    }
    value["sha256"] = sha256(value)
    return value


def validate_input(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("contract") != CONTRACT or type(value.get("version")) is not int:
        raise ValueError("unsupported RiskGateSpec")
    packet = {key: value.get(key) for key in (
        "cycle_id", "stage", "as_of", "mandate", "evidence", "evidence_snapshot", "position_safety",
    )}
    if value != build_input(packet):
        raise ValueError("risk gate input policy/provenance/digest mismatch")
    return value


def _nodes(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _nodes(child)
    elif isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            yield from _nodes(json.loads(value))
        except ValueError:
            pass


def _texts(value: Any):
    if isinstance(value, dict):
        for key, child in value.items():
            if key != "publication":
                yield from _texts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _texts(child)
    elif isinstance(value, str):
        yield value


def _advice_candidate(value: Any) -> Any:
    """Project advice for checks, without changing the original published candidate.

    Attributed user quotes and past-tense trade confirmations are not recommendations.
    An advisory continuation remains checked, even beside a factual confirmation.
    """
    if isinstance(value, dict):
        return {key: _advice_candidate(child) for key, child in value.items() if key != "publication"}
    if isinstance(value, list):
        return [_advice_candidate(child) for child in value]
    if not isinstance(value, str):
        return value
    text = re.sub(r'(?:你|您|用户)(?:说|提到|问)[：:]?[“「"].*?[”」"]', "", value)
    clauses = re.split(r"[。；;，\n]", text)
    return "。".join(clause for clause in clauses if not (
        re.match(r"\s*(?:已(?:记录|确认|成交|记下)|你(?:已经|已|当前|目前)|(?:当前|现有)持仓)", clause)
        and not _ADVICE.search(clause)
    ))


def _current_prices(value: dict[str, Any]) -> dict[str, float]:
    """Only frozen, qualified external quote facts can replace cached valuation."""
    positions = value["position_safety"]
    if positions is None or positions["latest_trading_day"] is None:
        return {}
    cutoff = datetime.fromisoformat(value["as_of"].replace("Z", "+00:00"))
    prices: dict[str, set[float]] = {}
    for source in value["evidence"].get("sources") or []:
        qualification = source.get("evidence_qualification") or {}
        if qualification.get("state") != "qualified" or qualification.get("permitted_use") != "external_fact":
            continue
        try:
            payload = json.loads(source.get("excerpt") or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        for quote in payload.get("quotes") or []:
            try:
                at = datetime.fromisoformat(str(quote.get("quote_at") or "").replace("Z", "+00:00"))
                price = quote.get("price")
                code = str(quote.get("symbol") or "")
                if (at.tzinfo is None or at > cutoff
                        or at.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat() != positions["latest_trading_day"]
                        or quote.get("status") not in {"trading", "closed", "suspended"}
                        or isinstance(price, bool) or not isinstance(price, (int, float))
                        or not math.isfinite(price) or price <= 0):
                    continue
            except (AttributeError, TypeError, ValueError):
                continue
            prices.setdefault(code, set()).add(float(price))
    # Conflicting prices are unknown, not an opportunity to choose the favorable one.
    return {code: next(iter(values)) for code, values in prices.items() if len(values) == 1}


def build_output(value: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
    validate_input(value)
    evidence = value["evidence"]
    sources = evidence.get("sources") or []
    stops, directional, reduced, precision, new_risk = [], [], [], [], []
    if value["stage"] in {"m0_compose", "m1_judgment", "m2"}:
        if not sources:
            stops.append("market_evidence_missing")
        elif all((row.get("evidence_qualification") or {}).get("state") in {"rejected", "expired"} for row in sources):
            stops.append("usable_market_evidence_missing")
    try:
        assert_no_execution(output)
    except ValueError:
        stops.append("ownership_or_execution_violation")
    if value["stage"] == "m0_compose":
        directional.append("m0_cannot_advise")
    if any(row.get("blocking") is True and row.get("status") not in {"covered", "qualified"}
           for row in evidence.get("coverage") or []):
        directional.append("blocking_market_fact_missing")
    if evidence.get("critical_gaps"):
        reduced.append("critical_market_unknown")
    unresolved = [row for row in evidence.get("conflicts") or []
                  if row.get("materiality") in {"high", "critical"}
                  and row.get("resolution") not in {"primary_precedence", "scope_difference", "resolved"}]
    if unresolved:
        reduced.append("unresolved_material_conflict")
        if any(row.get("materiality") == "critical" for row in unresolved):
            directional.append("critical_market_conflict")
    if any((row.get("evidence_qualification") or {}).get("state") in {"degraded", "conflicted", "expired", "rejected"}
           for row in sources):
        reduced.append("market_evidence_not_fully_qualified")
    nodes = list(_nodes(output))
    advice = _advice_candidate(output)
    if any(any(key in _FACT_WRITES and child not in (None, [], {}, False) for key, child in row.items())
           or str(row.get("operation", row.get("action_type", row.get("action", "")))) in _FACT_WRITES for row in nodes):
        stops.append("ownership_or_execution_violation")
    clauses = [clause for text in _texts(advice) for clause in re.split(r"[。；;，,\n]", text)]
    if any(_LEVERAGE.search(clause) and _ADVICE.search(clause) and not _NEGATED.search(clause)
           for clause in clauses):
        stops.append("leverage_not_approved")
    positions = value["position_safety"]
    position_receipt = None
    if positions is None:
        precision.append("portfolio_truth_missing")
        unknown = position_input({}, stage=value["stage"], as_of=value["as_of"], source_ref=value["sha256"])
        position_receipt = position_output(unknown, advice, sizing=output.get("sizing_proposal"))
    else:
        precision.extend(freshness_problems(positions))
        prices = _current_prices(value)
        required_codes = {row["code"] for row in positions["truth"]["positions"]}
        if isinstance(output.get("sizing_proposal"), dict):
            required_codes.add(str(output["sizing_proposal"].get("code") or ""))
        precision.extend("current_qualified_price_missing:" + code for code in sorted(required_codes - prices.keys()))
        state, truth = positions["truth"]["risk_state"], positions["truth"]
        themes = state.get("theme_by_code") or {}
        if any(code not in themes for code in required_codes):
            precision.append("theme_exposure_unknown")
        if (state.get("synchronized") is True and state.get("peak_assets") and truth["total_assets"]
                and truth["total_assets"] <= state["peak_assets"] * (1 - CANONICAL_RISK["drawdown_review_threshold"])
                and state.get("review_completed") is not True):
            new_risk.append("drawdown_requires_review")
        valuation = positions
        if output.get("sizing_proposal") is not None:
            priced_truth = copy.deepcopy(truth)
            for row in priced_truth["positions"]:
                row["last_price"] = prices.get(row["code"])
            valuation = position_input(
                priced_truth, stage=value["stage"], as_of=value["as_of"],
                source_ref=value["sha256"] + ":qualified-quotes", latest_session=positions["latest_trading_day"],
            )
        position_receipt = position_output(valuation, advice, sizing=output.get("sizing_proposal"))
    permissions = {"continue": not stops, "direction": not stops and not directional,
                   "precision": not stops and not directional and not precision,
                   "new_risk": not stops and not directional and not reduced and not new_risk,
                   "confidence_ceiling": "low" if reduced or directional else "high", "write_permissions": []}
    problems = list(stops)
    if directional and any(row.get("direction") not in (None, "neutral", "unknown")
                           or row.get("current_action", row.get("action")) == "allow_add_risk" for row in nodes):
        problems.extend(directional)
    if reduced and any(row.get("confidence") in {"medium", "high"} for row in nodes):
        problems.extend(reduced)
    if directional and any(_DIRECTION.search(clause) and not _NEGATED.search(clause) for clause in clauses):
        problems.extend(directional)
    sizing = output.get("sizing_proposal")
    sizing_adds_risk = False
    if isinstance(sizing, dict) and type(sizing.get("target_shares")) is int:
        current = next((row["shares"] for row in positions["truth"]["positions"]
                        if row["code"] == sizing.get("code")), 0) if positions else 0
        sizing_adds_risk = sizing["target_shares"] > current
    adding = (sizing_adds_risk or any(row.get("current_action", row.get("action")) == "allow_add_risk"
                  or row.get("status") == "selected" for row in nodes)
              or any(_ADD_RISK.search(clause) and not _NEGATED.search(clause) for clause in clauses))
    if adding and not permissions["new_risk"]:
        problems.extend([*stops, *directional, *reduced, *new_risk])
    if output.get("sizing_proposal") is not None and not permissions["precision"]:
        problems.extend([*stops, *directional, *precision])
    if position_receipt and position_receipt["state"] == "refused":
        problems.extend(position_receipt["problems"])
    states = (["STOP"] if stops else []) + (["NO_DIRECTION"] if directional else []) + (
        ["REDUCED_CONFIDENCE"] if reduced else []) + (["NO_PRECISION"] if precision else []) + (
        ["NO_NEW_RISK"] if new_risk else [])
    receipt = {"contract": RESULT_CONTRACT, "version": 1, "policy": POLICY,
               "input": copy.deepcopy(value), "source_output": copy.deepcopy(output),
               "state": "refused" if problems else "qualified", "restrictions": states,
               "permissions": permissions, "reasons": {"stop": stops, "no_direction": directional,
                   "reduced_confidence": reduced, "no_precision": precision, "no_new_risk": new_risk},
               "problems": list(dict.fromkeys(problems)), "position_safety": position_receipt,
               "provenance": {"source": "runtime", "input_sha256": value["sha256"], "output_sha256": sha256(output)}}
    receipt["sha256"] = sha256(receipt)
    return receipt


def validate_output(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or type(value.get("version")) is not int or value.get("version") != 1:
        raise ValueError("unsupported risk gate receipt version")
    if value != build_output(value["input"], value["source_output"]):
        raise ValueError("risk gate receipt qualification/digest mismatch")
    return value


def publication_receipt(packet: dict[str, Any], output: dict[str, Any]) -> dict[str, Any] | None:
    if "risk_gate_spec" not in packet:
        return None  # Historical attempts retain their original contract.
    if packet["risk_gate_spec"] != SPEC:
        raise ValueError("unsupported risk gate packet contract")
    return build_output(build_input(packet), output)


def assert_publication(packet: dict[str, Any] | None, output: dict[str, Any], verifier: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(packet, dict):
        return None
    receipt = publication_receipt(packet, output)
    if receipt is not None:
        if receipt["state"] == "refused":
            raise ValueError("risk gate publication refused: " + ", ".join(receipt["problems"]))
        if verifier.get("risk_gate") is not None and verifier["risk_gate"] != receipt:
            raise ValueError("risk gate publication receipt mismatch")
    return receipt
