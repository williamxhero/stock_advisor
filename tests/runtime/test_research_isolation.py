from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from unittest.mock import patch
from jsonschema import Draft202012Validator

from ai_trading_companion.broker_client import canonical_packet_hash
from ai_trading_companion.m1_judgment import build_input as build_m1_input
from ai_trading_companion.mandate_spec import build_mandate, sha256
from ai_trading_companion.memory_port import InMemoryMemoryAdapter
from ai_trading_companion.packet_builder import RuntimePacketBuilder
from ai_trading_companion.research_isolation import (
    CONTRACT,
    EVIDENCE_CONTRACT,
    InMemoryQuantResearchPort,
    QuantResearchPort,
    access_descriptor,
    apply_research_evidence,
    build_evidence,
    build_request,
    frozen_replay,
    install_qualification,
    validate_evidence,
    validate_replay,
)
from test_m1_judgment import AS_OF, TASK_KEY, m1_output, m1_packet


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def evidence(**overrides):
    value = build_evidence(
        research_run_id="run-319",
        dataset_version="cn-a-20261005",
        code_version="research-code-42",
        parameter_version="params-7",
        intervals={
            "in_sample": {"start": "2020-01-01", "end": "2023-12-31"},
            "validation": {"start": "2024-01-01", "end": "2024-12-31"},
            "out_of_sample": {"start": "2025-01-01", "end": "2026-10-05"},
        },
        out_of_sample={"return": 0.12, "sharpe": 0.8, "observations": 100},
        cost_assumptions={"commission_bps": 3, "slippage_bps": 5, "liquidity_model": "close"},
        risk_metrics={"max_drawdown": 0.2, "volatility": 0.3, "turnover": 1.2},
        applicability={"market": "CN_A_SHARE", "conditions": ["liquid"], "limitations": ["candidate only"]},
        baseline_comparison={"baseline_version": "baseline-1", "relative_return": 0.02},
        conclusion={"direction": "bullish", "summary": "research is supportive", "status": "evidence_only"},
        reproducibility={"uri": "quantresearch://run-319", "commit": "research-code-42"},
        as_of=AS_OF,
        artifact_ref="artifact:run-319",
        request_sha256=request()["sha256"],
    )
    value.update(overrides)
    return value


def request():
    return build_request(
        task_key=TASK_KEY,
        as_of=AS_OF,
        market_scope={"market": "CN_A_SHARE", "stage": "m1_judgment"},
        universe=["000001", "600000"],
        research_goal="independent evidence",
        baseline_strategy_version="runtime-baseline-v1",
        strategy_package={"contract": "CompanionResearchSubject/v1", "task_key": TASK_KEY},
    )


def test_evidence_is_complete_versioned_and_schema_qualified():
    value = evidence()
    assert value["contract"] == EVIDENCE_CONTRACT
    for field in (
        "research_run_id", "dataset_version", "code_version", "parameter_version",
        "intervals", "out_of_sample", "cost_assumptions", "risk_metrics", "applicability",
    ):
        assert value[field]
    assert validate_evidence(value) == value
    schema = json.loads((PROJECT_ROOT / "resources/contracts/research-isolation-spec-v1.schema.json").read_text(encoding="utf-8"))
    assert not list(Draft202012Validator(schema).iter_errors(value))


def test_unversioned_or_tampered_evidence_is_rejected():
    with pytest.raises(ValueError, match="fields|versioned"):
        validate_evidence({"research_run_id": "raw-run", "conclusion": {"direction": "bullish"}})
    broken = copy.deepcopy(evidence())
    broken["risk_metrics"]["max_drawdown"] = 0.9
    with pytest.raises(ValueError, match="digest"):
        validate_evidence(broken)
    future = copy.deepcopy(evidence())
    future["intervals"]["out_of_sample"]["end"] = "2027-01-01"
    future["sha256"] = sha256({key: value for key, value in future.items() if key != "sha256"})
    with pytest.raises(ValueError, match="cutoff|interval"):
        validate_evidence(future)


def test_port_reads_only_and_rejects_writes_and_raw_provider_results():
    calls = []
    port = QuantResearchPort(lambda value: calls.append(value) or evidence())
    result = port.read(request())
    assert result == evidence()
    assert calls and calls[0]["permissions"] == {"access": "read_only", "write_permissions": []}
    for operation in (
        "write", "write_evidence", "start_research", "update_strategy", "promote_strategy",
        "update_backtest", "delete_strategy", "run_experiment",
    ):
        with pytest.raises(PermissionError, match="read_only"):
            getattr(port, operation)()

    raw = QuantResearchPort(lambda _request: {"research_run_id": "raw-run"})
    with pytest.raises(ValueError, match="fields|versioned"):
        raw.read(request())

    future = copy.deepcopy(evidence())
    future["provenance"]["as_of"] = "2026-10-06T01:45:00Z"
    future["provenance"]["known_at"] = "2026-10-06T02:00:00Z"
    future["sha256"] = sha256({key: value for key, value in future.items() if key != "sha256"})
    future["provenance"]["request_sha256"] = request()["sha256"]
    future["sha256"] = sha256({key: value for key, value in future.items() if key != "sha256"})
    with pytest.raises(ValueError, match="cutoff"):
        QuantResearchPort(lambda _request: future).read(request())


