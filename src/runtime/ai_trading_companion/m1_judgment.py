"""Runtime-owned independent M1 judgment qualification and frozen replay.

The provider result stays companion-m1-result-v5. This receipt qualifies that
reviewed product, not a second judgment or a new user-facing message. Runtime
owns the frozen packet and evidence; consumers, including QuantResearch, have
no permission to write facts or promote a shadow result into publication.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime
from typing import Any, Iterable

from .cycle_contract import validate_m1_blind_packet
from .evidence_snapshot import build_snapshot, descriptor
from .m0_observation import canonical_json, sha256
from .mandate_spec import validate_mandate
from .research_isolation import validate_access_descriptor, validate_evidence, validate_request


CONTRACT = "M1JudgmentSpec/v1"
VERSION = 1
RESULT_CONTRACT = "M1JudgmentResult/v1"
REPLAY_CONTRACT = "M1JudgmentReplay/v1"
_BOUNDARY = {
    "allowed_inputs": ["frozen_m0", "frozen_public_evidence", "private_facts_before_h0",
                       "as_of_bounded_memory", "prior_verified_market_context", "runtime_mandate",
                       "versioned_quantresearch_evidence"],
    "forbidden_inputs": ["h0_source_text", "h0_propositions", "h0_actions", "h0_action_results", "h0_derived_signals",
                         "current_chat", "m2", "premarket_raw_text", "private_reasoning"],
    "h0_visible": False, "m2_visible": False, "published_chat_after_cutoff_visible": False,
}
_FORBIDDEN_KEYS = frozenset({
    "message_batch", "human_messages", "published_chat_after_cutoff", "chat_human", "current_chat",
    "m2", "m2_output", "m1_output", "private_reasoning", "chain_of_thought",
    "pre_m0", "premarket", "premarket_raw_text", "premarket_artifact", "premarket_chat", "premarket_submission",
    "h0", "h0_source_text", "h0_propositions", "h0_actions", "h0_action_result", "h0_action_results", "h0_derived_signals",
    "portfolio_fact_view", "cognition_result", "cognition_receipt", "cognition_signal",
})
_FORBIDDEN_KINDS = frozenset({"h0", "m1", "m2", "chat_human", "ai_chat", "pre_m0",
                              "premarket", "premarket_chat", "premarket_submission"})
_PACKET_FIELDS = frozenset({
    "schema_version", "cycle_id", "task_key", "stage", "as_of", "scheduled_for", "calendar_context",
    "mandate", "mandate_reference", "m1_judgment_spec", "position_safety", "risk_gate_spec", "cycle_reference", "task_profile",
    "prior_opportunity_plans", "prior_opportunity_followups", "protocol", "risk_doctrine",
    "business_context", "frozen_m0", "frozen_public_evidence", "evidence_snapshot", "evidence",
    "research_isolation", "research_request", "research_evidence", "prior_market_understanding", "artifacts", "memories", "active_workflow_policy", "context",
    "verification_repair", "runtime_strategy_controls", "allowed_research_backends", "sha256",
    "agent_role_inputs", "spec_issue_states", "spec_evidence_gates", "spec95_baseline_sha256",
})


def assert_blind(value: Any, *, human_texts: Iterable[str] = ()) -> None:
    """Reject channels, including JSON encoded artifacts, before any provider call."""
    from .decision_cycle import assert_m1_blind
    assert_m1_blind(value, human_texts=human_texts)
    validate_m1_blind_packet(value)

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                key = str(key).strip().casefold().replace("-", "_")
                if key in _FORBIDDEN_KEYS or key.startswith("h0_") and key != "h0_visible":
                    raise ValueError("M1 input contains a forbidden channel: " + key)
                if key in {"kind", "artifact_kind", "episode_type"} and str(child).casefold() in _FORBIDDEN_KINDS:
                    raise ValueError("M1 input contains a forbidden artifact channel")
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)
        elif isinstance(item, str) and item.lstrip().startswith(("{", "[")):
            try:
                decoded = json.loads(item)
            except ValueError:
                return
            walk(decoded)
    walk(value)


def _time(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("M1 requires a valid frozen as_of") from exc
    if parsed.tzinfo is None:
        raise ValueError("M1 requires timezone-aware as_of")
    return parsed


def _validate_packet(packet: dict[str, Any]) -> None:
    if not isinstance(packet, dict) or packet.get("stage") != "m1_judgment":
        raise ValueError("M1 judgment input must target m1_judgment")
    unknown = set(packet) - _PACKET_FIELDS
    if unknown:
        raise ValueError("M1 packet contains unknown context fields: " + ", ".join(sorted(unknown)))
    assert_blind(packet)
    if packet.get("sha256") != sha256({key: item for key, item in packet.items() if key != "sha256"}):
        raise ValueError("M1 frozen packet digest mismatch")
    cutoff = _time(packet.get("as_of"))
    mandate = validate_mandate(copy.deepcopy(packet.get("mandate")))
    if mandate["stage"] != "m1_judgment" or mandate["task_key"] != packet.get("task_key"):
        raise ValueError("M1 mandate identity mismatch")
    snapshot = packet.get("evidence_snapshot")
    evidence = packet.get("evidence")
    if not isinstance(snapshot, dict) or not isinstance(evidence, dict):
        raise ValueError("M1 requires an immutable evidence snapshot and baseline")
    expected = descriptor(build_snapshot(
        cycle_id=str(packet.get("cycle_id") or ""), as_of=str(snapshot.get("as_of") or ""),
        evidence=evidence, source_watermarks=snapshot.get("source_watermarks"),
        parent_snapshot_id=snapshot.get("parent_snapshot_id"), version=snapshot.get("version", 1),
    ))
    if snapshot != expected or _time(snapshot["as_of"]) > cutoff:
        raise ValueError("M1 evidence snapshot identity or frozen baseline mismatch")
    if evidence.get("as_of") and _time(evidence["as_of"]) > cutoff:
        raise ValueError("M1 cannot consume future evidence")
    for source in evidence.get("sources") or []:
        for field in ("fact_as_of", "published_at"):
            if source.get(field) and _time(source[field]) > cutoff:
                raise ValueError("M1 cannot consume future evidence")
    # known_at is a receipt clock, not the fact clock; frozen replay may acquire
    # historical evidence later without admitting future market information.
    m0 = packet.get("frozen_m0")
    if not isinstance(m0, dict) or set(m0) != {"artifact_id", "sha256", "as_of", "known_at"} or not m0.get("artifact_id") or not m0.get("sha256"):
        raise ValueError("M1 requires a frozen M0 descriptor")
    if m0.get("as_of") and _time(m0["as_of"]) > cutoff:
        raise ValueError("M1 cannot consume a future M0")
    research_access = packet.get("research_isolation")
    research_request = packet.get("research_request")
    research_evidence = packet.get("research_evidence")
    if research_access is not None:
        validate_access_descriptor(research_access)
    if research_request is not None:
        validate_request(research_request)
        if (research_request["task_key"] != packet["task_key"]
                or _time(research_request["as_of"]) != cutoff
                or research_request["market_scope"].get("stage") != "m1_judgment"):
            raise ValueError("M1 research request identity mismatch")
    if research_evidence is not None:
        if research_access is None or research_request is None:
            raise ValueError("M1 research evidence requires a QuantResearch access descriptor and request")
        if mandate["quantresearch_permission"]["enabled"] is not True:
            raise ValueError("M1 research evidence is not authorized by the mandate")
        validate_evidence(research_evidence)
        if research_evidence["provenance"]["request_sha256"] != research_request["sha256"]:
            raise ValueError("M1 research evidence request binding mismatch")
        if (
            _time(research_evidence["provenance"]["as_of"]) > cutoff
            or _time(research_evidence["provenance"]["known_at"]) > cutoff
        ):
            raise ValueError("M1 cannot consume future research evidence")


def build_input(packet: dict[str, Any]) -> dict[str, Any]:
    _validate_packet(packet)
    research_evidence = packet.get("research_evidence")
    evidence_refs = list(packet["evidence_snapshot"]["included_sources"])
    if research_evidence is not None:
        evidence_refs.extend(research_evidence["evidence_refs"])
    value = {
        "contract": CONTRACT, "version": VERSION, "stage": "m1_judgment",
        "source_packet": copy.deepcopy(packet),
        "evidence_snapshot": copy.deepcopy(packet["evidence_snapshot"]),
        "evidence_refs": list(dict.fromkeys(evidence_refs)),
        "mandate_reference": {"contract": packet["mandate"]["contract"], "sha256": packet["mandate"]["sha256"]},
        "boundary": copy.deepcopy(_BOUNDARY), "permissions": {"write_permissions": []},
        "quantresearch": {"access": "read_only", "write_permissions": []},
        "provenance": {"source": "runtime", "cycle_id": packet["cycle_id"],
                       "as_of": packet["as_of"], "packet_sha256": packet["sha256"]},
    }
    if packet.get("research_request") is not None:
        value["research_request"] = copy.deepcopy(packet["research_request"])
    if research_evidence is not None:
        value["research_evidence"] = copy.deepcopy(research_evidence)
    return validate_input(value)


def validate_input(value: dict[str, Any]) -> dict[str, Any]:
    required = {"contract", "version", "stage", "source_packet", "evidence_snapshot", "evidence_refs",
                "mandate_reference", "boundary", "permissions", "quantresearch", "provenance"}
    optional = {"research_request", "research_evidence"}
    if not isinstance(value, dict) or not set(value).issubset(required | optional) or not required.issubset(value):
        raise ValueError("M1JudgmentSpec input fields are not exact")
    if value["contract"] != CONTRACT or type(value["version"]) is not int or value["version"] != VERSION or value["stage"] != "m1_judgment":
        raise ValueError("unsupported M1JudgmentSpec input")
    packet = value["source_packet"]
    _validate_packet(packet)
    if value["boundary"] != _BOUNDARY or value["permissions"] != {"write_permissions": []}:
        raise ValueError("M1 blind read-only boundary mismatch")
    if value["quantresearch"] != {"access": "read_only", "write_permissions": []}:
        raise ValueError("M1 QuantResearch must be read_only")
    research_request = value.get("research_request")
    if packet.get("research_request") != research_request:
        raise ValueError("M1 research request reference mismatch")
    if research_request is not None:
        validate_request(research_request)
    research_evidence = value.get("research_evidence")
    if packet.get("research_evidence") != research_evidence:
        raise ValueError("M1 research evidence reference mismatch")
    if research_evidence is not None:
        if research_request is None:
            raise ValueError("M1 research evidence reference mismatch")
        validate_evidence(research_evidence)
        if research_evidence["provenance"]["request_sha256"] != research_request["sha256"]:
            raise ValueError("M1 research evidence request binding mismatch")
    expected_refs = list(packet["evidence_snapshot"]["included_sources"])
    if research_evidence is not None:
        expected_refs.extend(research_evidence["evidence_refs"])
    if value["evidence_snapshot"] != packet["evidence_snapshot"] or value["evidence_refs"] != list(dict.fromkeys(expected_refs)):
        raise ValueError("M1 frozen evidence reference mismatch")
    if value["mandate_reference"] != {"contract": packet["mandate"]["contract"], "sha256": packet["mandate"]["sha256"]}:
        raise ValueError("M1 mandate reference mismatch")
    if value["provenance"] != {"source": "runtime", "cycle_id": packet["cycle_id"], "as_of": packet["as_of"], "packet_sha256": packet["sha256"]}:
        raise ValueError("M1 input provenance mismatch")
    return value


def validate_stage_output(output: dict[str, Any], input_contract: dict[str, Any]) -> dict[str, Any]:
    from .judgment_publication import publication_problems
    validate_input(input_contract)
    if not isinstance(output, dict) or output.get("result_version") != 5:
        raise ValueError("M1 judgment requires companion-m1-result-v5")
    problems = publication_problems(output, input_contract["source_packet"])
    if problems:
        raise ValueError("M1 judgment is not qualified: " + "; ".join(problems))
    refs = set(input_contract["evidence_refs"])

    def check(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if key in {"evidence_refs", "considered_evidence_refs", "competing_evidence_refs"} and child is not None:
                    if not isinstance(child, list) or any(ref not in refs for ref in child):
                        raise ValueError("M1 cites evidence outside the frozen snapshot")
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
    check(output)
    return output


def _product(output: dict[str, Any]) -> dict[str, Any]:
    core = output["decision_core"]
    coordination = output["publication"]["coordination"]
    refs = sorted({
        str(ref) for row in core["reasons"] for ref in row["evidence_refs"]
    } | {
        str(ref) for ref in core["counterargument"]["evidence_refs"]
    } | {
        str(ref) for row in core["position_focus"] for ref in row["evidence_refs"]
    })
    risk_items = [{"kind": "action", "text": core["action_reason"], "evidence_refs": refs}]
    risk_items.extend({"kind": "risk_cluster", "text": row["risk_cluster"], "evidence_refs": row["evidence_refs"]}
                      for row in coordination["risk_stance"]["candidates"] if row.get("risk_cluster"))
    return copy.deepcopy({
        "conclusion_claims": [{"claim": core["thesis"], "direction": core["direction"], "horizon": core["horizon"],
                               "evidence_refs": refs}],
        "supporting_evidence": core["reasons"], "counter_evidence": [core["counterargument"]],
        "risks": risk_items,
        "invalidation_conditions": [row for row in core["transition_conditions"] if row["outcome"] == "downgrade"],
        "unknowns": coordination["critical_unknowns"],
        "confidence_state": {"level": core["confidence"], "qualified": True},
    })


def build_output(input_contract: dict[str, Any], output: dict[str, Any], *, attempt_id: str | None = None) -> dict[str, Any]:
    validate_stage_output(output, input_contract)
    value = {
        "contract": RESULT_CONTRACT, "version": VERSION, "spec_contract": CONTRACT,
        "stage": "m1_judgment", "state": "qualified", "source_result_version": 5,
        "input": copy.deepcopy(input_contract), "source_output": copy.deepcopy(output),
        "product": _product(output), "permissions": {"write_permissions": []},
        "quantresearch": {"access": "read_only", "write_permissions": []},
        "provenance": {**input_contract["provenance"], "input_sha256": sha256(input_contract),
                       "output_sha256": sha256(output), "attempt_id": attempt_id},
    }
    value["sha256"] = sha256(value)
    return validate_output(value)


def validate_output(value: dict[str, Any]) -> dict[str, Any]:
    required = {"contract", "version", "spec_contract", "stage", "state", "source_result_version", "input",
                "source_output", "product", "permissions", "quantresearch", "provenance", "sha256"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("M1JudgmentResult fields are not exact")
    if (value["contract"] != RESULT_CONTRACT or type(value["version"]) is not int or value["version"] != VERSION
            or value["spec_contract"] != CONTRACT or value["stage"] != "m1_judgment"
            or value["state"] != "qualified" or value["source_result_version"] != 5):
        raise ValueError("unsupported M1JudgmentResult identity or state")
    validate_stage_output(value["source_output"], value["input"])
    if value["product"] != _product(value["source_output"]):
        raise ValueError("M1 judgment product mismatch")
    if value["permissions"] != {"write_permissions": []} or value["quantresearch"] != {"access": "read_only", "write_permissions": []}:
        raise ValueError("M1 judgment consumers must remain read_only")
    provenance = value["provenance"]
    if not isinstance(provenance, dict) or set(provenance) != {*value["input"]["provenance"], "input_sha256", "output_sha256", "attempt_id"}:
        raise ValueError("M1 judgment provenance fields are not exact")
    expected = {**value["input"]["provenance"], "input_sha256": sha256(value["input"]),
                "output_sha256": sha256(value["source_output"]), "attempt_id": provenance["attempt_id"]}
    if provenance != expected or value["sha256"] != sha256({k: v for k, v in value.items() if k != "sha256"}):
        raise ValueError("M1 judgment receipt digest or provenance mismatch")
    return value


def bind_attempt(receipt: dict[str, Any], attempt_id: str) -> dict[str, Any]:
    validate_output(receipt)
    value = copy.deepcopy(receipt)
    value["provenance"]["attempt_id"] = str(attempt_id)
    value["sha256"] = sha256({k: v for k, v in value.items() if k != "sha256"})
    return validate_output(value)


def frozen_replay(input_contract: dict[str, Any], output: dict[str, Any], *, expected_output_sha256: str | None = None) -> dict[str, Any]:
    receipt = build_output(copy.deepcopy(input_contract), copy.deepcopy(output))
    if expected_output_sha256 is not None and sha256(output) != expected_output_sha256:
        raise ValueError("M1 judgment replay digest mismatch")
    return {"contract": REPLAY_CONTRACT, "source_input": copy.deepcopy(input_contract),
            "source_output": copy.deepcopy(output), "source_output_sha256": sha256(output), "receipt": receipt,
            "qualification": {"valid": True, "h0_blind": True, "m2_blind": True, "read_only": True}}


def install_qualification() -> dict[str, Any]:
    from .mandate_spec import build_mandate
    from .judgment_publication import m1_coordination, render_core
    from .broker_client import canonical_packet_hash
    as_of = "2026-10-05T01:45:00Z"
    evidence = {"as_of": as_of, "sources": [{"evidence_ref": "install-market-1", "excerpt": "市场宽度仍待持续确认。"}]}
    packet = {"stage": "m1_judgment", "cycle_id": "install-m1", "task_key": "daily.execution.0945", "as_of": as_of,
              "evidence": evidence, "evidence_snapshot": descriptor(build_snapshot(cycle_id="install-m1", as_of=as_of, evidence=evidence, source_watermarks={})),
              "mandate": build_mandate("daily.execution.0945", "m1_judgment", as_of=as_of),
              "frozen_m0": {"artifact_id": "install-m0", "sha256": sha256("市场宽度仍待持续确认。"), "as_of": as_of, "known_at": as_of}}
    packet["sha256"] = sha256(packet)
    core = {"version": 1, "thesis": "我倾向等待持续确认。", "direction": "neutral", "confidence": "low", "horizon": "当前",
            "current_action": "observe", "action_reason": "我不因短暂反弹扩大风险。",
            "reasons": [{"fact": "市场宽度仍待持续确认。", "evidence_refs": ["install-market-1"], "mechanism": "扩散尚未稳定。", "implication": "暂不扩大风险。"}],
            "counterargument": {"claim": "反弹可能持续。", "evidence_refs": ["install-market-1"], "why_not_base": "缺少持续确认。"},
            "portfolio_stance": "先观察。", "position_focus": [], "critical_unknowns": [],
            "transition_conditions": [{"outcome": "upgrade", "price": "指数企稳", "breadth": "上涨家数占优", "persistence": "持续一个交易日"},
                                      {"outcome": "downgrade", "price": "指数走弱", "breadth": "下跌家数扩大", "persistence": "持续一个交易日"}]}
    coordination = m1_coordination(core, packet)
    text = render_core(core)
    review = {"core_hash": sha256(core), "draft_hash": canonical_packet_hash({"text": text}), "coordination_hash": sha256(coordination),
              "grounded": True, "faithful": True, "scores": {"specificity": 2, "causality": 2, "counterargument": 2, "portfolio": 2, "naturalness": 2, "broadcast_risk": 0},
              "problems": [], "suggestions": []}
    output = {"result_version": 5, "decision_core": core, "narrative": text,
              "publication": {"core_hash": sha256(core), "core_attempt_id": "install-core", "core_review_attempt_id": "install-review",
                              "core_review": review, "fallback": True, "coordination": coordination, "coordination_hash": sha256(coordination)}}
    replay = frozen_replay(build_input(packet), output)
    return {"contract": "M1JudgmentInstallQualification/v1", "qualified": replay["qualification"]["valid"], "replay_sha256": sha256(replay),
            "evaluation_vector": {"schema": True, "structured_judgment": True, "frozen_replay": True,
                                  "h0_m2_isolation": True, "quantresearch_read_only": True, "write_permissions_empty": True}}


if __name__ == "__main__":
    print(canonical_json(install_qualification()))
