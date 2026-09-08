from __future__ import annotations

import copy
import json
import time
from pathlib import Path

import pytest

from ai_trading_companion.broker_client import BrokerResponse, _validate_schema, canonical_packet_hash
from ai_trading_companion.judgment_publication import (
    JudgmentPublicationPipeline, JudgmentUnavailable, core_problems, model_business_context,
    model_evidence, render_core,
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
                      "problems": ["unsupported assertion"] if reject else [], "suggestions": []}
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


def test_premarket_judgment_cannot_replace_selection_with_market_commentary(tmp_path):
    pipeline, store, cycle = runtime(tmp_path, Broker())
    premarket = packet()
    premarket["task_key"] = "daily.opportunity.0900"
    premarket.pop("task_profile")
    with pytest.raises(JudgmentUnavailable):
        pipeline.produce("m1_judgment", cycle, premarket, time.monotonic() + 60)
    assert store.latest_artifact(cycle["cycle_id"], "m1") is None


def test_intraday_cannot_silently_drop_a_premarket_candidate(tmp_path):
    pipeline, _, cycle = runtime(tmp_path, Broker())
    intraday = packet()
    intraday.update(task_key="daily.execution.0945", task_profile={}, prior_opportunity_plans=[{
        "artifact_id": "morning-plan", "candidates": [{"symbol": "600001", "status": "selected",
                                                       "trigger": "同业转强", "invalidation": "订单否定"}],
    }])
    with pytest.raises(JudgmentUnavailable):
        pipeline.produce("m1_judgment", cycle, intraday, time.monotonic() + 60)


@pytest.mark.parametrize("status", ["supported", "pending", "abandoned"])
def test_intraday_followup_preserves_original_reference_and_reason(tmp_path, status):
    reason = {"supported": "样本科技的订单获确认，原触发条件获得支持。",
              "pending": "样本科技的订单仍待确认，我保留原来的验证条件。",
              "abandoned": "样本科技的订单被否定，原机会应当放弃。"}[status]
    class FollowupBroker(Broker):
        def invoke(self, request):
            response = super().invoke(request)
            if request.stage.endswith("reasoning"):
                response.result["opportunity_followup"] = [{
                    "source_artifact_id": "morning-plan", "symbol": "600001", "status": status,
                    "reason": reason, "evidence_refs": ["ev_company"],
                }]
            return response
    pipeline, _, cycle = runtime(tmp_path, FollowupBroker(fail_expression=True))
    intraday = packet()
    intraday.update(task_key="daily.execution.0945", task_profile={}, as_of="2026-09-07T01:45:00Z", prior_opportunity_plans=[{
        "artifact_id": "morning-plan", "as_of": "2026-09-07T01:00:00Z", "candidates": [{"symbol": "600001", "status": "selected"}],
    }])
    intraday["evidence"]["sources"].append({"evidence_ref": "ev_company", "excerpt": "600001 " + reason, "fact_as_of": "2026-09-07T01:40:00Z"})
    output = pipeline.produce("m1_judgment", cycle, intraday, time.monotonic() + 60)
    assert output["decision_core"]["opportunity_followup"][0]["source_artifact_id"] == "morning-plan"
    assert reason in output["narrative"]
    assert CognitiveRouter().verify("m1_judgment", intraday, output)["passed"]


