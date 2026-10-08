from __future__ import annotations

import copy
import json

import pytest

from ai_trading_companion.mandate_spec import build_mandate
from ai_trading_companion.position_safety import build_input as position_input
from ai_trading_companion.router import CognitiveRouter
from test_m1_judgment import m1_output, m1_packet
from test_judgment_publication import core


AT = "2026-10-05T01:45:00Z"


def test_frozen_replay_preserves_actual_runtime_packet_output_and_publication(tmp_path):
    from ai_trading_companion.m1_judgment import bind_attempt, build_input, build_output
    from ai_trading_companion.mandate_spec import sha256
    from ai_trading_companion.risk_gate import freeze, frozen_replay
    from test_m1_judgment import _runtime_builder_fixture

    store, engine, cycle, raw = _runtime_builder_fixture(tmp_path)
    decision = core()
    decision["position_focus"] = []
    output = m1_output(raw, decision)
    verifier = CognitiveRouter().verify("m1_judgment", raw, output)
    assert verifier["passed"] is True
    research = store.begin_attempt(cycle["cycle_id"], "m1_research", AT, "research-hash")
    store.finish_attempt(research["attempt_id"], "succeeded", output={}, output_sha256=sha256({}), verifier={"passed": True})
    attempt = store.begin_attempt(cycle["cycle_id"], "m1_judgment", AT, raw["sha256"], input_packet=raw,
                                 model="recorded-model", runner_fingerprint="recorded-runner/v1")
    verifier["m1_judgment"] = bind_attempt(build_output(build_input(raw), output), attempt["attempt_id"])
    store.finish_attempt(attempt["attempt_id"], "succeeded", output=output, output_sha256=sha256(output), verifier=verifier)
    engine.m1_ready(cycle["cycle_id"], output["narrative"], research_attempt_id=research["attempt_id"],
                    judgment_attempt_id=attempt["attempt_id"], research_packet_hash="research-hash", judgment_packet_hash=raw["sha256"])
    artifact = store.latest_artifact(cycle["cycle_id"], "m1")
    receipt = json.loads(artifact["metadata_json"])["risk_gate"]
    provenance = {"attempt_id": attempt["attempt_id"], "model": "recorded-model", "runner_fingerprint": "recorded-runner/v1"}
    frozen = freeze(raw, output, original_receipt=receipt, original_artifact=artifact, provenance=provenance)
    original = copy.deepcopy(frozen)
    first = frozen_replay(frozen)
    assert first == frozen_replay(copy.deepcopy(frozen))
    assert first["frozen"] == original == frozen
    assert first["requalification"] == receipt
    assert first["historical_receipt_matches"] is True
    assert frozen["source_packet"] == raw and frozen["source_output"] == output
    assert frozen["provenance"] == provenance
    assert store.latest_artifact(cycle["cycle_id"], "m1") == artifact


def test_install_cli_records_independent_unmeasured_axes_and_fails_closed():
    import os
    import subprocess
    import sys
    from pathlib import Path
    from ai_trading_companion.risk_gate import install_qualification

    value = install_qualification()
    assert value["contract"] == "RiskGateInstallQualification/v1"
    assert value["qualified"] is True
    assert all(value["checks"].values())
    assert set(value["evaluation_vector"]) == {
        "delivery_speed", "qualification_probability", "research_quality", "judgment_outcome", "safety_reliability",
    }
    for axis in ("delivery_speed", "qualification_probability", "research_quality", "judgment_outcome"):
        assert value["evaluation_vector"][axis]["status"] == "not_measured"
        assert value["evaluation_vector"][axis]["measurements"]["measured"] is False
        assert value["evaluation_vector"][axis]["reason"]
    assert value["evaluation_vector"]["safety_reliability"]["status"] == "pass"
    assert value["source_unavailable_smoke"]["requalification"]["state"] == "refused"
    root = Path(__file__).resolve().parents[2]
    environment = {**os.environ, "PYTHONPATH": str(root / "src/runtime")}
    command = [sys.executable, "-m", "ai_trading_companion.risk_gate"]
    first = subprocess.run(command, env=environment, cwd=root, check=True, capture_output=True).stdout
    second = subprocess.run(command, env=environment, cwd=root, check=True, capture_output=True).stdout
    assert first == second
    assert json.loads(first) == value


def test_versioned_schema_validates_real_envelopes_and_rejects_permission_and_shape_forgery():
    from pathlib import Path
    from jsonschema import Draft202012Validator, FormatChecker
    from ai_trading_companion.risk_gate import install_qualification

    schema_path = Path(__file__).resolve().parents[2] / "resources/contracts/risk-gate-spec-v1.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    installation = install_qualification()
    replay = installation["replay"]
    frozen = replay["frozen"]
    receipt = replay["requalification"]
    for value in (receipt["input"], receipt, frozen, replay, installation["source_unavailable_smoke"]):
        validator.validate(value)
    negatives = []
    for field, bad in (("version", True), ("policy", {"contract": "CanonicalRiskPolicy/v2"}), ("as_of", "not-a-time")):
        value = copy.deepcopy(receipt["input"])
        value[field] = bad
        negatives.append(value)
    for field in ("provenance", "permissions", "reasons", "source_output"):
        value = copy.deepcopy(receipt)
        value.pop(field)
        negatives.append(value)
    for field, bad in (("write_permissions", ["place_order"]), ("precision", "yes"), ("confidence_ceiling", "unlimited")):
        value = copy.deepcopy(receipt)
        value["permissions"][field] = bad
        negatives.append(value)
    value = copy.deepcopy(receipt)
    value["restrictions"] = ["BYPASS"]
    negatives.append(value)
    for bad in negatives:
        assert list(validator.iter_errors(bad))


