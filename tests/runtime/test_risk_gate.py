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


def test_precision_requires_qualified_current_price_not_runtime_cached_price():
    from ai_trading_companion.risk_gate import publication_receipt
    value = packet()
    output = {"direction": "bullish", "confidence": "low", "current_action": "observe"}
    missing = publication_receipt(value, output)
    assert missing["state"] == "qualified"  # qualitative research remains available
    assert missing["permissions"]["precision"] is False
    assert "current_qualified_price_missing:300421" in missing["reasons"]["no_precision"]
    value["evidence"]["sources"].append({
        "evidence_ref": "quote", "evidence_qualification": {"state": "qualified", "permitted_use": "external_fact"},
        "excerpt": json.dumps({"quotes": [{"symbol": "300421", "price": 10, "quote_at": AT, "status": "trading"}]}),
    })
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


def test_high_risk_analysis_is_allowed_but_prose_cannot_approve_new_risk_after_drawdown():
    from ai_trading_companion.risk_gate import publication_receipt
    value = packet()
    result = publication_receipt(value, {"text": "杠杆资金正在撤出，可能加剧踩踏；我不认可杠杆交易。"})
    assert result["state"] == "qualified"
    assert publication_receipt(value, {"narrative": "我会加仓，预计收益足以弥补风险。"})["state"] == "refused"


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
    value["evidence"]["sources"].append({"evidence_ref": "quote", "evidence_qualification": {
        "state": "qualified", "permitted_use": "external_fact"}, "excerpt": json.dumps({"quotes": [
        {"symbol": code, "price": 10, "quote_at": AT, "status": "trading"} for code in codes]})})
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
    value["evidence"]["sources"].append({"evidence_ref": "quote", "evidence_qualification": {
        "state": "qualified", "permitted_use": "external_fact"}, "excerpt": json.dumps({"quotes": [
        {"symbol": "300421", "price": 10, "quote_at": AT, "status": "trading"}]})})
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
    value["evidence"]["sources"].append({"evidence_ref": "quote", "evidence_qualification": {
        "state": "qualified", "permitted_use": "external_fact"}, "excerpt": json.dumps({"quotes": [
        {"symbol": "300421", "price": 10, "quote_at": AT, "status": "trading"}]})})
    sizing = {"code": "300421", "target_shares": 1000, "stop_price": 9, "leverage": False}
    assert publication_receipt(value, {"sizing_proposal": sizing})["state"] == "qualified"
    sizing.update(proposal)
    result = publication_receipt(value, {"sizing_proposal": sizing, "claimed_loss_ratio": 0, "claimed_stock_ratio": 0})
    assert result["state"] == "refused"
    assert reason in result["problems"]
