from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from ai_trading_companion.judgment_publication import render_core
from ai_trading_companion.m0_observation import sha256
from ai_trading_companion.m2_judgment import (
    CONTRACT,
    REPLAY_CONTRACT,
    RESULT_CONTRACT,
    bind_attempt,
    build_input,
    build_output,
    frozen_replay,
    install_qualification,
    validate_input,
    validate_output,
    validate_stage_output,
)
from ai_trading_companion.mandate_spec import build_mandate
from ai_trading_companion.router import CognitiveRouter
from test_judgment_publication import core


AS_OF = "2026-10-05T03:00:00Z"
CYCLE_ID = "cycle-m2-contract"
TASK_KEY = "manual.non_trading_outlook"


def _facts() -> dict:
    value = {
        "fact_source": "runtime_database",
        "source_artifact_id": "h0-artifact",
        "known_at": "2026-10-05T02:00:00Z",
        "updated_at": "2026-10-05T02:00:00Z",
        "assets_as_of": None,
        "positions": [{"code": "300421", "name": "力星股份", "shares": 200}],
        "total_assets": 100000.0,
    }
    value["fact_view_sha256"] = sha256(value)
    return value


def _packet(*, h0_text: str = "用户判断：我倾向先保留仓位。", conflicts: list | None = None) -> dict:
    evidence = {
        "schema_version": 3,
        "as_of": "2026-10-05T02:30:00Z",
        "spoken_summary": "成交放大但下跌家数仍占优。",
        "sources": [{"evidence_ref": "ev_market", "excerpt": "成交放大15.50%，下跌家数仍占优；力星弱于指数。"}],
        "coverage": [], "critical_gaps": [], "conflicts": conflicts or [], "high_impact_events": [],
    }
    facts = _facts()
    m1_snapshot = {
        "direction": "neutral", "qualified": True, "triggers": ["指数止跌"],
        "invalidations": ["下跌家数扩大"], "risks": [], "unknowns": [],
    }
    value = {
        "schema_version": 2, "cycle_id": CYCLE_ID, "task_key": TASK_KEY, "stage": "m2",
        "as_of": AS_OF, "scheduled_for": AS_OF,
        "mandate": build_mandate(TASK_KEY, "m2", as_of=AS_OF),
        "mandate_reference": {},
        "business_context": {"portfolio_fact_view": facts},
        "current_position_facts": facts,
        "risk_doctrine": {"revision": 1, "doctrine": {"default_action": "observe"}},
        "position_safety": {"stage": "m2", "positions": [], "write_permissions": []},
        "risk_constraints": {
            "risk_doctrine": {"revision": 1, "doctrine": {"default_action": "observe"}},
            "position_safety": {"stage": "m2", "positions": [], "write_permissions": []},
            "mandate_risk_level": {"version": 1, "value": "low"},
            "write_permissions": [],
        },
        "frozen_m0": {"artifact_id": "m0-artifact", "sha256": sha256("m0 observation"), "as_of": "2026-10-05T01:30:00Z", "known_at": "2026-10-05T02:00:00Z"},
        "frozen_h0": {"artifact_id": "h0-artifact", "sha256": __import__("hashlib").sha256(h0_text.encode()).hexdigest(), "as_of": "2026-10-05T02:00:00Z", "known_at": "2026-10-05T02:01:00Z", "source_text": h0_text},
        "frozen_m1": {
            "artifact_id": "m1-artifact", "sha256": __import__("hashlib").sha256("M1原文：中性，暂不扩大风险。".encode()).hexdigest(),
            "as_of": "2026-10-05T02:30:00Z", "known_at": "2026-10-05T02:31:00Z",
            "original_judgment_text": "M1原文：中性，暂不扩大风险。", "snapshot": m1_snapshot,
            "snapshot_sha256": sha256(m1_snapshot),
        },
        "evidence": evidence,
        "artifacts": [{"kind": "evidence", "body": __import__("json").dumps(evidence, ensure_ascii=False)}],
        "conflicts": copy.deepcopy(conflicts or []),
        "user_preferences": [{"kind": "preference", "subject": "user.expression", "value": "concise"}],
        "research_isolation": {"access": "read_only", "write_permissions": []},
        "protocol": {"protocol_id": "m2-test"}, "memories": [],
    }
    value["mandate_reference"] = {"contract": value["mandate"]["contract"], "sha256": value["mandate"]["sha256"]}
    value["sha256"] = sha256(value)
    return value