@pytest.mark.parametrize("value", [None, {}, {"contract": "RiskGateResult/v1", "version": 1},
                                   {"version": 1, "input": {}, "source_output": []}])
def test_receipt_validator_rejects_incomplete_contract_as_value_error(value):
    from ai_trading_companion.risk_gate import validate_output
    with pytest.raises(ValueError):
        validate_output(value)


def packet(**evidence_changes):
    value = m1_packet()
    value["evidence"].update(evidence_changes)
    value["position_safety"] = position_input({
        "positions": [{"code": "300421", "shares": 200, "last_price": 10, "updated_at": AT}],
        "total_assets": 85000, "holdings_as_of": AT, "assets_as_of": AT,
        "risk_state": {"synchronized": True, "peak_assets": 100000,
                       "theme_by_code": {"300421": "auto"}, "review_completed": False},
    }, stage="m1_judgment", as_of=AT, source_ref="pre-h0-frozen", latest_session="2026-10-05")
    # The runtime packet builder declares this boundary; provider output cannot remove it.
    value["risk_gate_spec"] = {"contract": "RiskGateSpec/v1", "version": 1}
    return value


def add_qualified_quotes(value, codes, price=10):
    from ai_trading_companion.evidence_qualification import qualify_record
    from ai_trading_companion.evidence_snapshot import build_snapshot, descriptor
    from ai_trading_companion.evidence_spec import from_observation
    content = json.dumps({"quotes": [{"symbol": code, "price": price, "quote_at": AT, "status": "trading"}
                                    for code in codes]})
    record = from_observation({"evidence_ref": "quote", "evidence_kind": "market_fact",
        "url": "https://example.test/quote", "excerpt_text": content, "fact_as_of": AT, "factual_status": "verified"},
        {"attempt_id": "quote-attempt", "observation_id": "quote-observation", "backend": "market", "known_at": AT})
    value["evidence"]["sources"].append({"evidence_ref": "quote", "excerpt": content,
        "evidence_spec": record, "evidence_qualification": qualify_record(record, as_of=AT)})
    value["evidence_snapshot"] = descriptor(build_snapshot(cycle_id=value["cycle_id"], as_of=AT,
        evidence=value["evidence"], source_watermarks={}))


def test_replay_preserves_absent_or_disagreeing_historical_receipts_without_backfill():
    from ai_trading_companion.risk_gate import freeze, frozen_replay, publication_receipt
    value = packet()
    output = {"text": "建议融资加仓"}
    historical = {"contract": "HistoricalReview/v1", "qualified": True, "model": "original-model"}
    frozen = freeze(value, output, original_receipt=historical, original_artifact={"text": "original published text"})
    replay = frozen_replay(frozen)
    assert replay["requalification"]["state"] == "refused"
    assert replay["historical_receipt_matches"] is False
    assert replay["frozen"]["original_receipt"] == historical
    assert replay["frozen"]["original_artifact"] == {"text": "original published text"}
    assert frozen["original_receipt"] == historical
    value.pop("risk_gate_spec")
    legacy = frozen_replay(freeze(value, output, original_receipt=None))
    assert legacy["requalification"] is None
    assert legacy["frozen"]["original_receipt"] is None
    assert "risk_gate_spec" not in legacy["frozen"]["source_packet"]
    assert publication_receipt(value, output) is None


@pytest.mark.parametrize("field", ["source_packet", "source_output", "original_receipt", "original_artifact", "provenance"])
def test_frozen_replay_rejects_tampering_with_any_original_material(field):
    from ai_trading_companion.risk_gate import freeze, frozen_replay
    frozen = freeze(packet(), {"text": "我先核对事实。"}, original_receipt={}, original_artifact={})
    frozen[field]["tampered"] = True
    with pytest.raises(ValueError, match="digest"):
        frozen_replay(frozen)


