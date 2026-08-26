from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from ai_trading_companion.store import CompanionStore


class MessageBatchTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.store = CompanionStore(Path(self.temporary.name) / "companion.sqlite3")
        self.store.initialize()
        self.cycle = self.store.create_cycle("daily.execution.0945", "2026-08-26T09:45:00+08:00", "2026-08-26T09:45:00+08:00")

    def tearDown(self):
        self.temporary.cleanup()

    def test_pending_is_editable_and_only_completed_reply_closes_batch(self):
        message = self.store.stage_message(self.cycle["cycle_id"], "初稿", "chat", message_id="message")
        edited = self.store.update_staged_message(self.cycle["cycle_id"], message["message_id"], "修改后")
        self.assertEqual("修改后", edited["body_text"])
        batch_id, _ = self.store.commit_staged_messages(self.cycle["cycle_id"], "chat")
        self.assertEqual([batch_id], [item["batch_id"] for item in self.store.pending_message_batches(self.cycle["cycle_id"])])
        stream = self.store.start_stream_message(self.cycle["cycle_id"], [batch_id], "ai_chat")
        self.store.append_stream_chunk(stream["stream_id"], "部分回复")
        self.store.finish_stream_message(stream["stream_id"], error="network")
        self.assertEqual(1, len(self.store.pending_message_batches(self.cycle["cycle_id"])))
        self.store.mark_batches_responded([batch_id], "artifact")
        self.assertEqual([], self.store.pending_message_batches(self.cycle["cycle_id"]))
