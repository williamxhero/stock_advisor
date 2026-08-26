from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ai_trading_companion.cognition import ReplyMarkdownStream, UnifiedCognition
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.packet_builder import RuntimePacketBuilder
from ai_trading_companion.portfolio import PortfolioService
from ai_trading_companion.store import CompanionStore


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class UnifiedCognitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = CompanionStore(self.root / "runtime.sqlite3")
        self.engine = CompanionEngine(self.store)
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

    def test_structured_cognition_stream_exposes_only_reply_text(self) -> None:
        parser = ReplyMarkdownStream()

        chunks = [
            parser.feed('{"reply_mark'),
            parser.feed('down":"你好\\n'),
            parser.feed('世界\\"好","needs_fresh_search":false}'),
        ]

        self.assertEqual("你好\n世界\"好", "".join(chunks))

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
        private = next(item for item in packet["local_inputs"] if item["path"].startswith("runtime://"))

        self.assertEqual(100, json.loads(private["text"])["positions"][0]["shares"])
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

        outcome = UnifiedCognition(self.store, self.portfolio).apply(
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
        outcome = UnifiedCognition(self.store, self.portfolio).apply(
            conversation, artifact, messages, "conversation",
            {"reply_markdown": "收到", "needs_fresh_search": False, "public_search_request": None,
             "propositions": [proposition], "actions": [self._asset_action("m", text)]},
        )

        self.assertEqual("applied", outcome.receipts[0]["state"])
        self.assertEqual(240000, self.portfolio.snapshot()["total_assets"])
        self.assertEqual(1, len(self.store.current_propositions(datetime.now(timezone.utc).isoformat())))
        with self.store.connection() as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM portfolio_interpretation_job").fetchone()[0])

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
        UnifiedCognition(self.store, self.portfolio).apply(
            conversation, first_artifact, first_messages, "conversation",
            {"reply_markdown": "记住了", "needs_fresh_search": False, "public_search_request": None,
             "propositions": [first], "actions": []},
        )
        prior = self.store.current_propositions(datetime.now(timezone.utc).isoformat())[0]
        second_text = "更正：我偏好长线"
        self.store.stage_message(conversation["cycle_id"], second_text, "conversation", message_id="second")
        second_batch, second_messages = self.store.commit_staged_messages(conversation["cycle_id"], "conversation")
        second_artifact = self.store.append_artifact(
            conversation["cycle_id"], "chat_human", "human", second_text, conversation["as_of"], {"batch_id": second_batch}
        )
        corrected = {"kind": "user_fact", "subject": "user", "predicate": "style", "object_json": '"长线"',
                     "confidence": 1.0, "supersedes_id": prior["proposition_id"],
                     "source_span": {"message_id": "second", "start": 0, "end": len(second_text), "quote": second_text}}

        UnifiedCognition(self.store, self.portfolio).apply(
            conversation, second_artifact, second_messages, "conversation",
            {"reply_markdown": "已更正", "needs_fresh_search": False, "public_search_request": None,
             "propositions": [corrected], "actions": []},
        )

        current = self.store.current_propositions(datetime.now(timezone.utc).isoformat())
        self.assertEqual(1, len(current))
        self.assertEqual("长线", json.loads(current[0]["object_json"]))

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


if __name__ == "__main__":
    unittest.main()
