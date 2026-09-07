from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ai_trading_companion.__main__ import _call_stage
from ai_trading_companion.broker_client import BrokerError, BrokerResponse
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.store import CompanionStore
from ai_trading_companion.router import CognitiveRouter
from ai_trading_companion.stage_expression import normalize_stage_output
from ai_trading_companion.evidence_contract import EvidenceContractFactory
from ai_trading_companion.packet_builder import RuntimePacketBuilder
from ai_trading_companion.memory_port import InMemoryMemoryAdapter
from ai_trading_companion.opportunities import plan_problems


def test_scheduled_and_manual_premarket_require_company_research():
    class Calendar:
        def is_trading_day(self, day):
            return day.weekday() < 5

    factory = EvidenceContractFactory(Calendar())
    for profile in (None, {
        "profile_id": "pre_market_opportunity", "version": 4,
        "evidence_family": "previous_close", "stage_strategy": "pre_market_baseline",
    }):
        contract = factory.build(
            task_key="daily.opportunity.0900", stage="m0_research",
            as_of="2026-09-07T01:00:00Z", task_profile=profile,
        )
        requirements = {row["key"]: row for row in contract["requirements"]}
        assert requirements["candidate_business_research"]["blocking"]
        assert requirements["candidate_business_research"]["allowed_coverage"] == ["covered"]


def test_candidate_observation_reaches_message_without_a_buy_ranking():
    packet = {
        "task_key": "daily.opportunity.0900", "stage": "m0_compose",
        "evidence": {"sources": [{
            "evidence_ref": "event-1",
            "excerpt": "样本科技600001发布新供电产品。订单仍在验证。",
        }]},
    }
    output = {
        "result_version": 4,
        "narrative": "样本科技的新供电产品已经发布，和这次产业变化有直接关联。不过订单仍在验证，产品发布还不能等同于收入兑现。",
        "candidate_research": [{
            "symbol": "600001", "name": "样本科技", "event": "新供电产品发布",
            "business_link": "供电产品与产业需求直接相关", "counterevidence": "订单仍在验证",
            "observation_condition": "后续订单是否得到确认", "evidence_refs": ["event-1"],
        }],
    }
    assert CognitiveRouter().verify("m0_compose", packet, output)["passed"]
    assert normalize_stage_output("m0_compose", output).text == output["narrative"]


def test_candidate_review_does_not_reject_optional_narrative_duplication_when_all_quality_gates_pass():
    output = {
        "grounded": True,
        "objective": True,
        "specific": True,
        "natural": True,
        "problems": ["候选字段还可以在正文中展开更多业务数字和反证细节。"],
    }

    verdict = CognitiveRouter().verify("m0_candidate_review", {}, output)

    assert verdict["passed"]


def test_premarket_cannot_publish_a_close_summary_as_opportunity_discovery():
    packet = {
        "task_key": "daily.opportunity.0900",
        "stage": "m0_compose",
        "as_of": "2026-09-07T01:00:00Z",
        "evidence": {"sources": []},
    }
    output = {
        "result_version": 3,
        "semantic": {
            "summary": "收盘后看，三大指数接近平盘。",
            "observations": ["市场广度偏弱，持仓表现有分化。"],
            "risks": [], "unknowns": [],
        },
    }
    verdict = CognitiveRouter().verify("m0_compose", packet, output)
    assert not verdict["passed"]
    assert "premarket_candidate_research_missing" in verdict["problems"]


@pytest.mark.parametrize("specific", [True, False])
def test_formal_candidate_publication_requires_independent_content_review(tmp_path, specific):
    store = CompanionStore(tmp_path / "runtime.sqlite3")
    engine = CompanionEngine(store)
    cycle = store.create_cycle("daily.opportunity.0900", "2026-09-07T09:00:00+08:00", "2026-09-07T01:00:00Z")
    engine.research_started(cycle["cycle_id"], as_of=cycle["as_of"])
    packet = {"task_key": cycle["task_key"], "stage": "m0_compose", "as_of": cycle["as_of"],
              "evidence": {"sources": [{"evidence_ref": "ev-company", "excerpt": "样本科技600001发布供电产品；订单仍在验证。"}]}}
    output = {"result_version": 4,
              "narrative": "样本科技发布了供电产品，业务和这次产业变化有直接关联。不过订单仍在验证，不能把发布等同于收入兑现。",
              "candidate_research": [{"symbol": "600001", "name": "样本科技", "event": "供电产品发布",
                                      "business_link": "供电产品业务", "counterevidence": "订单仍在验证",
                                      "observation_condition": "后续订单确认", "evidence_refs": ["ev-company"]}]}
    calls = []
    class Broker:
        def invoke(self, request):
            calls.append(request)
            result = output if request.stage == "m0_compose" else {
                "grounded": True, "objective": True, "specific": specific, "natural": True,
                "problems": [] if specific else ["正文没有完成公司研究"],
            }
            return BrokerResponse("", result, "test", "test", request.intellect, request.intellect, request.stage)

    settings = SimpleNamespace(research={}, broker={"url": "http://broker.test:8817"})
    with patch("ai_trading_companion.__main__.load_settings", return_value=settings), patch(
        "ai_trading_companion.__main__.ProviderBrokerClient", return_value=Broker(),
    ):
        if not specific:
            with pytest.raises(BrokerError):
                _call_stage(store, cycle, "m0_compose", packet, "companion-m0-result-v3.schema.json", search=False, timeout=60)
            assert store.latest_artifact(cycle["cycle_id"], "m0") is None
            specific = True
            calendar = SimpleNamespace(is_trading_day=lambda day: day.weekday() < 5)
            packet = RuntimePacketBuilder(Path(__file__).resolve().parents[2] / "resources", store,
                memory=InMemoryMemoryAdapter(), evidence_contract_factory=EvidenceContractFactory(calendar)).build(
                    cycle, "m0_compose", evidence=packet["evidence"])
            retried = _call_stage(store, cycle, "m0_compose", packet, "companion-m0-result-v3.schema.json", search=False, timeout=60)
            assert retried.verifier["passed"]
            assert "正文没有完成公司研究" in [r for r in calls if r.stage == "m0_compose"][-1].packet.get("opportunity_review_feedback", [])
        result = _call_stage(store, cycle, "m0_compose", packet, "companion-m0-result-v3.schema.json", search=False, timeout=60)
    research = store.begin_attempt(cycle["cycle_id"], "m0_research", cycle["as_of"], "research-packet")
    store.finish_attempt(research["attempt_id"], "succeeded", output=packet["evidence"], verifier={"passed": True})
    engine.research_ready(cycle["cycle_id"], normalize_stage_output("m0_compose", result.output).text,
                          evidence_attempt_id=research["attempt_id"], compose_attempt_id=result.attempt_id,
                          evidence_packet_hash="research-packet", packet_hash=result.packet_hash)
    event = next(event for event in store.pending_events() if event["event_type"] == "m0.ready")
    assert json.loads(event["payload_json"])["m0"] == output["narrative"]
    assert result.verifier["candidate_review_attempt_id"]