@pytest.mark.parametrize("status", ["selected", "rejected"])
def test_premarket_selection_and_rejection_survive_expression_failure(tmp_path, status):
    candidate = {
        "symbol": "600001", "name": "样本科技", "status": status, "priority": 1 if status == "selected" else 0,
        "why_now": "新产品进入订单验证期", "business_link": "供电产品直接关联产业需求",
        "comparison": "相比只有概念关联的样本乙，业务依据更直接", "priced_in": "价格是否透支要看开盘承接",
        "counterargument": "订单没有兑现的风险", "trigger": "订单确认且同行同步转强后才考虑",
        "invalidation": "订单被否定或独自冲高回落则放弃", "risk_cluster": "数据中心供电",
        "horizon": "未来一周", "decision_reason": "选择直接业务依据，但不把发布当成收入",
        "evidence_refs": ["ev_company"],
    }
    planned = {**core(), "opportunity_plan": {
        "research_complete": True, "candidates": [candidate],
        "no_selection_reason": "订单尚未兑现，暂不承担风险" if status == "rejected" else "",
    }}
    class SelectionBroker(Broker):
        def invoke(self, request):
            response = super().invoke(request)
            if request.stage.endswith("reasoning"):
                response.result.update(planned)
            return response
    pipeline, _, cycle = runtime(tmp_path, SelectionBroker(fail_expression=True))
    premarket = packet()
    premarket.update(task_key="daily.opportunity.0900", task_profile={})
    premarket["evidence"]["sources"].append({"evidence_ref": "ev_company", "excerpt": "样本科技600001的新供电产品进入订单验证；样本乙只有概念关联。"})
    output = pipeline.produce("m1_judgment", cycle, premarket, time.monotonic() + 60)
    assert output["decision_core"]["opportunity_plan"]["candidates"][0]["status"] == status
    assert candidate["trigger"] in output["narrative"]
    assert candidate["invalidation"] in output["narrative"]
    assert "样本科技" in output["narrative"]
    assert CognitiveRouter().verify("m1_judgment", premarket, output)["passed"]


def test_premarket_recovery_renderer_leads_with_the_decision_and_stays_concise():
    candidate = {
        "symbol": "600001", "name": "样本科技", "status": "observe", "priority": 0,
        "why_now": "今天产业链重新获得资金关注", "business_link": "供电产品直接关联产业需求",
        "comparison": "相比只有概念关联的样本乙，它的业务依据更直接",
        "priced_in": "单日已上涨10%，价格先反映了乐观预期",
        "counterargument": "订单尚未兑现，板块反弹也可能很快分化",
        "trigger": "订单确认且同行同步转强后再考虑", "invalidation": "若订单被否定或独自冲高回落就放弃",
        "risk_cluster": "数据中心供电", "horizon": "未来一周",
        "decision_reason": "列为观察：不在价格已经明显反应时追买",
        "evidence_refs": ["ev_company"],
    }
    decision = {**core(), "opportunity_plan": {
        "research_complete": True, "candidates": [candidate],
        "no_selection_reason": "价格已经先反映预期，暂不承担追高风险",
    }}
    decision["counterargument"] = {
        "claim": "反方认为这只是一次快速反弹", "evidence_refs": ["ev_market"],
        "why_not_base": "我没有把它作为基准，是因为产业链仍有业务验证",
    }

    text = render_core(decision)

    assert text.startswith(decision["thesis"])
    assert "样本科技我暂时只观察" in text
    assert candidate["trigger"] in text and candidate["invalidation"] in text
    assert "15.50%" not in text and "10%" not in text
    assert "。。" not in text and "；，" not in text
    assert "列为观察" not in text and "如果若" not in text
    assert "反方解释是反方认为" not in text and "因为我没有把它作为基准" not in text
    assert text.count("样本科技") <= 2


def test_fact_reference_and_active_position_validation():
    altered = core()
    altered["reasons"][0]["fact"] = "成交放大99.99%"
    altered["position_focus"] = []
    assert "decision_unbound_numeric_fact:99.99" in core_problems(altered, packet())
    assert "decision_missing_portfolio_focus" in core_problems(altered, packet())


def test_followup_cannot_confirm_with_premarket_or_future_evidence():
    context = packet()
    context.update(as_of="2026-09-07T01:45:00Z", prior_opportunity_plans=[{
        "artifact_id": "original", "as_of": "2026-09-07T01:00:00Z",
        "candidates": [{"symbol": "600001", "status": "selected"}],
    }])
    decision = {**core(), "opportunity_followup": [{"source_artifact_id": "original", "symbol": "600001",
                "status": "supported", "reason": "出现承接", "evidence_refs": ["ev_company"]}]}
    for fact_at in (None, "2026-09-07T01:00:00Z", "2026-09-07T01:46:00Z"):
        context["evidence"]["sources"].append({"evidence_ref": "ev_company", "excerpt": "600001承接", "fact_as_of": fact_at})
        assert "opportunity_followup_fresh_evidence_missing" in core_problems(decision, context)


