from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ai_trading_companion.__main__ import _conversation_retry_intellect, run_unified_cognition
from ai_trading_companion.broker_client import BrokerError, BrokerResponse
from ai_trading_companion.cognition import ReplyMarkdownStream, UnifiedCognition
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.evidence_contract import EvidenceContractFactory
from ai_trading_companion.packet_builder import RuntimePacketBuilder as _RuntimePacketBuilder
from ai_trading_companion.memory_port import InMemoryMemoryAdapter
from ai_trading_companion.portfolio import PortfolioService
from ai_trading_companion.store import CompanionStore
from ai_trading_companion.task_profiles import ManualAnalysisProfileResolver


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def RuntimePacketBuilder(*args, **kwargs):
    kwargs.setdefault("memory", InMemoryMemoryAdapter())
    return _RuntimePacketBuilder(*args, **kwargs)


class _WeekdayCalendar:
    def is_trading_day(self, value: date) -> bool:
        return value.weekday() < 5


class UnifiedCognitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = CompanionStore(self.root / "runtime.sqlite3")
        calendar = _WeekdayCalendar()
        self.memory = InMemoryMemoryAdapter()
        self.engine = CompanionEngine(
            self.store, task_profiles=ManualAnalysisProfileResolver(calendar),
            evidence_contract_factory=EvidenceContractFactory(calendar), memory=self.memory,
        )
        self.portfolio = PortfolioService(self.root, self.store)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_daily_rollover_moves_only_unsent_messages_and_preserves_identity(self) -> None:
        first = self.store.ensure_daily_conversation("2026-08-26")
        staged = self.store.stage_message(first["cycle_id"], "输入后还没提交", "conversation", message_id="staged")
        self.store.stage_message(first["cycle_id"], "已经提交", "conversation", message_id="submitted")
        self.store.commit_staged_messages(first["cycle_id"], "conversation")
        later = self.store.stage_message(first["cycle_id"], "跨日消息", "conversation", message_id="later")

        second = self.store.ensure_daily_conversation("2026-08-27")

        self.assertEqual(first["cycle_id"], self.store.get_message(staged["message_id"])["cycle_id"])
        self.assertEqual(first["cycle_id"], self.store.get_message("submitted")["cycle_id"])
        moved = self.store.get_message(later["message_id"])
        self.assertEqual(second["cycle_id"], moved["cycle_id"])
        self.assertEqual("staged", moved["state"])
        self.assertEqual("closed", self.store.get_cycle(first["cycle_id"])["state"])

    def test_cognition_job_claim_allows_one_worker_and_a_failed_retry(self) -> None:
        conversation = self.store.ensure_daily_conversation("2026-08-27")
        artifact = self.store.append_artifact(
            conversation["cycle_id"], "chat_human", "human", "Analyze AI.", conversation["as_of"], {}
        )
        job = self.store.start_cognition_job(conversation["cycle_id"], artifact["artifact_id"], "conversation", "Analyze AI.")

        first = self.store.claim_cognition_job(job["job_id"])
        second = self.store.claim_cognition_job(job["job_id"])
        self.store.finish_cognition_job(job["job_id"], error="temporary failure")
        retry = self.store.claim_cognition_job(job["job_id"])

        self.assertTrue(first["claimed"])
        self.assertFalse(second["claimed"])
        self.assertEqual("running", second["state"])
        self.assertTrue(retry["claimed"])
        self.assertEqual(2, retry["attempt_count"])

    def test_retried_standard_conversation_escalates_to_smart(self) -> None:
        self.assertEqual("standard", _conversation_retry_intellect("standard", 1))
        self.assertEqual("smart", _conversation_retry_intellect("standard", 2))
        self.assertEqual("expert", _conversation_retry_intellect("expert", 2))

    def test_cognition_schema_declares_types_for_enum_and_const_properties(self) -> None:
        schema = json.loads((PROJECT_ROOT / "resources" / "contracts" / "companion-cognition-result-v1.schema.json").read_text(encoding="utf-8"))
        missing: list[str] = []

        def visit(node: object, path: str = "$") -> None:
            if isinstance(node, dict):
                if ("enum" in node or "const" in node) and "type" not in node:
                    missing.append(path)
                for key, value in node.items():
                    visit(value, f"{path}.{key}")
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    visit(value, f"{path}[{index}]")

        visit(schema)
        self.assertEqual([], missing)

    def test_h0_analysis_request_creates_a_separate_cycle_without_entering_current_m1_packet(self) -> None:
        cycle = self.engine.start_cycle("daily.execution.0945", "2026-08-27T09:45:00+08:00", "2026-08-27T01:45:00Z")
        text = "Analyze the AI sector for the current session."
        self.store.stage_message(cycle["cycle_id"], text, "h0", message_id="h0-analysis")
        batch_id, messages = self.store.commit_staged_messages(cycle["cycle_id"], "h0")
        artifact = self.store.append_artifact(
            cycle["cycle_id"], "h0", "human", text, cycle["as_of"], {"batch_id": batch_id}
        )

        outcome = UnifiedCognition(self.store, self.portfolio, self.engine).apply(
            cycle, artifact, messages, "h0",
            {"reply_markdown": None, "needs_fresh_search": False, "public_search_request": None,
             "propositions": [], "actions": [self._analysis_action("h0-analysis", text)]},
        )
        packet = RuntimePacketBuilder(PROJECT_ROOT / "resources", self.root, self.store).build(
            self.store.get_cycle(cycle["cycle_id"]), "m1_judgment", evidence={}, as_of="2026-08-27T02:00:00Z"
        )

        self.assertEqual("created", outcome.receipts[0]["state"])
        self.assertNotEqual(cycle["cycle_id"], outcome.receipts[0]["cycle_id"])
        rendered = json.dumps(packet, ensure_ascii=False)
        self.assertNotIn(text, rendered)
        self.assertNotIn(outcome.receipts[0]["cycle_id"], rendered)

    def test_failed_stream_keeps_only_prefix_without_unreceipted_success_claim(self) -> None:
        conversation = self.store.ensure_daily_conversation("2026-08-27")
        text = "Analyze the AI sector."
        self.store.stage_message(conversation["cycle_id"], text, "conversation", message_id="stream")
        batch_id, messages = self.store.commit_staged_messages(conversation["cycle_id"], "conversation")
        created = self.store.append_artifact(
            conversation["cycle_id"], "chat_human", "human", text, conversation["as_of"], {"batch_id": batch_id}
        )
        source = next(item for item in self.store.artifacts(conversation["cycle_id"])
                      if item["artifact_id"] == created["artifact_id"])
        broker = Mock()

        def interrupted(request):
            request.on_delta('{"reply_markdown":"Checking market first. Task created"}')
            raise BrokerError("connection lost", category="broker_unavailable")

        broker.invoke.side_effect = interrupted
        with patch("ai_trading_companion.__main__.ProviderBrokerClient", return_value=broker), self.assertRaises(BrokerError):
            run_unified_cognition(self.engine, self.store, self.portfolio, conversation["cycle_id"], source, messages, [batch_id], True, mode="conversation")

        stream = self.store.stream_messages(conversation["cycle_id"])[0]
        self.assertEqual("failed", stream["state"])
        self.assertEqual("Checking market first.", stream["text"])
        self.assertNotIn("Task created", stream["text"])

    @unittest.skip("superseded by MemoryHub adaptive retrieval contract")
    def test_relevant_memory_keeps_matching_and_necessary_facts_but_excludes_unrelated(self) -> None:
        conversation = self.store.ensure_daily_conversation("2026-08-27")
        records = [
            ("ai", "AI semiconductor exposure", "market.ai", "topic", '"semiconductor"'),
            ("oil", "Oil price observation", "market.oil", "topic", '"crude"'),
            ("assets", "Total assets", "user.account", "total_assets", "240000"),
        ]
        for message_id, text, subject, predicate, object_json in records:
            message = self.store.stage_message(conversation["cycle_id"], text, "conversation", message_id=message_id)
            self.store.record_proposition(
                f"proposition-{message_id}",
                {"kind": "user_fact", "subject": subject, "predicate": predicate,
                 "object": json.loads(object_json), "confidence": 1.0, "supersedes_id": None,
                 "source_span": {"message_id": message_id, "start": 0, "end": len(text), "quote": text}},
                message,
            )

        rows = self.store.relevant_propositions(datetime.now(timezone.utc).isoformat(), "AI semiconductor outlook")

        self.assertEqual({"market.ai", "user.account"}, {item["subject"] for item in rows})

    def test_structured_cognition_stream_exposes_only_reply_text(self) -> None:
        parser = ReplyMarkdownStream()

        chunks = [
            parser.feed('{"reply_mark'),
            parser.feed('down":"你好\\n'),
            parser.feed('世界\\"好","needs_fresh_search":false}'),
        ]

        self.assertEqual("你好\n世界\"好", "".join(chunks))

    def test_unified_cognition_uses_visible_provider_broker_without_native_search(self) -> None:
        conversation = self.store.ensure_daily_conversation("2026-08-27")
        text = "请记住我偏好长线"
        self.store.stage_message(conversation["cycle_id"], text, "conversation", message_id="m")
        batch_id, messages = self.store.commit_staged_messages(conversation["cycle_id"], "conversation")
        created = self.store.append_artifact(
            conversation["cycle_id"], "chat_human", "human", text, conversation["as_of"], {"batch_id": batch_id}
        )
        source = next(item for item in self.store.artifacts(conversation["cycle_id"])
                      if item["artifact_id"] == created["artifact_id"])
        result = {
            "reply_markdown": "记住了",
            "needs_fresh_search": False,
            "public_search_request": None,
            "propositions": [],
            "actions": [],
        }
        broker = Mock()
        broker.invoke.return_value = BrokerResponse(
            output_text=json.dumps(result, ensure_ascii=False), result=result, actual_model="test",
            provider="fake", intellect="standard", fulfilled_intellect="standard", request_id="request",
        )

        with patch("ai_trading_companion.__main__.ProviderBrokerClient", return_value=broker):
            output = run_unified_cognition(
                self.engine, self.store, self.portfolio, conversation["cycle_id"], source,
                messages, [batch_id], True, mode="conversation",
            )

        request = broker.invoke.call_args.args[0]
        self.assertTrue(request.visible_stream)
        self.assertEqual("standard", request.intellect)
        self.assertEqual(6_000, request.output_token_limit)
        self.assertNotIn("tools", json.dumps(request.packet, ensure_ascii=False))
        self.assertEqual("记住了", output["reply_markdown"])
        with self.store.connection() as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM companion_cycle WHERE trigger='manual_chat'").fetchone()[0])

    def test_auto_submit_claim_is_once_per_formal_task(self) -> None:
        conversation = self.store.ensure_daily_conversation("2026-08-27")
        self.engine._stage_message(conversation, "请在下个任务前带上", "message")

        first = self.engine.auto_submit_conversation(
            conversation["cycle_id"], "daily.opportunity.0900", "2026-08-27T09:00:00+08:00"
        )
        second = self.engine.auto_submit_conversation(
            conversation["cycle_id"], "daily.opportunity.0900", "2026-08-27T09:00:00+08:00"
        )

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual("submitted", self.store.get_message("message")["state"])

    def test_empty_auto_submit_window_does_not_consume_later_message(self) -> None:
        conversation = self.store.ensure_daily_conversation("2026-08-27")
        self.assertIsNone(self.engine.auto_submit_conversation(
            conversation["cycle_id"], "daily.opportunity.0900", "2026-08-27T09:00:00+08:00"
        ))
        self.engine._stage_message(conversation, "稍后才发送", "later")

        submitted = self.engine.auto_submit_conversation(
            conversation["cycle_id"], "daily.opportunity.0900", "2026-08-27T09:00:00+08:00"
        )

        self.assertIsNotNone(submitted)
        self.assertEqual("submitted", self.store.get_message("later")["state"])

    def test_daily_conversation_open_event_is_emitted_once(self) -> None:
        at = datetime.fromisoformat("2026-08-27T08:00:00+08:00")
        self.engine.ensure_daily_conversation(at)
        self.engine.ensure_daily_conversation(at)

        self.assertEqual(
            1,
            sum(event["event_type"] == "conversation.opened" for event in self.store.pending_events()),
        )

    def test_m1_reads_frozen_pre_h0_portfolio_not_later_live_update(self) -> None:
        cycle = self.store.create_cycle(
            "daily.execution.0945", "2026-08-27T09:45:00+08:00", "2026-08-27T01:30:00Z"
        )
        with self.store.connection() as connection:
            connection.execute(
                "INSERT INTO portfolio_position(code,name,shares,updated_at) VALUES('603179','新泉股份',100,'2026-08-27T01:20:00Z')"
            )
        self.store.freeze_private_context(cycle["cycle_id"])
        with self.store.connection() as connection:
            connection.execute("UPDATE portfolio_position SET shares=300 WHERE code='603179'")
        cycle = self.store.get_cycle(cycle["cycle_id"])

        packet = RuntimePacketBuilder(PROJECT_ROOT / "resources", self.root, self.store).build(
            cycle, "m1_judgment", evidence={}, as_of="2026-08-27T01:40:00Z"
        )
        private = packet["business_context"]["private_context_before_h0"]

        self.assertEqual(100, private["positions"][0]["shares"])
        self.assertNotIn('"shares": 300', json.dumps(packet, ensure_ascii=False))

    def test_invalid_source_span_rejects_only_affected_action(self) -> None:
        conversation = self.store.ensure_daily_conversation("2026-08-27")
        message = self.store.stage_message(conversation["cycle_id"], "现在总资产是24万元", "conversation", message_id="m")
        batch_id, messages = self.store.commit_staged_messages(conversation["cycle_id"], "conversation")
        artifact = self.store.append_artifact(
            conversation["cycle_id"], "chat_human", "human", message["body_text"], conversation["as_of"], {"batch_id": batch_id}
        )
        action = self._asset_action("m", message["body_text"])
        action["source_span"]["quote"] = "不在原文"

        outcome = UnifiedCognition(self.store, self.portfolio, self.engine).apply(
            conversation, artifact, messages, "conversation",
            {"reply_markdown": "收到", "needs_fresh_search": False, "public_search_request": None, "propositions": [], "actions": [action]},
        )

        self.assertEqual("rejected", outcome.receipts[0]["state"])
        self.assertIsNone(self.portfolio.snapshot()["total_assets"])

    def test_one_cognition_result_records_memory_and_applies_portfolio_with_receipt(self) -> None:
        conversation = self.store.ensure_daily_conversation("2026-08-27")
        text = "现在总资产是24万元"
        self.store.stage_message(conversation["cycle_id"], text, "conversation", message_id="m")
        batch_id, messages = self.store.commit_staged_messages(conversation["cycle_id"], "conversation")
        artifact = self.store.append_artifact(
            conversation["cycle_id"], "chat_human", "human", text, conversation["as_of"], {"batch_id": batch_id}
        )
        proposition = {
            "kind": "user_fact", "subject": "user.account", "predicate": "total_assets",
            "object_json": "240000", "confidence": 1.0, "supersedes_id": None,
            "source_span": {"message_id": "m", "start": 0, "end": len(text), "quote": text},
        }
        outcome = UnifiedCognition(self.store, self.portfolio, self.engine).apply(
            conversation, artifact, messages, "conversation",
            {"reply_markdown": "收到", "needs_fresh_search": False, "public_search_request": None,
             "propositions": [proposition], "actions": [self._asset_action("m", text)]},
        )

        self.assertEqual("applied", outcome.receipts[0]["state"])
        self.assertEqual(240000, self.portfolio.snapshot()["total_assets"])
        self.assertEqual(1, len([row for row in self.memory._episodes if row["episode_type"] == "personal_fact"]))
        with self.store.connection() as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM portfolio_interpretation_job").fetchone()[0])

    def test_explicit_analysis_intent_creates_a_formal_cycle_after_its_receipt(self) -> None:
        conversation = self.store.ensure_daily_conversation("2026-08-27")
        text = "Analyze the AI sector for the current session."
        self.store.stage_message(conversation["cycle_id"], text, "conversation", message_id="analysis")
        batch_id, messages = self.store.commit_staged_messages(conversation["cycle_id"], "conversation")
        artifact = self.store.append_artifact(
            conversation["cycle_id"], "chat_human", "human", text, conversation["as_of"], {"batch_id": batch_id}
        )

        outcome = UnifiedCognition(self.store, self.portfolio, self.engine).apply(
            conversation, artifact, messages, "conversation",
            {"reply_markdown": "I will verify it.", "needs_fresh_search": False, "public_search_request": None,
             "propositions": [], "actions": [self._analysis_action("analysis", text)]},
        )

        receipt = outcome.receipts[0]
        self.assertEqual("created", receipt["state"], receipt)
        cycle = self.store.get_cycle(receipt["cycle_id"])
        self.assertEqual("manual_chat", cycle["trigger"])
        self.assertEqual("analysis", json.loads(cycle["request_source_json"])["message_id"])
        self.assertIn("正式研判任务已创建", outcome.reply_markdown)

    def test_unique_verbatim_analysis_quote_rebinds_miscalculated_offsets(self) -> None:
        conversation = self.store.ensure_daily_conversation("2026-08-27")
        text = "Analyze the AI sector for the current session."
        self.store.stage_message(conversation["cycle_id"], text, "conversation", message_id="rebind-analysis")
        batch_id, messages = self.store.commit_staged_messages(conversation["cycle_id"], "conversation")
        artifact = self.store.append_artifact(
            conversation["cycle_id"], "chat_human", "human", text, conversation["as_of"], {"batch_id": batch_id}
        )
        action = self._analysis_action("rebind-analysis", text)
        quote = "AI sector"
        action["source_span"] = {"message_id": "rebind-analysis", "start": 0, "end": len(quote), "quote": quote}

        outcome = UnifiedCognition(self.store, self.portfolio, self.engine).apply(
            conversation, artifact, messages, "conversation",
            {"reply_markdown": "I will verify it.", "needs_fresh_search": False, "public_search_request": None,
             "propositions": [], "actions": [action]},
        )

        self.assertEqual("created", outcome.receipts[0]["state"], outcome.receipts[0])
        cycle = self.store.get_cycle(outcome.receipts[0]["cycle_id"])
        source_span = json.loads(cycle["request_source_json"])["source_span"]
        self.assertEqual(text.index(quote), source_span["start"])
        self.assertEqual(text.index(quote) + len(quote), source_span["end"])
        self.assertEqual(quote, source_span["quote"])

    def test_repeated_verbatim_quote_with_bad_offsets_remains_rejected(self) -> None:
        conversation = self.store.ensure_daily_conversation("2026-08-27")
        text = "Analyze AI, then analyze AI sector."
        self.store.stage_message(conversation["cycle_id"], text, "conversation", message_id="ambiguous-quote")
        batch_id, messages = self.store.commit_staged_messages(conversation["cycle_id"], "conversation")
        artifact = self.store.append_artifact(
            conversation["cycle_id"], "chat_human", "human", text, conversation["as_of"], {"batch_id": batch_id}
        )
        action = self._analysis_action("ambiguous-quote", text)
        action["source_span"] = {"message_id": "ambiguous-quote", "start": 1, "end": 3, "quote": "AI"}

        outcome = UnifiedCognition(self.store, self.portfolio, self.engine).apply(
            conversation, artifact, messages, "conversation",
            {"reply_markdown": "I will verify it.", "needs_fresh_search": False, "public_search_request": None,
             "propositions": [], "actions": [action]},
        )

        self.assertEqual("rejected", outcome.receipts[0]["state"])
        with self.store.connection() as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM companion_cycle WHERE trigger='manual_chat'").fetchone()[0])

    def test_brokered_natural_language_analysis_request_uses_the_formal_orchestrator(self) -> None:
        conversation = self.store.ensure_daily_conversation("2026-08-27")
        text = "Analyze the AI sector for the current session."
        self.store.stage_message(conversation["cycle_id"], text, "conversation", message_id="broker-analysis")
        batch_id, messages = self.store.commit_staged_messages(conversation["cycle_id"], "conversation")
        created = self.store.append_artifact(
            conversation["cycle_id"], "chat_human", "human", text, conversation["as_of"], {"batch_id": batch_id}
        )
        source = next(item for item in self.store.artifacts(conversation["cycle_id"])
                      if item["artifact_id"] == created["artifact_id"])
        model_result = {
            "reply_markdown": "I will verify it.", "needs_fresh_search": False, "public_search_request": None,
            "propositions": [], "actions": [self._analysis_action("broker-analysis", text)],
        }
        broker = Mock()
        broker.invoke.return_value = BrokerResponse(
            output_text=json.dumps(model_result), result=model_result, actual_model="test", provider="fake",
            intellect="standard", fulfilled_intellect="standard", request_id="analysis-request",
        )

        with patch("ai_trading_companion.__main__.ProviderBrokerClient", return_value=broker):
            output = run_unified_cognition(
                self.engine, self.store, self.portfolio, conversation["cycle_id"], source,
                messages, [batch_id], True, mode="conversation",
            )

        self.assertEqual("created", output["receipts"][0]["state"])
        self.assertIn("正式研判任务已创建", output["reply_markdown"])
        with self.store.connection() as connection:
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM companion_cycle WHERE trigger='manual_chat'").fetchone()[0])

    def test_ambiguous_analysis_intent_needs_clarification_without_creating_a_cycle(self) -> None:
        conversation = self.store.ensure_daily_conversation("2026-08-27")
        text = "Analyze this."
        self.store.stage_message(conversation["cycle_id"], text, "conversation", message_id="ambiguous")
        batch_id, messages = self.store.commit_staged_messages(conversation["cycle_id"], "conversation")
        artifact = self.store.append_artifact(
            conversation["cycle_id"], "chat_human", "human", text, conversation["as_of"], {"batch_id": batch_id}
        )
        action = self._analysis_action("ambiguous", text)
        action["subject"] = ""

        outcome = UnifiedCognition(self.store, self.portfolio, self.engine).apply(
            conversation, artifact, messages, "conversation",
            {"reply_markdown": "Please clarify.", "needs_fresh_search": False, "public_search_request": None,
             "propositions": [], "actions": [action]},
        )

        self.assertEqual("needs_clarification", outcome.receipts[0]["state"])
        with self.store.connection() as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM companion_cycle WHERE trigger='manual_chat'").fetchone()[0])

    def test_invalid_analysis_span_is_local_and_does_not_block_a_valid_portfolio_action(self) -> None:
        conversation = self.store.ensure_daily_conversation("2026-08-27")
        text = "总资产是24万元；分析AI板块。"
        self.store.stage_message(conversation["cycle_id"], text, "conversation", message_id="mixed")
        batch_id, messages = self.store.commit_staged_messages(conversation["cycle_id"], "conversation")
        artifact = self.store.append_artifact(
            conversation["cycle_id"], "chat_human", "human", text, conversation["as_of"], {"batch_id": batch_id}
        )
        analysis = self._analysis_action("mixed", text)
        analysis["source_span"]["quote"] = "not in the immutable source"

        outcome = UnifiedCognition(self.store, self.portfolio, self.engine).apply(
            conversation, artifact, messages, "conversation",
            {"reply_markdown": "Received.", "needs_fresh_search": False, "public_search_request": None,
             "propositions": [], "actions": [self._asset_action("mixed", text), analysis]},
        )

        self.assertEqual("applied", outcome.receipts[0]["state"])
        self.assertEqual("rejected", outcome.receipts[1]["state"])
        self.assertEqual(240000, self.portfolio.snapshot()["total_assets"])
        with self.store.connection() as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM companion_cycle WHERE trigger='manual_chat'").fetchone()[0])

    def test_one_batch_can_record_a_fact_apply_portfolio_and_create_analysis(self) -> None:
        conversation = self.store.ensure_daily_conversation("2026-08-27")
        text = "总资产是24万元；分析AI板块。"
        self.store.stage_message(conversation["cycle_id"], text, "conversation", message_id="complete-mixed")
        batch_id, messages = self.store.commit_staged_messages(conversation["cycle_id"], "conversation")
        artifact = self.store.append_artifact(
            conversation["cycle_id"], "chat_human", "human", text, conversation["as_of"], {"batch_id": batch_id}
        )
        proposition = {
            "kind": "user_fact", "subject": "user.account", "predicate": "total_assets",
            "object_json": "240000", "confidence": 1.0, "supersedes_id": None,
            "source_span": {"message_id": "complete-mixed", "start": 0, "end": len(text), "quote": text},
        }

        outcome = UnifiedCognition(self.store, self.portfolio, self.engine).apply(
            conversation, artifact, messages, "conversation",
            {"reply_markdown": "Received.", "needs_fresh_search": False, "public_search_request": None,
             "propositions": [proposition],
             "actions": [self._asset_action("complete-mixed", text), self._analysis_action("complete-mixed", text)]},
        )

        self.assertEqual(1, outcome.propositions_recorded)
        self.assertEqual(["applied", "created"], [item["state"] for item in outcome.receipts])

    def test_user_correction_supersedes_only_the_same_personal_fact(self) -> None:
        conversation = self.store.ensure_daily_conversation("2026-08-27")
        first_text = "我喜欢短线"
        self.store.stage_message(conversation["cycle_id"], first_text, "conversation", message_id="first")
        first_batch, first_messages = self.store.commit_staged_messages(conversation["cycle_id"], "conversation")
        first_artifact = self.store.append_artifact(
            conversation["cycle_id"], "chat_human", "human", first_text, conversation["as_of"], {"batch_id": first_batch}
        )
        first = {"kind": "user_fact", "subject": "user", "predicate": "style", "object_json": '"短线"',
                 "confidence": 1.0, "supersedes_id": None,
                 "source_span": {"message_id": "first", "start": 0, "end": len(first_text), "quote": first_text}}
        UnifiedCognition(self.store, self.portfolio, self.engine).apply(
            conversation, first_artifact, first_messages, "conversation",
            {"reply_markdown": "记住了", "needs_fresh_search": False, "public_search_request": None,
             "propositions": [first], "actions": []},
        )
        prior = self.memory._episodes[-1]["metadata"]
        second_text = "更正：我偏好长线"
        self.store.stage_message(conversation["cycle_id"], second_text, "conversation", message_id="second")
        second_batch, second_messages = self.store.commit_staged_messages(conversation["cycle_id"], "conversation")
        second_artifact = self.store.append_artifact(
            conversation["cycle_id"], "chat_human", "human", second_text, conversation["as_of"], {"batch_id": second_batch}
        )
        corrected = {"kind": "user_fact", "subject": "user", "predicate": "style", "object_json": '"长线"',
                     "confidence": 1.0, "supersedes_id": prior["proposition_id"],
                     "source_span": {"message_id": "second", "start": 0, "end": len(second_text), "quote": second_text}}

        UnifiedCognition(self.store, self.portfolio, self.engine).apply(
            conversation, second_artifact, second_messages, "conversation",
            {"reply_markdown": "已更正", "needs_fresh_search": False, "public_search_request": None,
             "propositions": [corrected], "actions": []},
        )

        episodes = [row for row in self.memory._episodes if row["episode_type"] == "personal_fact"]
        self.assertEqual(2, len(episodes))
        self.assertIn("style", episodes[-1]["body"])

    @staticmethod
    def _asset_action(message_id: str, text: str) -> dict:
        return {
            "action_type": "portfolio.apply", "statement_type": "current_state",
            "changes": [{
                "action": "asset_correction", "code": None, "name": None, "shares": None,
                "price": None, "average_cost": None, "total_assets": 240000, "occurred_at": None,
                "evidence": {"instrument": None, "action": "总资产", "shares": None, "price": None,
                             "average_cost": None, "total_assets": "24万元"},
            }],
            "workflow_proposal": None,
            "source_span": {"message_id": message_id, "start": 0, "end": len(text), "quote": text},
        }

    @staticmethod
    def _analysis_action(message_id: str, text: str) -> dict:
        return {
            "action_type": "analysis.request", "subject": "AI sector", "time_scope": "current_session",
            "goal": "verify current risks and opportunities",
            "source_span": {"message_id": message_id, "start": 0, "end": len(text), "quote": text},
        }


if __name__ == "__main__":
    unittest.main()
