from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ai_trading_companion.asr import lexicon
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.evidence_contract import EvidenceContractFactory
from ai_trading_companion.governance import RouterGovernance, classify_regime
from ai_trading_companion.learning import JudgmentLifecycle, WorkflowEvolution
from ai_trading_companion.packet_builder import RuntimePacketBuilder as _RuntimePacketBuilder
from ai_trading_companion.memory_port import InMemoryMemoryAdapter
from ai_trading_companion.router import CognitiveRouter
from ai_trading_companion.stage_expression import normalize_stage_output, safe_stage_output
from ai_trading_companion.store import CompanionStore


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def RuntimePacketBuilder(*args, **kwargs):
    kwargs.setdefault("memory", InMemoryMemoryAdapter())
    return _RuntimePacketBuilder(*args, **kwargs)


class CompanionLearningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = CompanionStore(Path(self.temp.name) / "companion.sqlite3")
        self.engine = CompanionEngine(self.store)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def cycle(self, task_key: str, scheduled: str, as_of: str) -> dict:
        return self.engine.start_cycle(task_key, scheduled, as_of)

    def test_partial_daily_ledger_does_not_fake_a_baseline_and_preserves_gaps(self):
        baseline = self.cycle("daily.opportunity.0900", "2026-08-25T09:00:00+08:00", "2026-08-25T01:00:00Z")
        evidence = {
            "as_of": "2026-08-25T01:20:00Z",
            "spoken_summary": "盘前基线",
            "sources": [{
                "url": "https://example.com/a", "title": "公告A",
                "published_or_retrieved_at": "2026-08-25T01:10:00Z", "excerpt": "公开事实",
                "factual_reliability": "high", "market_propagation": "medium",
            }],
            "critical_gaps": ["论坛样本暂时不可用"],
        }
        self.store.record_evidence(baseline, "m0_research", evidence)
        with self.store.connection() as connection:
            connection.execute(
                "UPDATE evidence_ledger_entry SET known_at='2026-08-25T01:20:00Z' WHERE cycle_id=?",
                (baseline["cycle_id"],),
            )
        intraday = self.cycle("daily.execution.0945", "2026-08-25T09:45:00+08:00", "2026-08-25T01:45:00Z")

        packet = RuntimePacketBuilder(PROJECT_ROOT / "resources", PROJECT_ROOT / "data", self.store).build(intraday, "m0_research")

        scope = packet["public_research_scope"]
        self.assertEqual("baseline_recovery", scope["mode"])
        self.assertIn("公告A", [item["title"] for item in scope["daily_ledger"]])
        self.assertIn("missing", [item["coverage_state"] for item in scope["daily_ledger"]])

    def test_missing_daily_baseline_is_explicit_recovery_not_fake_incremental(self):
        intraday = self.cycle("daily.execution.0945", "2026-08-25T09:45:00+08:00", "2026-08-25T01:45:00Z")
        packet = RuntimePacketBuilder(PROJECT_ROOT / "resources", PROJECT_ROOT / "data", self.store).build(intraday, "m0_research")
        self.assertEqual("baseline_recovery", packet["public_research_scope"]["mode"])

    def test_h0_snapshot_freezes_latest_direction_and_schedules_three_horizons(self):
        cycle = self.cycle("daily.execution.0945", "2026-08-25T09:45:00+08:00", "2026-08-25T01:45:00Z")
        artifact = self.store.append_artifact(
            cycle["cycle_id"], "h0", "human", "先看转强。修正：我认为会冲高回落。", "2026-08-25T02:00:00Z"
        )

        snapshot = JudgmentLifecycle(self.store).capture(artifact, "h0", "先看转强。修正：我认为会冲高回落。")

        frozen = json.loads(snapshot["snapshot_json"])
        self.assertEqual("bearish", frozen["direction"])
        self.assertEqual(1, len(frozen["claims"]))
        self.assertEqual("bearish", frozen["claims"][0]["direction"])
        with self.store.connection() as connection:
            horizons = {row[0] for row in connection.execute(
                "SELECT horizon FROM outcome_checkpoint WHERE snapshot_id=?", (snapshot["snapshot_id"],)
            )}
        self.assertEqual({"T+1", "T+3", "T+5"}, horizons)

    def test_outcome_updates_verification_and_failed_case_is_retrievable(self):
        cycle = self.cycle("daily.execution.0945", "2026-08-25T09:45:00+08:00", "2026-08-25T01:45:00Z")
        artifact = self.store.append_artifact(cycle["cycle_id"], "m1", "model", "603179短线看多。", "2026-08-25T02:00:00Z")
        snapshot = JudgmentLifecycle(self.store).capture(artifact, "m1", "603179短线看多。")
        checkpoint = self.store.schedule_outcome(snapshot["snapshot_id"], "T+1", "2026-08-26T08:10:00Z")
        checkpoint.update({"cycle_id": cycle["cycle_id"], "snapshot_id": snapshot["snapshot_id"]})

        JudgmentLifecycle(self.store).record_outcome(checkpoint, {
            "as_of": "2026-08-26T08:10:00Z", "verification_status": "incorrect",
            "summary": "603179的方向验证错误，价格没有按预期走强。", "observations": [], "data_gaps": [],
        })

        self.assertEqual("incorrect", self.store.judgment_snapshots(cycle["cycle_id"])[0]["verification_status"])
        memory = InMemoryMemoryAdapter()
        memory.append({
            "memory_space_id": "ai-trading-companion", "source_system": "test", "source_event_id": "failed-case",
            "content_hash": "test", "episode_type": "outcome", "body": "603179的方向验证错误，价格没有按预期走强。",
            "occurred_at": "2026-08-26T08:10:00Z", "known_at": "2026-08-26T08:10:00Z", "submitted_at": "2026-08-26T08:10:00Z",
            "authority": "test", "protocol_version": "memoryhub/v1",
        })
        packet = RuntimePacketBuilder(PROJECT_ROOT / "resources", PROJECT_ROOT / "data", self.store, memory=memory).build(
            cycle, "m0_compose", evidence={"sources": [{"title": "603179"}]}, as_of="2026-08-26T09:00:00Z"
        )
        self.assertIn("验证错误", json.dumps(packet["memories"], ensure_ascii=False))
        self.assertTrue(packet["memories"][0]["episode_id"].startswith("test-episode-"))

    def test_workflow_change_requires_approval_and_can_rollback(self):
        cycle = self.cycle("daily.execution.0945", "2026-08-25T09:45:00+08:00", "2026-08-25T01:45:00Z")
        evolution = WorkflowEvolution(self.store)
        proposal = evolution.propose(cycle["cycle_id"], {
            "category": "search_coverage", "title": "补充论坛反证", "problem": "传播覆盖不足",
            "change": "以后固定检查反向论坛观点", "evidence": ["cycle-a"],
            "policy_patch": {
                "add_categories": ["投资者互动"],
                "add_standing_questions": [],
                "add_counterevidence_questions": ["市场当前最强的反向传播是什么"],
            },
        })
        self.assertEqual([], evolution.active_policy()["extra_categories"])

        applied = evolution.decide(proposal["proposal_id"], True, note="用户同意")

        self.assertEqual("applied", applied["state"])
        self.assertIn("投资者互动", evolution.active_policy()["extra_categories"])
        evolution.rollback()
        self.assertEqual([], evolution.active_policy()["extra_categories"])

    def test_investment_method_cannot_be_approved_from_one_case(self):
        cycle = self.cycle("daily.execution.0945", "2026-08-25T09:45:00+08:00", "2026-08-25T01:45:00Z")
        evolution = WorkflowEvolution(self.store)
        proposal = evolution.propose(cycle["cycle_id"], {
            "category": "investment_method", "title": "提高传言权重", "problem": "单次遗漏",
            "change": "改变判断权重", "evidence": ["one-case"],
            "policy_patch": {"add_categories": [], "add_standing_questions": [], "add_counterevidence_questions": ["传言是否扩散"]},
        })
        with self.assertRaisesRegex(ValueError, "lacks repeated historical evidence"):
            evolution.decide(proposal["proposal_id"], True)

        for evidence in ("case-two", "case-three"):
            proposal = evolution.propose(cycle["cycle_id"], {
                "category": "investment_method", "title": "提高传言权重", "problem": "重复遗漏",
                "change": "把传播响应作为独立假说", "evidence": [evidence],
                "policy_patch": {
                    "add_categories": [], "add_standing_questions": [],
                    "add_counterevidence_questions": ["传言是否扩散"],
                    "add_method_hypotheses": ["传言真实性与传播价格影响分别评价"],
                },
            })
        self.assertEqual("awaiting_approval", proposal["state"])
        applied = evolution.decide(proposal["proposal_id"], True)
        self.assertEqual("applied", applied["state"])
        self.assertIn("传言真实性与传播价格影响分别评价", evolution.active_policy()["method_hypotheses"])

    def test_cognitive_router_keeps_official_judgments_on_expert_and_shadows_effort(self):
        router = CognitiveRouter()
        routine = router.route("m1_judgment", {"task_key": "daily.execution.0945", "evidence": {"sources": [{}, {}, {}]}}, 300, False)
        impact = router.route("m1_judgment", {"task_key": "daily.execution.1430", "evidence": {"sources": [{}, {}, {}]}}, 300, False)
        plan = router.plan("m1_judgment", {"task_key": "daily.execution.1430", "evidence": {"sources": [{}, {}, {}]}}, 300, False)
        research = router.route("m0_research", {"task_key": "daily.opportunity.0900"}, 300, True)
        self.assertEqual("expert", routine.intellect)
        self.assertEqual("medium", routine.reasoning_effort)
        self.assertEqual("medium", impact.reasoning_effort)
        self.assertIsNotNone(plan.candidate)
        self.assertEqual("xhigh", plan.candidate.reasoning_effort)
        self.assertEqual("smart", research.intellect)

        cycle = self.cycle("daily.execution.1430", "2026-08-25T14:30:00+08:00", "2026-08-25T06:30:00Z")
        attempt = self.store.begin_attempt(
            cycle["cycle_id"], "m1_judgment", cycle["as_of"], "hash",
            model=None, reasoning_effort=impact.reasoning_effort,
            search_enabled=False, timeout_seconds=impact.timeout_seconds,
            routing_reason=impact.reason,
        )
        audited = self.store.attempts(cycle["cycle_id"])[0]
        self.assertEqual("medium", audited["reasoning_effort"])
        self.assertEqual(0, audited["search_enabled"])
        self.assertIn("Broker expert", audited["routing_reason"])

    def test_cognitive_router_fails_closed_on_m1_human_leak_and_data_block(self):
        router = CognitiveRouter()
        leak = router.plan("m1_judgment", {"task_key": "daily.execution.1430", "h0": "看多", "evidence": {"sources": [{}]}}, 300, False)
        self.assertFalse(leak.profile.m1_blind)
        self.assertIsNone(leak.candidate)
        market_code = router.plan("m1_judgment", {
            "task_key": "daily.review.1520",
            "evidence": {"sources": [{"url": "https://example.test/q=sh000001"}]},
        }, 300, False)
        self.assertTrue(market_code.profile.m1_blind)
        blocked = router.plan("m1_judgment", {"task_key": "daily.execution.1430", "evidence": {"sources": [{}], "critical_gaps": ["网络不可用"]}}, 300, False)
        self.assertTrue(blocked.profile.data_blocked)
        self.assertIsNone(blocked.candidate)
        conflict = router.verify("m1_judgment", {"task_key": "daily.execution.1430"}, {
            "judgment_qualified": False,
            "snapshot": {"qualified": True, "direction": "bullish", "triggers": ["x"], "invalidations": ["y"]},
        })
        self.assertFalse(conflict["passed"])
        self.assertIn("judgment_qualification_conflicts_with_snapshot", conflict["problems"])

    def test_v2_m1_uses_semantic_qualification_and_checks_snapshot_boundaries(self):
        router = CognitiveRouter()
        semantic = {
            "summary": "核心承接改善。", "direction": "bullish", "qualified": True,
            "triggers": ["核心同步转强"], "invalidations": ["放量跌破"],
            "risks": ["冲高回落"], "unknowns": ["扩散持续性"],
        }
        snapshot = {
            "direction": "bullish", "qualified": True,
            "triggers": ["核心同步转强"], "invalidations": ["放量跌破"],
            "risks": ["冲高回落"], "unknowns": ["扩散持续性"],
        }

        accepted = router.verify("m1_judgment", {"task_key": "daily.review.1520"}, {
            "semantic": semantic, "snapshot": snapshot,
        })
        conflicting = router.verify("m1_judgment", {"task_key": "daily.review.1520"}, {
            "semantic": {**semantic, "direction": "bearish"}, "snapshot": snapshot,
        })

        self.assertTrue(accepted["passed"], accepted["problems"])
        self.assertFalse(conflicting["passed"])
        self.assertIn("judgment_semantic_conflicts_with_snapshot", conflicting["problems"])

    def test_v3_m1_derives_the_immutable_snapshot_from_one_semantic_payload(self):
        semantic = {
            "summary": "证据支持谨慎看多。", "direction": "bullish", "qualified": True,
            "triggers": ["量价同步转强"], "invalidations": ["放量跌破"],
            "risks": ["冲高回落"], "unknowns": ["扩散持续性"],
        }

        accepted = CognitiveRouter().verify("m1_judgment", {"task_key": "daily.review.1520"}, {
            "result_version": 3, "semantic": semantic,
        })

        self.assertTrue(accepted["passed"], accepted["problems"])

    def test_v3_m1_canonicalizes_a_natural_chinese_direction_without_inverting_risk_claims(self):
        semantic = {
            "summary": "收盘市场宽度显著偏弱。",
            "direction": "谨慎偏空，短线风险偏好明显下降",
            "qualified": True,
            "triggers": ["上涨家数重新超过下跌家数"],
            "invalidations": ["主要指数收复本次跌幅"],
            "risks": ["下跌3901家、上涨1541家，宽度显著偏弱"],
            "unknowns": ["缺少跨来源核验"],
        }

        normalized = normalize_stage_output("m1_judgment", {
            "result_version": 3,
            "semantic": semantic,
        })

        self.assertEqual("bearish", normalized.snapshot["direction"])
        self.assertEqual("bearish", normalized.snapshot["claims"][0]["direction"])
        self.assertEqual(semantic["summary"], normalized.snapshot["claims"][0]["original_text"])

        cycle = self.cycle("daily.review.1520", "2026-09-02T15:20:00+08:00", "2026-09-02T07:00:00Z")
        artifact = self.store.append_artifact(
            cycle["cycle_id"], "m1", "model", normalized.text, "2026-09-02T07:00:00Z",
        )
        row = JudgmentLifecycle(self.store).capture(
            artifact, "m1", normalized.text, snapshot=normalized.snapshot, qualified=True,
        )
        frozen = json.loads(row["snapshot_json"])
        self.assertEqual("bearish", frozen["direction"])
        self.assertEqual(["bearish"], [claim["direction"] for claim in frozen["claims"]])

    def test_v3_m1_contract_requires_a_canonical_direction_and_expression_localizes_it(self):
        schema = json.loads((
            PROJECT_ROOT / "resources" / "contracts" / "companion-m1-result-v3.schema.json"
        ).read_text(encoding="utf-8"))

        self.assertEqual(
            ["bullish", "bearish", "neutral", "avoid", "unqualified", "unknown"],
            schema["properties"]["semantic"]["properties"]["direction"]["enum"],
        )
        normalized = normalize_stage_output("m1_judgment", {
            "result_version": 3,
            "semantic": {
                "summary": "市场宽度偏弱。", "direction": "bearish", "qualified": True,
                "triggers": ["宽度修复"], "invalidations": ["指数收复跌幅"],
                "risks": [], "unknowns": [],
            },
        })
        self.assertIn("偏空", normalized.text)
        self.assertNotIn("bearish", normalized.text)

    def test_v4_m1_leads_with_current_action_and_requires_joint_confirmation(self):
        normalized = normalize_stage_output("m1_judgment", {
            "result_version": 4,
            "semantic": {
                "summary": "指数小幅走高，但下跌家数仍明显更多，强势没有扩散。",
                "direction": "neutral",
                "qualified": True,
                "horizon": "午后",
                "current_action": "observe",
                "key_evidence": [
                    "三大指数约上涨0.4%",
                    "午间下跌家数仍明显多于上涨家数",
                ],
                "transition_conditions": [{
                    "outcome": "upgrade",
                    "price": "三大指数守住午间区域",
                    "breadth": "上涨家数持续超过下跌家数并出现成交扩散",
                    "persistence": "连续一段时间保持",
                }, {
                    "outcome": "downgrade",
                    "price": "指数陆续跌回前收以下",
                    "breadth": "下跌家数继续扩大并出现更多弱势股",
                    "persistence": "持续而非单次瞬时波动",
                }],
                "position_focus": [{
                    "symbol": "力星股份",
                    "priority": 1,
                    "reason": "若午后明显落后所属板块，风险先于浮亏处理",
                    "action": "reduce_risk",
                }],
                "risks": [],
                "unknowns": ["午后成交是否扩散到更多板块"],
            },
        })

        self.assertTrue(normalized.text.startswith("午后我维持中性，当前继续观察"))
        self.assertIn("指数小幅走高，但下跌家数仍明显更多", normalized.text)
        self.assertIn("三大指数守住午间区域", normalized.text)
        self.assertIn("上涨家数持续超过下跌家数", normalized.text)
        self.assertIn("连续一段时间保持", normalized.text)
        self.assertIn("优先盯力星股份", normalized.text)
        self.assertNotIn("我现在更倾向于", normalized.text)
        self.assertNotIn("接下来主要看", normalized.text)
        self.assertEqual("neutral", normalized.snapshot["direction"])
        self.assertEqual("observe", normalized.snapshot["current_action"])
        self.assertEqual("午后", normalized.snapshot["claims"][0]["horizon"])

    def test_v4_m1_rejects_single_signal_turns_and_cost_anchored_position_priority(self):
        semantic = {
            "summary": "市场仍在分化。", "direction": "neutral", "qualified": True,
            "horizon": "午后", "current_action": "observe", "key_evidence": ["广度偏弱"],
            "transition_conditions": [{
                "outcome": "upgrade", "price": "指数守住午间区域",
                "breadth": "上涨家数持续超过下跌家数", "persistence": "连续一段时间保持",
            }, {
                "outcome": "downgrade", "price": "指数陆续跌回前收以下",
                "breadth": "下跌家数继续扩大", "persistence": "持续而非单次瞬时波动",
            }],
            "position_focus": [{
                "symbol": "力星股份", "priority": 1, "reason": "午后明显落后所属板块",
                "action": "reduce_risk",
            }],
            "risks": [], "unknowns": [],
        }
        accepted = CognitiveRouter().verify("m1_judgment", {"task_key": "daily.execution.1030"}, {
            "result_version": 4, "semantic": semantic,
        })
        weak_condition = CognitiveRouter().verify("m1_judgment", {"task_key": "daily.execution.1030"}, {
            "result_version": 4,
            "semantic": {**semantic, "transition_conditions": [{
                "outcome": "downgrade", "price": "指数跌破前收", "breadth": "", "persistence": "",
            }]},
        })
        cost_anchored = CognitiveRouter().verify("m1_judgment", {"task_key": "daily.execution.1030"}, {
            "result_version": 4,
            "semantic": {**semantic, "position_focus": [{
                "symbol": "力星股份", "priority": 1, "reason": "浮亏最多，成本最高",
                "action": "reduce_risk",
            }]},
        })

        self.assertTrue(accepted["passed"], accepted["problems"])
        self.assertIn("judgment_transition_lacks_joint_confirmation", weak_condition["problems"])
        self.assertIn("judgment_position_priority_is_cost_anchored", cost_anchored["problems"])

    def test_weekend_m1_requires_an_explicit_completed_week_comparison(self):
        sources = []
        for symbol, end in (("sh000001", 98.0), ("sz399001", 97.0), ("sz399006", 96.0)):
            sources.append({"excerpt": json.dumps({
                "symbol": symbol, "start": "2026-08-31", "end": "2026-09-04",
                "series": [
                    {"date": "2026-08-31", "close": 100.0},
                    {"date": "2026-09-04", "close": end},
                ],
            })})
        packet = {
            "stage": "m1_judgment", "task_key": "manual.non_trading_outlook",
            "task_profile": {"evidence_family": "completed_trading_week"},
            "evidence": {"sources": sources},
        }
        semantic = {
            "summary": "9月4日三大指数收跌，市场偏弱。", "direction": "neutral", "qualified": True,
            "horizon": "下周初", "current_action": "observe", "key_evidence": ["广度偏弱"],
            "transition_conditions": [{
                "outcome": "upgrade", "price": "三大指数收盘转强",
                "breadth": "上涨家数超过下跌家数", "persistence": "至少持续一个交易日",
            }, {
                "outcome": "downgrade", "price": "三大指数继续下跌",
                "breadth": "下跌家数继续扩大", "persistence": "连续两个交易日",
            }],
            "position_focus": [], "risks": [], "unknowns": [],
        }

        single_day = CognitiveRouter().verify(
            "m1_judgment", packet, {"result_version": 4, "semantic": semantic},
        )
        completed_week = CognitiveRouter().verify("m1_judgment", packet, {
            "result_version": 4,
            "semantic": {
                **semantic,
                "summary": "本周8月31日至9月4日，上证周跌2%，深成指周跌3%，创业板指周跌4%。",
            },
        })

        self.assertIn("weekend_review_lacks_completed_week_comparison", single_day["problems"])
        self.assertTrue(completed_week["passed"], completed_week["problems"])
        self.assertIn("整周", _RuntimePacketBuilder.prompt(packet))

    def test_safe_formal_fallback_is_natural_and_conservative(self):
        m0 = normalize_stage_output("m0_compose", safe_stage_output("m0_compose"))
        m1 = normalize_stage_output("m1_judgment", safe_stage_output("m1_judgment", horizon="午后"))
        m2 = normalize_stage_output("m2", safe_stage_output("m2", horizon="午后"))

        self.assertIn("先只保留客观观察", m0.text)
        self.assertNotIn("偏多", m0.text)
        for result in (m1, m2):
            self.assertTrue(result.text.startswith("午后我维持暂不形成方向，当前继续观察"))
            self.assertEqual("unqualified", result.snapshot["direction"])
            self.assertEqual("observe", result.snapshot["current_action"])
            self.assertNotIn("provider_candidate_not_publishable", result.text)

    def test_packet_and_verifier_reject_a_false_non_trading_day_m0(self):
        class TradingDayCalendar:
            @staticmethod
            def is_trading_day(_value):
                return True

        cycle = self.cycle("daily.execution.1430", "2026-08-31T14:30:00+08:00", "2026-08-31T06:30:00Z")
        packet = RuntimePacketBuilder(
            PROJECT_ROOT / "resources", PROJECT_ROOT / "data", self.store,
            evidence_contract_factory=EvidenceContractFactory(calendar=TradingDayCalendar()),
        ).build(cycle, "m0_compose")

        self.assertEqual({
            "date": "2026-08-31",
            "weekday_iso": 1,
            "weekday_name_zh": "星期一",
            "is_xshg_trading_day": True,
            "authority": "deterministic_local_xshg_calendar",
        }, packet["calendar_context"])
        self.assertEqual("m0_objective_observation_only", packet["protocol"]["stage_scope"])
        self.assertNotIn("建议总股票仓位", packet["protocol"]["text"])
        compact_protocol = "".join(packet["protocol"]["text"].split()).lower()
        for internal_marker in ("本阶段", "m0客观观察", "冻结工具", "确定性投影", "冻结证据"):
            self.assertNotIn(internal_marker, compact_protocol)
        self.assertIn("整篇最多提及两只持仓", _RuntimePacketBuilder.prompt(packet))
        self.assertEqual(2, packet["m0_compose_requirements"]["maximum_entities_to_mention"])
        self.assertNotIn("For every portfolio entity", packet["m0_compose_requirements"]["instruction"])
        rejected = CognitiveRouter().verify("m0_compose", packet, {
            "m0_markdown": "`状态: skipped` 2026-08-31为周日，非A股交易日。",
        })
        accepted = CognitiveRouter().verify("m0_compose", packet, {
            "m0_markdown": "截至14:30，沪深主要指数与成交数据已经更新。",
        })
        action_leak = CognitiveRouter().verify("m0_compose", packet, {
            "m0_markdown": "今天的可执行建议为：不新增仓、不加仓、不减仓，建议卖出股数为0。",
        })
        v2_action_leak = CognitiveRouter().verify("m0_compose", packet, {
            "semantic": {
                "summary": "今天建议买入核心股。", "observations": ["可以加仓"],
                "risks": [], "unknowns": [],
            },
        })
        v2_direction_leak = CognitiveRouter().verify("m0_compose", packet, {
            "semantic": {
                "summary": "当前方向偏多。", "observations": ["后续继续看多"],
                "risks": [], "unknowns": [],
            },
        })

        self.assertFalse(rejected["passed"])
        self.assertIn("m0_calendar_context_conflict", rejected["problems"])
        self.assertIn("m0_calendar_weekday_conflict", rejected["problems"])
        self.assertTrue(accepted["passed"], accepted["problems"])
        self.assertFalse(action_leak["passed"])
        self.assertIn("m0_contains_direction_or_action", action_leak["problems"])
        self.assertFalse(v2_action_leak["passed"])
        self.assertIn("m0_contains_direction_or_action", v2_action_leak["problems"])
        self.assertFalse(v2_direction_leak["passed"])
        self.assertIn("m0_contains_direction_or_action", v2_direction_leak["problems"])

    def test_router_store_queues_frozen_shadow_with_daily_budget(self):
        cycle = self.cycle("daily.execution.1430", "2026-08-25T14:30:00+08:00", "2026-08-25T06:30:00Z")
        router = CognitiveRouter()
        packet = {"task_key": cycle["task_key"], "evidence": {"sources": [{}, {}, {}]}}
        plan = router.plan("m1_judgment", packet, 300, False)
        cell = self.store.router_policy_cell(plan.profile.cell_key, plan.baseline.as_json(), plan.candidate.as_json())
        decision = self.store.record_route_decision(cycle["cycle_id"], "m1_judgment", plan.profile.cell_key, cell["mode"], plan.profile.as_json(), plan.baseline.as_json(), plan.candidate.as_json(), plan.selected.as_json())
        job = self.store.queue_router_shadow(decision, cycle["cycle_id"], "m1_judgment", packet, "companion-m1-result-v1.schema.json", plan.candidate.as_json(), priority=1)
        claimed = self.store.next_router_shadow()
        self.assertEqual(job, claimed["job_id"])
        self.assertEqual("running", claimed["state"])
        self.store.finish_router_shadow(job, output={"m1_markdown": "影子输出"})
        with self.store.connection() as connection:
            row = connection.execute("SELECT state, packet_sha256, output_sha256 FROM router_shadow_job WHERE job_id=?", (job,)).fetchone()
        self.assertEqual("succeeded", row["state"])
        self.assertTrue(row["packet_sha256"])
        self.assertTrue(row["output_sha256"])

    def test_regime_classifier_and_router_promotion_require_diverse_resolved_pairs(self):
        self.assertEqual("trend_expansion", classify_regime({"index_trend": 1, "breadth": .6, "turnover_change": .1, "volatility": .3}))
        self.assertEqual("risk_contraction", classify_regime({"index_trend": -1, "breadth": .3, "turnover_change": -.1, "volatility": .8}))
        self.assertEqual("unknown", classify_regime({}))
        key = "m1:daily.execution.1430"
        self.store.router_policy_cell(key, {"reasoning_effort": "medium"}, {"reasoning_effort": "xhigh"})
        for index in range(20):
            regime = ("trend_expansion", "divergence", "risk_contraction")[index % 3]
            self.store.record_router_evaluation(
                key, f"cycle-{index}", "T+1", regime, None, f"shadow-{index}",
                {"value": 0.0}, {"value": 1.0}, "resolved",
            )
        verdict = RouterGovernance(self.store).promote_if_qualified(key)
        self.assertEqual("promote", verdict["action"])
        self.assertEqual("shadow", self.store.get_router_policy_cell(key)["mode"])

    def test_ai_risk_doctrine_is_versioned_and_not_a_user_account_setting(self):
        doctrine = self.store.risk_doctrine()
        rules = json.loads(doctrine["doctrine_json"])["rules"]
        self.assertEqual(0.20, rules["single_stock_max_known_assets"])
        self.assertEqual(0.01, rules["planned_loss_max_known_assets"])
        self.assertIn("事实权威", json.loads(doctrine["doctrine_json"])["boundary"])

    def test_chat_research_packet_contains_only_sanitized_public_scope(self):
        cycle = self.cycle("daily.execution.0945", "2026-08-25T09:45:00+08:00", "2026-08-25T01:45:00Z")
        private_text = "这是我的私人持仓和家庭资金安排，不能交给联网研究"
        self.store.append_artifact(cycle["cycle_id"], "chat_human", "human", private_text, "2026-08-25T02:00:00Z")

        packet = RuntimePacketBuilder(PROJECT_ROOT / "resources", PROJECT_ROOT / "data", self.store).build(
            cycle, "chat_research",
            context={"topics": ["机器人板块"], "questions": ["今天是否有新增公告"]},
            as_of="2026-08-25T02:01:00Z",
        )

        serialized = json.dumps(packet, ensure_ascii=False)
        self.assertIn("机器人板块", serialized)
        self.assertNotIn(private_text, serialized)
        self.assertNotIn("local_inputs", packet)

    def test_all_companion_output_schemas_are_strict_at_every_object(self):
        names = (
            "companion-evidence-result-v1.schema.json", "companion-evidence-result-v2.schema.json",
            "companion-m0-result-v1.schema.json",
            "companion-m1-result-v1.schema.json", "companion-m2-result-v1.schema.json",
            "companion-chat-result-v2.schema.json", "companion-reflection-result-v2.schema.json",
            "companion-cognition-result-v2.schema.json",
            "companion-outcome-result-v1.schema.json", "portfolio-interpretation-result-v1.schema.json",
        )

        def check(node: object, path: str) -> None:
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertFalse(node.get("additionalProperties", True), path)
                    properties = set((node.get("properties") or {}).keys())
                    self.assertEqual(properties, set(node.get("required") or []), path)
                for key, value in node.items():
                    check(value, f"{path}/{key}")
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    check(value, f"{path}/{index}")

        for name in names:
            schema = json.loads((PROJECT_ROOT / "resources" / "contracts" / name).read_text(encoding="utf-8"))
            check(schema, name)

    def test_learning_state_remains_in_database_without_file_projection(self):
        cycle = self.cycle("daily.execution.0945", "2026-08-25T09:45:00+08:00", "2026-08-25T01:45:00Z")
        artifact = self.store.append_artifact(cycle["cycle_id"], "m1", "model", "603179短线看多。", "2026-08-25T02:00:00Z")
        JudgmentLifecycle(self.store).capture(artifact, "m1", artifact["kind"] + " 603179短线看多。")

        self.assertTrue(any("603179" in row["snapshot_json"] for row in self.store.judgment_snapshots()))
        self.assertEqual([], list(Path(self.temp.name).rglob("*.md")))

    def test_asr_lexicon_uses_current_task_context_without_copying_sentences(self):
        context = Path(self.temp.name) / "current-task.context.txt"
        context.write_text("新泉股份准备观察，机器人链相对地位需要核实。", encoding="utf-8")

        words = lexicon(context)

        self.assertIn("新泉股份准备观察", words)
        self.assertIn("机器人链相对地位需要核实", words)
        self.assertNotIn("新泉股份准备观察，机器人链相对地位需要核实。", words)


if __name__ == "__main__":
    unittest.main()