@pytest.mark.parametrize("trigger,outcome", [("not_triggered", "rose"), ("triggered", "fell"), ("unknown", "unknown")])
def test_close_review_requires_both_selected_and_rejected_and_preserves_restart(tmp_path, trigger, outcome):
    context = packet()
    context.update(task_key="daily.review.1520", as_of="2026-09-07T07:20:00Z", prior_opportunity_plans=[{
        "artifact_id": "original", "as_of": "2026-09-07T01:00:00Z",
        "candidates": [{"symbol": "600001", "status": "selected"}, {"symbol": "600002", "status": "rejected"}],
    }])
    decision = core()
    decision["opportunity_followup"] = [{"source_artifact_id": "original", "symbol": symbol, "status": "pending",
        "reason": "原先条件仍需区分盘中触发和收盘表现", "evidence_refs": []} for symbol in ("600001", "600002")]
    assert "opportunity_review_incomplete_or_unbound" in core_problems(decision, context)
    decision["opportunity_review"] = [{"source_artifact_id": "original", "symbol": symbol,
        "trigger_status": trigger, "price_outcome": outcome,
        "evidence_quality": "只按冻结的当日事实核查，不以收盘涨跌反推触发",
        "process_assessment": "原来关注订单兑现的理由仍合理，但需区别概念炒作",
        "lesson": "单次结果不足以升级方法", "reason": "样本公司的涨跌与当时条件是否成立是两回事，不能算成成交收益",
        "evidence_refs": ["ev_" + symbol] if outcome != "unknown" else []} for symbol in ("600001", "600002")]
    context["evidence"]["sources"] += [{"evidence_ref": "ev_" + symbol,
        "excerpt": symbol + "当日条件核查与收盘变化", "fact_as_of": "2026-09-07T07:00:00Z"} for symbol in ("600001", "600002")]
    class ReviewBroker(Broker):
        def invoke(self, request):
            response = super().invoke(request)
            if request.stage.endswith("reasoning"):
                response.result.update(decision)
            return response
    pipeline, store, cycle = runtime(tmp_path, ReviewBroker(fail_expression=True))
    output = pipeline.produce("m1_judgment", cycle, context, time.monotonic() + 60)
    assert output["decision_core"]["opportunity_review"] == decision["opportunity_review"]
    assert "不能算成成交收益" in output["narrative"]
    restarted = JudgmentPublicationPipeline(ReviewBroker(fail_expression=True), store, SCHEMAS, intellect="expert", effort="medium")
    replay = restarted.produce("m1_judgment", cycle, context, time.monotonic() + 60)
    assert replay["decision_core"] == output["decision_core"]


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


def test_event_transition_is_evidence_bound_and_renders_without_market_proxy():
    decision = core()
    decision["transition_conditions"][0] = {
        "outcome": "upgrade", "kind": "event", "event": "official order confirmation",
        "evidence_refs": ["ev_market"],
    }

    assert not core_problems(decision, packet())
    assert "official order confirmation" in render_core(decision)


def test_high_impact_event_must_reach_the_decision_core():
    frozen = copy.deepcopy(packet())
    frozen["evidence"]["sources"].append({"evidence_ref": "ev_event", "excerpt": "official event"})
    frozen["evidence"]["high_impact_events"] = [{
        "materiality": "high", "evidence_refs": ["ev_event"],
    }]

    assert "decision_omits_high_impact_event_evidence" in core_problems(core(), frozen)
    decision = core()
    decision["reasons"][0]["evidence_refs"].append("ev_event")
    assert "decision_omits_high_impact_event_evidence" not in core_problems(decision, frozen)


