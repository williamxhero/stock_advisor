from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ai_trading_companion.asr import lexicon
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.governance import RouterGovernance, classify_regime
from ai_trading_companion.learning import JudgmentLifecycle, WorkflowEvolution
from ai_trading_companion.packet_builder import RuntimePacketBuilder
from ai_trading_companion.projection import LearningProjectionRenderer
from ai_trading_companion.router import CognitiveRouter
from ai_trading_companion.store import CompanionStore


PROJECT_ROOT = Path(__file__).resolve().parents[2]


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
        packet = RuntimePacketBuilder(PROJECT_ROOT / "resources", PROJECT_ROOT / "data", self.store).build(
            cycle, "m0_compose", evidence={"sources": [{"title": "603179"}]}, as_of="2026-08-26T09:00:00Z"
        )
        self.assertIn("验证错误", json.dumps(packet["memories"], ensure_ascii=False))
        with self.store.connection() as connection:
            audit = connection.execute("SELECT selected_artifact_ids_json FROM memory_retrieval_audit ORDER BY created_at DESC LIMIT 1").fetchone()
        self.assertIn(packet["memories"][0]["artifact_id"], json.loads(audit[0]))

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
        blocked = router.plan("m1_judgment", {"task_key": "daily.execution.1430", "evidence": {"sources": [{}], "critical_gaps": ["网络不可用"]}}, 300, False)
        self.assertTrue(blocked.profile.data_blocked)
        self.assertIsNone(blocked.candidate)
        conflict = router.verify("m1_judgment", {"task_key": "daily.execution.1430"}, {
            "judgment_qualified": False,
            "snapshot": {"qualified": True, "direction": "bullish", "triggers": ["x"], "invalidations": ["y"]},
        })
        self.assertFalse(conflict["passed"])
        self.assertIn("judgment_qualification_conflicts_with_snapshot", conflict["problems"])

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
            "companion-chat-result-v1.schema.json", "companion-reflection-result-v1.schema.json",
            "companion-cognition-result-v1.schema.json",
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

    def test_markdown_memory_is_a_rebuildable_projection_not_the_fact_source(self):
        cycle = self.cycle("daily.execution.0945", "2026-08-25T09:45:00+08:00", "2026-08-25T01:45:00Z")
        artifact = self.store.append_artifact(cycle["cycle_id"], "m1", "model", "603179短线看多。", "2026-08-25T02:00:00Z")
        JudgmentLifecycle(self.store).capture(artifact, "m1", artifact["kind"] + " 603179短线看多。")

        path = LearningProjectionRenderer(Path(self.temp.name), self.store).render()

        text = path.read_text(encoding="utf-8")
        self.assertIn("由确定性 renderer", text)
        self.assertIn("603179", text)

    def test_asr_lexicon_uses_current_task_context_without_copying_sentences(self):
        context = Path(self.temp.name) / "current-task.context.txt"
        context.write_text("新泉股份准备观察，机器人链相对地位需要核实。", encoding="utf-8")

        words = lexicon(PROJECT_ROOT, context)

        self.assertIn("新泉股份准备观察", words)
        self.assertIn("机器人链相对地位需要核实", words)
        self.assertNotIn("新泉股份准备观察，机器人链相对地位需要核实。", words)


if __name__ == "__main__":
    unittest.main()
