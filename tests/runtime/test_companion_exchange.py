from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from ai_trading_companion.__main__ import _gateway_command, consume
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.exchange import MAX_CAUSAL_RETRIES, LocalExchange
from ai_trading_companion.memory_port import InMemoryMemoryAdapter
from ai_trading_companion.portfolio import PortfolioService
from ai_trading_companion.store import CompanionStore


class CompanionExchangeTests(unittest.TestCase):
    def _runtime(self, root: Path):
        store = CompanionStore(root / "runtime.sqlite3")
        engine = CompanionEngine(store, memory=InMemoryMemoryAdapter(), memory_space_id="test-space")
        portfolio = PortfolioService(root, store)
        exchange = LocalExchange(root / "exchange")
        return store, engine, portfolio, exchange

    def test_causal_sequence_wins_over_inverse_random_filenames(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, engine, portfolio, exchange = self._runtime(root)
            cycle = store.ensure_daily_conversation("2026-09-09")
            stage = {
                "contract": "companion-user-command/v1", "command_id": "stage-causal",
                "cycle_id": cycle["cycle_id"], "type": "stage_message", "message_id": "causal-message",
                "text": "先提交一条消息", "causal_stream": cycle["cycle_id"], "causal_sequence": 1,
            }
            commit = {
                "contract": "companion-user-command/v1", "command_id": "commit-causal",
                "cycle_id": cycle["cycle_id"], "type": "commit_conversation_batch",
                "causal_stream": cycle["cycle_id"], "causal_sequence": 2,
            }
            # The later command deliberately has the lexicographically earlier file name.
            exchange.send("to-runtime", "z-stage", stage)
            exchange.send("to-runtime", "a-commit", commit)

            with patch("ai_trading_companion.__main__.run_chat", return_value={"state": "queued"}):
                results = consume(engine, store, exchange, portfolio)

            self.assertEqual(2, len(results))
            self.assertEqual("submitted", store.messages(cycle["cycle_id"])[0]["state"])
            self.assertEqual(1, len(store.pending_message_batches(cycle["cycle_id"], "conversation")))
            self.assertTrue((exchange.root / "to-runtime" / "processed" / "z-stage.json").exists())
            self.assertTrue((exchange.root / "to-runtime" / "processed" / "a-commit.json").exists())

    def test_commit_is_bounded_deferred_until_stage_becomes_visible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, engine, portfolio, exchange = self._runtime(root)
            cycle = store.ensure_daily_conversation("2026-09-09")
            commit = {
                "contract": "companion-user-command/v1", "command_id": "commit-late-stage",
                "cycle_id": cycle["cycle_id"], "type": "commit_conversation_batch",
                "causal_stream": cycle["cycle_id"], "causal_sequence": 2,
            }
            exchange.send("to-runtime", "commit-late-stage", commit)

            first = consume(engine, store, exchange, portfolio)
            self.assertTrue(first[0]["deferred"])
            self.assertTrue((exchange.root / "to-runtime" / "pending" / "commit-late-stage.json").exists())

            stage = {
                "contract": "companion-user-command/v1", "command_id": "stage-late-stage",
                "cycle_id": cycle["cycle_id"], "type": "stage_message", "message_id": "late-stage-message",
                "text": "stage 后可见", "causal_stream": cycle["cycle_id"], "causal_sequence": 1,
            }
            exchange.send("to-runtime", "stage-late-stage", stage)
            with patch("ai_trading_companion.__main__.run_chat", return_value={"state": "queued"}):
                consume(engine, store, exchange, portfolio)

            self.assertEqual("submitted", store.messages(cycle["cycle_id"])[0]["state"])
            self.assertFalse((exchange.root / "to-runtime" / "pending" / "commit-late-stage.json").exists())

    def test_runtime_projects_command_receipts_for_submitted_and_deferred_states(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, engine, portfolio, exchange = self._runtime(root)
            cycle = store.ensure_daily_conversation("2026-09-09")
            exchange.send("to-runtime", "commit-first", {
                "contract": "companion-user-command/v1", "command_id": "commit-first",
                "cycle_id": cycle["cycle_id"], "type": "commit_conversation_batch",
                "causal_stream": cycle["cycle_id"], "causal_sequence": 2,
            })

            deferred = consume(engine, store, exchange, portfolio)

            exchange.send("to-runtime", "stage-first", {
                "contract": "companion-user-command/v1", "command_id": "stage-first",
                "cycle_id": cycle["cycle_id"], "type": "stage_message", "message_id": "message-1",
                "text": "先写入", "causal_stream": cycle["cycle_id"], "causal_sequence": 1,
            })
            with patch("ai_trading_companion.__main__.run_chat", return_value={"state": "queued"}):
                submitted = consume(engine, store, exchange, portfolio)

            receipts = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in (exchange.root / "to-client" / "pending").glob("*.json")
                if json.loads(path.read_text(encoding="utf-8")).get("type") == "command.receipt"
            ]
            states = {(item["payload"]["receipt"]["command_id"], item["payload"]["receipt"]["state"]) for item in receipts}
            self.assertTrue(deferred[0]["deferred"])
            self.assertTrue(submitted)
            self.assertIn(("commit-first", "deferred"), states)
            self.assertIn(("stage-first", "submitted"), states)

    def test_runtime_projects_recovered_receipt_before_submission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, engine, portfolio, exchange = self._runtime(root)
            cycle = store.ensure_daily_conversation("2026-09-09")
            exchange.send("to-runtime", "restart-stage", {
                "contract": "companion-user-command/v1", "command_id": "restart-stage",
                "cycle_id": cycle["cycle_id"], "type": "stage_message", "message_id": "message-1",
                "text": "重启后仍保留原文", "causal_stream": cycle["cycle_id"], "causal_sequence": 1,
            })
            claimed = exchange.receive("to-runtime")
            self.assertEqual("restart-stage", claimed[0][1]["command_id"])

            restarted = LocalExchange(root / "exchange")
            consume(engine, store, restarted, portfolio)

            receipts = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in (restarted.root / "to-client" / "pending").glob("*.json")
                if json.loads(path.read_text(encoding="utf-8")).get("type") == "command.receipt"
            ]
            states = [item["payload"]["receipt"]["state"] for item in receipts]
            self.assertEqual({"recovered", "submitted"}, set(states))
            self.assertEqual("重启后仍保留原文", store.get_message("message-1")["body_text"])

    def test_missing_causal_predecessor_becomes_auditable_after_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, engine, portfolio, exchange = self._runtime(root)
            cycle = store.ensure_daily_conversation("2026-09-09")
            commit = {
                "contract": "companion-user-command/v1", "command_id": "commit-missing-stage",
                "cycle_id": cycle["cycle_id"], "type": "commit_conversation_batch",
                "causal_stream": cycle["cycle_id"], "causal_sequence": 2,
            }
            exchange.send("to-runtime", "commit-missing-stage", commit)

            results = []
            for _ in range(MAX_CAUSAL_RETRIES):
                results = consume(engine, store, exchange, portfolio)

            self.assertIn("causal predecessor", results[0]["error"])
            dead = exchange.root / "to-runtime" / "dead-letter" / "commit-missing-stage.json"
            self.assertTrue(dead.exists())
            audit = json.loads(dead.read_text(encoding="utf-8"))
            self.assertEqual(MAX_CAUSAL_RETRIES, audit["recovery"]["attempts"])
            self.assertTrue(audit["recovery"]["bounded"])
            self.assertEqual([], store.messages(cycle["cycle_id"]))
            receipts = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in (exchange.root / "to-client" / "pending").glob("*.json")
                if json.loads(path.read_text(encoding="utf-8")).get("type") == "command.receipt"
            ]
            self.assertIn("permanent_failure", {
                item["payload"]["receipt"]["state"] for item in receipts
            })

    def test_processing_commands_are_recovered_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exchange = LocalExchange(root / "exchange")
            exchange.send("to-runtime", "z-stage", {
                "command_id": "restart-stage", "type": "stage_message",
                "causal_stream": "restart", "causal_sequence": 1,
            })
            claimed = exchange.receive("to-runtime")
            self.assertEqual("restart-stage", claimed[0][1]["command_id"])

            restarted = LocalExchange(root / "exchange")
            replayed = restarted.receive("to-runtime")
            self.assertEqual("restart-stage", replayed[0][1]["command_id"])
            restarted.acknowledge("to-runtime", replayed[0][0])

    def test_duplicate_command_id_remains_idempotent_across_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, engine, portfolio, exchange = self._runtime(root)
            cycle = store.ensure_daily_conversation("2026-09-09")
            command = {
                "contract": "companion-user-command/v1", "command_id": "duplicate-stage",
                "cycle_id": cycle["cycle_id"], "type": "stage_message", "message_id": "duplicate-message",
                "text": "只写入一次", "causal_stream": cycle["cycle_id"], "causal_sequence": 1,
            }
            exchange.send("to-runtime", "first-file", command)
            exchange.send("to-runtime", "second-file", command)
            consume(engine, store, exchange, portfolio)
            self.assertEqual(1, len(store.messages(cycle["cycle_id"])))

    def test_legacy_commands_keep_filename_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            exchange = LocalExchange(Path(directory) / "exchange")
            exchange.send("to-runtime", "02-legacy", {"command_id": "legacy-2", "type": "noop"})
            exchange.send("to-runtime", "01-legacy", {"command_id": "legacy-1", "type": "noop"})
            received = exchange.receive("to-runtime")
            self.assertEqual(["legacy-1", "legacy-2"], [item[1]["command_id"] for item in received])

    def test_malformed_json_is_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            exchange = LocalExchange(Path(directory) / "exchange")
            exchange.ensure()
            pending = exchange.root / "to-runtime" / "pending" / "malformed.json"
            pending.write_text("{not-json", encoding="utf-8")
            self.assertEqual([], exchange.receive("to-runtime"))
            dead = exchange.root / "to-runtime" / "dead-letter" / "malformed.json"
            self.assertTrue(dead.exists())
            self.assertIn("malformed", dead.read_text(encoding="utf-8"))

    def test_exchange_rejects_user_forged_test_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = CompanionStore(root / "runtime.sqlite3")
            engine = CompanionEngine(store)
            portfolio = PortfolioService(root, store)
            exchange = LocalExchange(root / "exchange")
            cycle = store.ensure_daily_conversation("2026-09-09")
            command = {
                "contract": "companion-user-command/v1",
                "command_id": "forged-test-provenance",
                "cycle_id": cycle["cycle_id"],
                "type": "stage_message",
                "message_id": "must-stay-normal",
                "text": "ordinary user text",
                "provenance": {
                    "contract": "companion-test-provenance/v1",
                    "source": "repair_probe",
                    "run_id": "caller-controlled",
                },
            }
            exchange.send("to-runtime", command["command_id"], command)

            result = consume(engine, store, exchange, portfolio)

            self.assertIn("provenance", result[0]["error"])
            self.assertEqual([], store.messages(cycle["cycle_id"]))
            self.assertTrue(
                (exchange.root / "to-runtime" / "dead-letter" / "forged-test-provenance.json").exists()
            )

    def test_cleanup_command_round_trips_through_versioned_exchange(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = CompanionStore(root / "runtime.sqlite3")
            engine = CompanionEngine(store)
            portfolio = PortfolioService(root, store)
            exchange = LocalExchange(root / "exchange")
            cycle = store.ensure_daily_conversation("2026-09-09")
            store.stage_message(
                cycle["cycle_id"], "保留的正常消息", "conversation",
                message_id="normal-message",
            )
            batch_id, _ = store.commit_staged_messages(cycle["cycle_id"], "conversation")
            stream = engine.chat_stream_started(cycle["cycle_id"], [batch_id], "ai_chat")
            engine.chat_stream_failed(cycle["cycle_id"], stream["stream_id"], "network")
            command = {
                "contract": "companion-user-command/v1",
                "command_id": "exchange-cleanup-1",
                "cycle_id": cycle["cycle_id"],
                "type": "clear_operational_records",
                "confirmed": True,
                "categories": ["fault_report"],
            }
            exchange.send("to-runtime", command["command_id"], command)

            results = consume(engine, store, exchange, portfolio)

            self.assertEqual("companion-operational-record-cleanup-result/v1", results[0]["contract"])
            self.assertEqual(1, results[0]["deleted"]["fault_report"])
            self.assertTrue(
                (exchange.root / "to-runtime" / "processed" / "exchange-cleanup-1.json").exists()
            )
            projected_events = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in (exchange.root / "to-client" / "pending").glob("*.json")
            ]
            cleanup_event = next(
                event for event in projected_events
                if event.get("type") == "operational_records.cleared"
            )
            self.assertEqual("exchange-cleanup-1", cleanup_event["payload"]["receipt"]["command_id"])
            self.assertEqual([], engine.command({
                "contract": "companion-user-command/v1",
                "command_id": "exchange-cleanup-projection",
                "cycle_id": cycle["cycle_id"],
                "type": "request_projection",
            })["fault_episodes"])

    def test_gateway_commit_dispatches_conversation_cognition_without_blocking_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = CompanionStore(root / "runtime.sqlite3")
            engine = CompanionEngine(
                store, memory=InMemoryMemoryAdapter(), memory_space_id="test-space",
            )
            portfolio = PortfolioService(root, store)
            exchange = LocalExchange(root / "exchange")
            cycle = store.ensure_daily_conversation("2026-09-04")
            store.stage_message(
                cycle["cycle_id"], "复盘今天14:30的执行", "conversation", message_id="message-1",
            )
            dispatcher = unittest.mock.Mock()

            with patch("ai_trading_companion.__main__.flush", return_value=0):
                result = _gateway_command(
                    engine, store, exchange, portfolio,
                    {
                        "contract": "companion-user-command/v1",
                        "command_id": "gateway-commit-1",
                        "cycle_id": cycle["cycle_id"],
                        "type": "commit_conversation_batch",
                    },
                    dispatcher,
                )

            self.assertTrue(result["committed_batch_id"])
            dispatcher.submit.assert_called_once_with()

    def test_receive_accepts_utf8_bom_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            exchange = LocalExchange(Path(directory))
            exchange.ensure()
            command = {
                "contract": "companion-user-command/v1",
                "command_id": "command-with-bom",
                "cycle_id": "cycle-1",
                "type": "begin_voice_capture",
            }
            pending = Path(directory) / "to-runtime" / "pending" / "command-with-bom.json"
            pending.write_text(json.dumps(command), encoding="utf-8-sig")

            received = exchange.receive("to-runtime")

            self.assertEqual(command, received[0][1])
            self.assertFalse(any((Path(directory) / "to-runtime" / "dead-letter").iterdir()))

    def test_reject_preserves_utf8_bom_command_and_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            exchange = LocalExchange(Path(directory))
            exchange.ensure()
            command = {
                "contract": "companion-user-command/v1",
                "command_id": "rejected-command-with-bom",
                "cycle_id": "missing-cycle",
                "type": "begin_voice_capture",
            }
            pending = Path(directory) / "to-runtime" / "pending" / "rejected-command-with-bom.json"
            pending.write_text(json.dumps(command), encoding="utf-8-sig")
            claimed, received = exchange.receive("to-runtime")[0]

            exchange.reject("to-runtime", claimed, "cycle not found")

            dead_letter = Path(directory) / "to-runtime" / "dead-letter" / pending.name
            payload = json.loads(dead_letter.read_text(encoding="utf-8"))
            self.assertEqual("cycle not found", payload["error"])
            self.assertEqual(received, payload["received"])

    def test_acknowledged_chat_failure_does_not_reject_twice_or_strand_later_claims(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = CompanionStore(root / "runtime.sqlite3")
            memory = InMemoryMemoryAdapter()
            engine = CompanionEngine(store, memory=memory, memory_space_id="test-space")
            portfolio = PortfolioService(root, store)
            exchange = LocalExchange(root / "exchange")
            cycle = store.ensure_daily_conversation("2026-09-03")
            store.stage_message(cycle["cycle_id"], "please analyse my holdings", "conversation", message_id="message-1")
            commit = {
                "contract": "companion-user-command/v1",
                "command_id": "commit-1",
                "cycle_id": cycle["cycle_id"],
                "type": "commit_conversation_batch",
            }
            projection = {
                "contract": "companion-user-command/v1",
                "command_id": "projection-2",
                "cycle_id": cycle["cycle_id"],
                "type": "request_projection",
            }
            exchange.send("to-runtime", "01-commit", commit)
            exchange.send("to-runtime", "02-projection", projection)

            with (
                patch("ai_trading_companion.__main__.run_chat", side_effect=TimeoutError("chat deadline reached")) as run_chat,
                patch("ai_trading_companion.__main__.flush", return_value=0),
            ):
                results = consume(engine, store, exchange, portfolio, True)

            processed = {path.name for path in (exchange.root / "to-runtime" / "processed").glob("*.json")}
            self.assertEqual({"01-commit.json", "02-projection.json"}, processed)
            self.assertEqual([], list((exchange.root / "to-runtime" / "processing").glob("*.json")))
            self.assertEqual(2, len(results))
            self.assertEqual("chat deadline reached", results[0]["error"])
            pending = store.pending_message_batches(cycle["cycle_id"], "conversation")
            self.assertEqual(1, len(pending))
            self.assertEqual("pending", pending[0]["state"])
            self.assertEqual(pending[0]["batch_id"], store.receipt("commit-1", commit)["committed_batch_id"])
            self.assertTrue(any(event["event_type"] == "chat_research.failed" for event in store.pending_events()))
            self.assertEqual(pending[0]["batch_id"], run_chat.call_args.args[4])

            exchange.send("to-runtime", "03-retry", commit)
            with (
                patch("ai_trading_companion.__main__.run_chat", return_value={"state": "recovered"}) as retried_chat,
                patch("ai_trading_companion.__main__.flush", return_value=0),
            ):
                retry_results = consume(engine, store, exchange, portfolio, True)

            self.assertEqual([{"state": "recovered"}], retry_results)
            self.assertEqual(pending[0]["batch_id"], retried_chat.call_args.args[4])
            self.assertTrue((exchange.root / "to-runtime" / "processed" / "03-retry.json").exists())


class RuntimeOrderingReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        # Keep the unrelated tool-manager projection out of the installed Exchange.
        publisher = patch("ai_trading_companion.__main__.ToolManagerRuntime.publish_projection")
        publisher.start()
        self.addCleanup(publisher.stop)

    def _runtime(self, root: Path):
        store = CompanionStore(root / "runtime.sqlite3")
        engine = CompanionEngine(store, memory=InMemoryMemoryAdapter(), memory_space_id="test-space")
        return store, engine, PortfolioService(root, store), LocalExchange(root / "exchange")

    def _events(self, exchange: LocalExchange, event_type: str) -> list[dict]:
        events = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in (exchange.root / "to-client" / "pending").glob("*.json")
        ]
        return [event for event in events if event.get("type") == event_type]

    def test_incident_replay_preserves_commit_and_audit_across_duplicate_restart(self) -> None:
        for inverse in (False, True):
            with self.subTest(inverse_filenames=inverse), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                store, engine, portfolio, exchange = self._runtime(root)
                cycle_id = store.ensure_daily_conversation("2026-09-09")["cycle_id"]
                stage = {
                    "contract": "companion-user-command/v1", "command_id": "replay-stage",
                    "cycle_id": cycle_id, "type": "stage_message", "message_id": "replay-message",
                    "text": "事故回放原文，提交后不可改写", "causal_stream": cycle_id, "causal_sequence": 1,
                }
                commit = {
                    "contract": "companion-user-command/v1", "command_id": "replay-commit",
                    "cycle_id": cycle_id, "type": "commit_conversation_batch",
                    "causal_stream": cycle_id, "causal_sequence": 2,
                }
                names = sorted(uuid4().hex for _ in range(2))
                stage_name, commit_name = reversed(names) if inverse else names
                commit_path = exchange.send("to-runtime", commit_name, commit)
                stage_path = exchange.send("to-runtime", stage_name, stage)
                original_files = {path.name: path.read_bytes() for path in (stage_path, commit_path)}
                with patch("ai_trading_companion.__main__.run_chat", return_value={"state": "queued"}):
                    results = consume(engine, store, exchange, portfolio)
                self.assertEqual(2, len(results))
                self.assertTrue(all("error" not in result for result in results))
                message = store.get_message("replay-message")
                self.assertEqual("submitted", message["state"])
                self.assertEqual(stage["text"], message["body_text"])
                receipt = store.receipt(commit["command_id"], commit)
                self.assertEqual(message["batch_id"], receipt["committed_batch_id"])
                batches = store.pending_message_batches(cycle_id, "conversation")
                self.assertEqual([message["batch_id"]], [batch["batch_id"] for batch in batches])
                artifacts = store.artifacts(cycle_id)
                self.assertEqual(1, len(artifacts))

                exchange.send("to-runtime", "duplicate-stage", stage)
                exchange.send("to-runtime", "duplicate-commit", commit)
                self.assertEqual(2, len(exchange.receive("to-runtime")))
                # Recreate every Runtime component with the same durable files,
                # after the duplicates have been claimed but before acknowledgement.
                store, engine, portfolio, exchange = self._runtime(root)
                with patch("ai_trading_companion.__main__.run_chat", return_value={"state": "queued"}):
                    replayed = consume(engine, store, exchange, portfolio)
                self.assertEqual(2, len(replayed))
                self.assertTrue(all("error" not in result for result in replayed))
                self.assertEqual([message], store.messages(cycle_id))
                self.assertEqual(batches, store.pending_message_batches(cycle_id, "conversation"))
                self.assertEqual(artifacts, store.artifacts(cycle_id))
                self.assertEqual(receipt, store.receipt(commit["command_id"], commit))

                # Conflicting reuse of an identity must leave its successful
                # receipt and submitted history intact, with preserved failure input.
                conflict = {**commit, "causal_sequence": 1}
                conflict_path = exchange.send("to-runtime", "conflicting-commit", conflict)
                conflict_bytes = conflict_path.read_bytes()
                rejected = consume(engine, store, exchange, portfolio)
                self.assertIn("command id conflict", rejected[0]["error"])
                dead = exchange.root / "to-runtime" / "dead-letter" / conflict_path.name
                audit = json.loads(dead.read_text(encoding="utf-8"))
                self.assertEqual(json.loads(conflict_bytes), audit["received"])
                self.assertIn("command id conflict", audit["error"])
                self.assertEqual(receipt, store.receipt(commit["command_id"], commit))
                self.assertEqual([message], store.messages(cycle_id))
                self.assertEqual(artifacts, store.artifacts(cycle_id))

                projection_command = {
                    "contract": "companion-user-command/v1", "command_id": "replay-projection",
                    "cycle_id": cycle_id, "type": "request_projection",
                }
                exchange.send("to-runtime", "projection", projection_command)
                consume(engine, store, exchange, portfolio)
                projected = self._events(exchange, "projection.ready")
                self.assertEqual(1, len(projected))
                self.assertEqual(receipt["user_messages"], projected[0]["payload"]["user_messages"])
                staged = self._events(exchange, "message.staged")
                accepted = self._events(exchange, "human.message_batch.accepted")
                self.assertEqual(1, len(staged))
                self.assertEqual(stage["text"], staged[0]["payload"]["message"]["body_text"])
                self.assertEqual(1, len(accepted))
                self.assertEqual(message["batch_id"], accepted[0]["payload"]["batch_id"])
                self.assertEqual(["replay-message"], [item["message_id"] for item in accepted[0]["payload"]["messages"]])
                receipts = [event["payload"]["receipt"] for event in self._events(exchange, "command.receipt")]
                for command in (stage, commit):
                    states = [item["state"] for item in receipts if item["command_id"] == command["command_id"]]
                    self.assertEqual(2, states.count("submitted"))
                    self.assertEqual(1, states.count("recovered"))
                self.assertEqual(1, sum(item["state"] == "permanent_failure" for item in receipts))
                processed = exchange.root / "to-runtime" / "processed"
                self.assertEqual(
                    {*original_files, "duplicate-stage.json", "duplicate-commit.json", "projection.json"},
                    {path.name for path in processed.glob("*.json")},
                )
                for name, raw in original_files.items():
                    self.assertEqual(raw, (processed / name).read_bytes())
                for state in ("pending", "processing"):
                    self.assertEqual([], list((exchange.root / "to-runtime" / state).glob("*.json")))

    def test_malformed_causal_metadata_is_quarantined_without_business_writes(self) -> None:
        malformed = [
            {"causal_stream": "stream", "causal_sequence": sequence}
            for sequence in (True, False, 0, -1, 1.5, "1", None)
        ] + [
            {"causal_stream": "stream"},
            {"causal_sequence": 1},
            {"causal_stream": "", "causal_sequence": 1},
            {"causal_stream": "  ", "causal_sequence": 1},
            {"causal_stream": 123, "causal_sequence": 1},
        ]
        for metadata in malformed:
            with self.subTest(metadata=metadata), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                store, engine, portfolio, exchange = self._runtime(root)
                cycle_id = store.ensure_daily_conversation("2026-09-09")["cycle_id"]
                command = {
                    "contract": "companion-user-command/v1", "command_id": "malformed-sequence",
                    "cycle_id": cycle_id, "type": "stage_message", "message_id": "must-not-exist",
                    "text": "非法序列不能写入", **metadata,
                }
                path = exchange.send("to-runtime", "malformed-sequence", command)
                raw = path.read_text(encoding="utf-8")
                self.assertEqual([], consume(engine, store, exchange, portfolio))
                dead = exchange.root / "to-runtime" / "dead-letter" / path.name
                audit = json.loads(dead.read_text(encoding="utf-8"))
                expected = "causal_sequence" if metadata.get("causal_stream") == "stream" else "causal_stream"
                self.assertIn(expected, audit["error"])
                self.assertEqual(raw, audit["raw"])
                self.assertEqual({"bounded": True, "malformed": True}, audit["recovery"])
                self.assertEqual([], store.messages(cycle_id))
                self.assertIsNone(store.receipt(command["command_id"], command))
                self.assertEqual([], store.artifacts(cycle_id))
                self.assertEqual([], self._events(exchange, "command.receipt"))
                for state in ("pending", "processing", "processed"):
                    self.assertEqual([], list((exchange.root / "to-runtime" / state).glob("*.json")))

    def test_retry_bound_survives_runtime_restart_and_inflight_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, engine, portfolio, exchange = self._runtime(root)
            cycle_id = store.ensure_daily_conversation("2026-09-09")["cycle_id"]
            commit = {
                "contract": "companion-user-command/v1", "command_id": "restart-missing-stage",
                "cycle_id": cycle_id, "type": "commit_conversation_batch",
                "causal_stream": cycle_id, "causal_sequence": 2,
            }
            path = exchange.send("to-runtime", "restart-missing-stage", commit)
            raw = path.read_bytes()
            for attempt in range(1, MAX_CAUSAL_RETRIES + 1):
                # An abrupt exit may leave the retry in processing instead of pending.
                self.assertEqual(1, len(exchange.receive("to-runtime")))
                store, engine, portfolio, exchange = self._runtime(root)
                result = consume(engine, store, exchange, portfolio)[0]
                self.assertEqual(attempt, result["attempts"])
                self.assertEqual(attempt < MAX_CAUSAL_RETRIES, result["deferred"])
                self.assertIsNone(store.receipt(commit["command_id"], commit))
                if result["deferred"]:
                    self.assertEqual(raw, path.read_bytes())
                    self.assertNotIn("error", result)
                else:
                    self.assertIn("causal predecessor", result["error"])
            dead = exchange.root / "to-runtime" / "dead-letter" / path.name
            audit_bytes = dead.read_bytes()
            audit = json.loads(audit_bytes)
            self.assertEqual(json.loads(raw), audit["received"])
            self.assertEqual({"attempts": MAX_CAUSAL_RETRIES, "bounded": True, "causal": True}, audit["recovery"])
            receipts = [event["payload"]["receipt"] for event in self._events(exchange, "command.receipt")]
            self.assertEqual(list(range(1, MAX_CAUSAL_RETRIES + 1)), sorted(item["attempts"] for item in receipts))
            self.assertEqual(MAX_CAUSAL_RETRIES - 1, sum(item["state"] == "deferred" for item in receipts))
            self.assertEqual(1, sum(item["state"] == "permanent_failure" for item in receipts))
            self.assertEqual([], store.messages(cycle_id))
            self.assertEqual([], store.pending_message_batches(cycle_id, "conversation"))
            self.assertEqual([], self._events(exchange, "human.message_batch.accepted"))
            for state in ("pending", "processing", "processed"):
                self.assertEqual([], list((exchange.root / "to-runtime" / state).glob("*.json")))
            store, engine, portfolio, exchange = self._runtime(root)
            self.assertEqual([], consume(engine, store, exchange, portfolio))
            self.assertEqual(audit_bytes, dead.read_bytes())
            self.assertEqual(MAX_CAUSAL_RETRIES, len(self._events(exchange, "command.receipt")))

    def test_deferred_commit_recovers_after_restart_when_stage_arrives(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, engine, portfolio, exchange = self._runtime(root)
            cycle_id = store.ensure_daily_conversation("2026-09-09")["cycle_id"]
            commit = {
                "contract": "companion-user-command/v1", "command_id": "restart-late-stage-commit",
                "cycle_id": cycle_id, "type": "commit_conversation_batch",
                "causal_stream": cycle_id, "causal_sequence": 2,
            }
            path = exchange.send("to-runtime", "commit", commit)
            raw = path.read_bytes()
            deferred = consume(engine, store, exchange, portfolio)[0]
            self.assertTrue(deferred["deferred"])
            self.assertEqual(1, deferred["attempts"])
            self.assertIsNone(store.receipt(commit["command_id"], commit))
            self.assertEqual(1, len(exchange.receive("to-runtime")))
            store, engine, portfolio, exchange = self._runtime(root)
            stage = {
                "contract": "companion-user-command/v1", "command_id": "restart-late-stage",
                "cycle_id": cycle_id, "type": "stage_message", "message_id": "restart-late-message",
                "text": "跨重启恢复保留原文", "causal_stream": cycle_id, "causal_sequence": 1,
            }
            exchange.send("to-runtime", "stage", stage)
            with patch("ai_trading_companion.__main__.run_chat", return_value={"state": "queued"}):
                results = consume(engine, store, exchange, portfolio)
            self.assertEqual(2, len(results))
            self.assertTrue(all("error" not in result for result in results))
            message = store.get_message(stage["message_id"])
            self.assertEqual("submitted", message["state"])
            self.assertEqual(stage["text"], message["body_text"])
            self.assertEqual(message["batch_id"], store.receipt(commit["command_id"], commit)["committed_batch_id"])
            self.assertEqual(raw, (exchange.root / "to-runtime" / "processed" / path.name).read_bytes())
            self.assertEqual({}, json.loads((exchange.root / "to-runtime" / "causal-retries.json").read_text(encoding="utf-8")))
            receipts = [event["payload"]["receipt"] for event in self._events(exchange, "command.receipt")]
            commit_states = [item["state"] for item in receipts if item["command_id"] == commit["command_id"]]
            self.assertCountEqual(["deferred", "recovered", "submitted"], commit_states)
            self.assertEqual(1, len(self._events(exchange, "human.message_batch.accepted")))
            for state in ("pending", "processing", "dead-letter"):
                self.assertEqual([], list((exchange.root / "to-runtime" / state).glob("*.json")))


if __name__ == "__main__":
    unittest.main()