def test_covered_overseas_and_theme_context_must_reach_the_decision_core():
    frozen = packet()
    frozen["evidence"]["sources"].extend([
        {"evidence_ref": "ev_overseas", "excerpt": "Japan and Korea markets weakened"},
        {"evidence_ref": "ev_theme", "excerpt": "industry theme expectations are already priced in"},
    ])
    frozen["evidence"]["coverage"] = [
        {"requirement_key": "overseas_market_context", "status": "covered", "evidence_refs": ["ev_overseas"]},
        {"requirement_key": "theme_business_and_expectations", "status": "covered", "evidence_refs": ["ev_theme"]},
    ]

    assert "decision_omits_market_context_evidence:overseas_market_context" in core_problems(core(), frozen)
    decision = core()
    decision["reasons"].extend([
        {"fact": "Japan and Korea markets weakened", "evidence_refs": ["ev_overseas"], "mechanism": "risk appetite", "implication": "keep risk contained"},
        {"fact": "industry theme expectations are already priced in", "evidence_refs": ["ev_theme"], "mechanism": "expectations", "implication": "do not chase"},
    ])
    assert not any(
        problem.startswith("decision_omits_market_context_evidence:")
        for problem in core_problems(decision, frozen)
    )


def test_event_transition_schema_rejects_an_unbound_event():
    schema = json.loads((SCHEMAS / "companion-m1-result-v4.schema.json").read_text(encoding="utf-8"))
    item_schema = schema["properties"]["semantic"]["properties"]["transition_conditions"]["items"]
    valid = {"outcome": "upgrade", "kind": "event", "event": "official order confirmation",
             "evidence_refs": ["ev_market"]}
    invalid = {"outcome": "upgrade", "kind": "event", "event": "official order confirmation"}

    assert _validate_schema(valid, item_schema)["passed"]
    assert not _validate_schema(invalid, item_schema)["passed"]


def test_recovery_conditions_do_not_duplicate_punctuation():
    decision = core()
    decision["transition_conditions"][0]["price"] += "；"
    assert "；，" not in render_core(decision)


def test_optional_review_suggestions_do_not_block_qualified_judgment(tmp_path):
    class SuggestingBroker(Broker):
        def invoke(self, request):
            response = super().invoke(request)
            if request.stage.endswith("review"):
                response.result["suggestions"] = ["Optional shorter wording"]
            return response
    pipeline, _, cycle = runtime(tmp_path, SuggestingBroker())
    output = pipeline.produce("m1_judgment", cycle, packet(), time.monotonic() + 60)
    assert CognitiveRouter().verify("m1_judgment", packet(), output)["passed"]


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


def test_reasoning_repairs_retain_all_prior_semantic_feedback(tmp_path):
    class RejectingBroker(Broker):
        def invoke(self, request):
            response = super().invoke(request)
            if request.stage.endswith("review"):
                number = sum(r.stage.endswith("review") for r in self.calls)
                response.result["problems"] = [f"material-issue-{number}"]
            return response
    broker = RejectingBroker(reject_core=True)
    pipeline, _, cycle = runtime(tmp_path, broker)
    with pytest.raises(JudgmentUnavailable):
        pipeline.produce("m1_judgment", cycle, packet(), time.monotonic() + 60)
    last = [r for r in broker.calls if r.stage.endswith("reasoning")][-1]
    assert set(last.packet["feedback"]) >= {"material-issue-1", "material-issue-2"}
    assert last.packet["previous_candidate"] == core()


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


def test_model_context_is_bounded_without_losing_evidence_identity_or_relevant_memory(tmp_path):
    original = packet()
    long_body = "公告开头：样本科技进入订单验证。" + "中段资料" * 2000 + "公告结尾：尚未形成收入，存在兑现风险。"
    original["evidence"]["sources"] = [
        {
            "evidence_ref": "ev_market" if number == 0 else f"ev_{number}",
            "title": f"证据{number}",
            "excerpt": "成交放大15.50%，下跌占优；力星弱于指数；" + long_body,
            "analysis": "保留可判断的事实与反证",
            "fact_as_of": "2026-09-07T01:00:00Z",
            "source_identity": "example.test",
            "source_tier": "secondary",
            "primary": False,
            "market_propagation": "observed",
            "tool_arguments": {"duplicated_transport_detail": "x" * 1000},
        }
        for number in range(40)
    ]
    snapshot = {
        "authority": "mutable_source_snapshot", "episode_type": "external_evidence",
        "known_at": "2026-09-07T01:00:00Z", "summary": "重复行情快照" * 100,
    }
    learned = {
        "authority": "verified_knowledge", "episode_type": "outcome",
        "known_at": "2026-09-06T01:00:00Z", "summary": "订单公告不等于收入兑现",
    }
    original["memories"] = [copy.deepcopy(snapshot) for _ in range(73)] + [learned, copy.deepcopy(learned)]

    broker = Broker(fail_expression=True)
    pipeline, _, cycle = runtime(tmp_path, broker)
    pipeline.produce("m1_judgment", cycle, original, time.monotonic() + 60)

    model_packets = [request.packet for request in broker.calls if request.stage in {"m1_reasoning", "m1_review"}]
    assert model_packets
    assert all(len(json.dumps(value, ensure_ascii=False)) < 35_000 for value in model_packets)
    reasoning_context = next(request.packet["context"] for request in broker.calls if request.stage == "m1_reasoning")
    sources = reasoning_context["evidence"]["sources"]
    assert {row["evidence_ref"] for row in sources} == {row["evidence_ref"] for row in original["evidence"]["sources"]}
    assert all("tool_arguments" not in row for row in sources)
    assert "公告开头" in sources[0]["excerpt"] and "公告结尾" in sources[0]["excerpt"]
    assert "中段省略" in sources[0]["excerpt"]
    assert reasoning_context["memories"] == [learned]