def _output(packet: dict | None = None, *, outside_ref: bool = False) -> dict:
    packet = packet or _packet()
    decision = copy.deepcopy(core())
    if outside_ref:
        decision["reasons"][0]["evidence_refs"] = ["outside"]
    text = render_core(decision)
    digest = sha256(decision)
    draft = sha256({"text": text})
    review = {
        "core_hash": digest, "draft_hash": draft, "grounded": True, "faithful": True,
        "scores": {"specificity": 2, "causality": 2, "counterargument": 2, "portfolio": 2, "naturalness": 2, "broadcast_risk": 0},
        "problems": [], "suggestions": [],
    }
    return {
        "result_version": 4, "decision_core": decision, "narrative": text,
        "publication": {"core_hash": digest, "core_attempt_id": "core", "core_review_attempt_id": "review", "core_review": review, "fallback": True},
    }


@pytest.fixture
def valid_input():
    return build_input(_packet())


def test_contract_identity_and_read_only_boundary(valid_input):
    assert valid_input["contract"] == CONTRACT
    assert valid_input["stage"] == "m2"
    assert valid_input["boundary"]["h0_visible"] is True
    assert valid_input["boundary"]["m1_visible"] is True
    assert valid_input["boundary"]["m2_visible"] is False
    assert valid_input["permissions"] == {"write_permissions": []}
    assert valid_input["quantresearch"] == {"access": "read_only", "write_permissions": []}


def test_m2_refuses_without_frozen_h0():
    packet = _packet()
    packet["frozen_h0"] = {"artifact_id": None, "sha256": None, "as_of": None, "known_at": None, "source_text": ""}
    packet["sha256"] = sha256({key: value for key, value in packet.items() if key != "sha256"})
    with pytest.raises(ValueError, match="H0"):
        build_input(packet)


def test_m2_refuses_without_qualified_m1_descriptor():
    packet = _packet()
    packet["frozen_m1"] = {"artifact_id": None, "sha256": None, "as_of": None, "known_at": None, "original_judgment_text": "", "snapshot": {}, "snapshot_sha256": None}
    packet["sha256"] = sha256({key: value for key, value in packet.items() if key != "sha256"})
    with pytest.raises(ValueError, match="M1"):
        build_input(packet)


@pytest.mark.parametrize("field,value", [("contract", "M2SynthesisSpec/v2"), ("version", True), ("stage", "m1")])
def test_input_rejects_wrong_identity(valid_input, field, value):
    broken = copy.deepcopy(valid_input)
    broken[field] = value
    with pytest.raises(ValueError):
        validate_input(broken)


def test_packet_digest_tampering_is_rejected():
    packet = _packet()
    packet["frozen_h0"]["source_text"] = "rewritten"
    with pytest.raises(ValueError, match="digest"):
        build_input(packet)


def test_h0_identity_tampering_is_rejected():
    packet = _packet()
    packet["frozen_h0"]["sha256"] = "0" * 64
    packet["sha256"] = sha256({key: value for key, value in packet.items() if key != "sha256"})
    with pytest.raises(ValueError, match="M2"):
        build_input(packet)


def test_future_h0_and_m1_are_rejected():
    for field in ("frozen_h0", "frozen_m1"):
        packet = _packet()
        packet[field]["as_of"] = "2026-10-05T04:00:00Z"
        packet["sha256"] = sha256({key: value for key, value in packet.items() if key != "sha256"})
        with pytest.raises(ValueError, match="future"):
            build_input(packet)


