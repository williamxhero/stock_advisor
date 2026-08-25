from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.companion_runtime.engine import CompanionEngine, iso, parse
from scripts.companion_runtime.memory import MemoryQuery, SqliteMemoryRetriever
from scripts.companion_runtime.packet_builder import RuntimePacketBuilder
from scripts.companion_runtime.scheduler import run_daily_schedule
from scripts.companion_runtime.store import CompanionStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class CompanionEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = CompanionStore(Path(self.temp.name) / "companion.sqlite3")
        self.engine = CompanionEngine(self.store)
        self.now = datetime(2026, 8, 25, 1, 45, tzinfo=timezone.utc)
        self.cycle = self.engine.start_cycle(
            "daily.execution.0945", "2026-08-25T09:45:00+08:00", iso(self.now)
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def ready(self) -> dict:
        self.engine.research_started(self.cycle["cycle_id"])
        return self.engine.research_ready(self.cycle["cycle_id"], "今天先看客观信息。")

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

    def test_empty_h0_commit_means_no_comment_and_still_starts_blind_m1(self):
        self.ready()
        result = self.engine.command({
            "command_id": "no-comment", "cycle_id": self.cycle["cycle_id"], "type": "commit_h0"
        })

        self.assertEqual("researching_m1", result["cycle"]["state"])
        self.assertFalse(result["has_h0"])
        self.assertIsNone(self.store.latest_artifact(self.cycle["cycle_id"], "h0"))

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

        packet = RuntimePacketBuilder(PROJECT_ROOT, self.store).build(
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

        packet = RuntimePacketBuilder(PROJECT_ROOT, self.store).build(
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

        result = self.engine.m1_ready(self.cycle["cycle_id"], "我的独立判断是减仓。")

        self.assertEqual("synthesizing_m2", result["state"])
        with self.assertRaisesRegex(ValueError, "formal M1 already exists"):
            self.engine.m1_ready(self.cycle["cycle_id"], "另一份 M1")
        completed = self.engine.m2_ready(self.cycle["cycle_id"], "综合后仍然偏谨慎。")
        self.assertEqual("complete", completed["state"])

    def test_no_h0_skips_m2(self):
        self.ready()
        self.engine.command({"command_id": "commit", "cycle_id": self.cycle["cycle_id"], "type": "commit_h0"})
        self.engine.m1_judgment_started(self.cycle["cycle_id"])

        result = self.engine.m1_ready(self.cycle["cycle_id"], "独立判断为观望。")

        self.assertEqual("complete", result["state"])
        with self.assertRaisesRegex(ValueError, "M2 requires H0"):
            self.engine.m2_ready(self.cycle["cycle_id"], "不应生成")

    def test_m2_memory_is_frozen_at_m1_completion(self):
        self.ready()
        self.stage("stage", "偏弱")
        self.engine.command({"command_id": "commit", "cycle_id": self.cycle["cycle_id"], "type": "commit_h0"})
        self.engine.m1_judgment_started(self.cycle["cycle_id"])
        self.engine.m1_ready(self.cycle["cycle_id"], "独立判断偏谨慎。")
        cycle = self.store.get_cycle(self.cycle["cycle_id"])
        other = self.engine.start_cycle("daily.execution.1030", "2026-08-25T10:30:00+08:00", "2026-08-25T02:30:00Z")
        future_text = "M1完成之后才出现的信息不能进入原始M2"
        self.store.append_artifact(
            other["cycle_id"], "reflection", "model", future_text, "2026-08-25T03:00:00Z",
            known_at="2099-01-01T00:00:00Z",
        )

        packet = RuntimePacketBuilder(PROJECT_ROOT, self.store).build(
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

        events = [event for event in self.store.pending_events() if event["event_type"] == "m1.failed"]
        payload = json.loads(events[-1]["payload_json"])
        self.assertEqual("output_schema_invalid", payload["diagnostic_code"])
        self.assertIn("输出格式配置错误", payload["reason"])
        self.assertNotIn("C:\\Users", payload["reason"])

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

    def test_scheduler_starts_due_cycle_once(self):
        completed: list[str] = []

        def execute(cycle: dict) -> dict:
            completed.append(cycle["cycle_id"])
            self.engine.research_started(cycle["cycle_id"])
            return self.engine.research_ready(cycle["cycle_id"], "m0")

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
            return self.engine.research_ready(cycle["cycle_id"], "m0")

        at = datetime(2026, 8, 25, 0, 30, tzinfo=timezone.utc)
        results = run_daily_schedule(self.engine, self.store, at, execute)

        premarket = next(item for item in results if item["task_key"] == "daily.opportunity.0900")
        self.assertEqual("started", premarket["action"])
        self.assertEqual("2026-08-25T09:00:00+08:00", completed[0])

    def test_scheduler_records_late_cycle_as_missed_without_running_research(self):
        at = datetime(2026, 8, 25, 3, 0, tzinfo=timezone.utc)
        results = run_daily_schedule(self.engine, self.store, at, lambda cycle: self.fail("late task must not run research"))
        missed = [item for item in results if item["task_key"] == "daily.execution.1030"]
        self.assertEqual("missed", missed[0]["action"])
        cycle = self.store.find_cycle("daily.execution.1030", "2026-08-25T10:30:00+08:00")
        self.assertEqual("missed", cycle["state"])


if __name__ == "__main__":
    unittest.main()