def test_semantic_validators_reject_rehashed_qualifications_and_unsupported_versions():
    from ai_trading_companion.evidence_spec import fingerprint
    from ai_trading_companion.risk_gate import (build_input, freeze, frozen_replay, publication_receipt,
                                               validate_input, validate_output, validate_replay)
    value = packet()
    input_value = build_input(value)
    input_value["policy"]["leverage_allowed"] = True
    input_value["sha256"] = fingerprint({k: v for k, v in input_value.items() if k != "sha256"})
    with pytest.raises(ValueError, match="policy"):
        validate_input(input_value)
    receipt = publication_receipt(value, {"text": "建议融资加仓"})
    receipt["state"] = "qualified"
    receipt["sha256"] = fingerprint({k: v for k, v in receipt.items() if k != "sha256"})
    with pytest.raises(ValueError, match="receipt"):
        validate_output(receipt)
    replay = frozen_replay(freeze(value, {"text": "我先核对事实。"}, original_receipt=None))
    replay["permissions"]["write_permissions"] = ["place_order"]
    replay["sha256"] = fingerprint({k: v for k, v in replay.items() if k != "sha256"})
    with pytest.raises(ValueError, match="replay"):
        validate_replay(replay)
    value["risk_gate_spec"]["version"] = 2
    with pytest.raises(ValueError, match="unsupported"):
        frozen_replay(freeze(value, {}, original_receipt=None))


def test_restart_and_retry_preserve_published_prefix_and_risk_permission_boundary(tmp_path):
    from ai_trading_companion.engine import CompanionEngine
    from ai_trading_companion.memory_port import InMemoryMemoryAdapter
    from ai_trading_companion.store import CompanionStore
    database = tmp_path / "risk-recovery.sqlite3"
    memory = InMemoryMemoryAdapter()
    engine = CompanionEngine(CompanionStore(database), memory=memory)
    cycle = engine.ensure_daily_conversation()
    stream = engine.chat_stream_started(cycle["cycle_id"], [], "ai_chat")
    engine.chat_stream_delta(cycle["cycle_id"], stream["stream_id"], "我先核对事实。")
    with pytest.raises(ValueError, match="risk gate.*refused"):
        engine.chat_stream_delta(cycle["cycle_id"], stream["stream_id"], "建议融资加仓。")
    restarted_store = CompanionStore(database)
    recovered = CompanionEngine(restarted_store, memory=memory)
    with pytest.raises(ValueError, match="risk gate.*refused"):
        recovered.chat_stream_delta(cycle["cycle_id"], stream["stream_id"], "建议融资加仓。")
    assert restarted_store.stream_message(stream["stream_id"])["text"] == "我先核对事实。"
    recovered.chat_ready(cycle["cycle_id"], "我不认可杠杆；先核对当前资产。")
    artifact = restarted_store.latest_artifact(cycle["cycle_id"], "ai_chat")
    receipt = json.loads(artifact["metadata_json"])["risk_gate"]
    assert receipt["state"] == "qualified"
    assert receipt["permissions"]["precision"] is False
    assert receipt["permissions"]["write_permissions"] == []
    assert restarted_store.stream_message(stream["stream_id"])["text"] == "我先核对事实。"


def test_regression_gate_cannot_compensate_a_risk_qualification_failure_with_other_axes():
    from ai_trading_companion.regression_gate import case_registry, run_regression_gate
    from ai_trading_companion.risk_gate import publication_receipt
    baseline = {case["case_id"]: {"safety": True, "quality": True, "recovery": True} for case in case_registry()}
    candidate = copy.deepcopy(baseline)
    receipt = publication_receipt(packet(conflicts=[{"materiality": "critical", "resolution": "unresolved"}]),
                                  {"direction": "bullish", "expected_return": 999999, "latency_ms": 0})
    candidate["missing_conflicting_data"]["safety"] = receipt["state"] == "qualified"
    candidate["missing_conflicting_data"]["evaluation"] = {
        axis: True for axis in ("delivery_speed", "qualification_probability", "research_quality", "judgment_outcome", "safety_reliability")
    }
    verdict = run_regression_gate(candidate, baseline=baseline)
    assert verdict["passed"] is False
    assert verdict["failure_cases"] == ["missing_conflicting_data"]
    assert verdict["protection_vector"]["safety"]["passed"] is False
    assert verdict["evaluation_vector"]["delivery_speed"]["passed"] is True


def test_selected_opportunity_cannot_bypass_drawdown_gate_with_positive_review_or_fallback():
    value = packet()
    decision = core()
    candidate = {key: "有公开证据支持的条件" for key in (
        "name", "why_now", "business_link", "comparison", "priced_in", "counterargument",
        "trigger", "invalidation", "risk_cluster", "horizon", "decision_reason",
    )}
    candidate.update(symbol="300421", status="selected", priority=1, evidence_refs=["ev_market"])
    decision["opportunity_plan"] = {"research_complete": True, "candidates": [candidate], "no_selection_reason": ""}
    result = CognitiveRouter().verify("m1_judgment", value, m1_output(value, decision))
    assert not result["passed"]
    assert "risk_gate:drawdown_requires_review" in result["problems"]
    assert result["risk_gate"]["permissions"]["new_risk"] is False
    assert result["risk_gate"]["input"]["position_safety"]["provenance"]["source_ref"] == "pre-h0-frozen"


def test_rejected_only_evidence_cannot_qualify_even_a_low_confidence_direction():
    from ai_trading_companion.risk_gate import publication_receipt
    value = packet()
    value["evidence"]["sources"][0]["evidence_qualification"] = {"state": "rejected", "permitted_use": "none"}
    receipt = publication_receipt(value, {"direction": "bullish", "confidence": "low"})
    assert receipt["state"] == "refused"
    assert "STOP" in receipt["restrictions"]
    assert "usable_market_evidence_missing" in receipt["problems"]