def test_m1_snapshot_digest_tampering_is_rejected():
    packet = _packet()
    packet["frozen_m1"]["snapshot"]["direction"] = "bearish"
    packet["sha256"] = sha256({key: value for key, value in packet.items() if key != "sha256"})
    with pytest.raises(ValueError, match="snapshot"):
        build_input(packet)


def test_position_fact_digest_tampering_is_rejected():
    packet = _packet()
    packet["current_position_facts"]["positions"][0]["shares"] = 999
    packet["business_context"]["portfolio_fact_view"]["positions"][0]["shares"] = 999
    packet["sha256"] = sha256({key: value for key, value in packet.items() if key != "sha256"})
    with pytest.raises(ValueError, match="fact"):
        build_input(packet)


def test_position_facts_must_be_runtime_business_context():
    packet = _packet()
    packet["current_position_facts"] = copy.deepcopy(_facts())
    packet["current_position_facts"]["positions"][0]["shares"] = 201
    packet["current_position_facts"]["fact_view_sha256"] = sha256({key: value for key, value in packet["current_position_facts"].items() if key != "fact_view_sha256"})
    packet["sha256"] = sha256({key: value for key, value in packet.items() if key != "sha256"})
    with pytest.raises(ValueError, match="business context"):
        build_input(packet)


def test_risk_constraints_cannot_grant_writes():
    packet = _packet()
    packet["risk_constraints"]["write_permissions"] = ["portfolio"]
    packet["sha256"] = sha256({key: value for key, value in packet.items() if key != "sha256"})
    with pytest.raises(ValueError, match="write"):
        build_input(packet)


def test_quantresearch_is_read_only():
    packet = _packet()
    packet["research_isolation"]["write_permissions"] = ["evidence"]
    packet["sha256"] = sha256({key: value for key, value in packet.items() if key != "sha256"})
    with pytest.raises(ValueError, match="read_only"):
        build_input(packet)


@pytest.mark.parametrize("nested", [{"private_reasoning": "secret"}, {"m1_rewrite": "replace"}, {"memoryhub_write": {"x": 1}}])
def test_forbidden_write_and_private_channels_are_rejected(nested):
    packet = _packet()
    packet.update(nested)
    packet["sha256"] = sha256({key: value for key, value in packet.items() if key != "sha256"})
    with pytest.raises(ValueError, match="forbidden"):
        build_input(packet)


def test_unknown_packet_fields_are_rejected():
    packet = _packet()
    packet["unapproved"] = True
    packet["sha256"] = sha256({key: value for key, value in packet.items() if key != "sha256"})
    with pytest.raises(ValueError, match="unknown"):
        build_input(packet)


def test_valid_provider_output_is_qualified(valid_input):
    receipt = build_output(valid_input, _output(valid_input["source_packet"]), attempt_id="m2-attempt")
    assert receipt["contract"] == RESULT_CONTRACT
    assert receipt["product"]["preservation"] == {"h0_verbatim": True, "m1_verbatim": True, "m1_snapshot_unchanged": True, "append_only": True}
    assert receipt["provenance"]["attempt_id"] == "m2-attempt"


def test_runtime_router_attaches_formal_m2_receipt(valid_input):
    packet = copy.deepcopy(valid_input["source_packet"])
    packet.pop("position_safety")
    packet["sha256"] = sha256({key: value for key, value in packet.items() if key != "sha256"})
    result = CognitiveRouter().verify("m2", packet, _output(packet))
    assert result["passed"] is True
    assert result["m2_synthesis"]["contract"] == RESULT_CONTRACT


def test_h0_m1_divergence_is_retained_verbatim(valid_input):
    receipt = build_output(valid_input, _output(valid_input["source_packet"]))
    assert receipt["product"]["frozen_h0"]["source_text"] == "用户判断：我倾向先保留仓位。"
    assert receipt["product"]["frozen_m1"]["original_judgment_text"] == "M1原文：中性，暂不扩大风险。"
    assert receipt["product"]["frozen_h0"] != receipt["product"]["frozen_m1"]


