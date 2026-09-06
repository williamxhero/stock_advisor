from __future__ import annotations

import copy
import json
import time
from pathlib import Path

import pytest

from ai_trading_companion.broker_client import BrokerResponse, canonical_packet_hash
from ai_trading_companion.judgment_publication import (
    JudgmentPublicationPipeline, JudgmentUnavailable, core_problems, render_core,
)
from ai_trading_companion.router import CognitiveRouter
from ai_trading_companion.stage_expression import normalize_stage_output
from ai_trading_companion.store import CompanionStore


SCHEMAS = Path(__file__).resolve().parents[2] / "resources" / "contracts"


def core():
    return {
        "version": 1, "thesis": "下周初我更倾向弱势震荡，局部反弹还不足以扭转调整。",
        "direction": "neutral", "confidence": "medium", "horizon": "下周初",
        "current_action": "observe", "action_reason": "我会保留现金，不因局部反弹扩大风险。",
        "reasons": [{"fact": "成交放大15.50%但下跌家数仍占优", "evidence_refs": ["ev_market"],
                     "mechanism": "新增成交仍伴随卖压", "implication": "我把反弹视为轮动，暂不加仓"}],
        "counterargument": {"claim": "局部股票开始反弹，可能先于指数企稳", "evidence_refs": ["ev_market"],
                            "why_not_base": "不过反弹尚未扩散，我暂时不给它更高权重"},
        "portfolio_stance": "组合保持低风险参与，先检查弱于市场的持仓。",
        "position_focus": [{"symbol": "300421", "priority": 1, "action": "reduce_risk",
                            "reason": "力星股份弱于指数，我优先减少它的风险敞口", "evidence_refs": ["ev_market"]}],
        "transition_conditions": [
            {"outcome": "upgrade", "price": "成长指数止跌", "breadth": "上涨家数占优", "persistence": "维持一个交易日"},
            {"outcome": "downgrade", "price": "指数跌破本周低位", "breadth": "下跌家数扩大", "persistence": "持续到收盘"},
        ], "critical_unknowns": [],
    }


def packet():
    return {"stage": "m1_judgment", "task_key": "manual.non_trading_outlook",
            "task_profile": {"evidence_family": "completed_trading_week"},
            "business_context": {"private_context_before_h0": {"positions": [{"code": "300421", "shares": 200}]}},
            "evidence": {"sources": [{"evidence_ref": "ev_market", "excerpt": "成交放大15.50%，下跌占优；力星弱于指数；局部反弹"}]}}


class Broker:
    def __init__(self, *, fail_expression=False, reject_core=False, reject_draft=False, bad_ref=False, revoke_after_expression=False):
        self.calls = []
        self.fail_expression, self.reject_core, self.reject_draft, self.bad_ref = fail_expression, reject_core, reject_draft, bad_ref
        self.revoke_after_expression = revoke_after_expression

    def invoke(self, request):
        self.calls.append(request)
        if request.stage.endswith("reasoning"):
            result = core()
            if self.bad_ref:
                result["reasons"][0]["evidence_refs"] = ["invented"]
        elif request.stage.endswith("expression"):
            if self.fail_expression:
                raise TimeoutError("injected expression timeout")
            result = {"core_hash": request.packet["core_hash"], "paragraphs": render_core(core()).split("\n\n")}
        else:
            expressed = any(r.stage.endswith("expression") for r in self.calls)
            core_rejected = self.reject_core or self.revoke_after_expression and expressed
            reject = core_rejected or self.reject_draft and expressed
            result = {"core_hash": request.packet["core_hash"], "draft_hash": request.packet["draft_hash"],
                      "grounded": not core_rejected, "faithful": not (self.reject_draft and expressed),
                      "scores": dict(specificity=2, causality=2, counterargument=2, portfolio=2, naturalness=2, broadcast_risk=0),
                      "problems": ["unsupported assertion"] if reject else []}
        return BrokerResponse("", result, "test", "test", "expert", "expert", "test-id")


def runtime(tmp_path, broker):
    store = CompanionStore(tmp_path / "runtime.sqlite3")
    cycle = store.create_cycle("manual.non_trading_outlook", "2026-09-06T10:21:31Z", "2026-09-06T10:21:31Z")
    pipeline = JudgmentPublicationPipeline(broker, store, SCHEMAS, intellect="expert", effort="medium")
    return pipeline, store, cycle


