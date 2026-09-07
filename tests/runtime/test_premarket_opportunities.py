from __future__ import annotations

import json
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
    class Broker:
        def invoke(self, request):
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
            return
        result = _call_stage(store, cycle, "m0_compose", packet, "companion-m0-result-v3.schema.json", search=False, timeout=60)
    research = store.begin_attempt(cycle["cycle_id"], "m0_research", cycle["as_of"], "research-packet")
    store.finish_attempt(research["attempt_id"], "succeeded", output=packet["evidence"], verifier={"passed": True})
    engine.research_ready(cycle["cycle_id"], normalize_stage_output("m0_compose", result.output).text,
                          evidence_attempt_id=research["attempt_id"], compose_attempt_id=result.attempt_id,
                          evidence_packet_hash="research-packet", packet_hash=result.packet_hash)
    event = next(event for event in store.pending_events() if event["event_type"] == "m0.ready")
    assert json.loads(event["payload_json"])["m0"] == output["narrative"]
    assert result.verifier["candidate_review_attempt_id"]