def test_detached_quote_excerpt_cannot_understate_exposure_in_publication_or_replay():
    from ai_trading_companion.evidence_snapshot import build_snapshot, descriptor
    from ai_trading_companion.risk_gate import freeze, frozen_replay, install_qualification, publication_receipt
    value = install_qualification()["replay"]["frozen"]["source_packet"]
    output = {"sizing_proposal": {"code": "603179", "target_shares": 20000, "stop_price": .999, "leverage": False}}
    assert publication_receipt(value, output)["state"] == "refused"
    value["evidence"]["sources"][0]["excerpt"] = json.dumps({"quotes": [{
        "symbol": "603179", "price": 1, "quote_at": value["as_of"], "status": "trading",
    }]})
    value["evidence_snapshot"] = descriptor(build_snapshot(
        cycle_id=value["cycle_id"], as_of=value["as_of"], evidence=value["evidence"], source_watermarks={}))
    receipt = publication_receipt(value, output)
    assert receipt["state"] == "refused"
    assert "single_stock_limit" in receipt["problems"]
    assert frozen_replay(freeze(value, output, original_receipt=receipt))["requalification"] == receipt


@pytest.mark.parametrize("broken", ["labels_only", "record_hash", "qualification_record", "source_ref", "snapshot"])
def test_precision_cannot_use_unbound_quote_contracts(broken):
    from ai_trading_companion.evidence_snapshot import build_snapshot, descriptor
    from ai_trading_companion.risk_gate import install_qualification, publication_receipt
    value = install_qualification()["replay"]["frozen"]["source_packet"]
    source = value["evidence"]["sources"][0]
    if broken == "labels_only":
        source.pop("evidence_spec")
        source["evidence_qualification"] = {"state": "qualified", "permitted_use": "external_fact"}
    elif broken == "record_hash":
        source["evidence_spec"]["content"] = source["excerpt"].replace('"price": 10', '"price": 1')
    elif broken == "qualification_record":
        source["evidence_qualification"]["input_record_refs"][0]["record_id"] = "another-record"
    elif broken == "source_ref":
        source["evidence_ref"] = "unrelated-source"
    if broken != "snapshot":
        value["evidence_snapshot"] = descriptor(build_snapshot(cycle_id=value["cycle_id"], as_of=value["as_of"],
            evidence=value["evidence"], source_watermarks={}))
    else:
        value["evidence_snapshot"]["content_hash"] = "0" * 64
    result = publication_receipt(value, {"sizing_proposal": {
        "code": "603179", "target_shares": 1000, "stop_price": 9, "leverage": False}})
    assert result["state"] == "refused"
    assert result["permissions"]["precision"] is False
    assert "current_qualified_price_missing:603179" in result["problems"]


def test_precision_requires_qualified_current_price_not_runtime_cached_price():
    from ai_trading_companion.risk_gate import publication_receipt
    value = packet()
    output = {"direction": "bullish", "confidence": "low", "current_action": "observe"}
    missing = publication_receipt(value, output)
    assert missing["state"] == "qualified"  # qualitative research remains available
    assert missing["permissions"]["precision"] is False
    assert "current_qualified_price_missing:300421" in missing["reasons"]["no_precision"]
    add_qualified_quotes(value, ["300421"])
    qualified = publication_receipt(value, output)
    assert qualified["permissions"]["precision"] is True
    value["evidence"]["sources"][-1]["evidence_qualification"]["state"] = "expired"
    assert publication_receipt(value, output)["permissions"]["precision"] is False


def test_runtime_packet_and_publication_recheck_cannot_accept_a_forged_pass(tmp_path):
    from ai_trading_companion.m1_judgment import bind_attempt, build_input, build_output
    from ai_trading_companion.mandate_spec import sha256
    from ai_trading_companion.position_safety import build_input as position_input
    from test_m1_judgment import _runtime_builder_fixture

    store, engine, cycle, raw = _runtime_builder_fixture(tmp_path)
    assert raw["risk_gate_spec"] == {"contract": "RiskGateSpec/v1", "version": 1}
    # Simulate a provider/adapter review claiming qualification on an unsafe frozen input.
    raw["position_safety"] = position_input({
        "positions": [], "total_assets": 85000, "holdings_as_of": AT, "assets_as_of": AT,
        "risk_state": {"synchronized": True, "peak_assets": 100000},
    }, stage="m1_judgment", as_of=AT, source_ref="frozen-private", latest_session="2026-10-05")
    raw["sha256"] = sha256({key: val for key, val in raw.items() if key != "sha256"})
    decision = core()
    decision["position_focus"] = []
    decision["current_action"] = "allow_add_risk"
    output = m1_output(raw, decision)
    research = store.begin_attempt(cycle["cycle_id"], "m1_research", AT, "research-hash")
    store.finish_attempt(research["attempt_id"], "succeeded", output={}, output_sha256=sha256({}), verifier={"passed": True})
    judgment = store.begin_attempt(cycle["cycle_id"], "m1_judgment", AT, raw["sha256"], input_packet=raw)
    store.finish_attempt(judgment["attempt_id"], "succeeded", output=output, output_sha256=sha256(output),
        verifier={"passed": True, "m1_judgment": bind_attempt(build_output(build_input(raw), output), judgment["attempt_id"])})
    with pytest.raises(ValueError, match="risk gate.*refused"):
        engine.m1_ready(cycle["cycle_id"], output["narrative"], research_attempt_id=research["attempt_id"],
            judgment_attempt_id=judgment["attempt_id"], research_packet_hash="research-hash", judgment_packet_hash=raw["sha256"])
    assert store.latest_artifact(cycle["cycle_id"], "m1") is None