@pytest.mark.parametrize("fallback", [False, True])
def test_published_output_preserves_real_decision_and_account_focus(tmp_path, fallback):
    broker = Broker(fail_expression=fallback)
    pipeline, store, cycle = runtime(tmp_path, broker)
    output = pipeline.produce("m1_judgment", cycle, packet(), time.monotonic() + 60)
    assert output["publication"]["fallback"] is fallback
    assert output["decision_core"] == core()
    assert CognitiveRouter().verify("m1_judgment", packet(), output)["passed"]
    normalized = normalize_stage_output("m1_judgment", output)
    assert normalized.text.startswith("下周初我更倾向弱势震荡")
    assert "力星股份" in normalized.text and normalized.snapshot["position_focus"][0]["action"] == "reduce_risk"
    assert normalized.snapshot["horizon"] == "下周初"
    assert normalized.snapshot["confidence"] == "medium"
    assert all(request.h0_forbidden for request in broker.calls)
    assert {a["stage"] for a in store.attempts(cycle["cycle_id"])} >= {"m1_reasoning", "m1_expression", "m1_review"}


def test_rejected_drafts_reuse_reviewed_core_and_restart_checkpoint(tmp_path):
    broker = Broker(reject_draft=True)
    pipeline, store, cycle = runtime(tmp_path, broker)
    output = pipeline.produce("m1_judgment", cycle, packet(), time.monotonic() + 60)
    assert output["publication"]["fallback"]
    assert sum(r.stage == "m1_reasoning" for r in broker.calls) == 1
    assert sum(r.stage == "m1_expression" for r in broker.calls) == 2
    second = Broker(fail_expression=True)
    restarted = JudgmentPublicationPipeline(second, store, SCHEMAS, intellect="expert", effort="medium")
    recovered = restarted.produce("m1_judgment", cycle, packet(), time.monotonic() + 60)
    assert recovered["decision_core"] == output["decision_core"]
    assert not any(r.stage == "m1_reasoning" for r in second.calls)


@pytest.mark.parametrize("kwargs", [{"reject_core": True}, {"bad_ref": True}])
def test_no_qualified_core_never_produces_generic_neutral_reply(tmp_path, kwargs):
    broker = Broker(**kwargs)
    pipeline, store, cycle = runtime(tmp_path, broker)
    with pytest.raises(JudgmentUnavailable):
        pipeline.produce("m1_judgment", cycle, packet(), time.monotonic() + 60)
    assert sum(r.stage == "m1_reasoning" for r in broker.calls) == 3
    assert not any(r.stage == "m1_expression" for r in broker.calls)
    assert store.latest_artifact(cycle["cycle_id"], "m1") is None
    if kwargs.get("reject_core"):
        assert all(a["status"] == "rejected" for a in store.attempts(cycle["cycle_id"]) if a["stage"] == "m1_review")


def test_fact_reference_and_active_position_validation():
    altered = core()
    altered["reasons"][0]["fact"] = "成交放大99.99%"
    altered["position_focus"] = []
    assert "decision_unbound_numeric_fact:99.99" in core_problems(altered, packet())
    assert "decision_missing_portfolio_focus" in core_problems(altered, packet())


def test_final_gate_detects_changed_body_core_or_review(tmp_path):
    pipeline, _, cycle = runtime(tmp_path, Broker())
    output = pipeline.produce("m1_judgment", cycle, packet(), time.monotonic() + 60)
    for key in ("body", "core", "review"):
        altered = copy.deepcopy(output)
        if key == "body":
            altered["narrative"] = "当前我维持中性，当前继续观察，不追涨也不仓促改变判断。"
        elif key == "core":
            altered["decision_core"]["current_action"] = "allow_add_risk"
        else:
            altered["publication"]["narrative_review"]["scores"]["broadcast_risk"] = 3
        assert not CognitiveRouter().verify("m1_judgment", packet(), altered)["passed"]


def test_m2_uses_frozen_evidence_and_shared_pipeline(tmp_path):
    broker = Broker()
    pipeline, _, cycle = runtime(tmp_path, broker)
    m2_packet = packet()
    m2_packet["artifacts"] = [{"kind": "m1_evidence", "body": json.dumps(m2_packet.pop("evidence"))}]
    output = pipeline.produce("m2", cycle, m2_packet, time.monotonic() + 60)
    assert output["result_version"] == 4
    assert CognitiveRouter().verify("m2", packet(), output)["passed"]
    assert normalize_stage_output("m2", output).snapshot["position_focus"]
    assert not any(r.h0_forbidden for r in broker.calls)


