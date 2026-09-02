from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from ai_trading_companion.engine import CompanionEngine, iso, parse
from ai_trading_companion.message_presentation import MessageQualificationError
from ai_trading_companion.stage_expression import express_stage_semantics
from ai_trading_companion.memory_port import InMemoryMemoryAdapter
from ai_trading_companion.__main__ import run_pending_premarket_reply
from ai_trading_companion.packet_builder import RuntimePacketBuilder as _RuntimePacketBuilder
from ai_trading_companion.publication_registry import published_event_types
from ai_trading_companion.scheduler import conversation_auto_submit_at, load_schedules, run_daily_schedule, run_periodic_schedule
from ai_trading_companion.store import CompanionStore
from ai_trading_companion.evidence_contract import EvidenceContractFactory
from ai_trading_companion.task_profiles import ManualAnalysisProfileResolver


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def RuntimePacketBuilder(*args, **kwargs):
    kwargs.setdefault("memory", InMemoryMemoryAdapter())
    return _RuntimePacketBuilder(*args, **kwargs)


class _WeekdayCalendar:
    def is_trading_day(self, day: date) -> bool:
        return day.weekday() < 5


def packet_builder(store: CompanionStore) -> RuntimePacketBuilder:
    return RuntimePacketBuilder(
        PROJECT_ROOT / "resources",
        PROJECT_ROOT / "data",
        store,
        memory=InMemoryMemoryAdapter(),
        evidence_contract_factory=EvidenceContractFactory(_WeekdayCalendar()),
    )


