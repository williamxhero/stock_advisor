import json
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from jsonschema import Draft202012Validator

from ai_trading_companion.__main__ import (
    M1_MAX_JUDGMENT_ATTEMPTS,
    _call_stage,
    _evidence_read_cutoff,
    _frozen_m0_source_attempt,
    _m1_research_as_of,
    _m1_retry_feedback,
    _m1_should_retry,
)
from ai_trading_companion.acquisition import AcquisitionBoundary
from ai_trading_companion.broker_client import BrokerError
from ai_trading_companion.evidence_contract import EvidenceContractFactory
from ai_trading_companion.evidence_gate import EvidenceGate, EvidenceInsufficient
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.local_research import BrokerResearchPlanner
from ai_trading_companion.router import CognitiveRouter
from ai_trading_companion.stage_expression import express_stage_semantics, safe_stage_output
from ai_trading_companion.runtime_strategy_policy import RuntimeStrategyControls
from ai_trading_companion.store import CompanionStore


class EvidenceV3Tests(TestCase):
    def test_m0_schema_requires_traceable_natural_observation_items(self):
        schema = json.loads((Path(__file__).parents[2] / "resources" / "contracts" / "companion-m0-result-v3.schema.json").read_text(encoding="utf-8"))
        valid = {
            "result_version": 3,
            "semantic": {
                "summary": {"text": "市场概括", "evidence_refs": ["delta-1"]},
                "observations": [{"text": "新增事件", "evidence_refs": ["delta-1"]}],
                "connections": [{"text": "事件与盘面存在联系", "evidence_refs": ["delta-1"]}],
                "attention": [{"text": "留意后续核验", "evidence_refs": ["delta-1"]}],
                "unknowns": [{"text": "传播范围仍未知", "evidence_refs": ["delta-1"]}],
            },
        }

        self.assertEqual([], list(Draft202012Validator(schema).iter_errors(valid)))
        invalid = json.loads(json.dumps(valid, ensure_ascii=False))
        invalid["semantic"]["summary"] = "市场概括"
        invalid["semantic"]["risks"] = []
        self.assertTrue(list(Draft202012Validator(schema).iter_errors(invalid)))

    def test_m0_emitter_orders_natural_sections_without_exposing_evidence_refs(self):
        rendered = express_stage_semantics("m0", {
            "summary": {"text": "市场概括", "evidence_refs": ["delta-1"]},
            "observations": [{"text": "新变化", "evidence_refs": ["delta-2"]}],
            "connections": [{"text": "关键联系", "evidence_refs": ["delta-2"]}],
            "attention": [{"text": "轻度关注", "evidence_refs": ["delta-2"]}],
            "unknowns": [{"text": "必要不确定性", "evidence_refs": ["delta-2"]}],
        })

        self.assertLess(rendered.index("市场概括"), rendered.index("新变化"))
        self.assertLess(rendered.index("新变化"), rendered.index("关键联系"))
        self.assertLess(rendered.index("关键联系"), rendered.index("轻度关注"))
        self.assertLess(rendered.index("轻度关注"), rendered.index("必要不确定性"))
        self.assertNotIn("evidence_refs", rendered)

    def test_m0_verifier_rejects_foreign_refs_and_single_article_propagation_breadth(self):
        packet = {"stage": "m0_compose", "evidence": {
            "sources": [{"evidence_ref": "delta-1"}],
            "high_impact_events": [{
                "event_id": "rumor-1", "summary": "某政策消息",
                "truth_status": "unverified", "propagation_status": "observed",
                "truth_evidence_refs": ["delta-1"],
                "propagation_evidence_refs": ["delta-1"],
                "origin_evidence_refs": ["delta-1"],
            }],
        }}
        output = {"result_version": 3, "semantic": {
            "summary": {"text": "市场概括", "evidence_refs": ["outside"]},
            "observations": [{"text": "某政策消息尚未证实，但正在市场广泛传播", "evidence_refs": ["delta-1"]}],
            "connections": [], "attention": [], "unknowns": [],
        }}

        verdict = CognitiveRouter().verify("m0_compose", packet, output)

        self.assertIn("m0_evidence_ref_not_in_frozen_packet:outside", verdict["problems"])
        self.assertIn("m0_propagation_breadth_not_supported", verdict["problems"])

    def test_m0_verifier_reports_market_news_delta_page_guard_breach(self):
        packet = {"stage": "m0_compose", "evidence": {
            "market_news_delta": {"pagination": {"max_pages": 32, "pages": 33}},
        }}
        output = {"result_version": 3, "semantic": {
            "summary": {"text": "市场概括", "evidence_refs": []},
            "observations": [], "connections": [], "attention": [], "unknowns": [],
        }}

        verdict = CognitiveRouter().verify("m0_compose", packet, output)

        self.assertIn("m0_news_delta_pagination_limit_exceeded", verdict["problems"])

    def test_m0_verifier_does_not_claim_delta_comparison_without_a_predecessor(self):
        packet = {"stage": "m0_compose", "evidence": {
            "market_news_delta": {"predecessor_missing": True},
        }}
        output = {"result_version": 3, "semantic": {
            "summary": {"text": "市场概括", "evidence_refs": []},
            "observations": [{"text": "较前序新增政策消息", "evidence_refs": []}],
            "connections": [], "attention": [], "unknowns": [],
        }}

        verdict = CognitiveRouter().verify("m0_compose", packet, output)

        self.assertIn("m0_delta_comparison_without_predecessor", verdict["problems"])

    def test_m0_verifier_rejects_prediction_opportunity_sorting_and_trading_language(self):
        output = {"result_version": 3, "semantic": {
            "summary": {"text": "预计市场将上涨，以下是机会排序并建议买入", "evidence_refs": []},
            "observations": [], "connections": [], "attention": [], "unknowns": [],
        }}

        verdict = CognitiveRouter().verify("m0_compose", {"stage": "m0_compose"}, output)

        self.assertIn("m0_contains_direction_or_action", verdict["problems"])

    def test_m0_expression_does_not_turn_missing_fact_into_delivery_status(self):
        rendered = express_stage_semantics("m0", {
            "summary": "收盘市场整体偏弱。",
            "observations": [],
            "risks": [],
            "unknowns": ["缺少成交额及较前一交易日比较，无法判断量价配合。"],
        })

        self.assertIn("在成交额及较前一交易日比较得到确认前，我暂不判断量价配合", rendered)
        self.assertNotIn("未取得", rendered)
        self.assertNotIn("还需要确认缺少", rendered)

    def test_m0_safe_fallback_uses_complete_verified_close_instead_of_claiming_a_gap(self):
        packet = {
            "stage": "m0_compose",
            "evidence_contract": {"requirements": [{
                "key": "portfolio_market_state", "required_entities": ["300378", "300421", "603861"],
            }]},
            "verified_fact_digest": [
                {"excerpt": json.dumps({"indices": [
                    {"name": "上证指数", "price": 3942.09, "change_percent": 0.0178},
                    {"name": "深证成指", "price": 13625.12, "change_percent": 0.0997},
                    {"name": "创业板指", "price": 3312.54, "change_percent": 0.0091},
                ]}, ensure_ascii=False)},
                {"excerpt": json.dumps({"breadth": {
                    "up": 1805, "down": 3275, "flat": 130, "limit_up": 57, "limit_down": 23,
                }}, ensure_ascii=False)},
                {"excerpt": json.dumps({"quotes": [
                    {"symbol": "300378", "name": "鼎捷数智", "price": 38.38, "change_percent": -0.8781},
                    {"symbol": "300421", "name": "力星股份", "price": 16.78, "change_percent": -0.119},
                    {"symbol": "603861", "name": "白云电器", "price": 11.73, "change_percent": 0.6867},
                ]}, ensure_ascii=False)},
            ],
        }

        output = safe_stage_output("m0_compose", packet=packet)
        rendered = express_stage_semantics("m0", output["semantic"])

        self.assertIn("3942.09", rendered)
        self.assertIn("上涨1805家、下跌3275家", rendered)
        self.assertNotIn("信息还在核对", rendered)
        self.assertTrue(CognitiveRouter().verify("m0_compose", packet, output)["passed"])

    def test_m0_intraday_safe_fallback_uses_snapshot_language_and_actual_breadth(self):
        packet = {
            "stage": "m0_compose",
            "task_key": "daily.execution.1430",
            "as_of": "2026-09-08T06:30:00Z",
            "evidence": {"high_impact_events": [{
                "event_id": "policy-semiconductor-20260908",
                "summary": "半导体产业支持政策已发布",
                "scope": "theme", "materiality": "high",
                "evidence_refs": ["policy"], "truth_status": "verified",
                "propagation_status": "observed", "truth_evidence_refs": ["policy"],
                "propagation_evidence_refs": ["policy"],
            }]},
            "verified_fact_digest": [
                {"excerpt": json.dumps({"indices": [
                    {"name": "上证指数", "price": 3942.09, "change_percent": 0.0178},
                    {"name": "深证成指", "price": 13625.12, "change_percent": 0.0997},
                    {"name": "创业板指", "price": 3312.54, "change_percent": 0.0091},
                ]}, ensure_ascii=False)},
                {"excerpt": json.dumps({"breadth": {
                    "up": 3132, "down": 1966, "flat": 118,
                }}, ensure_ascii=False)},
            ],
        }

        output = safe_stage_output("m0_compose", packet=packet)
        rendered = express_stage_semantics("m0", output["semantic"])

        self.assertIn("截至14:30", rendered)
        self.assertIn("上涨3132家、下跌1966家", rendered)
        self.assertIn("上涨家数多于下跌家", rendered)
        self.assertIn("半导体产业支持政策已发布", rendered)
        self.assertIn("传播", rendered)
        self.assertNotIn("收盘后", rendered)
        self.assertNotIn("收于", rendered)
        self.assertNotIn("下跌家数明显多于上涨家数", rendered)
        self.assertNotIn("个股普跌", rendered)
        self.assertNotIn("下一交易日", rendered)

    def test_m1_safe_fallback_preserves_complete_close_review_evidence(self):
        def source(value):
            return {"excerpt": json.dumps(value, ensure_ascii=False)}

        packet = {
            "task_key": "daily.review.1520",
            "task_profile": {
                "evidence_family": "completed_close",
                "analysis": {"goal": "给出成交额、全部持仓、来源和资料时点"},
            },
            "business_context": {"private_context_before_h0": {"positions": [
                {"code": code, "shares": 100} for code in ("000997", "002891", "300421", "601899", "603861")
            ]}},
            "evidence": {"sources": [
                source({"indices": [
                    {"name": "上证指数", "price": 3930.12, "change_percent": -0.3036},
                    {"name": "深证成指", "price": 13516.97, "change_percent": -0.7938},
                    {"name": "创业板指", "price": 3286.55, "change_percent": -0.7846},
                ]}),
                source({"breadth": {"up": 2225, "down": 2794, "flat": 188}}),
                source({"summary": "2026-09-04两市成交额20335.82亿元，上一交易日2026-09-03成交额17606.92亿元，较前一交易日+2728.90亿元（+15.50%）"}),
                source({
                    "leaders": [{"name": "畜禽饲料", "change_percent": 8.46,
                                 "core": {"name": "新希望", "symbol": "000876", "change_percent": 10.0}}],
                    "laggards": [{"name": "玻纤制造", "change_percent": -4.82,
                                  "core": {"name": "中国巨石", "symbol": "600176", "change_percent": -5.75}}],
                }),
                *[source({"quotes": [{
                    "symbol": code, "name": name, "price": price, "change_percent": change,
                }]}) for code, name, price, change in (
                    ("000997", "新大陆", 21.6, 2.4182), ("002891", "中宠股份", 28.61, 2.7289),
                    ("300421", "力星股份", 16.35, -2.5626), ("601899", "紫金矿业", 33.35, 0.1201),
                    ("603861", "白云电器", 11.69, -0.341),
                )],
            ]},
        }

        output = safe_stage_output("m1_judgment", packet=packet)
        rendered = express_stage_semantics("m1", output["semantic"])
        verdict = CognitiveRouter().verify("m1_judgment", packet, output)

        self.assertTrue(verdict["passed"], verdict["problems"])
        self.assertIn("20335.82亿元", rendered)
        self.assertIn("领涨", rendered)
        self.assertIn("市场情绪仍偏弱", rendered)
        self.assertNotIn("市场情绪用广度验证", rendered)
        self.assertNotIn("腾讯15:00", rendered)
        self.assertNotIn("未取得", rendered)
        self.assertNotIn("数据缺失", rendered)
        for code in ("000997", "002891", "300421", "601899", "603861"):
            self.assertIn(code, rendered)

    def test_unqualified_close_review_cannot_bypass_requested_coverage(self):
        packet = {
            "task_key": "daily.review.1520",
            "task_profile": {"evidence_family": "completed_close", "analysis": {"goal": "成交额、来源与时点"}},
        }
        output = safe_stage_output("m1_judgment")

        verdict = CognitiveRouter().verify("m1_judgment", packet, output)

        self.assertFalse(verdict["passed"])
        self.assertIn("close_review_lacks_numeric_turnover_comparison", verdict["problems"])

    def test_stage_expression_does_not_double_terminal_punctuation(self):
        output = safe_stage_output("m0_compose", packet={
            "verified_fact_digest": [
                {"excerpt": json.dumps({"indices": [{"name": "上证指数", "price": 1.0, "change_percent": 0.0}]})},
                {"excerpt": json.dumps({"breadth": {"up": 1, "down": 2, "flat": 3}})},
            ],
        })

        rendered = express_stage_semantics("m0", output["semantic"])

        self.assertNotIn("。。", rendered)

    def test_judgment_expression_cleans_punctuation_inside_compound_conditions(self):
        rendered = express_stage_semantics("m1", {
            "summary": "市场广度偏弱。", "direction": "bearish", "qualified": True,
            "horizon": "下一交易日", "current_action": "reduce_risk", "key_evidence": [],
            "transition_conditions": [{
                "outcome": "upgrade", "price": "指数站稳收盘位。",
                "breadth": "上涨家数超过下跌家数。", "persistence": "持续一个交易日。",
            }],
            "position_focus": [], "risks": [], "unknowns": [],
        })

        self.assertNotIn("。，", rendered)
        self.assertIn("指数站稳收盘位，上涨家数超过下跌家数，持续一个交易日", rendered)

    def test_official_index_close_accepts_complete_structured_tool_facts(self):
        close = "2026-09-03T07:00:00Z"
        rows = [
            {"symbol": "000001", "name": "上证指数", "price": 3942.09, "previous_close": 3941.39,
             "change": 0.7, "change_percent": 0.0178, "quote_at": close,
             "trading_date": "2026-09-03", "status": "closed"},
            {"symbol": "399001", "name": "深证成指", "price": 13625.12, "previous_close": 13611.55,
             "change": 13.57, "change_percent": 0.0997, "quote_at": close,
             "trading_date": "2026-09-03", "status": "closed"},
            {"symbol": "399006", "name": "创业板指", "price": 3312.54, "previous_close": 3312.24,
             "change": 0.3, "change_percent": 0.0091, "quote_at": close,
             "trading_date": "2026-09-03", "status": "closed"},
        ]
        items = []
        sources = []
        refs = []
        for number, row in enumerate(rows, 1):
            ref = f"index-{number}"
            excerpt = json.dumps({"finality": "official_close", "indices": [row]}, ensure_ascii=False)
            refs.append(ref)
            items.append({"evidence_ref": ref, "excerpt_text": excerpt, "fact_as_of": close,
                          "published_at": None, "acquired_at": "2026-09-03T07:10:00Z"})
            sources.append({"evidence_ref": ref, "excerpt": excerpt})
        contract = {
            "version": 4, "as_of": "2026-09-03T07:20:00Z", "requirements": [{
                "key": "indices_close", "blocking": True, "allowed_coverage": ["covered"],
                "finality": "official_close", "minimum_numeric_facts": 3,
                "evidence_terms": [["上证", "沪指"], ["深成指", "深证成指"], ["创业板"], ["涨", "跌", "%"]],
                "window": {"mode": "exact", "start": close, "end": close},
            }],
        }
        observations = [{"attempt_id": "attempt", "backend": "market", "status": "succeeded",
                         "non_empty": True, "evidence_items": items}]
        evidence = {"schema_version": 3, "as_of": contract["as_of"], "sources": sources,
                    "coverage": [{"requirement_key": "indices_close", "status": "covered",
                                  "evidence_refs": refs}], "high_impact_events": []}

        result = EvidenceGate().evaluate(evidence, contract, observations, contract["as_of"], attempt_id="attempt")

        self.assertTrue(result["passed"], result["problems"])

    def test_diagnostic_rerun_resolves_frozen_m0_attempt_from_source_cycle(self):
        evidence = {"as_of": "2026-08-31T07:20:00Z", "sources": []}
        source_attempt = {
            "attempt_id": "source-m0", "stage": "m0_research", "status": "succeeded",
            "output_json": json.dumps(evidence),
        }
        store = Mock()
        store.attempts.side_effect = lambda cycle_id: [] if cycle_id == "rerun" else [source_attempt]
        cycle = {
            "cycle_id": "rerun",
            "schedule_snapshot_json": json.dumps({
                "diagnostic_rerun": True, "diagnostic_rerun_of": "source",
            }),
        }

        self.assertEqual(source_attempt, _frozen_m0_source_attempt(store, cycle, evidence))
        self.assertEqual(["rerun", "source"], [call.args[0] for call in store.attempts.call_args_list])

    def test_gateway_reads_are_frozen_to_contract_not_later_stage_start(self):
        packet = {"as_of": "2026-08-31T05:22:27Z"}
        contract = {"as_of": "2026-08-31T05:21:50Z"}
        self.assertEqual("2026-08-31T05:21:50Z", _evidence_read_cutoff(packet, contract))

    def test_m1_reuse_keeps_the_frozen_m0_evidence_time(self):
        self.assertEqual(
            "2026-08-31T05:25:57Z",
            _m1_research_as_of({"as_of": "2026-08-31T05:25:57Z"}, None),
        )

    def test_1430_contract_requires_fresh_intraday_market_and_event_evidence(self):
        contract = EvidenceContractFactory(_WeekdayCalendar()).build(
            task_key="daily.execution.1430", stage="m0_research", as_of="2026-08-26T06:30:00Z",
            internal_context={"portfolio_entities": [], "prior_judgment_count": 1, "market_understanding_enabled": True},
        )
        market = next(item for item in contract["requirements"] if item["key"] == "current_market_state")
        events = next(item for item in contract["requirements"] if item["key"] == "material_events_and_counterevidence")

        self.assertEqual("after_start_to_end", market["window"]["mode"])
        self.assertEqual("2026-08-26T06:15:00Z", market["window"]["start"])
        self.assertEqual("2026-08-26T06:30:00Z", market["window"]["end"])
        self.assertEqual("2026-08-26T02:30:00Z", events["window"]["start"])

    def test_1430_contract_requires_overseas_and_theme_context(self):
        contract = EvidenceContractFactory(_WeekdayCalendar()).build(
            task_key="daily.execution.1430", stage="m0_research", as_of="2026-08-26T06:30:00Z",
            internal_context={"portfolio_entities": [], "prior_judgment_count": 1, "market_understanding_enabled": True},
        )
        requirements = {item["key"]: item for item in contract["requirements"]}

        self.assertEqual(["covered", "checked_no_change"], requirements["overseas_market_context"]["allowed_coverage"])
        self.assertEqual(["covered", "checked_no_change"], requirements["theme_business_and_expectations"]["allowed_coverage"])
        self.assertEqual(["日本股市", "韩国股市", "全球风险", "A股", "联动"], requirements["overseas_market_context"]["negative_query_terms"])
        self.assertEqual(["产业链", "题材", "催化", "预期", "反证"], requirements["theme_business_and_expectations"]["negative_query_terms"])
        self.assertEqual([["Japan", "日本"], ["Korea", "韩国"], ["A-share", "A股"]], requirements["overseas_market_context"]["evidence_terms"])
        self.assertEqual(2, len(requirements["theme_business_and_expectations"]["evidence_terms"]))
        self.assertEqual("internal_runtime", requirements["prior_market_understanding_changes"]["evidence_class"])
        self.assertEqual(1, requirements["prior_market_understanding_changes"]["internal_record_count"])
        self.assertEqual("2026-08-26T06:30:00Z", requirements["overseas_market_context"]["window"]["end"])

    def test_every_formal_daily_cycle_requires_market_understanding_context(self):
        factory = EvidenceContractFactory(_WeekdayCalendar())
        for task_key, as_of in (
            ("daily.opportunity.0900", "2026-08-26T01:00:00Z"),
            ("daily.execution.0945", "2026-08-26T01:45:00Z"),
            ("daily.execution.1030", "2026-08-26T02:30:00Z"),
            ("daily.execution.1430", "2026-08-26T06:30:00Z"),
            ("daily.review.1520", "2026-08-26T07:20:00Z"),
        ):
            contract = factory.build(
                task_key=task_key, stage="m0_research", as_of=as_of,
                internal_context={"portfolio_entities": [], "prior_judgment_count": 0, "market_understanding_enabled": True},
            )
            keys = {item["key"] for item in contract["requirements"]}
            self.assertTrue({
                "overseas_market_context", "theme_business_and_expectations",
                "prior_market_understanding_changes",
            }.issubset(keys), task_key)

    def test_premarket_and_early_sessions_keep_separate_fact_windows(self):
        factory = EvidenceContractFactory(_WeekdayCalendar())
        premarket = factory.build(task_key="daily.opportunity.0900", stage="m0_research", as_of="2026-08-26T00:30:00Z")
        at_0945 = factory.build(task_key="daily.execution.0945", stage="m0_research", as_of="2026-08-26T01:45:00Z")
        at_1030 = factory.build(task_key="daily.execution.1030", stage="m0_research", as_of="2026-08-26T02:30:00Z")

        premarket_market = next(item for item in premarket["requirements"] if item["key"] == "current_market_state")
        early_market = next(item for item in at_0945["requirements"] if item["key"] == "current_market_state")
        middle_market = next(item for item in at_1030["requirements"] if item["key"] == "current_market_state")
        self.assertEqual("2026-08-25T07:00:00Z", premarket_market["window"]["end"])
        self.assertEqual("2026-08-26T01:30:00Z", early_market["window"]["start"])
        self.assertEqual("2026-08-26T02:15:00Z", middle_market["window"]["start"])

    def test_baseline_contract_does_not_activate_market_understanding_before_promotion(self):
        contract = EvidenceContractFactory(_WeekdayCalendar()).build(
            task_key="daily.execution.1430", stage="m0_research", as_of="2026-08-26T06:30:00Z",
            internal_context={"portfolio_entities": [], "prior_judgment_count": 1},
        )
        keys = {item["key"] for item in contract["requirements"]}
        self.assertFalse({
            "overseas_market_context", "theme_business_and_expectations",
            "prior_market_understanding_changes",
        }.intersection(keys))

    def setUp(self):
        self.as_of = "2026-08-26T01:00:00Z"
        self.contract = EvidenceContractFactory(_WeekdayCalendar()).build(
            task_key="daily.opportunity.0900", stage="m0_research", as_of=self.as_of,
        )
        # These source/time/independence fixtures exercise the two market facts,
        # not complete premarket candidate discovery (covered by its own tests).
        self.contract["requirements"] = [row for row in self.contract["requirements"]
                                         if row["key"] != "candidate_business_research"]
        self.contract["contract_hash"] = EvidenceContractFactory.contract_hash(self.contract)
        self.planner_contract = {
            "version": 4, "as_of": self.as_of, "requirements": [{
                "key": "material_events_and_counterevidence", "blocking": True,
                "allowed_coverage": ["covered", "checked_no_change"],
                "window": {"mode": "after_start_to_end", "start": "2026-08-25T07:00:00Z", "end": self.as_of},
                "negative_query_terms": ["公告", "政策", "风险"],
            }],
        }
        self.market = next(item for item in self.contract["requirements"] if item["key"] == "current_market_state")
        self.events = next(item for item in self.contract["requirements"] if item["key"] == "material_events_and_counterevidence")
        self.observations = [{
            "attempt_id": "attempt-1", "status": "succeeded", "non_empty": True,
            "arguments": {"query": "A股 公告 政策 风险"},
            "evidence_items": [{
                "evidence_ref": "ev_attempt-1_1", "url": "https://www.sse.com.cn/close",
                "title": "收盘", "source_identity": "www.sse.com.cn", "independence_group": "sse",
                "primary": True, "excerpt_text": "8月25日收盘", "fact_as_of": "2026-08-25T07:00:00Z",
                "published_at": "2026-08-25T07:05:00Z", "acquired_at": "2026-08-26T00:50:00Z",
            }, {
                "evidence_ref": "ev_attempt-1_2", "url": "https://example.test/events",
                "title": "事件检索", "source_identity": "example.test", "independence_group": "example",
                "primary": False, "excerpt_text": "无新增重大公告", "fact_as_of": "2026-08-26T00:50:00Z",
                "published_at": "2026-08-26T00:50:00Z", "acquired_at": "2026-08-26T00:50:00Z",
            }],
        }]

    def _evidence(self, ref="ev_attempt-1_1", *, status="covered", fact_as_of="2026-08-25T07:00:00Z"):
        return {
            "schema_version": 3, "as_of": self.as_of,
            "sources": [
                {"evidence_ref": ref, "excerpt": "8月25日收盘", "analysis": "前收盘市场状态"},
                {"evidence_ref": "ev_attempt-1_2", "excerpt": "无新增重大公告", "analysis": "事件与反证"},
            ],
            "coverage": [
                {"requirement_key": "current_market_state", "status": "covered", "evidence_refs": [ref]},
                {"requirement_key": "material_events_and_counterevidence", "status": status, "evidence_refs": ["ev_attempt-1_2"]},
            ],
            "high_impact_events": [],
        }

    def test_0900_contract_binds_market_to_latest_completed_xshg_close(self):
        self.assertEqual("2026-08-25T07:00:00Z", self.market["window"]["start"])
        self.assertEqual("2026-08-25T07:00:00Z", self.market["window"]["end"])
        self.assertEqual(["covered"], self.market["allowed_coverage"])
        self.assertEqual("2026-08-25T07:00:00Z", self.events["window"]["start"])
        self.assertEqual(self.as_of, self.events["window"]["end"])

    def test_1520_review_binds_market_to_completed_close_and_events_to_cycle(self):
        as_of = "2026-08-27T07:20:02.555Z"
        contract = EvidenceContractFactory(_WeekdayCalendar()).build(
            task_key="daily.review.1520", stage="m0_research", as_of=as_of,
            internal_context={"prior_judgment_count": 3, "portfolio_entities": ["600000"], "market_understanding_enabled": True},
        )
        requirements = {row["key"]: row for row in contract["requirements"]}

        self.assertEqual(
            {"start": "2026-08-27T07:00:00Z", "end": "2026-08-27T07:00:00Z", "mode": "exact"},
            requirements["indices_close"]["window"],
        )
        self.assertEqual("2026-08-26T07:00:00Z", requirements["events_and_counterevidence"]["window"]["start"])
        self.assertEqual("2026-08-27T07:20:02.555000Z", requirements["events_and_counterevidence"]["window"]["end"])
        self.assertEqual(
            {"start": "2026-08-27T07:00:00Z", "end": "2026-08-27T07:00:00Z", "mode": "exact"},
            requirements["market_breadth"]["window"],
        )
        self.assertEqual("official_close", requirements["market_breadth"]["finality"])
        self.assertTrue(requirements["turnover_compare"]["blocking"])
        self.assertEqual(["covered"], requirements["turnover_compare"]["allowed_coverage"])
        self.assertTrue(requirements["themes_and_capacity_cores"]["blocking"])
        self.assertEqual(["covered"], requirements["themes_and_capacity_cores"]["allowed_coverage"])
        self.assertTrue(requirements["forum_and_sentiment"]["blocking"])
        self.assertEqual(["covered"], requirements["portfolio_market_state"]["allowed_coverage"])
        blockers = [row["key"] for row in contract["requirements"] if row["blocking"]]
        self.assertEqual([
            "indices_close", "turnover_compare", "market_breadth", "themes_and_capacity_cores",
            "events_and_counterevidence",
            "prior_judgment_changes", "portfolio_market_state",
            "portfolio_events_and_counterevidence", "forum_and_sentiment",
            "overseas_market_context", "theme_business_and_expectations",
            "prior_market_understanding_changes",
        ], blockers)

        rejected = EvidenceGate().evaluate(
            {"schema_version": 3, "as_of": as_of, "sources": [], "coverage": [], "high_impact_events": []},
            contract, [], as_of, attempt_id="attempt",
        )
        self.assertFalse(rejected["passed"])
        self.assertEqual(set(blockers), set(rejected["missing_requirements"]))

    def test_intraday_contract_accepts_recent_market_facts_and_since_prior_checkpoint_events(self):
        contract_0945 = EvidenceContractFactory(_WeekdayCalendar()).build(
            task_key="daily.execution.0945", stage="m0_research",
            as_of="2026-08-31T01:45:05.144Z",
        )
        market_0945, events_0945 = contract_0945["requirements"]
        self.assertEqual({
            "start": "2026-08-31T01:30:05.144000Z",
            "end": "2026-08-31T01:45:05.144000Z",
            "mode": "after_start_to_end",
        }, market_0945["window"])
        self.assertEqual("2026-08-31T01:00:00Z", events_0945["window"]["start"])
        self.assertEqual("2026-08-31T01:45:05.144000Z", events_0945["window"]["end"])

        contract_1030 = EvidenceContractFactory(_WeekdayCalendar()).build(
            task_key="daily.execution.1030", stage="m0_research",
            as_of="2026-08-31T02:30:01.533Z",
        )
        market_1030, events_1030 = contract_1030["requirements"]
        self.assertEqual("2026-08-31T02:15:01.533000Z", market_1030["window"]["start"])
        self.assertEqual("2026-08-31T01:45:00Z", events_1030["window"]["start"])

    def test_intraday_contract_passes_with_minute_precision_market_and_incremental_event(self):
        as_of = "2026-08-31T01:45:05.144Z"
        contract = EvidenceContractFactory(_WeekdayCalendar()).build(
            task_key="daily.execution.0945", stage="m0_research", as_of=as_of,
        )
        observations = [{
            "attempt_id": "intraday-attempt", "status": "succeeded", "non_empty": True,
            "backend": "gateway", "arguments": {"query": "A股 公告 政策 风险"},
            "evidence_items": [{
                "evidence_ref": "ev_market", "excerpt_text": "09:44 A股市场行情",
                "fact_as_of": "2026-08-31T01:44:00Z", "published_at": None,
                "acquired_at": as_of, "primary": True,
            }, {
                "evidence_ref": "ev_event", "excerpt_text": "09:30 新增政策反证",
                "fact_as_of": "2026-08-31T01:30:00Z", "published_at": "2026-08-31T01:30:00Z",
                "acquired_at": as_of, "primary": True,
            }],
        }]
        evidence = {
            "schema_version": 3, "as_of": as_of,
            "sources": [
                {"evidence_ref": "ev_market", "excerpt": "09:44 A股市场行情", "analysis": "当前行情"},
                {"evidence_ref": "ev_event", "excerpt": "09:30 新增政策反证", "analysis": "新增事件"},
            ],
            "coverage": [
                {"requirement_key": "current_market_state", "status": "covered", "evidence_refs": ["ev_market"]},
                {"requirement_key": "material_events_and_counterevidence", "status": "covered", "evidence_refs": ["ev_event"]},
            ],
            "high_impact_events": [],
        }

        result = EvidenceGate().evaluate(evidence, contract, observations, as_of, attempt_id="intraday-attempt")

        self.assertTrue(result["passed"], result["problems"])

    def test_intraday_contract_v4_blocks_publication_without_market_breadth_and_all_holdings(self):
        contract = EvidenceContractFactory(_WeekdayCalendar()).build(
            task_key="daily.execution.0945", stage="m0_research",
            as_of="2026-08-31T01:45:00Z",
            internal_context={"portfolio_entities": ["600487", "603861", "300421"]},
        )

        requirements = {item["key"]: item for item in contract["requirements"]}

        self.assertEqual(4, contract["version"])
        self.assertEqual(["600487", "603861", "300421"], requirements["portfolio_market_state"]["required_entities"])
        self.assertEqual(["covered"], requirements["portfolio_market_state"]["allowed_coverage"])
        self.assertEqual(["covered", "checked_no_change"], requirements["portfolio_events_and_counterevidence"]["allowed_coverage"])
        self.assertTrue(requirements["market_breadth"]["blocking"])

    def test_v4_portfolio_quotes_are_qualified_from_structured_tool_json(self):
        as_of = "2026-08-31T01:45:00Z"
        contract = {"version": 4, "as_of": as_of, "requirements": [{
            "key": "portfolio_market_state", "blocking": True, "allowed_coverage": ["covered"],
            "required_entities": ["600487", "603861"], "minimum_numeric_facts": 8,
            "window": {"mode": "after_start_to_end", "start": "2026-08-31T01:30:00Z", "end": as_of},
        }]}
        quotes = [
            {"symbol": "600487", "previous_close": 1.0, "price": 1.1, "change": 0.1, "change_percent": 10.0,
             "quote_at": "2026-08-31T01:44:00Z", "trading_date": "2026-08-31", "status": "trading"},
            {"symbol": "603861", "previous_close": 2.0, "price": 1.9, "change": -0.1, "change_percent": -5.0,
             "quote_at": "2026-08-31T01:44:00Z", "trading_date": "2026-08-31", "status": "trading"},
        ]
        sources, evidence_items = [], []
        for index, quote in enumerate(quotes, 1):
            ref = f"quote-{index}"
            excerpt = json.dumps({"quotes": [quote]}, ensure_ascii=False, sort_keys=True)
            sources.append({"evidence_ref": ref, "excerpt": excerpt})
            evidence_items.append({"evidence_ref": ref, "excerpt_text": excerpt, "fact_as_of": quote["quote_at"],
                                   "published_at": None, "acquired_at": as_of})
        observations = [{"attempt_id": "attempt", "backend": "market", "status": "succeeded", "non_empty": True,
                         "evidence_items": evidence_items}]
        evidence = {"schema_version": 3, "as_of": as_of, "sources": sources,
                    "coverage": [{"requirement_key": "portfolio_market_state", "status": "covered",
                                  "evidence_refs": ["quote-1", "quote-2"]}], "high_impact_events": []}

        self.assertTrue(EvidenceGate().evaluate(evidence, contract, observations, as_of, attempt_id="attempt")["passed"])
        quotes[1].pop("change_percent")
        excerpt = json.dumps({"quotes": [quotes[1]]}, ensure_ascii=False, sort_keys=True)
        evidence["sources"][1]["excerpt"] = excerpt
        observations[0]["evidence_items"][1]["excerpt_text"] = excerpt
        failed = EvidenceGate().evaluate(evidence, contract, observations, as_of, attempt_id="attempt")
        self.assertIn("blocking_requirement_lacks_numeric_facts:portfolio_market_state", failed["problems"])

    def test_v4_portfolio_current_bars_are_qualified_from_structured_tool_json(self):
        as_of = "2026-09-04T06:30:05Z"
        contract = {"version": 4, "as_of": as_of, "requirements": [{
            "key": "portfolio_current_bar", "blocking": True, "allowed_coverage": ["covered"],
            "required_entities": ["600487", "603861"], "minimum_numeric_facts": 8,
            "window": {"mode": "after_start_to_end", "start": "2026-09-04T06:25:05Z", "end": as_of},
        }]}
        bars = [{
            "symbol": symbol, "freq": "1m", "trade_time": "2026-09-04T14:29:00+08:00",
            "interval_start": "2026-09-04T14:29:00+08:00", "interval_end": "2026-09-04T14:30:00+08:00",
            "open": base, "high": base + 0.2, "low": base - 0.1, "close": base + 0.1,
            "volume": 1000.0, "amount": 10000.0, "is_final": False,
            "observed_at": "2026-09-04T14:30:00+08:00", "market_status": "trading",
            "provider": "tencent_minute", "source_semantics": "derived",
        } for symbol, base in (("600487", 10.0), ("603861", 20.0))]
        excerpt = json.dumps({"bars": bars, "finality": "intraday"}, ensure_ascii=False, sort_keys=True)
        evidence = {
            "schema_version": 3, "as_of": as_of,
            "sources": [{"evidence_ref": "bars", "excerpt": excerpt}],
            "coverage": [{"requirement_key": "portfolio_current_bar", "status": "covered",
                          "evidence_refs": ["bars"]}],
            "high_impact_events": [],
        }
        observations = [{
            "attempt_id": "attempt", "backend": "market", "status": "succeeded", "non_empty": True,
            "evidence_items": [{"evidence_ref": "bars", "excerpt_text": excerpt,
                                "fact_as_of": "2026-09-04T06:30:00Z", "published_at": None,
                                "acquired_at": as_of}],
        }]

        result = EvidenceGate().evaluate(evidence, contract, observations, as_of, attempt_id="attempt")

        self.assertTrue(result["passed"], result["problems"])
        bars[1].pop("provider")
        malformed_excerpt = json.dumps({"bars": bars, "finality": "intraday"}, ensure_ascii=False, sort_keys=True)
        evidence["sources"][0]["excerpt"] = malformed_excerpt
        observations[0]["evidence_items"][0]["excerpt_text"] = malformed_excerpt
        malformed = EvidenceGate().evaluate(evidence, contract, observations, as_of, attempt_id="attempt")
        self.assertIn("blocking_requirement_lacks_numeric_facts:portfolio_current_bar", malformed["problems"])

    def test_v4_market_breadth_is_qualified_from_structured_tool_json(self):
        as_of = "2026-08-31T01:45:00Z"
        contract = {"version": 4, "as_of": as_of, "requirements": [{
            "key": "market_breadth", "blocking": True, "allowed_coverage": ["covered"],
            "minimum_numeric_facts": 3,
            "evidence_terms": [["上涨"], ["下跌"], ["家", "只"]],
            "window": {"mode": "after_start_to_end", "start": "2026-08-31T01:30:00Z", "end": as_of},
        }]}
        excerpt = json.dumps({"breadth": {"up": 632, "down": 2203, "flat": 57}}, ensure_ascii=False)
        observations = [{"attempt_id": "attempt", "backend": "market", "status": "succeeded", "non_empty": True,
                         "evidence_items": [{"evidence_ref": "breadth", "excerpt_text": excerpt,
                                             "fact_as_of": "2026-08-31T01:44:00Z", "published_at": None, "acquired_at": as_of}]}]
        evidence = {"schema_version": 3, "as_of": as_of, "sources": [{"evidence_ref": "breadth", "excerpt": excerpt}],
                    "coverage": [{"requirement_key": "market_breadth", "status": "covered", "evidence_refs": ["breadth"]}],
                    "high_impact_events": []}

        self.assertTrue(EvidenceGate().evaluate(evidence, contract, observations, as_of, attempt_id="attempt")["passed"])
        excerpt = json.dumps({"breadth": {"up": 632, "down": 2203}}, ensure_ascii=False)
        evidence["sources"][0]["excerpt"] = excerpt
        observations[0]["evidence_items"][0]["excerpt_text"] = excerpt
        failed = EvidenceGate().evaluate(evidence, contract, observations, as_of, attempt_id="attempt")
        self.assertIn("blocking_requirement_lacks_numeric_facts:market_breadth", failed["problems"])

    def test_v4_official_close_breadth_at_the_frozen_close_satisfies_the_contract(self):
        close = "2026-09-02T07:00:00Z"
        contract = {"version": 4, "as_of": "2026-09-02T08:20:00Z", "requirements": [{
            "key": "market_breadth", "blocking": True, "allowed_coverage": ["covered"],
            "minimum_numeric_facts": 3,
            "window": {"mode": "exact", "start": close, "end": close},
        }]}
        excerpt = json.dumps({
            "breadth": {"universe_count": 5554, "up": 1541, "down": 3901, "flat": 105,
                        "suspended": 7, "unpriced": 0, "coverage_ratio": 1.0},
            "finality": "official_close",
        }, ensure_ascii=False)
        observations = [{
            "attempt_id": "attempt", "backend": "market", "status": "succeeded", "non_empty": True,
            "evidence_items": [{"evidence_ref": "breadth", "excerpt_text": excerpt,
                                "fact_as_of": close, "published_at": None,
                                "acquired_at": "2026-09-02T08:20:19Z"}],
        }]
        evidence = {
            "schema_version": 3, "as_of": contract["as_of"],
            "sources": [{"evidence_ref": "breadth", "excerpt": excerpt}],
            "coverage": [{"requirement_key": "market_breadth", "status": "covered",
                          "evidence_refs": ["breadth"]}],
            "high_impact_events": [],
        }

        result = EvidenceGate().evaluate(evidence, contract, observations, contract["as_of"], attempt_id="attempt")

        self.assertTrue(result["passed"], result["problems"])

    def test_v4_directional_sector_fund_flow_counts_only_verified_numeric_leaders(self):
        close = "2026-09-04T07:00:00Z"
        contract = {"version": 4, "as_of": "2026-09-05T02:00:00Z", "requirements": [{
            "key": "market_fund_flow", "blocking": True, "allowed_coverage": ["covered"],
            "minimum_numeric_facts": 3,
            "window": {"mode": "exact", "start": close, "end": close},
        }]}
        payload = {
            "trading_date": "2026-09-04",
            "coverage_level": "directional_sector",
            "currency": "CNY",
            "sector_inflow_leaders": [
                {"name": "虚拟数字人", "direction": "inflow", "rank": 1, "net_inflow": 5_281_000_000.0},
                {"name": "AI应用", "direction": "inflow", "rank": 2, "net_inflow": 5_163_000_000.0},
                {"name": "文化传媒概念", "direction": "inflow", "rank": 3, "net_inflow": 4_208_000_000.0},
            ],
            "sector_outflow_leaders": [{"name": "电子", "direction": "outflow", "rank": 1}],
            "limitations": ["full_market_net_flow_unavailable", "order_size_breakdown_unavailable"],
        }
        excerpt = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        evidence = {
            "schema_version": 3, "as_of": contract["as_of"],
            "sources": [{"evidence_ref": "flow", "excerpt": excerpt}],
            "coverage": [{"requirement_key": "market_fund_flow", "status": "covered",
                          "evidence_refs": ["flow"]}],
            "high_impact_events": [],
        }
        observations = [{
            "attempt_id": "attempt", "backend": "market", "status": "succeeded", "non_empty": True,
            "evidence_items": [{"evidence_ref": "flow", "excerpt_text": excerpt,
                                "fact_as_of": close, "published_at": None,
                                "acquired_at": contract["as_of"]}],
        }]

        passed = EvidenceGate().evaluate(
            evidence, contract, observations, contract["as_of"], attempt_id="attempt",
        )
        self.assertTrue(passed["passed"], passed["problems"])

        payload["sector_inflow_leaders"][2].pop("net_inflow")
        excerpt = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        evidence["sources"][0]["excerpt"] = excerpt
        observations[0]["evidence_items"][0]["excerpt_text"] = excerpt
        failed = EvidenceGate().evaluate(
            evidence, contract, observations, contract["as_of"], attempt_id="attempt",
        )
        self.assertIn("blocking_requirement_lacks_numeric_facts:market_fund_flow", failed["problems"])

    def test_v4_directional_fund_flow_combines_verified_article_sides(self):
        close = "2026-09-04T07:00:00Z"
        contract = {"version": 4, "as_of": "2026-09-06T05:58:23Z", "requirements": [{
            "key": "market_fund_flow", "blocking": True, "allowed_coverage": ["covered"],
            "minimum_numeric_facts": 3,
            "window": {"mode": "exact", "start": close, "end": close},
        }]}
        payloads = [{
            "coverage_level": "directional_sector",
            "title": "A股收评",
            "sector_inflow_leaders": [
                {"name": "虚拟数字人", "net_inflow": 5_281_000_000.0, "unit": "CNY"},
                {"name": "AI应用", "net_inflow": 5_163_000_000.0, "unit": "CNY"},
                {"name": "文化传媒概念", "net_inflow": 4_208_000_000.0, "unit": "CNY"},
            ],
            "sector_outflow_leaders": [],
        }, {
            "coverage_level": "directional_sector",
            "title": "数据看盘",
            "sector_inflow_leaders": [],
            "sector_outflow_leaders": [{"name": "电子"}],
        }]
        refs = ["flow-in", "flow-out"]
        evidence = {
            "schema_version": 3, "as_of": contract["as_of"],
            "sources": [
                {"evidence_ref": ref, "excerpt": json.dumps(payload, ensure_ascii=False)}
                for ref, payload in zip(refs, payloads)
            ],
            "coverage": [{"requirement_key": "market_fund_flow", "status": "covered",
                          "evidence_refs": refs}],
            "high_impact_events": [],
        }
        observations = [{
            "attempt_id": "attempt", "backend": "market", "status": "succeeded", "non_empty": True,
            "evidence_items": [{
                "evidence_ref": ref, "url": url, "excerpt_text": json.dumps(payload, ensure_ascii=False),
                "fact_as_of": close, "published_at": None, "acquired_at": contract["as_of"],
            } for ref, url, payload in zip(
                refs,
                ["https://news.10jqka.com.cn/20260904/close.shtml", "https://www.cls.cn/detail/close"],
                payloads,
            )],
        }]

        result = EvidenceGate().evaluate(
            evidence, contract, observations, contract["as_of"], attempt_id="attempt",
        )

        self.assertTrue(result["passed"], result["problems"])

        conflict = {
            "coverage_level": "directional_sector",
            "sector_inflow_leaders": [
                {"name": "AI应用", "net_inflow": 1.0, "unit": "CNY"},
            ],
            "sector_outflow_leaders": [],
        }
        evidence["sources"].append({
            "evidence_ref": "flow-conflict", "excerpt": json.dumps(conflict, ensure_ascii=False),
        })
        evidence["coverage"][0]["evidence_refs"].append("flow-conflict")
        observations[0]["evidence_items"].append({
            "evidence_ref": "flow-conflict", "url": "https://example.test/conflict",
            "excerpt_text": json.dumps(conflict, ensure_ascii=False), "fact_as_of": close,
            "published_at": None, "acquired_at": contract["as_of"],
        })

        rejected = EvidenceGate().evaluate(
            evidence, contract, observations, contract["as_of"], attempt_id="attempt",
        )

        self.assertIn("blocking_requirement_fund_flow_scope_invalid:market_fund_flow", rejected["problems"])

    def test_m0_rejects_utc_clock_and_requires_local_quote_time_and_status(self):
        packet = {
            "calendar_context": {},
            "evidence_contract": {"requirements": [{"key": "portfolio_market_state", "required_entities": ["600487"]}]},
            "verified_fact_digest": [{"excerpt": json.dumps({"quotes": [{
                "symbol": "600487", "price": 67.97, "previous_close": 67.34, "change": 0.63,
                "change_percent": 0.9356, "quote_at": "2026-09-02T05:22:00Z", "status": "trading",
            }]})}],
        }
        output = {"semantic": {"summary": "600487 价格67.97，前收67.34，变动0.63，变动幅度0.9356%，处于交易状态，截至今天早上五点二十二分。",
                               "observations": [], "risks": [], "unknowns": []}}
        rejected = CognitiveRouter().verify("m0_compose", packet, output)
        self.assertIn("m0_portfolio_quote_time_conflict:600487", rejected["problems"])

        output["semantic"]["summary"] = "600487 价格67.97，前收67.34，变动0.63，变动幅度0.9356%，处于交易状态，北京时间13:22。"
        self.assertTrue(CognitiveRouter().verify("m0_compose", packet, output)["passed"])

    def test_m0_verifier_rejects_unverified_market_numbers_without_rewriting_interpretation(self):
        packet = {"stage": "m0_compose", "verified_fact_digest": [{"excerpt": json.dumps({
            "indices": [{"name": "上证指数", "price": 3900, "previous_close": 4000, "change": -100, "change_percent": -2.5}],
            "breadth": {"up": 1, "down": 2, "flat": 3},
            "quotes": [{"name": "样本", "symbol": "600487", "price": 10, "previous_close": 9, "change": 1,
                        "change_percent": 11.1, "quote_at_china": "北京时间2026-08-31 13:22", "status": "trading"}],
        })}]}
        result = {"semantic": {"summary": "错误价格99", "observations": ["错误"], "risks": [], "unknowns": []}}
        text = " ".join([result["semantic"]["summary"], *result["semantic"]["observations"]])
        verdict = CognitiveRouter().verify("m0_compose", packet, result)

        self.assertIn("错误价格99", text)
        self.assertIn("m0_contains_unverified_numeric_claim:99", verdict["problems"])

    def test_m0_verifier_rejects_gap_claims_that_conflict_with_frozen_coverage(self):
        packet = {
            "stage": "m0_compose",
            "evidence": {
                "coverage": [
                    {"requirement_key": "themes_and_capacity_cores", "status": "covered"},
                    {"requirement_key": "market_fund_flow", "status": "covered"},
                    {"requirement_key": "portfolio_events_and_counterevidence", "status": "covered"},
                ],
                "critical_gaps": [],
            },
        }
        output = {"semantic": {
            "summary": "本周市场偏弱。", "observations": [], "risks": [],
            "unknowns": ["现有证据未提供行业涨跌分布、资金流向及个股层面的公告影响，无法判断具体驱动。"],
        }}

        verdict = CognitiveRouter().verify("m0_compose", packet, output)

        self.assertIn("m0_claims_covered_evidence_gap:themes_and_capacity_cores", verdict["problems"])
        self.assertIn("m0_claims_covered_evidence_gap:market_fund_flow", verdict["problems"])
        self.assertIn("m0_claims_covered_evidence_gap:portfolio_events_and_counterevidence", verdict["problems"])

    def test_m0_verifier_keeps_the_useful_observation_primary_and_uses_only_brief_fact_support(self):
        packet = {"stage": "m0_compose", "verified_fact_digest": [{"excerpt": json.dumps({
            "indices": [
                {"name": "上证指数", "price": 3959.46, "previous_close": 3941.39, "change": 18.07, "change_percent": 0.4585},
                {"name": "深证成指", "price": 13667.53, "previous_close": 13611.55, "change": 55.98, "change_percent": 0.4113},
                {"name": "创业板指", "price": 3329.65, "previous_close": 3312.24, "change": 17.41, "change_percent": 0.5256},
            ],
            "breadth": {"up": 1505, "down": 1227, "flat": 163},
            "quotes": [
                {"name": "力星股份", "symbol": "300421", "price": 17.0, "previous_close": 16.8, "change": 0.2, "change_percent": 1.1905, "quote_at_china": "北京时间2026-09-03 09:45", "status": "trading"},
                {"name": "白云电器", "symbol": "603861", "price": 11.77, "previous_close": 11.65, "change": 0.12, "change_percent": 1.03, "quote_at_china": "北京时间2026-09-03 09:45", "status": "trading"},
            ],
        })}]}
        summary = "开盘整体偏暖，但强度一般；你持有的两只票都略强于指数，目前没有出现需要立刻处理的异常。"

        result = {"semantic": {
            "summary": summary,
            "observations": ["三大指数小幅上涨，市场上涨家数略多于下跌家数。"],
            "risks": ["开盘强度还不够，后续若量价不能扩散，容易回落。"],
            "unknowns": ["成交额能否继续放大。"],
        }}

        self.assertEqual(summary, result["semantic"]["summary"])
        self.assertLessEqual(len(result["semantic"]["observations"]), 2)
        self.assertTrue(CognitiveRouter().verify("m0_compose", packet, result)["passed"])
        rendered = " ".join([result["semantic"]["summary"], *result["semantic"]["observations"]])
        self.assertNotIn("本阶段为 M0", rendered)
        self.assertNotIn("确定性投影", rendered)

    def test_m0_expression_verifier_accepts_interpretation_without_repeating_every_holding_quote(self):
        packet = {
            "stage": "m0_compose",
            "evidence_contract": {"requirements": [{
                "key": "portfolio_market_state", "required_entities": ["300421", "603861"],
            }]},
            "verified_fact_digest": [{"excerpt": json.dumps({"quotes": [
                {"name": "力星股份", "symbol": "300421", "price": 17.0, "previous_close": 16.8,
                 "change": 0.2, "change_percent": 1.1905, "quote_at": "2026-09-03T01:45:00Z", "status": "trading"},
                {"name": "白云电器", "symbol": "603861", "price": 11.77, "previous_close": 11.65,
                 "change": 0.12, "change_percent": 1.03, "quote_at": "2026-09-03T01:45:00Z", "status": "trading"},
            ]})}],
        }
        output = {"semantic": {
            "summary": "开盘整体偏暖，但强度一般；两只持仓都略强于指数，暂时没有独立异常。",
            "observations": ["上涨家数略多于下跌家数，但还不是强势普涨。"],
            "risks": ["强度若不能扩散，早盘优势容易收窄。"],
            "unknowns": [],
        }}

        verdict = CognitiveRouter().verify("m0_compose", packet, output)

        self.assertTrue(verdict["passed"], verdict["problems"])

    def test_m0_expression_verifier_rejects_internal_process_language_and_unverified_numbers(self):
        packet = {"stage": "m0_compose", "verified_fact_digest": [{"excerpt": json.dumps({
            "indices": [{"name": "上证指数", "price": 3959.46, "change_percent": 0.4585}],
        })}]}
        internal = {"semantic": {
            "summary": "本阶段为 M0 客观观察；以下字段由冻结工具结果确定性投影。",
            "observations": [], "risks": [], "unknowns": [],
        }}
        invented = {"semantic": {
            "summary": "上证指数现在是99点。", "observations": [], "risks": [], "unknowns": [],
        }}

        internal_verdict = CognitiveRouter().verify("m0_compose", packet, internal)
        invented_verdict = CognitiveRouter().verify("m0_compose", packet, invented)

        self.assertIn("m0_exposes_internal_process", internal_verdict["problems"])
        self.assertIn("m0_contains_unverified_numeric_claim:99", invented_verdict["problems"])

    def test_m0_expression_verifier_accepts_truthful_display_rounding_only(self):
        packet = {"stage": "m0_compose", "verified_fact_digest": [{"excerpt": json.dumps({
            "indices": [{"name": "上证指数", "price": 3959.46, "change_percent": 0.4585}],
        })}]}
        rounded = {"semantic": {
            "summary": "上证指数约3959点，涨幅约0.5%，盘面偏暖但不算强。",
            "observations": [], "risks": [], "unknowns": [],
        }}
        unrelated = {"semantic": {
            "summary": "上证指数涨幅约0.7%，盘面偏暖但不算强。",
            "observations": [], "risks": [], "unknowns": [],
        }}

        accepted = CognitiveRouter().verify("m0_compose", packet, rounded)
        rejected = CognitiveRouter().verify("m0_compose", packet, unrelated)

        self.assertTrue(accepted["passed"], accepted["problems"])
        self.assertIn("m0_contains_unverified_numeric_claim:0.7", rejected["problems"])

    def test_m0_expression_verifier_accepts_positive_signs_and_trailing_zeroes(self):
        packet = {"stage": "m0_compose", "verified_fact_digest": [{"excerpt": json.dumps({
            "quotes": [{"name": "力星股份", "price": 16.8, "change": 0.03, "change_percent": 0.2575}],
        })}]}
        output = {"semantic": {
            "summary": "力星股份报16.80，较前收上涨+0.03，涨幅+0.2575%。",
            "observations": [], "risks": [], "unknowns": [],
        }}

        verdict = CognitiveRouter().verify("m0_compose", packet, output)

        self.assertTrue(verdict["passed"], verdict["problems"])
        wrong_sign = {"semantic": {
            "summary": "力星股份变动-0.03。", "observations": [], "risks": [], "unknowns": [],
        }}
        packet["verified_fact_digest"][0]["excerpt"] = json.dumps({
            "quotes": [{"symbol": "002150", "change": 0.03}],
        })

        self.assertIn(
            "m0_contains_unverified_numeric_claim:-0.03",
            CognitiveRouter().verify("m0_compose", packet, wrong_sign)["problems"],
        )

    def test_rejects_foreign_reference_and_naive_runtime_time(self):
        foreign = EvidenceGate().evaluate(self._evidence("ev_other_1"), self.contract, self.observations, self.as_of, attempt_id="attempt-1")
        self.assertIn("source_ref_not_in_current_attempt", foreign["problems"])
        self.observations[0]["evidence_items"][0]["fact_as_of"] = "2026-08-25T15:00:00"
        naive = EvidenceGate().evaluate(self._evidence(), self.contract, self.observations, self.as_of, attempt_id="attempt-1")
        self.assertIn("source_fact_as_of_missing_timezone", naive["problems"])

    def test_historical_replay_allows_acquisition_after_frozen_as_of(self):
        self.observations[0]["evidence_items"][0]["acquired_at"] = "2026-08-27T07:20:00Z"
        self.observations[0]["evidence_items"][1]["acquired_at"] = "2026-08-27T07:20:00Z"

        result = EvidenceGate().evaluate(
            self._evidence(), self.contract, self.observations, self.as_of, attempt_id="attempt-1",
        )

        self.assertTrue(result["passed"], result["problems"])

    def test_checked_no_change_requires_matching_current_attempt_query(self):
        self.observations[0]["arguments"] = {"query": "A股 公告"}
        result = EvidenceGate().evaluate(self._evidence(status="checked_no_change"), self.contract, self.observations, self.as_of, attempt_id="attempt-1")
        self.assertIn("checked_no_change_query_not_matched:material_events_and_counterevidence", result["problems"])

    def test_checked_no_change_with_matching_query_is_allowed(self):
        self.assertTrue(EvidenceGate().evaluate(self._evidence(status="checked_no_change"), self.contract, self.observations, self.as_of, attempt_id="attempt-1")["passed"])

    def test_checked_no_change_without_traceable_results_is_rejected_even_when_query_matches(self):
        evidence = self._evidence(status="checked_no_change")
        evidence["coverage"][1]["evidence_refs"] = []

        result = EvidenceGate().evaluate(
            evidence, self.contract, self.observations, self.as_of, attempt_id="attempt-1",
        )

        self.assertIn(
            "checked_no_change_untraceable:material_events_and_counterevidence",
            result["problems"],
        )

    def test_high_impact_fact_needs_primary_or_independent_corroboration(self):
        evidence = self._evidence()
        self.observations[0]["evidence_items"][1]["market_propagation"] = "observed"
        evidence["high_impact_events"] = [{
            "summary": "传闻正在影响市场", "scope": "market", "materiality": "high",
            "evidence_refs": ["ev_attempt-1_2"], "truth_status": "unverified",
            "propagation_status": "observed", "truth_evidence_refs": [],
            "propagation_evidence_refs": ["ev_attempt-1_2"],
        }]
        result = EvidenceGate().evaluate(evidence, self.contract, self.observations, self.as_of, attempt_id="attempt-1")
        self.assertNotIn("high_impact_fact_lacks_primary_or_independent_confirmation", result["problems"])

    def test_acquisition_preserves_origin_and_collapses_reposts_into_one_chain(self):
        boundary = AcquisitionBoundary("attempt-1")
        observation, _ = boundary.observe("web_read", {}, {"results": [{
            "url": "https://mirror-a.test/story?utm_source=x", "title": "转载稿",
            "text": "同一通讯社原稿", "author": "记者甲", "publisher": "媒体甲",
            "original_source": "https://agency.test/wire/42", "citation_chain": ["agency.test"],
            "published_at": "2026-08-26T00:30:00Z",
        }, {
            "url": "https://mirror-b.test/story", "title": "另一转载",
            "text": "同一通讯社原稿", "publisher": "媒体乙",
            "original_source": "https://agency.test/wire/42", "published_at": "2026-08-26T00:31:00Z",
        }]}, True)

        first, second = observation["evidence_items"]
        self.assertEqual("https://mirror-a.test/story", first["canonical_url"])
        self.assertEqual("记者甲", first["author"])
        self.assertEqual("媒体甲", first["publisher"])
        self.assertEqual("https://agency.test/wire/42", first["original_source"])
        self.assertEqual(first["independence_group"], second["independence_group"])
        self.assertTrue(first["content_fingerprint"].startswith("sha256:"))

    def test_reposts_do_not_satisfy_independent_high_impact_confirmation(self):
        evidence = self._evidence()
        copied = dict(self.observations[0]["evidence_items"][1])
        copied.update({
            "evidence_ref": "ev_attempt-1_3", "url": "https://mirror.test/events",
            "canonical_url": "https://mirror.test/events", "source_identity": "mirror.test",
            "independence_group": "origin:https://agency.test/wire/42",
            "original_source": "https://agency.test/wire/42",
        })
        self.observations[0]["evidence_items"][1].update({
            "independence_group": "origin:https://agency.test/wire/42",
            "original_source": "https://agency.test/wire/42",
        })
        self.observations[0]["evidence_items"].append(copied)
        evidence["sources"].append({
            "evidence_ref": "ev_attempt-1_3", "excerpt": "无新增重大公告", "analysis": "转载确认",
        })
        evidence["high_impact_events"] = [{
            "summary": "重大事件已发生", "scope": "market", "materiality": "high",
            "evidence_refs": ["ev_attempt-1_2", "ev_attempt-1_3"], "truth_status": "verified",
            "propagation_status": "unknown", "truth_evidence_refs": ["ev_attempt-1_2", "ev_attempt-1_3"],
            "propagation_evidence_refs": [],
        }]

        result = EvidenceGate().evaluate(evidence, self.contract, self.observations, self.as_of, attempt_id="attempt-1")

        self.assertIn("high_impact_fact_lacks_primary_or_independent_confirmation", result["problems"])

    def test_two_genuinely_independent_sources_confirm_a_high_impact_fact(self):
        evidence = self._evidence()
        independent = dict(self.observations[0]["evidence_items"][1])
        independent.update({
            "evidence_ref": "ev_attempt-1_3", "url": "https://independent.test/events",
            "source_identity": "independent.test", "independence_group": "independent.test",
        })
        self.observations[0]["evidence_items"][1]["independence_group"] = "example.test"
        self.observations[0]["evidence_items"].append(independent)
        evidence["sources"].append({
            "evidence_ref": "ev_attempt-1_3", "excerpt": "无新增重大公告", "analysis": "独立确认",
        })
        evidence["high_impact_events"] = [{
            "event_id": "confirmed-event-20260826", "summary": "重大事件已独立确认", "scope": "market", "materiality": "high",
            "evidence_refs": ["ev_attempt-1_2", "ev_attempt-1_3"], "truth_status": "verified",
            "propagation_status": "not_observed",
            "truth_evidence_refs": ["ev_attempt-1_2", "ev_attempt-1_3"],
            "propagation_evidence_refs": [], "origin_evidence_refs": ["ev_attempt-1_2"],
        }]

        result = EvidenceGate().evaluate(evidence, self.contract, self.observations, self.as_of, attempt_id="attempt-1")

        self.assertTrue(result["passed"], result["problems"])

    def test_truth_and_market_propagation_are_qualified_independently(self):
        evidence = self._evidence()
        self.observations[0]["evidence_items"][1]["market_propagation"] = "observed"
        evidence["high_impact_events"] = [{
            "event_id": "refuted-rumor-20260826", "summary": "传闻已被官方否认但仍广泛传播", "scope": "market", "materiality": "high",
            "evidence_refs": ["ev_attempt-1_1", "ev_attempt-1_2"], "truth_status": "refuted",
            "propagation_status": "observed", "truth_evidence_refs": ["ev_attempt-1_1"],
            "propagation_evidence_refs": ["ev_attempt-1_2"], "origin_evidence_refs": ["ev_attempt-1_2"],
            "propagation_observed_from": "2026-08-26T00:40:00Z",
            "propagation_observed_to": self.as_of,
        }]

        result = EvidenceGate().evaluate(evidence, self.contract, self.observations, self.as_of, attempt_id="attempt-1")

        self.assertTrue(result["passed"], result["problems"])
        event = result["normalized_evidence"]["high_impact_events"][0]
        self.assertEqual("refuted", event["truth_status"])
        self.assertEqual("observed", event["propagation_status"])

    def test_observed_rumor_requires_a_trackable_origin_and_observation_range(self):
        evidence = self._evidence()
        self.observations[0]["evidence_items"][1]["market_propagation"] = "observed"
        evidence["high_impact_events"] = [{
            "event_id": "rumor-20260826", "summary": "rumor remains in circulation",
            "scope": "market", "materiality": "high", "evidence_refs": ["ev_attempt-1_2"],
            "truth_status": "unverified", "propagation_status": "observed",
            "truth_evidence_refs": [], "propagation_evidence_refs": ["ev_attempt-1_2"],
            "origin_evidence_refs": ["ev_attempt-1_2"],
            "propagation_observed_from": "2026-08-26T00:40:00Z",
            "propagation_observed_to": self.as_of,
        }]

        accepted = EvidenceGate().evaluate(evidence, self.contract, self.observations, self.as_of, attempt_id="attempt-1")
        self.assertTrue(accepted["passed"], accepted["problems"])

        evidence["high_impact_events"][0].pop("origin_evidence_refs")
        evidence["high_impact_events"][0].pop("propagation_observed_to")
        rejected = EvidenceGate().evaluate(evidence, self.contract, self.observations, self.as_of, attempt_id="attempt-1")
        self.assertIn("event_origin_evidence_missing", rejected["problems"])
        self.assertIn("event_propagation_observed_to_missing", rejected["problems"])

    def test_equal_tier_exact_fact_conflict_blocks_only_dependent_requirement(self):
        evidence = self._evidence()
        conflict_ref = "ev_attempt-1_3"
        for item, value, group in (
            (self.observations[0]["evidence_items"][1], 123.0, "media-a"),
            ({
                "evidence_ref": conflict_ref, "url": "https://media-b.test/flow",
                "title": "资金数据", "source_identity": "media-b.test", "independence_group": "media-b",
                "primary": False, "excerpt_text": "净流入124亿元", "fact_as_of": "2026-08-26T00:50:00Z",
                "published_at": "2026-08-26T00:50:00Z", "acquired_at": "2026-08-26T00:50:00Z",
            }, 124.0, "media-b"),
        ):
            item["independence_group"] = group
            item["source_tier"] = "secondary"
            item["claims"] = [{
                "proposition": "market_fund_flow", "field": "net_inflow", "value": value,
                "unit": "亿元", "scope": "SSE+SZSE", "fact_as_of": "2026-08-26T00:50:00Z",
                "precision": "exact", "requirement_key": "material_events_and_counterevidence",
            }]
            if item.get("evidence_ref") == conflict_ref:
                self.observations[0]["evidence_items"].append(item)
        evidence["sources"].append({"evidence_ref": conflict_ref, "excerpt": "净流入124亿元", "analysis": "资金数据"})
        evidence["coverage"][1]["evidence_refs"].append(conflict_ref)

        result = EvidenceGate().evaluate(evidence, self.contract, self.observations, self.as_of, attempt_id="attempt-1")

        self.assertIn("blocking_requirement_conflicted:material_events_and_counterevidence", result["problems"])
        self.assertNotIn("blocking_requirement_conflicted:current_market_state", result["problems"])
        conflict = result["normalized_evidence"]["conflicts"][0]
        self.assertEqual("unresolved_equal_tier", conflict["resolution"])
        self.assertEqual([123.0, 124.0], sorted(row["value"] for row in conflict["observations"]))

    def test_different_numeric_scopes_are_explained_without_averaging(self):
        evidence = self._evidence()
        second = dict(self.observations[0]["evidence_items"][1])
        second.update({
            "evidence_ref": "ev_attempt-1_3", "url": "https://official-b.test/flow",
            "source_identity": "official-b.test", "independence_group": "official-b",
            "primary": True, "source_tier": "primary_structured", "excerpt_text": "沪市净流入80亿元",
            "claims": [{
                "proposition": "market_fund_flow", "field": "net_inflow", "value": 80.0,
                "unit": "亿元", "scope": "SSE", "fact_as_of": "2026-08-26T00:50:00Z",
                "precision": "exact", "requirement_key": "material_events_and_counterevidence",
            }],
        })
        self.observations[0]["evidence_items"][1].update({
            "primary": True, "source_tier": "primary_structured", "claims": [{
                "proposition": "market_fund_flow", "field": "net_inflow", "value": 120.0,
                "unit": "亿元", "scope": "SSE+SZSE", "fact_as_of": "2026-08-26T00:50:00Z",
                "precision": "exact", "requirement_key": "material_events_and_counterevidence",
            }],
        })
        self.observations[0]["evidence_items"].append(second)
        evidence["sources"].append({
            "evidence_ref": "ev_attempt-1_3", "excerpt": "沪市净流入80亿元", "analysis": "沪市口径",
        })
        evidence["coverage"][1]["evidence_refs"].append("ev_attempt-1_3")

        result = EvidenceGate().evaluate(evidence, self.contract, self.observations, self.as_of, attempt_id="attempt-1")

        self.assertTrue(result["passed"], result["problems"])
        conflict = result["normalized_evidence"]["conflicts"][0]
        self.assertEqual("scope_difference", conflict["resolution"])
        self.assertNotIn("average", conflict)

    def test_natural_judgment_must_explain_scope_and_truth_propagation_boundaries(self):
        packet = {"task_key": "manual.analysis", "evidence": {
            "conflicts": [{"resolution": "scope_difference", "materiality": "medium"}],
            "high_impact_events": [{
                "truth_status": "refuted", "propagation_status": "observed",
            }],
        }}
        omitted = safe_stage_output("m1_judgment")

        rejected = CognitiveRouter().verify("m1_judgment", packet, omitted)

        self.assertIn("judgment_omits_explainable_source_scope_conflict", rejected["problems"])
        self.assertIn("judgment_omits_observed_market_propagation", rejected["problems"])
        self.assertIn("judgment_omits_event_refutation", rejected["problems"])

        explained = json.loads(json.dumps(omitted, ensure_ascii=False))
        explained["semantic"]["summary"] = "两个数字的统计口径不同；相关说法已被否认，但传播影响仍在。"
        accepted = CognitiveRouter().verify("m1_judgment", packet, explained)
        self.assertTrue(accepted["passed"], accepted["problems"])

    def test_terminal_attempt_cannot_be_finalized_twice(self):
        with TemporaryDirectory() as temporary:
            store = CompanionStore(Path(temporary) / "companion.sqlite3")
            cycle = CompanionEngine(store).start_cycle("daily.execution.0945", "2026-08-26T09:45:00+08:00", self.as_of)
            attempt = store.begin_attempt(cycle["cycle_id"], "m0_research", self.as_of, "packet")
            store.finish_attempt(attempt["attempt_id"], "rejected", verifier={"passed": False})
            with self.assertRaisesRegex(ValueError, "already terminal"):
                store.finish_attempt(attempt["attempt_id"], "failed", error="again")

    def test_rejected_evidence_records_failure_event_and_only_operational_fault_artifact(self):
        with TemporaryDirectory() as temporary:
            store = CompanionStore(Path(temporary) / "companion.sqlite3")
            engine = CompanionEngine(store)
            cycle = engine.start_cycle("daily.execution.0945", "2026-08-26T09:45:00+08:00", self.as_of)
            engine.research_started(cycle["cycle_id"])
            engine.research_failed(cycle["cycle_id"], "evidence rejected", details={"problems": ["foreign ref"]})

            artifacts = store.artifacts(cycle["cycle_id"])
            self.assertEqual(["system_fault"], [artifact["kind"] for artifact in artifacts])
            fault = artifacts[0]
            metadata = json.loads(fault["metadata_json"])
            self.assertEqual("companion-fault-episode/v1", metadata["fault_contract"])
            self.assertEqual("fault_report", metadata["record_class"])
            self.assertEqual(
                [{"capability": "m0", "scope_key": "m0", "scope_kind": "stage"}],
                metadata["fault_targets"],
            )
            self.assertEqual("formal_stage_unavailable", metadata["user_impact"])

            failed_events = [
                event for event in store.pending_events()
                if event["event_type"] == "research.failed"
            ]
            self.assertEqual(1, len(failed_events))
            failed_event = failed_events[0]
            payload = json.loads(failed_event["payload_json"])
            self.assertEqual(fault["artifact_id"], payload["source_artifact_id"])
            self.assertEqual("companion-published-message/v2", payload["message"]["contract"])
            self.assertEqual("system_fault", payload["message"]["kind"])

            projection = engine.command({
                "contract": "companion-user-command/v1",
                "command_id": "rejected-evidence-projection",
                "cycle_id": cycle["cycle_id"],
                "type": "request_projection",
            })
            self.assertIsNone(projection["m0"])
            self.assertEqual([], projection["ai_messages"])
            self.assertEqual(1, len(projection["fault_episodes"]))
            with store.connection() as connection:
                checkpoint = connection.execute("SELECT 1 FROM stage_checkpoint WHERE cycle_id=?", (cycle["cycle_id"],)).fetchone()
            self.assertIsNone(checkpoint)
            self.assertIsNone(store.valid_daily_baseline("2026-08-26", self.as_of))

    def test_failed_research_attempt_persists_completed_tool_trace(self):
        trace = [{
            "backend": "gateway", "tool": "web_read", "status": "succeeded",
            "non_empty": True, "arguments": {"query": "A股 盘前 公告"},
        }]
        with TemporaryDirectory() as temporary:
            store = CompanionStore(Path(temporary) / "companion.sqlite3")
            cycle = CompanionEngine(store).start_cycle(
                "daily.opportunity.0900", "2026-08-26T09:00:00+08:00", self.as_of,
            )
            broker = Mock()
            broker.invoke.side_effect = BrokerError(
                "research stopped", category="research_loop_limit", tool_trace=trace,
            )
            settings = SimpleNamespace(research={}, broker={"url": "http://broker.test:8817"})
            packet = {
                "task_key": "daily.opportunity.0900", "stage": "m0_research", "as_of": self.as_of,
                "evidence_contract": self.planner_contract,
            }
            with patch("ai_trading_companion.__main__.load_settings", return_value=settings), patch(
                "ai_trading_companion.__main__.ProviderBrokerClient", return_value=broker,
            ):
                with self.assertRaisesRegex(BrokerError, "research stopped"):
                    _call_stage(
                        store, cycle, "m0_research", packet,
                        "companion-research-result-v1.schema.json", search=False, timeout=60,
                        frozen_controls=RuntimeStrategyControls(60, 0, (), ()),
                    )

            attempt = store.attempts(cycle["cycle_id"])[0]
            self.assertEqual("failed", attempt["status"])
            self.assertEqual(trace, json.loads(attempt["tool_trace_json"]))

    def test_broker_absolute_timeout_is_persisted_as_timed_out(self):
        with TemporaryDirectory() as temporary:
            store = CompanionStore(Path(temporary) / "companion.sqlite3")
            cycle = CompanionEngine(store).start_cycle(
                "daily.review.1520", "2026-08-26T15:20:00+08:00", self.as_of,
            )
            broker = Mock()
            broker.invoke.side_effect = BrokerError(
                "Broker request deadline expired", category="broker_timeout",
            )
            settings = SimpleNamespace(research={}, broker={"url": "http://broker.test:8817"})
            packet = {"task_key": cycle["task_key"], "stage": "m1_judgment", "as_of": self.as_of}
            packet["evidence"] = {"sources": [{"evidence_ref": "market", "excerpt": "verified market"}]}

            with patch("ai_trading_companion.__main__.load_settings", return_value=settings), patch(
                "ai_trading_companion.__main__.ProviderBrokerClient", return_value=broker,
            ), self.assertRaisesRegex(BrokerError, "deadline expired"):
                _call_stage(
                    store, cycle, "m1_judgment", packet,
                    "companion-m1-result-v4.schema.json", search=False, timeout=60,
                )

            self.assertEqual("timed_out", store.attempts(cycle["cycle_id"])[0]["status"])

    def test_qualified_deterministic_research_needs_no_synthetic_broker_call(self):
        evidence = {
            "schema_version": 3, "as_of": self.as_of, "spoken_summary": "verified",
            "sources": [], "coverage": [], "critical_gaps": [], "conflicts": [],
            "high_impact_events": [],
        }
        qualified = SimpleNamespace(
            qualified=True, evidence=evidence,
            verifier={"passed": True, "problems": [], "successful_tool_results": 9},
            observations=[{"operation": "market_breadth", "status": "succeeded", "ok": True}],
        )
        with TemporaryDirectory() as temporary:
            store = CompanionStore(Path(temporary) / "companion.sqlite3")
            cycle = CompanionEngine(store).start_cycle(
                "daily.review.1520", "2026-09-02T15:20:00+08:00", self.as_of,
            )
            broker = Mock()
            settings = SimpleNamespace(research={}, broker={"url": "http://broker.test:8817"})
            packet = {
                "task_key": cycle["task_key"], "stage": "m0_research", "as_of": self.as_of,
                "evidence_contract": self.planner_contract,
            }

            with patch("ai_trading_companion.__main__.load_settings", return_value=settings), patch(
                "ai_trading_companion.__main__.ProviderBrokerClient", return_value=broker,
            ), patch("ai_trading_companion.__main__.LocalResearchChain.run", return_value=qualified):
                result = _call_stage(
                    store, cycle, "m0_research", packet,
                    "companion-evidence-result-v3.schema.json", search=True, timeout=60,
                    frozen_controls=RuntimeStrategyControls(60, 16, ("gateway", "market"), ()),
                )

            self.assertIsNone(result.broker)
            broker.invoke.assert_not_called()
            attempt = store.attempts(cycle["cycle_id"])[0]
            self.assertEqual("succeeded", attempt["status"])
            self.assertEqual("local_evidence_gate", json.loads(attempt["tool_trace_json"])[-1]["kind"])

    def test_failed_stage_persists_broker_verifier_for_auditable_repair(self):
        verifier = {
            "passed": False,
            "name": "cognitive-router/m1_judgment",
            "schema": {"passed": False, "problems": ["$.snapshot.triggers: required"]},
            "business": {"passed": True, "problems": []},
        }
        with TemporaryDirectory() as temporary:
            store = CompanionStore(Path(temporary) / "companion.sqlite3")
            cycle = CompanionEngine(store).start_cycle(
                "daily.review.1520", "2026-08-31T15:20:00+08:00", self.as_of,
            )
            broker = Mock()
            broker.invoke.side_effect = BrokerError(
                "Broker output did not pass local verification",
                category="broker_output_invalid",
                verifier=verifier,
                attempts=[{"provider": "test", "status": "completed"}],
            )
            settings = SimpleNamespace(research={}, broker={"url": "http://broker.test:8817"})
            packet = {"task_key": cycle["task_key"], "stage": "m1_judgment", "as_of": self.as_of}
            packet["evidence"] = {"sources": [{"evidence_ref": "market", "excerpt": "verified market"}]}

            with patch("ai_trading_companion.__main__.load_settings", return_value=settings), patch(
                "ai_trading_companion.__main__.ProviderBrokerClient", return_value=broker,
            ), self.assertRaisesRegex(BrokerError, "local verification"):
                _call_stage(
                    store, cycle, "m1_judgment", packet,
                    "companion-m1-result-v1.schema.json", search=False, timeout=60,
                )

            attempt = store.attempts(cycle["cycle_id"])[0]
            self.assertEqual(verifier, json.loads(attempt["verifier_json"]))
            self.assertEqual(6000, broker.invoke.call_args.args[0].output_token_limit)

    def test_m1_retry_policy_uses_four_attempts_and_supplies_verifier_feedback(self):
        verifier = {
            "passed": False,
            "schema": {"passed": False, "problems": ["$.snapshot.triggers: required"]},
            "business": {"passed": False, "problems": ["qualified_snapshot_lacks_execution_boundary"]},
        }
        error = BrokerError(
            "Broker output did not pass local verification",
            category="broker_output_invalid",
            verifier=verifier,
        )

        self.assertEqual(4, M1_MAX_JUDGMENT_ATTEMPTS)
        self.assertTrue(_m1_should_retry(error, attempt_number=1, remaining_seconds=300))
        self.assertTrue(_m1_should_retry(error, attempt_number=3, remaining_seconds=60))
        self.assertFalse(_m1_should_retry(error, attempt_number=4, remaining_seconds=300))
        self.assertFalse(_m1_should_retry(error, attempt_number=1, remaining_seconds=20))
        expression_rejection = EvidenceInsufficient({
            "passed": False, "problems": ["formal_reply_exposes_research_log"],
        })
        self.assertTrue(_m1_should_retry(expression_rejection, attempt_number=1, remaining_seconds=300))
        self.assertEqual(
            {
                "category": "broker_output_invalid",
                "schema_problems": ["$.snapshot.triggers: required"],
                "business_problems": ["qualified_snapshot_lacks_execution_boundary"],
            },
            _m1_retry_feedback(error),
        )

    def test_research_planning_uses_broker_and_never_calls_legacy_provider_tool_loop(self):
        broker = Mock()
        broker.invoke.side_effect = BrokerError("stop after inspecting request", category="test")
        planner = BrokerResearchPlanner(
            broker, intellect="smart", effort="medium", deadline=lambda: 2_000_000_000.0,
            market_tool_available=True,
        )
        packet = {
            "task_key": "daily.opportunity.0900", "stage": "m0_research", "as_of": self.as_of,
            "evidence_contract": self.planner_contract,
            "allowed_research_backends": ["gateway", "market"],
        }

        with self.assertRaisesRegex(BrokerError, "stop after inspecting request"):
            planner(packet, ["blocking_requirement_missing:material_events_and_counterevidence"], 0)

        request = broker.invoke.call_args.args[0]
        self.assertEqual("research", request.stage)
        self.assertFalse(request.visible_stream)
        self.assertIsNotNone(request.schema)


class _WeekdayCalendar:
    def is_trading_day(self, value: date) -> bool:
        return value.weekday() < 5
