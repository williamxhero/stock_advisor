from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from datetime import datetime, timedelta, timezone

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

    def test_transiently_failed_conversation_is_recoverable_after_cooldown(self):
        message = self.store.stage_message(self.cycle["cycle_id"], "外围消息？", "chat", message_id="message")
        batch_id, _ = self.store.commit_staged_messages(self.cycle["cycle_id"], "chat")
        artifact = self.store.append_artifact(
            self.cycle["cycle_id"], "chat_human", "human", message["body_text"],
            "2026-08-26T01:45:00Z",
        )
        job = self.store.start_cognition_job(
            self.cycle["cycle_id"], artifact["artifact_id"], "conversation", message["body_text"],
        )
        self.store.claim_cognition_job(job["job_id"])
        self.store.finish_cognition_job(job["job_id"], error="Broker HTTP 503")

        future = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat().replace("+00:00", "Z")
        recoverable = self.store.recoverable_conversation_jobs(before=future)

        self.assertEqual(1, len(recoverable))
        self.assertEqual(batch_id, recoverable[0]["batch_id"])
        self.assertEqual("chat_human", recoverable[0]["source_kind"])

    def test_expired_running_conversation_lease_becomes_recoverable(self):
        message = self.store.stage_message(self.cycle["cycle_id"], "盘后总结", "chat", message_id="message")
        batch_id, _ = self.store.commit_staged_messages(self.cycle["cycle_id"], "chat")
        artifact = self.store.append_artifact(
            self.cycle["cycle_id"], "chat_human", "human", message["body_text"],
            "2026-08-26T01:45:00Z",
        )
        job = self.store.start_cognition_job(
            self.cycle["cycle_id"], artifact["artifact_id"], "conversation", message["body_text"],
        )
        self.store.claim_cognition_job(job["job_id"])
        with self.store.connection() as connection:
            connection.execute(
                "UPDATE companion_cognition_job SET claimed_at=? WHERE job_id=?",
                ("2026-08-26T01:45:00Z", job["job_id"]),
            )

        recovered = self.store.recover_stale_cognition_jobs(before="2026-08-26T01:55:00Z")
        retry_before = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat().replace("+00:00", "Z")
        retryable = self.store.recoverable_conversation_jobs(before=retry_before)

        self.assertEqual([job["job_id"]], [item["job_id"] for item in recovered])
        self.assertEqual([batch_id], [item["batch_id"] for item in retryable])

    def test_non_transient_failed_conversation_is_not_automatically_retried(self):
        message = self.store.stage_message(self.cycle["cycle_id"], "外围消息？", "chat", message_id="message")
        self.store.commit_staged_messages(self.cycle["cycle_id"], "chat")
        artifact = self.store.append_artifact(
            self.cycle["cycle_id"], "chat_human", "human", message["body_text"],
            "2026-08-26T01:45:00Z",
        )
        job = self.store.start_cognition_job(
            self.cycle["cycle_id"], artifact["artifact_id"], "conversation", message["body_text"],
        )
        self.store.claim_cognition_job(job["job_id"])
        self.store.finish_cognition_job(job["job_id"], error="invalid_json_schema")
        future = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat().replace("+00:00", "Z")

        self.assertEqual([], self.store.recoverable_conversation_jobs(before=future))

    def test_transient_conversation_has_a_bounded_smart_fallback_attempt(self):
        message = self.store.stage_message(self.cycle["cycle_id"], "外围消息？", "chat", message_id="message")
        self.store.commit_staged_messages(self.cycle["cycle_id"], "chat")
        artifact = self.store.append_artifact(
            self.cycle["cycle_id"], "chat_human", "human", message["body_text"],
            "2026-08-26T01:45:00Z",
        )
        job = self.store.start_cognition_job(
            self.cycle["cycle_id"], artifact["artifact_id"], "conversation", message["body_text"],
        )
        for _ in range(3):
            claimed = self.store.claim_cognition_job(job["job_id"])
            self.assertTrue(claimed["claimed"])
            self.store.finish_cognition_job(job["job_id"], error="Broker HTTP 503")
        future = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat().replace("+00:00", "Z")

        self.assertEqual(1, len(self.store.recoverable_conversation_jobs(before=future)))
        claimed = self.store.claim_cognition_job(job["job_id"])
        self.assertEqual(4, claimed["attempt_count"])
        self.store.finish_cognition_job(job["job_id"], error="Broker HTTP 503")
        self.assertEqual(1, len(self.store.recoverable_conversation_jobs(before=future)))
        claimed = self.store.claim_cognition_job(job["job_id"])
        self.assertEqual(5, claimed["attempt_count"])
        self.store.finish_cognition_job(job["job_id"], error="Broker HTTP 503")
        self.assertEqual(1, len(self.store.recoverable_conversation_jobs(before=future)))
        claimed = self.store.claim_cognition_job(job["job_id"])
        self.assertEqual(6, claimed["attempt_count"])
        self.store.finish_cognition_job(job["job_id"], error="Broker HTTP 503")
        self.assertEqual(1, len(self.store.recoverable_conversation_jobs(before=future)))
        claimed = self.store.claim_cognition_job(job["job_id"])
        self.assertEqual(7, claimed["attempt_count"])
        self.store.finish_cognition_job(job["job_id"], error="Broker HTTP 503")
        self.assertEqual(1, len(self.store.recoverable_conversation_jobs(before=future)))
        claimed = self.store.claim_cognition_job(job["job_id"])
        self.assertEqual(8, claimed["attempt_count"])
        self.store.finish_cognition_job(job["job_id"], error="Broker HTTP 503")
        self.assertEqual(1, len(self.store.recoverable_conversation_jobs(before=future)))
        claimed = self.store.claim_cognition_job(job["job_id"])
        self.assertEqual(9, claimed["attempt_count"])
        self.store.finish_cognition_job(job["job_id"], error="Broker HTTP 503")
        self.assertEqual([], self.store.recoverable_conversation_jobs(before=future))
