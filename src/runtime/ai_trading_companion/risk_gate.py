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

from .evidence_spec import fingerprint
from .mandate_spec import sha256, validate_mandate
from .position_safety import (
    CANONICAL_RISK, assert_no_execution, build_input as position_input, build_output as position_output,
    advice_clauses, drawdown_blocked, freshness_problems, negated_advice, validate_input as validate_positions,
)

CONTRACT = "RiskGateSpec/v1"
RESULT_CONTRACT = "RiskGateResult/v1"
POLICY = "CanonicalRiskPolicy/v1"
SPEC = {"contract": CONTRACT, "version": 1}
STAGES = frozenset({"m0_compose", "m1_judgment", "m2", "chat", "reflection"})
_FACT_WRITES = frozenset({"memoryhub_write", "write_memory", "evidence_write", "write_evidence",
                          "production_strategy_write", "write_strategy", "schedule_write", "write_schedule"})
_LEVERAGE = re.compile(r"杠杆|融资(?:加仓|买入|交易)|配资|借钱(?:炒股|买入)|margin|leverag", re.I)
_ADVICE = re.compile(r"建议|可以|应当|应该|我会|认可|推荐|\b(?:recommend|should|allow|use)\b", re.I)
_ADD_RISK = re.compile(r"(?:建议|可以|应当|应该|我会|认可|推荐).{0,12}(?:加仓|买入|扩大敞口|新增风险)|"
                       r"(?:加仓|买入)吧|\b(?:recommend|should|allow).{0,20}(?:buy|add risk)\b", re.I)
_DIRECTION = re.compile(r"(?:我|建议|可以|应该).{0,8}(?:看多|看空|买入|卖出|加仓)|\b(?:bullish|bearish)\b", re.I)
# Bare imperatives are actionable even without an explicit recommendation verb.
_IMPERATIVE = re.compile(
    r"^\s*(?:(?:现在|立即|立刻|马上|直接|请)\s*)?(?:(?:使用|用)杠杆|融资|配资)?"
    r"(?:买入|加仓|扩大敞口|新增风险)(?:\s|\d|吧|$)|"
    r"\b(?:now|immediately)\s+(?:buy|add\s+risk)\b|^\s*(?:buy|add\s+risk)\b", re.I,
)
# Selling/reducing is directional advice, but does not by itself add risk.
_DIRECTIONAL_IMPERATIVE = re.compile(
    r"^\s*(?:(?:现在|立即|立刻|马上|直接|请)\s*)*(?:卖出|减仓)(?:\s|\d|吧|$)|"
    r"^\s*(?:(?:please|now|immediately)\s+)*(?:sell|reduce\s+(?:risk|exposure|position))\b", re.I,
)


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
    from .evidence_qualification import qualify_record, validate_qualification
    from .evidence_snapshot import build_snapshot, descriptor

    cutoff = datetime.fromisoformat(value["as_of"].replace("Z", "+00:00"))
    snapshot = value["evidence_snapshot"]
    try:
        expected = descriptor(build_snapshot(
            cycle_id=value["cycle_id"], as_of=snapshot["as_of"], evidence=value["evidence"],
            source_watermarks=snapshot["source_watermarks"],
            parent_snapshot_id=snapshot.get("parent_snapshot_id"), version=snapshot["version"],
        ))
        if snapshot != expected or datetime.fromisoformat(snapshot["as_of"].replace("Z", "+00:00")) > cutoff:
            return {}
    except (KeyError, TypeError, ValueError):
        return {}
    prices: dict[str, set[float]] = {}
    for source in value["evidence"].get("sources") or []:
        qualification = source.get("evidence_qualification") or {}
        if qualification.get("state") != "qualified" or qualification.get("permitted_use") != "external_fact":
            continue
        try:
            validate_qualification(qualification)
            record = source["evidence_spec"]
            refs = qualification["input_record_refs"]
            if (len(refs) != 1 or refs[0]["record_id"] != record["record_id"]
                    or record["provenance"]["evidence_ref"] != source["evidence_ref"]
                    or source["evidence_ref"] not in snapshot["included_sources"]):
                continue
            inputs = {"source_refs": refs[0]["source_refs"],
                      "source_conflict_refs": qualification["source_conflict_refs"],
                      "memory_receipt": refs[0].get("memory_receipt")}
            if qualification != qualify_record(record, as_of=qualification["as_of"], **inputs):
                continue
            current = qualify_record(record, as_of=value["as_of"], **inputs)
            if current["state"] != "qualified" or current["permitted_use"] != "external_fact":
                continue
            # The display excerpt is not a fact source; only validated content is.
            payload = json.loads(record["content"])
        except (KeyError, AttributeError, TypeError, ValueError):
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
    clauses = [clause for text in _texts(advice) for clause in advice_clauses(text)]
    if any(_LEVERAGE.search(clause) and (_ADVICE.search(clause) or _IMPERATIVE.search(clause))
           and not negated_advice(clause) for clause in clauses):
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
        if drawdown_blocked(truth):
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
    if directional and any((_DIRECTION.search(clause) or _ADD_RISK.search(clause) or _IMPERATIVE.search(clause)
                            or _DIRECTIONAL_IMPERATIVE.search(clause))
                           and not negated_advice(clause) for clause in clauses):
        problems.extend(directional)
    sizing = output.get("sizing_proposal")
    sizing_adds_risk = False
    if isinstance(sizing, dict) and type(sizing.get("target_shares")) is int:
        current = next((row["shares"] for row in positions["truth"]["positions"]
                        if row["code"] == sizing.get("code")), 0) if positions else 0
        sizing_adds_risk = sizing["target_shares"] > current
    adding = (sizing_adds_risk or any(row.get("current_action", row.get("action")) == "allow_add_risk"
                  or row.get("status") == "selected" for row in nodes)
              or any((_ADD_RISK.search(clause) or _IMPERATIVE.search(clause))
                     and not negated_advice(clause) for clause in clauses))
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