def test_conflicts_are_retained():
    conflicts = [{"claim": "A", "competing_evidence_refs": ["ev_market"], "materiality": "high", "resolution": "unresolved_equal_tier"}]
    packet = _packet(conflicts=conflicts)
    receipt = build_output(build_input(packet), _output(packet))
    assert receipt["product"]["conflicts"] == conflicts


def test_preferences_do_not_rewrite_m1():
    packet = _packet()
    original = packet["frozen_m1"]["original_judgment_text"]
    packet["user_preferences"].append({"kind": "preference", "subject": "risk", "value": "aggressive"})
    packet["sha256"] = sha256({key: value for key, value in packet.items() if key != "sha256"})
    receipt = build_output(build_input(packet), _output(packet))
    assert receipt["product"]["frozen_m1"]["original_judgment_text"] == original


def test_m1_snapshot_bytes_are_preserved(valid_input):
    before = copy.deepcopy(valid_input["frozen_m1"]["snapshot"])
    receipt = build_output(valid_input, _output(valid_input["source_packet"]))
    assert receipt["product"]["frozen_m1"]["snapshot"] == before


def test_output_citations_cannot_escape_frozen_evidence(valid_input):
    with pytest.raises(ValueError, match="evidence"):
        validate_stage_output(_output(valid_input["source_packet"], outside_ref=True), valid_input)


def test_receipt_tampering_and_unknown_fields_are_rejected(valid_input):
    receipt = build_output(valid_input, _output(valid_input["source_packet"]))
    broken = copy.deepcopy(receipt)
    broken["product"]["frozen_m1"]["original_judgment_text"] = "rewritten"
    with pytest.raises(ValueError):
        validate_output(broken)
    broken = copy.deepcopy(receipt)
    broken["extra"] = True
    with pytest.raises(ValueError):
        validate_output(broken)


def test_attempt_binding_is_append_only_and_does_not_mutate_source(valid_input):
    receipt = build_output(valid_input, _output(valid_input["source_packet"]))
    original = copy.deepcopy(receipt)
    bound = bind_attempt(receipt, "actual-m2-attempt")
    assert receipt == original
    assert bound["provenance"]["attempt_id"] == "actual-m2-attempt"
    assert bound["sha256"] != receipt["sha256"]


def test_frozen_replay_is_deterministic_and_non_mutating(valid_input):
    output = _output(valid_input["source_packet"])
    input_before, output_before = copy.deepcopy(valid_input), copy.deepcopy(output)
    first = frozen_replay(valid_input, output)
    second = frozen_replay(copy.deepcopy(valid_input), copy.deepcopy(output))
    assert first == second
    assert first["contract"] == REPLAY_CONTRACT
    assert first["qualification"]["conflicts_preserved"] is True
    first["source_input"]["frozen_h0"]["source_text"] = "changed"
    assert valid_input == input_before and output == output_before


def test_replay_rejects_wrong_output_digest(valid_input):
    with pytest.raises(ValueError, match="digest"):
        frozen_replay(valid_input, _output(valid_input["source_packet"]), expected_output_sha256="0" * 64)


def test_installation_qualification_is_deterministic():
    first = install_qualification()
    second = install_qualification()
    assert first == second
    assert first["qualified"] is True
    assert first["evaluation_vector"]["frozen_h0_gate"] is True


def test_result_contract_requires_exact_fields(valid_input):
    receipt = build_output(valid_input, _output(valid_input["source_packet"]))
    assert set(receipt) == {"contract", "version", "spec_contract", "stage", "state", "source_result_version", "input", "source_output", "product", "permissions", "quantresearch", "provenance", "sha256"}
    assert receipt["quantresearch"] == {"access": "read_only", "write_permissions": []}


def test_installed_schema_accepts_valid_input(valid_input):
    root = Path(__file__).parents[2]
    schema = json.loads((root / "resources/contracts/m2-synthesis-spec-v1.schema.json").read_text(encoding="utf-8"))
    assert list(Draft202012Validator(schema).iter_errors(valid_input)) == []