@pytest.mark.parametrize("output,reason", [
    ({"text": "买入100股"}, "structured_sizing_required"),
    ({"text": "建议融资加仓"}, "leverage_not_approved"),
    ({"text": "建议使用杠杆买入"}, "leverage_not_approved"),
    ({"memoryhub_write": {"fact": "new"}}, "ownership_or_execution_violation"),
    ({"operation": "production_strategy_write"}, "ownership_or_execution_violation"),
])
def test_unstructured_llm_agent_or_adapter_output_cannot_bypass_eligibility(output, reason):
    from ai_trading_companion.risk_gate import publication_receipt
    value = packet()
    value.pop("position_safety")
    result = publication_receipt(value, output)
    assert result["state"] == "refused"
    assert reason in result["problems"]


@pytest.mark.parametrize("streamed", [False, True])
def test_cognition_chat_cannot_publish_leverage_advice_even_without_a_router_packet(tmp_path, streamed):
    from ai_trading_companion.engine import CompanionEngine
    from ai_trading_companion.memory_port import InMemoryMemoryAdapter
    from ai_trading_companion.store import CompanionStore
    store = CompanionStore(tmp_path / "chat.sqlite3")
    engine = CompanionEngine(store, memory=InMemoryMemoryAdapter())
    cycle = engine.ensure_daily_conversation()
    if streamed:
        stream = engine.chat_stream_started(cycle["cycle_id"], [], "ai_chat")
        with pytest.raises(ValueError, match="risk gate.*refused"):
            engine.chat_stream_delta(cycle["cycle_id"], stream["stream_id"], "建议融资加仓。")
        assert store.stream_message(stream["stream_id"])["text"] == ""
    else:
        with pytest.raises(ValueError, match="risk gate.*refused"):
            engine.chat_ready(cycle["cycle_id"], "建议融资加仓。")
        assert store.latest_artifact(cycle["cycle_id"], "ai_chat") is None


@pytest.mark.parametrize("text", [
    "拿90%的总资产买入300421。", "100股300421现在买入。",
    "用总资产的三成配置300421。", "200 shares of 300421: buy now.",
    "我不建议观望而是建议融资加仓。",
])
def test_actual_chat_stream_refuses_reordered_precision_and_scoped_leverage(tmp_path, text):
    from ai_trading_companion.engine import CompanionEngine
    from ai_trading_companion.memory_port import InMemoryMemoryAdapter
    from ai_trading_companion.store import CompanionStore
    store = CompanionStore(tmp_path / "precision-chat.sqlite3")
    engine = CompanionEngine(store, memory=InMemoryMemoryAdapter())
    cycle = engine.ensure_daily_conversation()
    stream = engine.chat_stream_started(cycle["cycle_id"], [], "ai_chat")
    engine.chat_stream_delta(cycle["cycle_id"], stream["stream_id"], "我先核对事实。")
    with pytest.raises(ValueError, match="risk gate.*refused"):
        engine.chat_stream_delta(cycle["cycle_id"], stream["stream_id"], text)
    assert store.stream_message(stream["stream_id"])["text"] == "我先核对事实。"


@pytest.mark.parametrize("kind", ["ai_chat", "reflection", "outcome"])
def test_proactive_model_publication_is_gated_before_memory_artifact_or_visibility(tmp_path, kind):
    from ai_trading_companion.engine import CompanionEngine
    from ai_trading_companion.memory_port import InMemoryMemoryAdapter
    from ai_trading_companion.store import CompanionStore
    store = CompanionStore(tmp_path / "proactive.sqlite3")
    memory = InMemoryMemoryAdapter()
    engine = CompanionEngine(store, memory=memory)
    cycle = engine.ensure_daily_conversation()
    visible = store.pending_events()
    with pytest.raises(ValueError, match="risk gate.*refused"):
        engine.publish_proactive_message(cycle["cycle_id"], kind, "建议融资加仓。", meaningful=True)
    assert store.latest_artifact(cycle["cycle_id"], kind) is None
    assert memory._episodes == []
    assert store.pending_events() == visible
    assert engine.publish_proactive_message(cycle["cycle_id"], kind, "建议融资加仓。", meaningful=False) is None
    safe = engine.publish_proactive_message(cycle["cycle_id"], kind, "我不认可杠杆，先核对事实。", meaningful=True)
    assert safe["artifact_id"] == store.latest_artifact(cycle["cycle_id"], kind)["artifact_id"]
    receipt = json.loads(store.latest_artifact(cycle["cycle_id"], kind)["metadata_json"])["risk_gate"]
    assert receipt["state"] == "qualified"
    assert receipt["input"]["stage"] == ("reflection" if kind == "reflection" else "chat")