def freeze(packet: dict[str, Any], output: dict[str, Any], *, original_receipt: dict[str, Any] | None,
           original_artifact: dict[str, Any] | None = None,
           provenance: dict[str, Any] | None = None) -> dict[str, Any]:
    """Capture an actual attempt; never synthesize or replace its historical receipt."""
    value = {"contract": "RiskGateFrozen/v1", "version": 1,
             "source_packet": copy.deepcopy(packet), "source_output": copy.deepcopy(output),
             "original_receipt": copy.deepcopy(original_receipt),
             "original_artifact": copy.deepcopy(original_artifact),
             "provenance": copy.deepcopy(provenance or {})}
    value["sha256"] = fingerprint(value)
    return validate_frozen(value)


def validate_frozen(value: dict[str, Any]) -> dict[str, Any]:
    fields = {"contract", "version", "source_packet", "source_output", "original_receipt",
              "original_artifact", "provenance", "sha256"}
    if (not isinstance(value, dict) or set(value) != fields or value["contract"] != "RiskGateFrozen/v1"
            or type(value["version"]) is not int or value["version"] != 1):
        raise ValueError("unsupported risk gate frozen contract")
    if (any(not isinstance(value[key], dict) for key in ("source_packet", "source_output", "provenance"))
            or any(value[key] is not None and not isinstance(value[key], dict)
                   for key in ("original_receipt", "original_artifact"))):
        raise ValueError("risk gate frozen fields must be objects")
    if value["sha256"] != fingerprint({key: child for key, child in value.items() if key != "sha256"}):
        raise ValueError("risk gate frozen digest mismatch")
    return value