def test_expired_expression_deadline_recovers_without_new_broker_call(tmp_path):
    pipeline, store, cycle = runtime(tmp_path, Broker())
    original = pipeline.produce("m1_judgment", cycle, packet(), time.monotonic() + 60)
    broker = Broker()
    restarted = JudgmentPublicationPipeline(broker, store, SCHEMAS, intellect="expert", effort="medium")
    recovered = restarted.produce("m1_judgment", cycle, packet(), time.monotonic() - 1)
    assert recovered["publication"]["fallback"] and not broker.calls
    assert recovered["decision_core"] == original["decision_core"]
    assert CognitiveRouter().verify("m1_judgment", packet(), recovered)["passed"]


def test_shadow_never_reuses_production_core(tmp_path):
    pipeline, store, cycle = runtime(tmp_path, Broker())
    pipeline.produce("m1_judgment", cycle, packet(), time.monotonic() + 60)
    broker = Broker()
    shadow = JudgmentPublicationPipeline(broker, store, SCHEMAS, intellect="expert", effort="medium", is_shadow=True)
    shadow.produce("m1_judgment", cycle, packet(), time.monotonic() + 60)
    assert any(r.stage.endswith("reasoning") for r in broker.calls)
    assert any(a["is_shadow"] for a in store.attempts(cycle["cycle_id"]))


def test_malformed_final_core_is_rejected_without_crashing():
    output = {"result_version": 5, "decision_core": {"thesis": "missing required fields"}}
    assert not CognitiveRouter().verify("m1_judgment", packet(), output)["passed"]


def test_recovery_conditions_do_not_duplicate_punctuation():
    decision = core()
    decision["transition_conditions"][0]["price"] += "；"
    assert "；，" not in render_core(decision)


def test_later_grounding_rejection_revokes_core_including_restart(tmp_path):
    broker = Broker(revoke_after_expression=True)
    pipeline, store, cycle = runtime(tmp_path, broker)
    with pytest.raises(JudgmentUnavailable):
        pipeline.produce("m1_judgment", cycle, packet(), time.monotonic() + 60)
    assert sum(r.stage.endswith("reasoning") for r in broker.calls) <= 3
    restarted_broker = Broker(reject_core=True)
    restarted = JudgmentPublicationPipeline(restarted_broker, store, SCHEMAS, intellect="expert", effort="medium")
    with pytest.raises(JudgmentUnavailable):
        restarted.produce("m1_judgment", cycle, packet(), time.monotonic() + 60)
    assert not any(r.stage.endswith("expression") for r in restarted_broker.calls)


def test_model_contracts_declare_native_types_and_do_not_duplicate_evidence(tmp_path):
    def check(node):
        if isinstance(node, dict):
            if "enum" in node or "const" in node:
                assert "type" in node
            for value in node.values():
                check(value)
        elif isinstance(node, list):
            for value in node:
                check(value)
    for name in ("decision-core-v1", "narrative-draft-v1", "narrative-review-v1"):
        check(json.loads((SCHEMAS / (name + ".schema.json")).read_text(encoding="utf-8")))
    original = packet()
    original["evidence"]["sources"][0]["excerpt_text"] = original["evidence"]["sources"][0]["excerpt"]
    original["artifacts"] = [{"kind": "m1_evidence", "body": json.dumps(original["evidence"])}]
    original["memories"] = [{"authority": "published_ai_message", "summary": "obsolete broadcast"},
                            {"authority": "verified_knowledge", "summary": "learned counterexample"}]
    broker = Broker()
    pipeline, _, cycle = runtime(tmp_path, broker)
    pipeline.produce("m1_judgment", cycle, original, time.monotonic() + 60)
    context = broker.calls[0].packet["context"]
    assert context["artifacts"] == [] and len(context["evidence"]["sources"]) == 1
    assert "excerpt_text" not in context["evidence"]["sources"][0]
    assert context["memories"] == [original["memories"][1]]
    assert context["evidence"]["sources"][0]["excerpt"] == original["evidence"]["sources"][0]["excerpt"]