def test_chat_preserves_factual_confirmation_and_safe_prefix_when_later_advice_is_refused(tmp_path):
    from ai_trading_companion.engine import CompanionEngine
    from ai_trading_companion.memory_port import InMemoryMemoryAdapter
    from ai_trading_companion.store import CompanionStore
    store = CompanionStore(tmp_path / "safe-chat.sqlite3")
    engine = CompanionEngine(store, memory=InMemoryMemoryAdapter())
    cycle = engine.ensure_daily_conversation()
    # Facts and attributed quotes are not grants of advice eligibility.
    confirmation = "已记录你买入100股的成交事实；这不代表我认可杠杆。"
    engine.chat_ready(cycle["cycle_id"], confirmation)
    assert store.latest_artifact(cycle["cycle_id"], "ai_chat")["body_markdown"] == confirmation
    stream = engine.chat_stream_started(cycle["cycle_id"], [], "ai_chat")
    engine.chat_stream_delta(cycle["cycle_id"], stream["stream_id"], "我先核对事实。")
    with pytest.raises(ValueError, match="risk gate.*refused"):
        engine.chat_stream_delta(cycle["cycle_id"], stream["stream_id"], "建议融资加仓。")
    assert store.stream_message(stream["stream_id"])["text"] == "我先核对事实。"


@pytest.mark.parametrize("text", [
    "建议使用杠杆买入但不要追涨。",
    "I recommend buying with leverage but do not chase rallies.",
    "我不建议观望而是建议融资加仓。",
    "I do not recommend waiting but recommend buying on margin.",
    "现在融资加仓300421。", "用杠杆买入300421。",
    "建议融资加仓也不要追涨。",
])
def test_negation_of_another_action_does_not_qualify_leverage(text):
    from ai_trading_companion.risk_gate import publication_receipt
    receipt = publication_receipt(packet(), {"text": text})
    assert receipt["state"] == "refused"
    assert "leverage_not_approved" in receipt["problems"]


@pytest.mark.parametrize("text", [
    "我不建议使用杠杆买入，也不要追涨。",
    "I do not recommend buying with leverage.",
    "我会避免融资加仓。", "I recommend avoiding leverage.",
    "你说：“建议使用杠杆买入但不要追涨。”我不认可这个方案。",
    "杠杆资金正在撤出，可能加剧踩踏；机会值得研究，但不是执行建议。",
])
def test_negated_attributed_or_analytical_leverage_is_not_advice(text):
    from ai_trading_companion.risk_gate import publication_receipt
    assert publication_receipt(packet(), {"text": text})["state"] == "qualified"


@pytest.mark.parametrize("text", [
    "现在买入300421。", "立即加仓300421。", "买入300421吧。",
    "Buy 300421 now.", "Add risk now.",
    "不建议观望而是现在买入300421。",
])
@pytest.mark.parametrize("restricted", ["drawdown", "conflict"])
def test_imperative_advice_obeys_drawdown_and_direction_restrictions(text, restricted):
    from ai_trading_companion.risk_gate import publication_receipt
    value = packet(**({"conflicts": [{"materiality": "critical", "resolution": "unresolved"}]}
                      if restricted == "conflict" else {}))
    receipt = publication_receipt(value, {"text": text})
    assert receipt["state"] == "refused"
    assert ("drawdown_requires_review" if restricted == "drawdown" else "critical_market_conflict") in receipt["problems"]


def test_formal_m1_action_reason_cannot_hide_imperative_advice_in_observe_core():
    from ai_trading_companion.mandate_spec import sha256
    value = packet()
    value["sha256"] = sha256({key: child for key, child in value.items() if key != "sha256"})
    decision = core()
    decision["current_action"] = "observe"
    decision["action_reason"] = "现在买入300421。"
    result = CognitiveRouter().verify("m1_judgment", value, m1_output(value, decision))
    assert result["passed"] is False
    assert "risk_gate:drawdown_requires_review" in result["problems"]


def test_high_risk_analysis_is_allowed_but_prose_cannot_approve_new_risk_after_drawdown():
    from ai_trading_companion.risk_gate import publication_receipt
    value = packet()
    result = publication_receipt(value, {"text": "杠杆资金正在撤出，可能加剧踩踏；我不认可杠杆交易。"})
    assert result["state"] == "qualified"
    assert publication_receipt(value, {"narrative": "我会加仓，预计收益足以弥补风险。"})["state"] == "refused"