class CompanionEngineTests(unittest.TestCase):
    def test_user_visible_event_cannot_bypass_the_v2_publication_contract(self):
        for event_type in published_event_types():
            with self.subTest(event_type=event_type):
                with self.assertRaisesRegex(ValueError, "requires companion-published-message/v2"):
                    self.engine.emit(self.cycle, event_type, {"cycle": self.cycle, "text": "raw bypass"})
        queued = {item["event_type"] for item in self.store.pending_events()}
        self.assertTrue(published_event_types().isdisjoint(queued))

    def test_unqualified_chat_candidate_is_repaired_before_it_is_sealed(self):
        self.engine.chat_ready(
            self.cycle["cycle_id"],
            "internal_flag: active\n我倾向于先等承接确认。",
        )

        artifact = self.store.latest_artifact(self.cycle["cycle_id"], "ai_chat")
        self.assertNotIn("internal_flag", artifact["body_markdown"])

    def test_chat_draft_closes_after_two_failed_repairs_without_sealing(self):
        cycle = self.engine.ensure_daily_conversation()
        self.store.stage_message(cycle["cycle_id"], "请继续看承接。", "conversation")
        batch_id, _ = self.store.commit_staged_messages(cycle["cycle_id"], "conversation")
        error = MessageQualificationError(["internal_token"])
        with patch("ai_trading_companion.engine.present_message", side_effect=error) as qualify:
            with self.assertRaises(MessageQualificationError):
                self.engine.chat_ready(cycle["cycle_id"], "bad draft", reply_to_batch_ids=[batch_id])

        self.assertEqual(3, qualify.call_count)
        self.assertIsNone(self.store.latest_artifact(cycle["cycle_id"], "ai_chat"))
        self.assertEqual([batch_id], [batch["batch_id"] for batch in self.store.pending_message_batches(cycle["cycle_id"], "conversation")])

    def test_machine_shaped_chat_is_qualified_before_it_reaches_the_projection(self):
        self.engine.chat_ready(
            self.cycle["cycle_id"],
            """盘前研判
time_scope: next_trading_session
reference_at: 2026-09–01 09:00 Asia/Shanghai
Protocol: OpportunityDiscovery-v1.3
状态：unqualified

结论
现有证据不够。
市场基线
指数偏弱。
新增事件
昨夜有一条消息。""",
        )

        projection = self.engine._projection(self.store.get_cycle(self.cycle["cycle_id"]))
        message = next(item for item in projection["ai_messages"] if item["kind"] == "ai_chat")
        visible = message["message"]["text_projection"]
        for leaked in ("time_scope", "reference_at", "Asia/Shanghai", "Protocol", "unqualified", "盘前研判", "市场基线", "新增事件"):
            self.assertNotIn(leaked, visible)
        self.assertIn("现有信息还不够，我先不下判断。", visible)

    def test_published_message_v2_survives_the_runtime_projection(self):
        memory = InMemoryMemoryAdapter()
        self.engine.memory = memory
        self.ready()

        projection = self.engine._projection(self.store.get_cycle(self.cycle["cycle_id"]))
        message = next(item for item in projection["ai_messages"] if item["kind"] == "m0")

        self.assertEqual("companion-published-message/v2", message["message"]["contract"])
        self.assertRegex(message["message"]["message_id"], r"^[0-9a-f-]{36}$")
        self.assertTrue(message["message"]["sealed_at"].endswith("Z"))
        self.assertEqual("今天先看客观信息。", message["message"]["text_projection"])
        self.assertEqual(
            [{"kind": "speech", "text": "今天先看客观信息。"}],
            message["message"]["parts"],
        )

        event = next(item for item in self.store.pending_events() if item["event_type"] == "m0.ready")
        payload = json.loads(event["payload_json"])
        self.assertEqual("companion-published-message/v2", payload["message"]["contract"])
        memory_message = next(row for row in memory._episodes if row["episode_type"] == "ai_message")
        self.assertEqual(message["message"]["message_id"], memory_message["metadata"]["message_id"])
        self.assertEqual(message["message"], memory_message["metadata"]["published_message"])

    def test_new_m0_result_publishes_from_structured_semantics_not_markdown(self):
        self.engine.research_started(self.cycle["cycle_id"])
        semantic = {
            "summary": "现在先看客观承接。", "observations": ["核心仍然偏弱"],
            "risks": ["量能没有改善"], "unknowns": ["开盘后的扩散强度"],
        }
        evidence_hash, compose_hash = "semantic-evidence", "semantic-compose"
        self.engine.research_ready(
            self.cycle["cycle_id"], express_stage_semantics("m0", semantic),
            evidence_attempt_id=self.qualified("m0_research", evidence_hash, output={}),
            compose_attempt_id=self.qualified("m0_compose", compose_hash, output={"semantic": semantic}),
            evidence_packet_hash=evidence_hash, packet_hash=compose_hash,
        )
        artifact = self.store.latest_artifact(self.cycle["cycle_id"], "m0")
        self.assertIn("核心仍然偏弱", artifact["body_markdown"])
        self.assertNotIn("m0_markdown", artifact["metadata_json"])

    def test_structured_judgment_expression_keeps_direction_and_qualification(self):
        text = express_stage_semantics("m1", {
            "summary": "承接还没有形成共振。", "direction": "继续观望", "qualified": False,
            "triggers": ["核心同步转强"], "invalidations": ["量能继续萎缩"],
            "risks": ["尾部冲高回落"], "unknowns": ["开盘后的扩散强度"],
        })

        self.assertIn("继续观望", text)
        self.assertIn("不会把它当成可以直接执行的判断", text)

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = CompanionStore(Path(self.temp.name) / "companion.sqlite3")
        self.engine = CompanionEngine(self.store, memory=InMemoryMemoryAdapter())
        self.now = datetime(2026, 8, 25, 1, 45, tzinfo=timezone.utc)
        self.cycle = self.engine.start_cycle(
            "daily.execution.0945", "2026-08-25T09:45:00+08:00", iso(self.now)
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def ready(self) -> dict:
        self.engine.research_started(self.cycle["cycle_id"])
        evidence_hash, compose_hash = "test-evidence", "test-compose"
        return self.engine.research_ready(
            self.cycle["cycle_id"], "今天先看客观信息。",
            evidence_attempt_id=self.qualified("m0_research", evidence_hash, output={}),
            compose_attempt_id=self.qualified("m0_compose", compose_hash, output={"m0_markdown": "今天先看客观信息。"}),
            evidence_packet_hash=evidence_hash, packet_hash=compose_hash,
        )

    def qualified(self, stage: str, packet_hash: str, cycle_id: str | None = None, output: dict | None = None) -> str:
        attempt = self.store.begin_attempt(cycle_id or self.cycle["cycle_id"], stage, iso(self.now), packet_hash)
        self.store.finish_attempt(attempt["attempt_id"], "succeeded", output=output or {}, verifier={"passed": True, "problems": [], "fixture": True})
        return attempt["attempt_id"]

    def publish_fixture_m0(self, cycle_id: str, text: str = "m0") -> dict:
        evidence_hash, compose_hash = "fixture-evidence", "fixture-compose"
        return self.engine.research_ready(
            cycle_id, text,
            evidence_attempt_id=self.qualified("m0_research", evidence_hash, cycle_id, {}),
            compose_attempt_id=self.qualified("m0_compose", compose_hash, cycle_id, {"m0_markdown": text}),
            evidence_packet_hash=evidence_hash, packet_hash=compose_hash,
        )

    def publish_m1(self, text: str) -> dict:
        research_hash, judgment_hash = "test-m1-research", "test-m1-judgment"
        return self.engine.m1_ready(
            self.cycle["cycle_id"], text,
            research_attempt_id=self.qualified("m1_research", research_hash, output={}),
            judgment_attempt_id=self.qualified("m1_judgment", judgment_hash, output={"m1_markdown": text}),
            research_packet_hash=research_hash, judgment_packet_hash=judgment_hash,
        )

    def publish_m2(self, text: str) -> dict:
        packet_hash = "test-m2"
        return self.engine.m2_ready(
            self.cycle["cycle_id"], text,
            attempt_id=self.qualified("m2", packet_hash, output={"m2_markdown": text}), packet_hash=packet_hash,
        )

    def stage(self, command_id: str, text: str) -> dict:
        return self.engine.command({
            "command_id": command_id,
            "cycle_id": self.cycle["cycle_id"],
            "type": "stage_message",
            "text": text,
        })

    def test_staged_messages_are_withdrawable_and_commit_atomically_as_h0(self):
        self.ready()
        first = self.stage("stage-1", "先看转强")
        first_id = first["user_messages"][0]["message_id"]
        self.stage("stage-2", "修正：冲高可能回落")
        self.engine.command({
            "command_id": "withdraw",
            "cycle_id": self.cycle["cycle_id"],
            "type": "withdraw_staged_message",
            "message_id": first_id,
        })

        result = self.engine.command({
            "command_id": "commit",
            "cycle_id": self.cycle["cycle_id"],
            "type": "commit_h0",
        })

        self.assertEqual("researching_m1", result["cycle"]["state"])
        self.assertTrue(result["has_h0"])
        h0 = self.store.latest_artifact(self.cycle["cycle_id"], "h0")
        self.assertEqual("修正：冲高可能回落", h0["body_markdown"])
        self.assertEqual(1, len(result["user_messages"]))
        self.assertEqual("submitted", result["user_messages"][0]["state"])

    def test_rejected_verifier_cannot_publish_m0(self):
        self.engine.research_started(self.cycle["cycle_id"])
        rejected = self.store.begin_attempt(self.cycle["cycle_id"], "m0_research", iso(self.now), "bad-evidence")
        self.store.finish_attempt(rejected["attempt_id"], "rejected", verifier={"passed": False, "problems": ["missing"]})
        compose = self.qualified("m0_compose", "good-compose", output={"m0_markdown": "should not publish"})
        with self.assertRaisesRegex(ValueError, "not qualified"):
            self.engine.research_ready(
                self.cycle["cycle_id"], "should not publish",
                evidence_attempt_id=rejected["attempt_id"], compose_attempt_id=compose,
                evidence_packet_hash="bad-evidence", packet_hash="good-compose",
            )
        self.assertIsNone(self.store.latest_artifact(self.cycle["cycle_id"], "m0"))

    def test_invalidates_published_m0_without_overwriting_it(self):
        ready = self.ready()
        original = self.store.latest_artifact(self.cycle["cycle_id"], "m0")

        result = self.engine.command({
            "command_id": "invalidate-m0-calendar-conflict",
            "type": "invalidate_m0",
            "cycle_id": self.cycle["cycle_id"],
            "expected_revision": self.store.get_cycle(self.cycle["cycle_id"])["revision"],
            "reason": "M0 与本地交易日历冲突。",
            "qualification_problems": ["m0_calendar_context_conflict"],
        })

        self.assertEqual("failed", result["state"])
        self.assertEqual(original["artifact_id"], self.store.latest_artifact(self.cycle["cycle_id"], "m0")["artifact_id"])
        with self.store.connection() as connection:
            event = connection.execute(
                "SELECT payload_json FROM client_event_log WHERE cycle_id=? AND event_type='m0.invalidated'",
                (self.cycle["cycle_id"],),
            ).fetchone()
        self.assertIn("m0_calendar_context_conflict", event[0])

    def test_m0_artifact_state_and_outbox_are_one_transaction(self):
        self.engine.research_started(self.cycle["cycle_id"])
        evidence_hash, compose_hash = "atomic-evidence", "atomic-compose"
        evidence = self.qualified("m0_research", evidence_hash, output={})
        compose = self.qualified("m0_compose", compose_hash, output={"m0_markdown": "atomic"})
        with patch.object(self.store, "queue_event", side_effect=RuntimeError("fault after state")):
            with self.assertRaisesRegex(RuntimeError, "fault after state"):
                self.engine.research_ready(
                    self.cycle["cycle_id"], "atomic", evidence_attempt_id=evidence,
                    compose_attempt_id=compose, evidence_packet_hash=evidence_hash, packet_hash=compose_hash,
                )
        self.assertIsNone(self.store.latest_artifact(self.cycle["cycle_id"], "m0"))
        self.assertEqual("researching_m0", self.store.get_cycle(self.cycle["cycle_id"])["state"])

    def test_empty_h0_commit_means_no_comment_and_still_starts_blind_m1(self):
        self.ready()
        result = self.engine.command({
            "command_id": "no-comment", "cycle_id": self.cycle["cycle_id"], "type": "commit_h0"
        })

        self.assertEqual("researching_m1", result["cycle"]["state"])
        self.assertFalse(result["has_h0"])
        self.assertIsNone(self.store.latest_artifact(self.cycle["cycle_id"], "h0"))

    def test_diagnostic_rerun_isolated_from_original_cycle_and_reuses_frozen_inputs(self):
        ready = self.ready()
        self.stage("rerun-h0", "保留原来的独立判断边界")
        self.engine.command({
            "command_id": "rerun-commit", "cycle_id": self.cycle["cycle_id"], "type": "commit_h0",
        })
        original = self.store.get_cycle(self.cycle["cycle_id"])

        rerun = self.engine.start_diagnostic_rerun(original["cycle_id"])

        self.assertNotEqual(original["cycle_id"], rerun["cycle_id"])
        self.assertEqual("researching_m1", rerun["state"])
        self.assertEqual(original["task_key"], rerun["task_key"])
        self.assertNotEqual(original["scheduled_for"], rerun["scheduled_for"])
        snapshot = json.loads(rerun["schedule_snapshot_json"])
        self.assertTrue(snapshot["diagnostic_rerun"])
        self.assertEqual(original["cycle_id"], snapshot["diagnostic_rerun_of"])
        self.assertIsNotNone(self.store.latest_artifact(rerun["cycle_id"], "m0"))
        self.assertIsNotNone(self.store.latest_artifact(rerun["cycle_id"], "h0"))
        self.assertEqual("researching_m1", self.store.get_cycle(original["cycle_id"])["state"])

    def test_deadline_auto_submits_only_staged_messages_once(self):
        ready = self.ready()
        self.assertEqual(600, ready["m1_reserve_seconds"])
        self.assertEqual(1, ready["timing_policy_version"])
        self.stage("stage", "这是已经点击发送的内容")

        changed = self.engine.run_due(parse(ready["h0_auto_submit_at"]) + timedelta(seconds=1))
        second = self.engine.run_due(parse(ready["h0_auto_submit_at"]) + timedelta(seconds=2))

        self.assertEqual(1, len(changed))
        self.assertEqual([], second)
        self.assertEqual("researching_m1", changed[0]["cycle"]["state"])
        self.assertEqual("这是已经点击发送的内容", self.store.latest_artifact(self.cycle["cycle_id"], "h0")["body_markdown"])

    def test_m1_packet_is_blind_to_current_h0(self):
        self.ready()
        secret_h0 = "我最后明确判断机器人板块会冲高回落"
        self.stage("stage", secret_h0)
        self.engine.command({"command_id": "commit", "cycle_id": self.cycle["cycle_id"], "type": "commit_h0"})
        cycle = self.store.get_cycle(self.cycle["cycle_id"])

        packet = RuntimePacketBuilder(PROJECT_ROOT / "resources", PROJECT_ROOT / "data", self.store).build(
            cycle,
            "m1_judgment",
            evidence={"as_of": "2026-08-25T02:00:00Z", "sources": []},
        )

        self.assertNotIn(secret_h0, json.dumps(packet, ensure_ascii=False))
        self.assertNotIn("h0", [artifact["kind"] for artifact in packet["artifacts"]])

    def test_m1_public_research_inherits_only_prior_public_context(self):
        self.ready()
        secret_h0 = "这是不能发给联网研究的私人判断"
        self.stage("stage", secret_h0)
        self.engine.command({"command_id": "commit", "cycle_id": self.cycle["cycle_id"], "type": "commit_h0"})
        cycle = self.store.get_cycle(self.cycle["cycle_id"])

        packet = RuntimePacketBuilder(PROJECT_ROOT / "resources", PROJECT_ROOT / "data", self.store).build(
            cycle,
            "m1_research",
            evidence={
                "as_of": "2026-08-25T02:00:00Z",
                "spoken_summary": "公开市场先前出现通信分化。",
                "sources": [{"title": "公开新闻标题"}],
                "critical_gaps": ["缺少最新市场广度"],
            },
        )

        serialized = json.dumps(packet, ensure_ascii=False)
        self.assertIn("公开市场先前出现通信分化", serialized)
        self.assertIn("当前 A 股主要指数", serialized)
        self.assertNotIn(secret_h0, serialized)
        self.assertNotIn("local_inputs", packet)

    def test_formal_m1_is_single_and_m2_only_exists_with_h0(self):
        self.ready()
        self.stage("stage", "偏弱")
        self.engine.command({"command_id": "commit", "cycle_id": self.cycle["cycle_id"], "type": "commit_h0"})
        self.engine.m1_judgment_started(self.cycle["cycle_id"])

        result = self.publish_m1("我的独立判断是减仓。")

        self.assertEqual("synthesizing_m2", result["state"])
        with self.assertRaisesRegex(ValueError, "formal M1 already exists"):
            self.publish_m1("另一份 M1")
        completed = self.publish_m2("综合后仍然偏谨慎。")
        self.assertEqual("complete", completed["state"])
        formal_events = {
            item["event_type"]: json.loads(item["payload_json"])
            for item in self.store.pending_events()
            if item["event_type"] in {"m0.ready", "m1.ready", "m2.ready"}
        }
        self.assertEqual({"m0.ready", "m1.ready", "m2.ready"}, set(formal_events))
        for payload in formal_events.values():
            self.assertEqual("companion-published-message/v2", payload["message"]["contract"])
            visible = payload["message"]["text_projection"]
            self.assertNotIn("time_scope:", visible)
            self.assertNotIn("reference_at:", visible)
            self.assertNotIn("Protocol:", visible)

    def test_companion_side_paths_publish_the_same_v2_message_contract(self):
        self.engine.chat_ready(self.cycle["cycle_id"], "我先看承接，不急着追。")
        self.engine.publish_proactive_message(
            self.cycle["cycle_id"], "outcome", "刚核对过，原来的担心还没有消失。", meaningful=True,
        )
        self.engine.publish_proactive_message(
            self.cycle["cycle_id"], "reflection", "这次我把量能看得太轻了。", meaningful=True,
        )
        prior = self.store.latest_artifact(self.cycle["cycle_id"], "outcome")
        self.engine.judgment_revision_ready(
            self.cycle["cycle_id"], "现在的新证据足以让我改成观望。", prior["artifact_id"],
        )
        failed_cycle = self.engine.start_cycle(
            "daily.execution.1030", "2026-08-25T10:30:00+08:00", iso(self.now),
        )
        self.engine.research_started(failed_cycle["cycle_id"])
        self.engine.research_failed(failed_cycle["cycle_id"], "provider timeout")

        visible_types = {"chat.ready", "outcome.ready", "reflection.ready", "judgment.revised", "research.failed"}
        events = [item for item in self.store.pending_events() if item["event_type"] in visible_types]
        self.assertEqual(visible_types, {item["event_type"] for item in events})
        for event in events:
            payload = json.loads(event["payload_json"])
            self.assertEqual("companion-published-message/v2", payload["message"]["contract"])
            self.assertTrue(payload["message"]["text_projection"].strip())

    def test_no_h0_skips_m2(self):
        self.ready()
        self.engine.command({"command_id": "commit", "cycle_id": self.cycle["cycle_id"], "type": "commit_h0"})
        self.engine.m1_judgment_started(self.cycle["cycle_id"])

        result = self.publish_m1("独立判断为观望。")

        self.assertEqual("complete", result["state"])
        with self.assertRaisesRegex(ValueError, "M2 requires H0"):
            self.publish_m2("不应生成")

    def test_unqualified_m1_never_starts_or_allows_m2(self):
        self.ready()
        self.stage("stage-unqualified", "偏弱")
        self.engine.command({"command_id": "commit-unqualified", "cycle_id": self.cycle["cycle_id"], "type": "commit_h0"})
        self.engine.m1_judgment_started(self.cycle["cycle_id"])
        research_hash, judgment_hash = "unqualified-research", "unqualified-judgment"
        output = {
            "m1_markdown": "证据不足，暂不形成判断。", "judgment_qualified": False,
            "snapshot": {"qualified": False, "direction": "uncertain", "triggers": [], "invalidations": []},
        }
        result = self.engine.m1_ready(
            self.cycle["cycle_id"], output["m1_markdown"],
            research_attempt_id=self.qualified("m1_research", research_hash, output={}),
            judgment_attempt_id=self.qualified("m1_judgment", judgment_hash, output=output),
            research_packet_hash=research_hash, judgment_packet_hash=judgment_hash,
            snapshot=output["snapshot"], qualified=False,
        )
        self.assertEqual("complete", result["state"])
        packet_hash = "blocked-m2"
        with self.assertRaisesRegex(ValueError, "qualified M1"):
            self.engine.m2_ready(
                self.cycle["cycle_id"], "不应生成",
                attempt_id=self.qualified("m2", packet_hash, output={"m2_markdown": "不应生成"}),
                packet_hash=packet_hash,
            )

    def test_m2_memory_is_frozen_at_m1_completion(self):
        self.ready()
        self.stage("stage", "偏弱")
        self.engine.command({"command_id": "commit", "cycle_id": self.cycle["cycle_id"], "type": "commit_h0"})
        self.engine.m1_judgment_started(self.cycle["cycle_id"])
        self.publish_m1("独立判断偏谨慎。")
        cycle = self.store.get_cycle(self.cycle["cycle_id"])
        other = self.engine.start_cycle("daily.execution.1030", "2026-08-25T10:30:00+08:00", "2026-08-25T02:30:00Z")
        future_text = "M1完成之后才出现的信息不能进入原始M2"
        self.store.append_artifact(
            other["cycle_id"], "reflection", "model", future_text, "2026-08-25T03:00:00Z",
            known_at="2099-01-01T00:00:00Z",
        )

        packet = RuntimePacketBuilder(PROJECT_ROOT / "resources", PROJECT_ROOT / "data", self.store).build(
            cycle, "m2", as_of=cycle["m1_completed_at"]
        )

        self.assertNotIn(future_text, json.dumps(packet, ensure_ascii=False))

    def test_m1_failure_event_is_human_readable_and_hides_local_diagnostics(self):
        self.ready()
        self.engine.command({"command_id": "commit", "cycle_id": self.cycle["cycle_id"], "type": "commit_h0"})

        self.engine.m1_failed(
            self.cycle["cycle_id"],
            r"C:\\Users\\will\\secret invalid_json_schema Missing factual_reliability",
            retryable=True,
        )

        events = [event for event in self.store.pending_events() if event["event_type"] == "m1.retrying"]
        payload = json.loads(events[-1]["payload_json"])
        self.assertEqual("output_schema_invalid", payload["diagnostic_code"])
        self.assertIn("输出格式配置错误", payload["reason"])
        self.assertNotIn("C:\\Users", payload["reason"])

    def test_m1_local_verifier_failure_has_an_honest_user_category(self):
        self.ready()
        self.engine.command({"command_id": "commit-verifier", "cycle_id": self.cycle["cycle_id"], "type": "commit_h0"})

        self.engine.m1_failed(
            self.cycle["cycle_id"],
            "Broker output did not pass local verification",
            retryable=True,
            details={
                "passed": False,
                "schema": {"passed": False, "problems": ["$.snapshot.triggers: required"]},
                "business": {"passed": True, "problems": []},
            },
        )

        event = [item for item in self.store.pending_events() if item["event_type"] == "m1.retrying"][-1]
        payload = json.loads(event["payload_json"])
        self.assertEqual("output_schema_invalid", payload["diagnostic_code"])
        self.assertIn("本地输出格式校验", payload["reason"])
        self.assertNotIn("详细诊断已保留", payload["reason"])

    def test_broker_unavailable_has_explicit_user_category(self):
        self.ready()
        self.engine.command({"command_id": "commit-http", "cycle_id": self.cycle["cycle_id"], "type": "commit_h0"})

        self.engine.m1_failed(
            self.cycle["cycle_id"],
            "broker_unavailable: Broker HTTP 503",
            retryable=False,
        )

        events = [event for event in self.store.pending_events() if event["event_type"] == "m1.failed"]
        payload = json.loads(events[-1]["payload_json"])
        self.assertEqual("broker_unavailable", payload["diagnostic_code"])
        self.assertIn("LLM 服务当前没有可用上游", payload["reason"])

    def test_plain_broker_http_503_has_explicit_user_category(self):
        self.assertEqual(
            "broker_unavailable",
            self.engine._diagnostic_code("Broker HTTP 503"),
        )

    def test_chat_batch_is_separate_from_frozen_h0(self):
        self.ready()
        self.stage("h0", "H0内容")
        self.engine.command({"command_id": "lock", "cycle_id": self.cycle["cycle_id"], "type": "commit_h0"})
        self.stage("chat", "H0之后的新问题")

        result = self.engine.command({
            "command_id": "chat-submit", "cycle_id": self.cycle["cycle_id"], "type": "commit_chat_batch"
        })

        self.assertIsNotNone(result["committed_batch_id"])
        self.assertEqual("H0内容", self.store.latest_artifact(self.cycle["cycle_id"], "h0")["body_markdown"])
        self.assertEqual("H0之后的新问题", self.store.latest_artifact(self.cycle["cycle_id"], "chat_human")["body_markdown"])

    @unittest.skip("superseded by MemoryHub snapshot contract tests")
    def test_memory_retrieval_obeys_known_at(self):
        self.ready()
        artifact = self.store.append_artifact(
            self.cycle["cycle_id"], "reflection", "model", "后来才知道的经验",
            "2026-08-25T01:00:00Z", known_at="2026-08-25T06:00:00Z",
        )
        retriever = SqliteMemoryRetriever(self.store)

        early = retriever.retrieve(MemoryQuery("daily.execution.0945", "2026-08-25T05:59:59Z"))
        late = retriever.retrieve(MemoryQuery("daily.execution.0945", "2026-08-25T06:00:00Z"))

        self.assertNotIn(artifact["artifact_id"], [item["artifact_id"] for item in early])
        self.assertIn(artifact["artifact_id"], [item["artifact_id"] for item in late])

    def test_command_revision_conflict_is_rejected(self):
        ready = self.ready()
        with self.assertRaisesRegex(ValueError, "revision conflict"):
            self.engine.command({
                "command_id": "stale", "cycle_id": self.cycle["cycle_id"], "type": "stage_message",
                "text": "判断", "expected_revision": ready["revision"] - 1,
            })

    def test_start_cycle_is_idempotent_after_state_revision_changes(self):
        ready = self.ready()
        duplicate = self.engine.start_cycle(
            "daily.execution.0945", "2026-08-25T09:45:00+08:00", iso(self.now)
        )
        self.assertEqual(ready["cycle_id"], duplicate["cycle_id"])
        with self.store.connection() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM companion_cycle WHERE task_key=? AND scheduled_for=?",
                ("daily.execution.0945", "2026-08-25T09:45:00+08:00"),
            ).fetchone()[0]
        self.assertEqual(1, count)

    def test_manual_analysis_request_creates_or_reuses_an_independent_formal_cycle(self):
        request = {
            "command_id": "manual-analysis-command-1",
            "type": "request_formal_analysis",
            "request_id": "analysis-request-1",
            "task_key": "daily.review.1520",
            "requested_at": "2026-08-29T10:00:00+08:00",
            "source": {"conversation_cycle_id": "conversation-2026-08-29", "batch_id": "batch-1"},
            "task_profile": {"profile_id": "non_trading_research", "version": 1},
        }

        created = self.engine.command(request)
        replayed = self.engine.command({**request, "command_id": "manual-analysis-command-1-replay"})
        another = self.engine.command({
            **request,
            "command_id": "manual-analysis-command-2",
            "request_id": "analysis-request-2",
            "source": {"conversation_cycle_id": "conversation-2026-08-29", "batch_id": "batch-2"},
        })

        self.assertEqual("created", created["receipt"]["state"])
        self.assertEqual("reused", replayed["receipt"]["state"])
        self.assertEqual(created["receipt"]["cycle_id"], replayed["receipt"]["cycle_id"])
        self.assertEqual("created", another["receipt"]["state"])
        self.assertNotEqual(created["receipt"]["cycle_id"], another["receipt"]["cycle_id"])
        self.assertEqual("manual_chat", created["projection"]["cycle"]["trigger"])
        self.assertEqual("analysis-request-1", created["projection"]["cycle"]["request_id"])
        self.assertEqual("non_trading_research", created["projection"]["cycle"]["task_profile_id"])

        today = self.engine.command({
            "command_id": "manual-analysis-today", "type": "request_today_projections",
            "scheduled_date": "2026-08-29",
        })
        self.assertEqual(
            {created["receipt"]["cycle_id"], another["receipt"]["cycle_id"]},
            {item["cycle"]["cycle_id"] for item in today["projections"]},
        )

    def test_dismiss_manual_analyses_hides_every_matching_cycle_without_deleting_audit_history(self):
        cycle_ids = []
        for index in range(2):
            result = self.engine.command({
                "command_id": f"manual-intraday-{index}",
                "type": "request_formal_analysis",
                "request_id": f"manual-intraday-request-{index}",
                "task_key": "daily.execution.0945",
                "requested_at": f"2026-08-29T13:0{index}:00+08:00",
                "source": {"kind": "test"},
                "task_profile": {"profile_id": "intraday_execution", "version": 1},
            })
            cycle_ids.append(result["receipt"]["cycle_id"])

        dismissed = self.engine.command({
            "command_id": "dismiss-all-intraday",
            "type": "dismiss_manual_analyses",
            "task_profile_id": "intraday_execution",
            "reason": "user_requested_cleanup",
        })

        self.assertEqual(2, dismissed["dismissed_count"])
        self.assertEqual(set(cycle_ids), set(dismissed["cycle_ids"]))
        self.assertEqual([], self.store.latest_cycles_for_date("2026-08-29"))
        visible_history_ids = {item["cycle_id"] for item in self.store.history_page(limit=20)["items"]}
        self.assertTrue(set(cycle_ids).isdisjoint(visible_history_ids))
        with self.store.connection() as connection:
            self.assertEqual(2, connection.execute(
                "SELECT COUNT(*) FROM companion_cycle WHERE task_profile_id='intraday_execution'"
            ).fetchone()[0])
            self.assertEqual(2, connection.execute(
                "SELECT COUNT(*) FROM companion_cycle_visibility WHERE dismissed_at IS NOT NULL"
            ).fetchone()[0])

    def test_manual_analysis_request_never_consumes_a_scheduled_occurrence(self):
        manual = self.engine.command({
            "command_id": "manual-at-scheduled-time",
            "type": "request_formal_analysis",
            "request_id": "analysis-request-at-scheduled-time",
            "task_key": "daily.review.1520",
            "requested_at": "2026-08-29T15:20:00+08:00",
            "source": {"conversation_cycle_id": "conversation-2026-08-29", "batch_id": "batch-3"},
            "task_profile": {"profile_id": "post_close_review", "version": 1},
        })

        scheduled = self.engine.start_cycle(
            "daily.review.1520", "2026-08-29T15:20:00+08:00", "2026-08-29T15:20:00+08:00",
        )

        self.assertNotEqual(manual["receipt"]["cycle_id"], scheduled["cycle_id"])
        self.assertEqual("manual_chat", manual["projection"]["cycle"]["trigger"])
        self.assertEqual("scheduled", scheduled["trigger"])

    def test_today_projection_keeps_the_started_scheduled_cycle_over_repair_copies(self):
        scheduled = self.store.create_cycle(
            "daily.review.1520", "2026-08-29T15:20:00+08:00", "2026-08-29T15:20:00+08:00",
            work_start_at="2026-08-29T15:20:00+08:00",
        )
        self.store.stage_message(scheduled["cycle_id"], "我的收盘复盘", "h0")
        repair = self.store.create_cycle(
            "daily.review.1520", "2026-08-29T08:53:35.624Z", "2026-08-29T08:53:35.624Z",
        )
        self.store.append_artifact(
            repair["cycle_id"], "m1", "model", "较新的修复结果", "2026-08-29T08:54:00Z", {},
        )

        cycles = self.store.latest_cycles_for_date("2026-08-29")

        self.assertEqual([scheduled["cycle_id"]], [item["cycle_id"] for item in cycles])

    def test_manual_cycle_survives_store_restart_and_keeps_schedule_claims_separate(self):
        manual = self.engine.command({
            "command_id": "restart-manual", "type": "request_formal_analysis",
            "request_id": "restart-manual-request", "task_key": "daily.review.1520",
            "requested_at": "2026-08-29T15:20:00+08:00",
            "source": {"conversation_cycle_id": "conversation-restart", "batch_id": "batch-restart"},
            "task_profile": {"profile_id": "post_close_review", "version": 1},
        })
        scheduled = self.engine.start_cycle(
            "daily.review.1520", "2026-08-29T15:20:00+08:00", "2026-08-29T15:20:00+08:00",
        )
        reopened = CompanionEngine(CompanionStore(self.store.database))

        cycles = reopened.store.latest_cycles_for_date("2026-08-29")
        self.assertEqual({manual["receipt"]["cycle_id"], scheduled["cycle_id"]}, {item["cycle_id"] for item in cycles})
        with reopened.store.connection() as connection:
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM companion_schedule_claim WHERE task_key=? AND scheduled_for=?",
                ("daily.review.1520", "2026-08-29T15:20:00+08:00"),
            ).fetchone()[0])
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM companion_manual_analysis_claim WHERE request_id=?",
                ("restart-manual-request",),
            ).fetchone()[0])
        history = reopened.store.history_page(limit=20)
        self.assertIn(manual["receipt"]["cycle_id"], [item["cycle_id"] for item in history["items"]])

    def test_structured_manual_request_freezes_selected_profile_contract_and_manual_deadlines(self):
        engine = CompanionEngine(
            self.store,
            task_profiles=ManualAnalysisProfileResolver(_WeekdayCalendar()),
            evidence_contract_factory=EvidenceContractFactory(_WeekdayCalendar()),
        )
        result = engine.command({
            "command_id": "structured-manual-analysis",
            "type": "request_formal_analysis",
            "request_id": "structured-analysis-request",
            "requested_at": "2026-08-28T14:58:00+08:00",
            "source": {"conversation_cycle_id": "conversation-1", "batch_id": "batch-1"},
            "analysis": {"subject": "人工智能", "time_scope": "current_session", "goal": "核验风险"},
        })
        cycle = result["projection"]["cycle"]

        self.assertEqual("intraday_execution", cycle["task_profile_id"])
        self.assertEqual("daily.execution.0945", cycle["task_key"])
        self.assertEqual("2026-08-28T14:58:00+08:00", cycle["requested_at"])
        self.assertEqual(64, len(cycle["evidence_contract_hash"]))
        packet = packet_builder(self.store).build(cycle, "m0_research")
        self.assertEqual(cycle["evidence_contract_hash"], packet["evidence_contract"]["contract_hash"])

        engine.research_started(cycle["cycle_id"])
        evidence_hash, compose_hash = "structured-evidence", "structured-compose"
        with patch("ai_trading_companion.engine.utc_now", return_value=datetime(2026, 8, 28, 7, 8, tzinfo=timezone.utc)):
            ready = engine.research_ready(
                cycle["cycle_id"], "M0",
                evidence_attempt_id=self.qualified("m0_research", evidence_hash, cycle["cycle_id"], {}),
                compose_attempt_id=self.qualified("m0_compose", compose_hash, cycle["cycle_id"], {"m0_markdown": "M0"}),
                evidence_packet_hash=evidence_hash, packet_hash=compose_hash,
            )
        self.assertEqual("2026-08-28T07:28:00.000Z", ready["h0_auto_submit_at"])
        self.assertEqual("2026-08-28T07:38:00.000Z", ready["m1_publish_deadline"])

    def test_rejected_manual_analysis_request_returns_a_receipt_without_a_cycle(self):
        result = self.engine.command({
            "command_id": "rejected-manual-analysis",
            "type": "request_formal_analysis",
            "request_id": "invalid-analysis-request",
            "task_key": "not-a-task-profile",
            "requested_at": "2026-08-29T15:20:00+08:00",
            "source": {"conversation_cycle_id": "conversation-2026-08-29", "batch_id": "batch-invalid"},
            "task_profile": {"profile_id": "post_close_review", "version": 1},
        })

        self.assertEqual("rejected", result["receipt"]["state"])
        self.assertEqual("invalid-analysis-request", result["receipt"]["request_id"])
        self.assertIn("unregistered task_key", result["receipt"]["reason"])
        today = self.engine.command({
            "command_id": "rejected-manual-analysis-today", "type": "request_today_projections",
            "scheduled_date": "2026-08-29",
        })
        self.assertEqual([], today["projections"])

    def test_manual_analysis_request_requires_a_non_empty_stable_request_id(self):
        result = self.engine.command({
            "command_id": "empty-request-id",
            "type": "request_formal_analysis",
            "request_id": "",
            "task_key": "daily.review.1520",
            "requested_at": "2026-08-29T15:20:00+08:00",
            "source": {"conversation_cycle_id": "conversation-2026-08-29", "batch_id": "batch-empty-id"},
            "task_profile": {"profile_id": "post_close_review", "version": 1},
        })

        self.assertEqual("rejected", result["receipt"]["state"])
        self.assertIn("request_id", result["receipt"]["reason"])

    def test_today_projection_request_replays_the_latest_cycle_per_task(self):
        ready = self.ready()
        with self.store.connection() as connection:
            connection.execute(
                """INSERT INTO companion_cycle(
                     cycle_id,task_key,scheduled_for,as_of,state,revision,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                ("empty-migration-copy", "daily.execution.0945", "2026-08-25T09:45:00+08:00",
                 iso(self.now), "complete", 2, "2099-01-01T00:00:00Z", "2099-01-01T00:00:00Z"),
            )

        result = self.engine.command({
            "command_id": "today-projections",
            "type": "request_today_projections",
            "scheduled_date": "2026-08-25",
        })

        self.assertEqual("2026-08-25", result["scheduled_date"])
        self.assertEqual([ready["cycle_id"]], [item["cycle"]["cycle_id"] for item in result["projections"]])
        events = [event for event in self.store.pending_events() if event["event_type"] == "projection.ready"]
        self.assertEqual(1, len(events))

    def test_projection_preserves_migrated_legacy_ai_message_for_display(self):
        self.store.append_artifact(
            self.cycle["cycle_id"], "legacy_message", "migration", "迁移保留的盘中 AI 消息", iso(self.now)
        )

        projection = self.engine._projection(self.cycle)

        self.assertEqual(["legacy_message"], [item["kind"] for item in projection["ai_messages"]])
        self.assertEqual("迁移保留的盘中 AI 消息", projection["ai_messages"][0]["text"])

    def test_pre_m0_messages_freeze_at_research_start_and_shape_m0_context(self):
        cycle = self.engine.start_cycle(
            "daily.opportunity.0900", "2026-08-25T09:00:00+08:00", "2026-08-24T16:05:00Z"
        )
        projection = self.engine.command({
            "command_id": "pre-m0-stage",
            "cycle_id": cycle["cycle_id"],
            "type": "stage_message",
            "text": "机器人板块昨晚讨论明显升温，帮我重点核实传播源头。",
        })

        self.assertEqual("pre_m0", projection["user_messages"][0]["phase"])
        self.assertEqual("staged", projection["user_messages"][0]["state"])

        started = self.engine.research_started(cycle["cycle_id"])
        frozen = self.store.messages(cycle["cycle_id"], state="submitted", phase="pre_m0")
        artifact = self.store.latest_artifact(cycle["cycle_id"], "pre_m0")
        builder = packet_builder(self.store)
        public_packet = builder.build(started, "m0_research")
        compose_packet = builder.build(started, "m0_compose", evidence={"sources": []})

        self.assertEqual(1, len(frozen))
        self.assertEqual("机器人板块昨晚讨论明显升温，帮我重点核实传播源头。", artifact["body_markdown"])
        self.assertIn("机器人板块昨晚讨论明显升温", json.dumps(public_packet, ensure_ascii=False))
        self.assertIn("pre_m0", [item["kind"] for item in compose_packet["artifacts"]])
        self.assertFalse(started["has_h0"])

    def test_historical_research_start_preserves_explicit_frozen_as_of(self):
        cycle = self.engine.start_cycle(
            "daily.review.1520", "2026-08-27T15:20:00+08:00", "2026-08-27T07:20:02.555Z"
        )

        started = self.engine.research_started(cycle["cycle_id"], as_of=cycle["as_of"])

        self.assertEqual("2026-08-27T07:20:02.555Z", started["as_of"])

    def test_pre_m0_messages_can_be_submitted_in_batches_before_research(self):
        cycle = self.engine.start_cycle(
            "daily.opportunity.0900", "2026-08-25T09:00:00+08:00", "2026-08-24T16:05:00Z"
        )
        self.engine.command({
            "command_id": "pre-m0-stage-first",
            "cycle_id": cycle["cycle_id"],
            "type": "stage_message",
            "text": "先核实机器人板块的传播源头。",
        })

        submitted = self.engine.command({
            "command_id": "pre-m0-submit-first",
            "cycle_id": cycle["cycle_id"],
            "type": "commit_pre_m0",
        })

        self.assertEqual("queued", submitted["cycle"]["state"])
        self.assertEqual("submitted", submitted["user_messages"][0]["state"])
        self.assertEqual("pre_m0", submitted["user_messages"][0]["phase"])
        submission = self.store.latest_artifact(cycle["cycle_id"], "pre_m0_submission")
        self.assertEqual("先核实机器人板块的传播源头。", submission["body_markdown"])

        self.engine.command({
            "command_id": "pre-m0-stage-second",
            "cycle_id": cycle["cycle_id"],
            "type": "stage_message",
            "text": "也看看政策端有没有新的催化。",
        })
        started = self.engine.research_started(cycle["cycle_id"])
        artifact = self.store.latest_artifact(cycle["cycle_id"], "pre_m0")
        builder = packet_builder(self.store)
        public_packet = builder.build(started, "m0_research")

        self.assertIn("先核实机器人板块", artifact["body_markdown"])
        self.assertIn("也看看政策端", artifact["body_markdown"])
        self.assertIn("先核实机器人板块", json.dumps(public_packet, ensure_ascii=False))
        self.assertIn("也看看政策端", json.dumps(public_packet, ensure_ascii=False))

    def test_submitted_pre_m0_batch_can_receive_a_first_class_premarket_reply(self):
        cycle = self.engine.start_cycle(
            "daily.opportunity.0900", "2026-08-25T09:00:00+08:00", "2026-08-24T16:05:00Z"
        )
        self.engine.command({
            "command_id": "pre-chat-stage",
            "cycle_id": cycle["cycle_id"],
            "type": "stage_message",
            "text": "早上先聊聊机器人板块。",
        })
        submitted = self.engine.command({
            "command_id": "pre-chat-submit",
            "cycle_id": cycle["cycle_id"],
            "type": "commit_pre_m0",
        })

        first = run_pending_premarket_reply(
            self.engine, self.store, None, cycle["cycle_id"], False
        )
        second = run_pending_premarket_reply(
            self.engine, self.store, None, cycle["cycle_id"], False
        )

        projection = self.engine._projection(self.store.get_cycle(cycle["cycle_id"]))
        reply = next(message for message in projection["ai_messages"] if message["kind"] == "premarket_chat")
        self.assertIn("已收到这批消息", reply["text"])
        events = [event["event_type"] for event in self.store.pending_events()]
        self.assertIn("premarket.reply.ready", events)
        self.assertEqual("replied", first["action"])
        self.assertEqual("already_replied", second["action"])
        self.assertEqual(submitted["committed_batch_id"], first["batch_id"])

    def test_scheduler_starts_due_cycle_once(self):
        completed: list[str] = []

        def execute(cycle: dict) -> dict:
            completed.append(cycle["cycle_id"])
            self.engine.research_started(cycle["cycle_id"])
            return self.publish_fixture_m0(cycle["cycle_id"])

        at = datetime(2026, 8, 25, 2, 30, 30, tzinfo=timezone.utc)
        first = run_daily_schedule(self.engine, self.store, at, execute)
        second = run_daily_schedule(self.engine, self.store, at, execute)

        self.assertIn("daily.execution.1030", [item["task_key"] for item in first if item["action"] == "started"])
        self.assertEqual([], second)
        self.assertEqual(1, len(completed))

    def test_premarket_cycle_prefetches_at_0830_but_keeps_0900_identity(self):
        completed: list[str] = []

        def execute(cycle: dict) -> dict:
            completed.append(cycle["scheduled_for"])
            self.engine.research_started(cycle["cycle_id"])
            return self.publish_fixture_m0(cycle["cycle_id"])

        at = datetime(2026, 8, 25, 0, 30, tzinfo=timezone.utc)
        results = run_daily_schedule(self.engine, self.store, at, execute)

        premarket = next(item for item in results if item["task_key"] == "daily.opportunity.0900")
        self.assertEqual("started", premarket["action"])
        self.assertEqual("2026-08-25T09:00:00+08:00", completed[0])

    def test_conversation_auto_submit_uses_configured_lead_before_actual_work_start(self):
        target = datetime.fromisoformat("2026-08-25T09:00:00+08:00")
        config = {"trigger": {"lead_minutes": 30}, "conversation_auto_submit_lead_minutes": None}

        self.assertEqual(
            datetime.fromisoformat("2026-08-25T08:10:00+08:00"),
            conversation_auto_submit_at(config, target, 20),
        )
        config["conversation_auto_submit_lead_minutes"] = False
        self.assertIsNone(conversation_auto_submit_at(config, target, 20))

    def test_registered_worker_claims_at_configured_work_start_not_formal_slot(self):
        cycle = self.engine.start_cycle(
            "daily.opportunity.0900", "2026-08-25T09:00:00+08:00", "2026-08-25T00:00:00Z",
            schedule_snapshot={"trigger": {"lead_minutes": 30}},
        )

        self.assertEqual([], self.store.claim_scheduled_workers(
            at=datetime.fromisoformat("2026-08-25T08:29:59+08:00")
        ))
        claimed = self.store.claim_scheduled_workers(
            at=datetime.fromisoformat("2026-08-25T08:30:00+08:00")
        )

        self.assertEqual([cycle["cycle_id"]], [item["cycle_id"] for item in claimed])

    def test_premarket_cycle_is_prepared_before_0830_without_starting_research(self):
        completed: list[str] = []
        before_lead = datetime(2026, 8, 24, 23, 15, tzinfo=timezone.utc)

        first = run_daily_schedule(
            self.engine, self.store, before_lead,
            lambda cycle: completed.append(cycle["cycle_id"]),
        )
        second = run_daily_schedule(
            self.engine, self.store, before_lead + timedelta(seconds=5),
            lambda cycle: completed.append(cycle["cycle_id"]),
        )

        prepared = [item for item in first if item["task_key"] == "daily.opportunity.0900"]
        self.assertEqual("prepared", prepared[0]["action"])
        self.assertEqual([], completed)
        self.assertEqual([], second)
        cycle = self.store.find_cycle("daily.opportunity.0900", "2026-08-25T09:00:00+08:00")
        self.assertEqual("queued", cycle["state"])

    def test_scheduler_records_late_cycle_as_missed_without_running_research(self):
        at = datetime(2026, 8, 25, 3, 0, tzinfo=timezone.utc)
        results = run_daily_schedule(self.engine, self.store, at, lambda cycle: self.fail("late task must not run research"))
        missed = [item for item in results if item["task_key"] == "daily.execution.1030"]
        self.assertEqual("missed", missed[0]["action"])
        cycle = self.store.find_cycle("daily.execution.1030", "2026-08-25T10:30:00+08:00")
        self.assertEqual("missed", cycle["state"])

    def test_periodic_schedule_starts_once_without_trading_day_filter(self):
        completed: list[str] = []

        def execute(cycle: dict) -> dict:
            completed.append(cycle["cycle_id"])
            self.engine.research_started(cycle["cycle_id"])
            return self.publish_fixture_m0(cycle["cycle_id"])

        _, periodic = load_schedules(PROJECT_ROOT / "resources")
        at = datetime(2026, 10, 2, 11, 31, tzinfo=timezone.utc)
        first = run_periodic_schedule(self.engine, self.store, at, execute, schedules=periodic)
        second = run_periodic_schedule(self.engine, self.store, at, execute, schedules=periodic)

        self.assertEqual(["periodic.quarterly"], [item["task_key"] for item in first if item["action"] == "started"])
        self.assertEqual([], second)
        self.assertEqual(1, len(completed))


if __name__ == "__main__":
    unittest.main()
