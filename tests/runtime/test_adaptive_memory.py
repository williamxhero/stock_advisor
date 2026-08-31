from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ai_trading_companion.__main__ import run_chat
from ai_trading_companion.adaptive_memory import AdaptiveMemoryResearch
from ai_trading_companion.broker_client import BrokerResponse
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.memory_port import InMemoryMemoryAdapter, MemoryUnavailable
from ai_trading_companion.portfolio import PortfolioService
from ai_trading_companion.store import CompanionStore


def _episode(space: str, event_id: str, body: str, known_at: str) -> dict[str, object]:
    return {
        "memory_space_id": space, "source_system": "test", "source_event_id": event_id,
        "content_hash": "auto", "episode_type": "note", "body": body,
        "occurred_at": known_at, "known_at": known_at, "submitted_at": known_at,
        "authority": "test", "protocol_version": "memoryhub/v1",
    }


class _RecordingMemory(InMemoryMemoryAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.operations: list[tuple[str, str]] = []

    def search(self, snapshot_id: str, query: str, *, limit: int = 20) -> list[dict[str, object]]:
        self.operations.append(("search", query))
        return super().search(snapshot_id, query, limit=limit)

    def expand(self, snapshot_id: str, episode_id: str) -> dict[str, object]:
        self.operations.append(("expand", episode_id))
        return super().expand(snapshot_id, episode_id)

    def related(self, snapshot_id: str, episode_id: str, *, limit: int = 20) -> list[dict[str, object]]:
        self.operations.append(("related", episode_id))
        return super().related(snapshot_id, episode_id, limit=limit)


class AdaptiveMemoryResearchTests(unittest.TestCase):
    def test_ai_can_search_expand_and_stop_on_one_frozen_snapshot_without_repeating_expand(self) -> None:
        memory = _RecordingMemory()
        memory.append(_episode("test-space", "prior", "semiconductor drawdown lesson", "2026-08-01T00:00:00Z"))
        decisions = iter([
            {"operation": "search", "query": "semiconductor", "episode_id": None},
            {"operation": "expand", "query": None, "episode_id": "test-episode-1"},
            {"operation": "expand", "query": None, "episode_id": "test-episode-1"},
            {"operation": "related", "query": None, "episode_id": "test-episode-1"},
            {"operation": "search", "query": "drawdown counterexample", "episode_id": None},
            {"operation": "complete", "query": None, "episode_id": None},
        ])

        result = AdaptiveMemoryResearch(memory, "test-space", lambda _state: next(decisions)).collect(
            "cycle", [{"message_id": "m", "body_text": "semiconductor outlook", "known_at": "2026-08-20T00:00:00Z"}],
            deadline=float("inf"),
        )

        self.assertEqual("chat", result.snapshot["stage"])
        self.assertEqual("2026-08-20T00:00:00Z", result.snapshot["as_of"])
        self.assertEqual(1, memory.operations.count(("expand", "test-episode-1")))
        self.assertEqual(("search", "semiconductor"), memory.operations[0])
        self.assertIn(("related", "test-episode-1"), memory.operations)
        self.assertIn(("search", "drawdown counterexample"), memory.operations)
        self.assertEqual("rejected_duplicate", result.actions[2]["state"])
        self.assertEqual("semiconductor drawdown lesson", result.context[-1]["body"])

    def test_memory_failure_is_not_substituted_with_local_memory(self) -> None:
        class _UnavailableMemory(_RecordingMemory):
            def begin_snapshot(self, request: dict[str, object]) -> dict[str, object]:
                raise MemoryUnavailable("ledger unavailable")

        with self.assertRaises(MemoryUnavailable):
            AdaptiveMemoryResearch(
                _UnavailableMemory(), "test-space", lambda _state: {"operation": "complete", "query": None, "episode_id": None},
            ).collect(
                "cycle", [{"message_id": "m", "body_text": "hello", "known_at": "2026-08-20T00:00:00Z"}],
                deadline=float("inf"),
            )

    def test_normal_chat_uses_memoryhub_context_not_sqlite_relevant_propositions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CompanionStore(root / "runtime.sqlite3")
            memory = _RecordingMemory()
            memory.append(_episode("test-space", "prior", "semiconductor drawdown lesson", "2026-08-01T00:00:00Z"))
            engine = CompanionEngine(store, memory=memory, memory_space_id="test-space")
            portfolio = PortfolioService(root, store)
            conversation = store.ensure_daily_conversation("2026-08-27")
            message = store.stage_message(conversation["cycle_id"], "What did we learn about semiconductor risk?", "conversation", message_id="m")
            batch_id, _ = store.commit_staged_messages(conversation["cycle_id"], "conversation")
            store.append_artifact(conversation["cycle_id"], "chat_human", "human", message["body_text"], conversation["as_of"], {"batch_id": batch_id})
            broker = Mock()
            broker.invoke.side_effect = [
                _response({"operation": "search", "query": "semiconductor", "episode_id": None}),
                _response({"operation": "complete", "query": None, "episode_id": None}),
                _response({
                    "reply_markdown": "The prior semiconductor drawdown remains the relevant lesson.",
                    "needs_fresh_search": False, "public_search_request": None, "propositions": [], "actions": [],
                }),
            ]

            with patch("ai_trading_companion.__main__.ProviderBrokerClient", return_value=broker):
                result = run_chat(engine, store, portfolio, conversation["cycle_id"], batch_id, True)

            self.assertEqual("The prior semiconductor drawdown remains the relevant lesson.", result["reply_markdown"])
            requests = [call.args[0] for call in broker.invoke.call_args_list]
            self.assertEqual(["chat_research", "chat_research", "chat"], [request.stage for request in requests])
            self.assertEqual("test-snapshot-1", requests[0].packet["research_state"]["snapshot"]["snapshot_id"])
            self.assertIn("semiconductor drawdown lesson", requests[-1].packet["cognition_prompt"])
            with store.connection() as connection:
                saved = json.loads(connection.execute("SELECT result_json FROM companion_cognition_job").fetchone()[0])
            self.assertEqual("test-snapshot-1", saved["memory_research"]["snapshot"]["snapshot_id"])
            self.assertIn("test-episode-1", saved["memory_research"]["episode_ids"])


def _response(result: dict[str, object]) -> BrokerResponse:
    return BrokerResponse(
        output_text=json.dumps(result), result=result, actual_model="test", provider="fake",
        intellect="smart", fulfilled_intellect="smart", request_id="request",
    )


if __name__ == "__main__":
    unittest.main()