def test_historical_asset_peak_excludes_late_recorded_assertion(tmp_path, monkeypatch):
    from ai_trading_companion.portfolio import PortfolioService
    from ai_trading_companion.store import CompanionStore
    store = CompanionStore(tmp_path / "late-assets.sqlite3")
    service = PortfolioService(store)
    def confirm(assets, occurred, known, source):
        monkeypatch.setattr("ai_trading_companion.portfolio.now", lambda: known)
        text = f"我的总资产是{assets}元"
        return service.apply_extraction(text, {"statement_type": "current_state", "changes": [{
            "action": "asset_correction", "total_assets": assets, "occurred_at": occurred,
            "evidence": {"total_assets": str(assets), "action": "总资产是"},
        }]}, None, source)
    assert confirm(100000, "2026-10-05T01:00:00Z", "2026-10-05T01:00:00Z", "early")["state"] == "applied"
    assert confirm(200000, "2026-10-05T01:30:00Z", "2026-10-05T02:00:00Z", "late")["state"] == "applied"
    assert store.portfolio_risk_state(AT)["peak_assets"] == 100000
    assert store.portfolio_risk_state("2026-10-05T02:00:00Z")["peak_assets"] == 200000


def test_source_known_h0_assets_and_later_reversal_preserve_historical_peak(tmp_path, monkeypatch):
    from ai_trading_companion.portfolio import PortfolioService
    from ai_trading_companion.store import CompanionStore
    store = CompanionStore(tmp_path / "source-assets.sqlite3")
    service = PortfolioService(store)
    cycle = store.create_cycle("daily.execution.0945", AT, AT)
    for assets, at in [(100000, "2026-10-05T01:00:00Z"), (200000, "2026-10-05T01:30:00Z")]:
        text = f"我的总资产是{assets}元"
        source = store.append_artifact(cycle["cycle_id"], "h0", "user", text, at, known_at=at)
        recorded = "2026-10-05T02:00:00Z" if assets == 100000 else "2026-10-05T02:01:00Z"
        monkeypatch.setattr("ai_trading_companion.portfolio.now", lambda: recorded)
        assert service.apply_extraction(text, {"statement_type": "current_state", "changes": [{
            "action": "asset_correction", "total_assets": assets, "occurred_at": at,
            "evidence": {"total_assets": str(assets), "action": "总资产是"},
        }]}, cycle["cycle_id"], source["artifact_id"])["state"] == "applied"
    # Processing may be late; the original user source genuinely was known before H0.
    assert store.portfolio_risk_state(AT)["peak_assets"] == 200000
    monkeypatch.setattr("ai_trading_companion.store.now", lambda: AT)
    frozen = store.freeze_private_context(cycle["cycle_id"])
    monkeypatch.setattr("ai_trading_companion.portfolio.now", lambda: "2026-10-05T02:10:00Z")
    service.revert_latest()
    assert store.portfolio_risk_state(AT)["peak_assets"] == 200000
    assert store.portfolio_risk_state("2026-10-05T02:10:00Z")["peak_assets"] == 100000
    assert store.freeze_private_context(cycle["cycle_id"]) == frozen


def test_verified_asset_peak_is_frozen_before_h0_and_not_replaced_by_later_facts(tmp_path):
    from ai_trading_companion.engine import CompanionEngine
    from ai_trading_companion.memory_port import InMemoryMemoryAdapter
    from ai_trading_companion.packet_builder import RuntimePacketBuilder
    from ai_trading_companion.portfolio import PortfolioService
    from ai_trading_companion.store import CompanionStore
    from pathlib import Path

    store = CompanionStore(tmp_path / "risk.sqlite3")
    engine = CompanionEngine(store, memory=InMemoryMemoryAdapter())
    service = PortfolioService(store)
    def confirm(assets, at, source):
        text = f"我的总资产是{assets}元"
        return service.apply_extraction(text, {"statement_type": "current_state", "changes": [{
            "action": "asset_correction", "total_assets": assets, "occurred_at": at,
            "evidence": {"total_assets": str(assets), "action": "总资产是"},
        }]}, None, source)
    assert confirm(100000, "2026-09-30T01:45:00Z", "peak")["state"] == "applied"
    assert confirm(85000, AT, "drawdown")["state"] == "applied"
    cycle = engine.start_cycle("daily.execution.0945", "2026-10-05T09:45:00+08:00", AT)
    frozen = store.freeze_private_context(cycle["cycle_id"])
    assert frozen["risk_state"]["peak_assets"] == 100000
    assert frozen["risk_state"]["synchronized"] is True
    assert confirm(120000, "2026-10-06T01:45:00Z", "after-h0")["state"] == "applied"
    raw = RuntimePacketBuilder(Path(__file__).parents[2] / "resources", store, memory=InMemoryMemoryAdapter()).build(
        store.get_cycle(cycle["cycle_id"]), "m1_judgment", evidence=packet()["evidence"])
    assert raw["position_safety"]["truth"]["risk_state"] == frozen["risk_state"]
    from ai_trading_companion.risk_gate import publication_receipt
    result = publication_receipt(raw, {"current_action": "allow_add_risk"})
    assert "drawdown_requires_review" in result["problems"]