def frozen_replay(value: dict[str, Any]) -> dict[str, Any]:
    """Requalify separately from immutable history; old packets are not upgraded."""
    frozen = copy.deepcopy(validate_frozen(value))
    receipt = publication_receipt(frozen["source_packet"], frozen["source_output"])
    replay = {"contract": "RiskGateReplay/v1", "version": 1, "frozen": frozen,
              "requalification": receipt,
              "historical_receipt_matches": (receipt == frozen["original_receipt"]
                                             if frozen["original_receipt"] is not None else None),
              "permissions": {"write_permissions": []}}
    replay["sha256"] = fingerprint(replay)
    return replay


def validate_replay(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or value != frozen_replay(value.get("frozen")):
        raise ValueError("risk gate replay qualification/digest mismatch")
    return value


def validate_output(value: dict[str, Any]) -> dict[str, Any]:
    if (not isinstance(value, dict) or value.get("contract") != RESULT_CONTRACT
            or type(value.get("version")) is not int or value.get("version") != 1
            or not isinstance(value.get("input"), dict) or not isinstance(value.get("source_output"), dict)):
        raise ValueError("unsupported risk gate receipt contract or fields")
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


def install_qualification() -> dict[str, Any]:
    """Provider-free installation fixtures, not live performance or promotion evidence."""
    from .evidence_spec import from_observation, install_qualification as evidence_install
    from .evidence_qualification import qualify_record
    from .evidence_snapshot import build_snapshot, descriptor
    from .mandate_spec import build_mandate

    at = "2026-09-21T01:45:00Z"
    quote = json.dumps({"quotes": [{"symbol": "603179", "price": 10, "quote_at": at, "status": "trading"}]})
    record = from_observation({
        "evidence_kind": "market_fact", "url": "https://example.test/install-risk-quote",
        "excerpt_text": quote, "fact_as_of": at, "factual_status": "verified", "evidence_ref": "install-risk-quote",
    }, {"attempt_id": "install-risk-attempt", "observation_id": "install-risk-observation",
        "backend": "market", "operation": "install_smoke", "known_at": at})
    evidence = {"sources": [{"evidence_ref": "install-risk-quote", "excerpt": quote,
                             "evidence_spec": record, "evidence_qualification": qualify_record(record, as_of=at)}],
                "coverage": [], "conflicts": [], "critical_gaps": []}
    packet = {"cycle_id": "install-risk-cycle", "stage": "m1_judgment", "as_of": at,
              "risk_gate_spec": SPEC, "mandate": build_mandate("daily.execution.0945", "m1_judgment", as_of=at),
              "evidence": evidence,
              "evidence_snapshot": descriptor(build_snapshot(cycle_id="install-risk-cycle", as_of=at,
                                                              evidence=evidence, source_watermarks={})),
              "position_safety": position_input({
                  "positions": [{"code": "603179", "shares": 100, "last_price": 10, "updated_at": at}],
                  "total_assets": 100000, "holdings_as_of": at, "assets_as_of": at,
                  "risk_state": {"theme_by_code": {"603179": "auto"}, "synchronized": True, "peak_assets": 100000},
              }, stage="m1_judgment", as_of=at, source_ref="install-runtime", latest_session="2026-09-21")}
    output = {"direction": "bullish", "confidence": "low", "sizing_proposal": {
        "code": "603179", "target_shares": 1000, "stop_price": 9, "leverage": False}}
    frozen = freeze(packet, output, original_receipt=publication_receipt(packet, output),
                    original_artifact={"artifact_id": "install-risk-artifact", "text": "条件确认后再考虑；交易由用户决定。"},
                    provenance={"source": "install_fixture", "model": "not_invoked", "runner_fingerprint": "RiskGateInstall/v1"})
    replay = validate_replay(frozen_replay(frozen))
    # Reuse the real read-only acquisition failure smoke, not a declared failure.
    acquisition = evidence_install()["source_unavailable_smoke"]
    unavailable_packet = copy.deepcopy(packet)
    unavailable_packet["evidence"] = {"sources": acquisition["evidence_items"], "acquisition": acquisition}
    unavailable_packet["evidence_snapshot"] = descriptor(build_snapshot(
        cycle_id=packet["cycle_id"], as_of=at, evidence=unavailable_packet["evidence"], source_watermarks={}))
    unavailable = frozen_replay(freeze(unavailable_packet, output,
                                      original_receipt=publication_receipt(unavailable_packet, output)))
    conflicted = copy.deepcopy(packet)
    conflicted["evidence"]["conflicts"] = [{"materiality": "critical", "resolution": "unresolved"}]
    conflicted["evidence_snapshot"] = descriptor(build_snapshot(
        cycle_id=packet["cycle_id"], as_of=at, evidence=conflicted["evidence"], source_watermarks={}))
    stale = copy.deepcopy(packet)
    stale_truth = copy.deepcopy(packet["position_safety"]["truth"])
    stale_truth["assets_as_of"] = "2026-09-17T01:45:00Z"
    stale["position_safety"] = position_input(stale_truth, stage="m1_judgment", as_of=at,
                                             source_ref="install-stale", latest_session="2026-09-21")
    historical = copy.deepcopy(packet)
    historical.pop("risk_gate_spec")
    historical_replay = frozen_replay(freeze(historical, output, original_receipt=None))
    checks = {
        "frozen_replay": replay == frozen_replay(copy.deepcopy(frozen)),
        "history_preserved": replay["frozen"] == frozen and replay["historical_receipt_matches"] is True,
        "precise_qualified": replay["requalification"]["state"] == "qualified"
                             and replay["requalification"]["permissions"]["precision"] is True,
        "source_unavailable_refused": (acquisition["status"] == "failed" and acquisition["qualified"] is False
                                       and all(acquisition["measurements"].values())
                                       and unavailable["requalification"]["state"] == "refused"
                                       and "market_evidence_missing" in unavailable["requalification"]["problems"]),
        "critical_conflict_refused": "critical_market_conflict" in publication_receipt(conflicted, output)["problems"],
        "stale_assets_refused": publication_receipt(stale, output)["state"] == "refused",
        "leverage_refused": "leverage_not_approved" in publication_receipt(packet, {"text": "建议融资加仓"})["problems"],
        "write_refused": "ownership_or_execution_violation" in publication_receipt(packet, {"operation": "memoryhub_write"})["problems"],
        "historical_not_upgraded": historical_replay["requalification"] is None,
        "read_only": replay["permissions"]["write_permissions"] == []
                     and replay["requalification"]["permissions"]["write_permissions"] == [],
    }
    vector = {
        "delivery_speed": {"status": "not_measured", "reason": "no_live_latency_baseline",
                           "measurements": {"measured": False, "sample_count": 0, "baseline_available": False}},
        "qualification_probability": {"status": "not_measured", "reason": "fixed_fixtures_are_not_a_population",
                                      "measurements": {"measured": False, "qualified_fixtures": int(checks["precise_qualified"]),
                                                       "unavailable_fixture_refused": checks["source_unavailable_refused"]}},
        "research_quality": {"status": "not_measured", "reason": "no_research_quality_baseline",
                             "measurements": {"measured": False, "evidence_record_qualified": evidence["sources"][0]["evidence_qualification"]["state"] == "qualified"}},
        "judgment_outcome": {"status": "not_measured", "reason": "no_observed_trade_outcome",
                             "measurements": {"measured": False, "outcome_observed": False}},
        "safety_reliability": {"status": "pass" if all(checks.values()) else "fail", "scope": "deterministic_install_fixtures",
                               "measurements": {"measured": True, **checks}},
    }
    return {"contract": "RiskGateInstallQualification/v1", "version": 1, "qualified": all(checks.values()),
            "checks": checks, "replay": replay, "source_unavailable_smoke": unavailable, "evaluation_vector": vector}


if __name__ == "__main__":
    print(json.dumps(install_qualification(), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