def test_restarted_intraday_and_close_packets_research_original_candidates_without_future_plans(tmp_path):
    class Calendar:
        def is_trading_day(self, day):
            return day.weekday() < 5
    store = CompanionStore(tmp_path / "runtime.sqlite3")
    morning = store.create_cycle("daily.opportunity.0900", "2026-09-07T09:00:00+08:00", "2026-09-07T01:00:00Z")
    candidate = {"symbol": "600001", "name": "样本科技", "status": "selected", "trigger": "同行承接", "invalidation": "独自冲高回落"}
    original = store.append_artifact(morning["cycle_id"], "m1", "model", "原计划", morning["as_of"], known_at="2026-09-07T01:10:00Z")
    store.save_judgment_snapshot(original["artifact_id"], morning["cycle_id"], "m1", {
        "decision_core": {"opportunity_plan": {"candidates": [candidate], "no_selection_reason": ""}}}, morning["as_of"])
    qualified = store.begin_attempt(morning["cycle_id"], "m1_judgment", morning["as_of"], "original-packet")
    store.finish_attempt(qualified["attempt_id"], "succeeded", verifier={"passed": True})
    late = store.append_artifact(morning["cycle_id"], "m2", "model", "稍后才形成", "2026-09-07T07:30:00Z", known_at="2026-09-07T01:15:00Z")
    store.save_judgment_snapshot(late["artifact_id"], morning["cycle_id"], "m2", {
        "decision_core": {"opportunity_plan": {"candidates": [{**candidate, "symbol": "600002"}], "no_selection_reason": ""}}}, late["as_of"])
    later_attempt = store.begin_attempt(morning["cycle_id"], "m2", late["as_of"], "later-packet")
    store.finish_attempt(later_attempt["attempt_id"], "succeeded", verifier={"passed": True})
    for task, at in (("daily.execution.0945", "2026-09-07T01:45:00Z"), ("daily.review.1520", "2026-09-07T07:20:00Z")):
        cycle = store.create_cycle(task, at, at)
        restarted = CompanionStore(tmp_path / "runtime.sqlite3")
        builder = RuntimePacketBuilder(Path(__file__).resolve().parents[2] / "resources", restarted,
            memory=InMemoryMemoryAdapter(), evidence_contract_factory=EvidenceContractFactory(Calendar()))
        research = builder.build(cycle, "m0_research")
        assert research["prior_opportunity_plans"][0]["artifact_id"] == original["artifact_id"]
        assert research["prior_opportunity_plans"][0]["candidates"] == [candidate]
        requirement = next(row for row in research["evidence_contract"]["requirements"] if row["key"] == "opportunity_condition_research")
        assert requirement["required_entities"] == ["600001"]
        assert requirement["blocking"]
        assert builder.build(cycle, "m1_judgment")["prior_opportunity_plans"] == research["prior_opportunity_plans"]
    assert store.latest_artifact(morning["cycle_id"], "m1")["body_markdown"] == "原计划"


@pytest.mark.parametrize("condition", ["站上33.41才考虑", "站上33.41元才考虑", "站上33 元才考虑"])
def test_candidate_price_is_not_licensed_by_a_substring_of_an_unrelated_number(condition):
    candidate = {key: "有可追溯依据" for key in (
        "why_now", "business_link", "comparison", "priced_in", "counterargument", "invalidation", "risk_cluster", "horizon", "decision_reason")}
    candidate.update(symbol="600001", name="样本科技", status="selected", priority=1, trigger=condition, evidence_refs=["source"])
    problems = plan_problems({"opportunity_plan": {"research_complete": True, "candidates": [candidate]}},
                             {"task_key": "daily.opportunity.0900"},
                             {"source": {"excerpt": "样本科技600001：133.41，133 元"}})
    assert any(problem.startswith("opportunity_unbound_price:") for problem in problems)