@pytest.mark.parametrize("changes,output,restriction,reason", [
    ({"sources": []}, {"direction": "neutral", "confidence": "low"}, "STOP", "market_evidence_missing"),
    ({"coverage": [{"blocking": True, "status": "missing"}]}, {"direction": "bullish"}, "NO_DIRECTION", "blocking_market_fact_missing"),
    ({"critical_gaps": ["关键因果尚未核验"]}, {"confidence": "high"}, "REDUCED_CONFIDENCE", "critical_market_unknown"),
    ({"conflicts": [{"materiality": "critical", "resolution": "unresolved"}]}, {"direction": "bearish"}, "NO_DIRECTION", "critical_market_conflict"),
])
def test_risk_states_are_independent_hard_qualifications_not_compensable_scores(changes, output, restriction, reason):
    from ai_trading_companion.risk_gate import publication_receipt, validate_output
    output.update(expected_return=999999, latency_ms=0, reviewer_score=100, adapter_qualified=True)
    value = packet(**changes)
    original = copy.deepcopy((value, output))
    result = publication_receipt(value, output)
    assert result["state"] == "refused"
    assert restriction in result["restrictions"]
    assert reason in result["problems"]
    assert result == publication_receipt(copy.deepcopy(value), copy.deepcopy(output))
    assert validate_output(result) == result
    assert (value, output) == original
    tampered = copy.deepcopy(result)
    tampered["permissions"]["new_risk"] = True
    with pytest.raises(ValueError, match="receipt"):
        validate_output(tampered)


def test_same_theme_exposure_uses_qualified_prices_for_every_frozen_holding():
    from ai_trading_companion.risk_gate import publication_receipt
    value = packet()
    codes = ["300421", "603179", "600000"]
    truth = copy.deepcopy(value["position_safety"]["truth"])
    truth["total_assets"] = 100000
    truth["positions"] = [{"code": code, "shares": shares, "last_price": 1, "updated_at": AT}
                          for code, shares in zip(codes, [1000, 1500, 1500])]
    truth["risk_state"]["theme_by_code"] = {code: "auto" for code in codes}
    value["position_safety"] = position_input(truth, stage="m1_judgment", as_of=AT,
        source_ref="verified-facts", latest_session="2026-10-05")
    add_qualified_quotes(value, codes)
    sizing = {"code": codes[0], "target_shares": 1000, "stop_price": 9.9, "leverage": False}
    assert publication_receipt(value, {"sizing_proposal": sizing})["state"] == "qualified"  # 40,000 / 100,000
    sizing["target_shares"] = 1001  # 40,010 / 100,000, while each stock remains below 20%.
    result = publication_receipt(value, {"sizing_proposal": sizing, "claimed_theme_ratio": 0})
    assert result["state"] == "refused"
    assert "same_theme_limit" in result["problems"]


def test_reduced_confidence_cannot_be_bypassed_by_an_otherwise_qualified_sizing_proposal():
    from ai_trading_companion.risk_gate import publication_receipt
    value = packet(critical_gaps=["关键因果尚未核验"])
    truth = copy.deepcopy(value["position_safety"]["truth"])
    truth["total_assets"] = 100000
    value["position_safety"] = position_input(truth, stage="m1_judgment", as_of=AT,
        source_ref="verified-facts", latest_session="2026-10-05")
    add_qualified_quotes(value, ["300421"])
    result = publication_receipt(value, {"sizing_proposal": {
        "code": "300421", "target_shares": 1000, "stop_price": 9, "leverage": False}})
    assert result["permissions"]["precision"] is True
    assert result["permissions"]["new_risk"] is False
    assert result["state"] == "refused"
    assert "critical_market_unknown" in result["problems"]


@pytest.mark.parametrize("proposal,reason", [
    ({"target_shares": 2001}, "single_stock_limit"),
    ({"target_shares": 1000, "stop_price": 8.99}, "planned_loss_limit"),
    ({"target_shares": 1000, "leverage": True}, "leverage_not_approved"),
])
def test_precision_limits_are_computed_from_qualified_prices_not_provider_ratios(proposal, reason):
    from ai_trading_companion.risk_gate import publication_receipt
    value = packet()
    truth = copy.deepcopy(value["position_safety"]["truth"])
    truth["total_assets"] = 100000
    truth["risk_state"]["review_completed"] = True
    # The cached price understates exposure and must not determine eligibility.
    truth["positions"][0]["last_price"] = 1
    value["position_safety"] = position_input(truth, stage="m1_judgment", as_of=AT,
        source_ref="verified-facts", latest_session="2026-10-05")
    add_qualified_quotes(value, ["300421"])
    sizing = {"code": "300421", "target_shares": 1000, "stop_price": 9, "leverage": False}
    assert publication_receipt(value, {"sizing_proposal": sizing})["state"] == "qualified"
    sizing.update(proposal)
    result = publication_receipt(value, {"sizing_proposal": sizing, "claimed_loss_ratio": 0, "claimed_stock_ratio": 0})
    assert result["state"] == "refused"
    assert reason in result["problems"]