def test_premarket_model_evidence_keeps_decision_inputs_not_noncritical_research_trails():
    sources = [
        {"evidence_ref": ref, "excerpt": ref, "title": ref}
        for ref in ("market", "breadth", "portfolio", "event", "no-change", "candidate")
    ]
    sources.append({
        "evidence_ref": "candidate-duplicate", "excerpt": "candidate", "title": "candidate",
        "analysis": "duplicate transport annotation", "market_propagation": "unknown",
    })
    packet = {
        "task_key": "daily.opportunity.0900",
        "evidence": {
            "sources": sources,
            "coverage": [
                {"requirement_key": "current_market_state", "status": "covered", "evidence_refs": ["market"]},
                {"requirement_key": "market_breadth", "status": "covered", "evidence_refs": ["breadth"]},
                {"requirement_key": "portfolio_market_state", "status": "covered", "evidence_refs": ["portfolio"]},
                {"requirement_key": "material_events_and_counterevidence", "status": "covered", "evidence_refs": ["event"]},
                {"requirement_key": "portfolio_events_and_counterevidence", "status": "checked_no_change", "evidence_refs": ["no-change"]},
                {"requirement_key": "candidate_business_research", "status": "covered", "evidence_refs": ["candidate", "candidate-duplicate"]},
            ],
            "research_gaps": [{"query": "transport-only trace"}],
        },
    }

    projected = model_evidence(packet)

    assert {row["evidence_ref"] for row in projected["sources"]} == {
        "market", "breadth", "portfolio", "candidate",
    }
    assert {row["requirement_key"] for row in projected["coverage"]} == {
        "current_market_state", "market_breadth", "portfolio_market_state", "candidate_business_research",
    }
    assert projected["coverage"][-1]["evidence_refs"] == ["candidate"]
    assert "analysis" not in projected["sources"][-1]
    assert "market_propagation" not in projected["sources"][-1]
    assert "research_gaps" not in projected


def test_model_business_context_keeps_active_risk_and_compacts_closed_positions():
    projected = model_business_context({"business_context": {
        "fact_source": "runtime_database",
        "private_context_before_h0": {
            "frozen_at": "2026-09-07T09:00:00Z", "total_assets": 100_000,
            "positions": [
                {"code": "000001", "name": "持仓", "shares": 100, "last_price": 10,
                 "price_as_of": "2026-09-07T07:00:00Z", "average_cost": 9,
                 "market_value": 1000, "unrealized_pnl": 100, "revision": 7},
                {"code": "000002", "name": "最近卖出", "shares": 0, "last_price": 20,
                 "price_as_of": "2026-09-07T07:00:00Z", "average_cost": 15,
                 "market_value": 0, "unrealized_pnl": 0, "revision": 3},
            ],
        },
    }})

    positions = projected["private_context_before_h0"]["positions"]
    assert positions[0]["average_cost"] == 9
    assert "revision" not in positions[0]
    assert positions[1] == {
        "code": "000002", "name": "最近卖出", "shares": 0, "last_price": 20,
        "price_as_of": "2026-09-07T07:00:00Z",
    }