def test_m1_rejects_research_evidence_when_mandate_disables_quantresearch():
    research = evidence()
    packet = m1_packet(research_isolation=access_descriptor(), research_request=request(), research_evidence=research)
    packet["mandate"] = build_mandate(
        packet["task_key"], "m1_judgment", as_of=packet["as_of"], quantresearch_enabled=False,
    )
    packet["sha256"] = canonical_packet_hash({key: value for key, value in packet.items() if key != "sha256"})
    with pytest.raises(ValueError, match="not authorized"):
        build_m1_input(packet)


def test_m1_rejects_evidence_bound_to_a_different_request():
    packet = m1_packet(
        research_isolation=access_descriptor(), research_request=request(), research_evidence=evidence(),
    )
    packet["research_request"] = build_request(
        task_key=TASK_KEY,
        as_of=AS_OF,
        market_scope={"market": "CN_A_SHARE", "stage": "m1_judgment"},
        universe=["different-subject"],
        research_goal="independent evidence",
        baseline_strategy_version="runtime-baseline-v1",
        strategy_package={"contract": "CompanionResearchSubject/v1", "task_key": TASK_KEY},
    )
    packet["sha256"] = canonical_packet_hash({key: value for key, value in packet.items() if key != "sha256"})
    with pytest.raises(ValueError, match="request binding"):
        build_m1_input(packet)


def test_research_conclusion_cannot_override_m1_verdict():
    research = evidence()
    packet = m1_packet(research_isolation=access_descriptor(), research_request=request(), research_evidence=research)
    contract = build_m1_input(packet)
    assert contract["research_evidence"] == research
    assert "quantresearch:run-319" in contract["evidence_refs"]
    output = m1_output(packet)
    original = copy.deepcopy(output)
    assert apply_research_evidence(output, research) == original
    receipt = __import__("ai_trading_companion.m1_judgment", fromlist=["build_output"]).build_output(contract, output)
    assert receipt["source_output"] == original
    assert receipt["input"]["research_evidence"]["conclusion"]["direction"] == "bullish"
    m1_schema = json.loads((PROJECT_ROOT / "resources/contracts/m1-judgment-spec-v1.schema.json").read_text(encoding="utf-8"))
    assert not list(Draft202012Validator(m1_schema).iter_errors(receipt))


def test_runtime_packet_builder_binds_port_evidence_before_m1_hash(tmp_path):
    from test_m1_judgment import _runtime_builder_fixture

    store, engine, cycle, _raw_packet = _runtime_builder_fixture(tmp_path)
    research = evidence()
    seen = {}
    calls = []

    def reader(value):
        calls.append(value)
        seen.update(value)
        result = copy.deepcopy(research)
        result["provenance"]["request_sha256"] = value["sha256"]
        result["sha256"] = sha256({key: item for key, item in result.items() if key != "sha256"})
        return result

    mandate = build_mandate(
        cycle["task_key"], "m1_judgment", as_of=AS_OF,
        memory_space_id=engine.memory_space_id, quantresearch_enabled=True,
    )
    with patch("ai_trading_companion.packet_builder.mandate_for_stage", return_value=mandate):
        packet = RuntimePacketBuilder(
            PROJECT_ROOT / "resources", store, memory=InMemoryMemoryAdapter(),
            quant_research_port=QuantResearchPort(reader),
        ).build(cycle, "m1_judgment", evidence={
            "schema_version": 3, "as_of": AS_OF, "spoken_summary": "公开证据",
            "sources": [{"evidence_ref": "ev_market", "excerpt": "市场观察", "fact_as_of": AS_OF}],
            "coverage": [], "critical_gaps": [], "conflicts": [], "high_impact_events": [],
        })
    assert packet["research_isolation"] == access_descriptor()
    assert packet["research_evidence"]["research_run_id"] == research["research_run_id"]
    assert packet["research_evidence"]["provenance"]["request_sha256"] == seen["sha256"]
    assert seen["permissions"] == {"access": "read_only", "write_permissions": []}
    assert build_m1_input(packet)["research_evidence"] == packet["research_evidence"]

    frozen_packet = RuntimePacketBuilder(
        PROJECT_ROOT / "resources", store, memory=InMemoryMemoryAdapter(),
        quant_research_port=QuantResearchPort(reader),
    ).build(
        cycle, "m1_judgment", evidence={
            "schema_version": 3, "as_of": AS_OF, "spoken_summary": "公开证据",
            "sources": [{"evidence_ref": "ev_market", "excerpt": "市场观察", "fact_as_of": AS_OF}],
            "coverage": [], "critical_gaps": [], "conflicts": [], "high_impact_events": [],
        }, research_evidence=packet["research_evidence"],
    )
    assert len(calls) == 1
    assert frozen_packet["research_evidence"] == packet["research_evidence"]


def test_frozen_replay_and_install_qualification_are_deterministic():
    first = frozen_replay(evidence())
    second = frozen_replay(copy.deepcopy(evidence()))
    assert first == second
    assert first["qualification"] == {
        "valid": True, "read_only": True, "m1_evidence_only": True,
        "no_auto_override": True, "no_auto_promotion": True,
    }
    tampered = copy.deepcopy(first)
    tampered["source_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="source digest"):
        validate_replay(tampered)
    assert install_qualification() == install_qualification()


def test_replay_schema_and_access_descriptor_are_versioned():
    schema = json.loads((PROJECT_ROOT / "resources/contracts/research-isolation-spec-v1.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    assert access_descriptor()["contract"] == CONTRACT
    assert not list(validator.iter_errors(access_descriptor()))
    assert not list(validator.iter_errors(frozen_replay(evidence())))
    assert InMemoryQuantResearchPort(evidence()).read(request()) == evidence()
