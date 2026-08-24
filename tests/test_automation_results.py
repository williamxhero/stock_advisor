import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from scripts.automation_results import CONTRACT, ResultStore


class ResultStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.database = root / "runtime" / "stock_advisor.sqlite3"
        self.runtime = root / "runtime"
        self.inbox = root / "app" / "inbox"
        self.store = ResultStore(self.database, self.runtime, self.inbox)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def prepare(self):
        return self.store.prepare(
            task_key="daily.execution.1430",
            task_name="A股 14:30操作决策",
            task_type="操作决策",
            scheduled_for="2026-08-24T14:30:00+08:00",
            registry_id="ScheduleRegistry-local-v1.3",
            protocol_id="DailyExecution-v1.5",
        )

    def complete(self, preparation, status="succeeded"):
        preparation.body_path.write_text("# 操作决策\n\n保持仓位。\n", encoding="utf-8")
        preparation.summary_path.write_text("保持仓位，等待进一步确认。\n", encoding="utf-8")
        preparation.payload_path.write_text('{"actions": []}\n', encoding="utf-8")
        return self.store.complete(
            preparation.run_id,
            status,
            completed_at="2026-08-24T14:36:12+08:00",
        )

    def test_complete_persists_response_and_delivers_contract(self):
        preparation = self.prepare()

        result = self.complete(preparation)

        self.assertFalse(result["idempotent"])
        message_path = self.inbox / "pending" / f"{preparation.run_id}.json"
        envelope = json.loads(message_path.read_text(encoding="utf-8"))
        self.assertEqual(CONTRACT, envelope["contract"])
        self.assertEqual("daily.execution.1430", envelope["task_key"])
        self.assertEqual("succeeded", envelope["status"])
        self.assertEqual(
            hashlib.sha256(envelope["body_markdown"].encode("utf-8")).hexdigest(),
            envelope["response_sha256"],
        )
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM automation_response").fetchone()[0])
            self.assertEqual("delivered", connection.execute("SELECT state FROM delivery_outbox").fetchone()[0])

    def test_complete_is_idempotent_for_same_content(self):
        preparation = self.prepare()
        self.complete(preparation)
        body = "# 操作决策\n\n保持仓位。\n"
        summary = "保持仓位，等待进一步确认。"
        body_path = self.runtime / "body-retry.md"
        summary_path = self.runtime / "summary-retry.txt"
        payload_path = self.runtime / "payload-retry.json"
        body_path.write_text(body, encoding="utf-8")
        summary_path.write_text(summary, encoding="utf-8")
        payload_path.write_text('{"actions": []}', encoding="utf-8")

        result = self.store.complete(
            preparation.run_id,
            "succeeded",
            body_path=body_path,
            summary_path=summary_path,
            payload_path=payload_path,
            completed_at="2026-08-24T14:36:12+08:00",
        )

        self.assertTrue(result["idempotent"])
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM delivery_outbox").fetchone()[0])

    def test_failed_delivery_stays_in_outbox_and_can_be_retried(self):
        preparation = self.prepare()
        blocking_file = self.inbox.parent / "inbox"
        blocking_file.parent.mkdir(parents=True, exist_ok=True)
        blocking_file.write_text("blocked", encoding="utf-8")

        result = self.complete(preparation)

        self.assertEqual(1, result["dispatch"]["failed"])
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE delivery_outbox SET next_attempt_at = '2000-01-01T00:00:00Z'"
            )
            connection.commit()
        blocking_file.unlink()
        retry = self.store.dispatch()
        self.assertEqual(1, retry["delivered"])
        self.assertTrue((self.inbox / "pending" / f"{preparation.run_id}.json").exists())

    def test_rejects_non_object_payload_without_completing_run(self):
        preparation = self.prepare()
        preparation.body_path.write_text("body", encoding="utf-8")
        preparation.summary_path.write_text("summary", encoding="utf-8")
        preparation.payload_path.write_text("[]", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "payload must be a JSON object"):
            self.store.complete(preparation.run_id, "succeeded")

        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual("running", connection.execute("SELECT status FROM automation_run").fetchone()[0])
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM delivery_outbox").fetchone()[0])

    def test_invalid_heartbeat_has_no_store_operation(self):
        self.store.initialize()
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM automation_run").fetchone()[0])

    def test_shared_contract_fixture_matches_source_contract(self):
        fixture = (
            Path(__file__).resolve().parents[1]
            / "windows-ai-decision-center"
            / "tests"
            / "AIDecisionCenter.Tests"
            / "Fixtures"
            / "ai-decision-message-v1.json"
        )
        envelope = json.loads(fixture.read_text(encoding="utf-8"))

        self.assertEqual(CONTRACT, envelope["contract"])
        self.assertEqual(
            hashlib.sha256(envelope["body_markdown"].encode("utf-8")).hexdigest(),
            envelope["response_sha256"],
        )
        self.assertIsInstance(envelope["payload"], dict)


if __name__ == "__main__":
    unittest.main()
