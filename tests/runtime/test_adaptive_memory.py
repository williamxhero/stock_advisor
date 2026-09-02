from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ai_trading_companion.__main__ import _discover_chat_external_evidence, run_chat
from ai_trading_companion.adaptive_memory import AdaptiveMemoryResearch
from ai_trading_companion.broker_client import BrokerResponse
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.memory_port import InMemoryMemoryAdapter, MemoryUnavailable
from ai_trading_companion.portfolio import PortfolioService
from ai_trading_companion.store import CompanionStore
from trading_memory_hub.core import MemoryHub


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
    def test_action_budget_returns_collected_context_without_an_unbounded_model_loop(self) -> None:
        memory = _RecordingMemory()
        memory.append(_episode("test-space", "prior", "close evidence", "2026-09-01T07:00:00Z"))

        result = AdaptiveMemoryResearch(
            memory, "test-space", lambda _state: {"operation": "search", "query": "close", "episode_id": None},
            max_actions=2,
        ).collect(
            "cycle-1", [{"message_id": "message-1", "body_text": "close", "known_at": "2026-09-01T07:20:00Z"}],
            deadline=time.monotonic() + 5,
        )

        self.assertEqual("budget_exhausted", result.actions[-1]["state"])
        self.assertEqual(1, len(result.context))
        self.assertEqual(1, memory.operations.count(("search", "close")))

    def test_invalid_external_decision_is_returned_to_the_loop_for_correction(self) -> None:
        memory = _RecordingMemory()
        decisions = iter([
            {"version": 1, "operation": "markethub_quote", "source_reference": {}},
            {"version": 1, "operation": "complete", "reason": "use available evidence"},
        ])
        observations: list[dict | None] = []

        def decide(state: dict) -> dict:
            observations.append(state["last_observation"])
            return next(decisions)

        result = AdaptiveMemoryResearch(memory, "test-space", decide).collect(
            "cycle-1", [{"message_id": "message-1", "body_text": "close", "known_at": "2026-09-01T07:20:00Z"}],
            deadline=time.monotonic() + 5,
        )

        self.assertEqual("rejected_invalid", observations[1]["state"])
        self.assertIn("source_reference", observations[1]["detail"])
        self.assertEqual("complete", result.actions[-1]["operation"])

    def test_credential_shaped_source_reference_is_rejected_without_echoing_it(self) -> None:
        memory = _RecordingMemory()
        decisions = iter([
            {"version": 1, "operation": "markethub_quote", "source_reference": {
                "source_system": "markethub", "token": "secret=abcdefghijklmnopqrstuvwx",
            }},
            {"version": 1, "operation": "complete", "reason": "stop"},
        ])
        observations: list[dict | None] = []

        def decide(state: dict) -> dict:
            observations.append(state["last_observation"])
            return next(decisions)

        AdaptiveMemoryResearch(memory, "test-space", decide).collect(
            "cycle-1", [{"message_id": "message-1", "body_text": "close", "known_at": "2026-09-01T07:20:00Z"}],
            deadline=time.monotonic() + 5,
        )

        self.assertEqual("rejected_invalid", observations[1]["state"])
        self.assertEqual("source reference contains credential-shaped fields", observations[1]["detail"])

    def test_external_gateway_failure_becomes_an_observation_instead_of_aborting_chat(self) -> None:
        memory = _RecordingMemory()
        decisions = iter([
            {"version": 1, "operation": "web_read", "url": "https://example.test/close"},
            {"version": 1, "operation": "complete", "reason": "continue with available evidence"},
        ])

        def unavailable(_action: dict, _snapshot: dict) -> list[dict]:
            raise RuntimeError("gateway returned malformed tool data")

        result = AdaptiveMemoryResearch(
            memory, "test-space", lambda _state: next(decisions), discover_external=unavailable,
        ).collect(
            "cycle-1", [{"message_id": "message-1", "body_text": "盘后总结", "known_at": "2026-09-01T07:00:00Z"}],
            deadline=time.monotonic() + 5,
        )

        self.assertEqual("failed", result.actions[0]["state"])
        self.assertEqual("complete", result.actions[1]["operation"])
        self.assertEqual((), result.context)

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
                    "answer": {"points": ["The prior semiconductor drawdown remains the relevant lesson."], "material_ids": []},
                    "needs_fresh_search": False, "public_search_request": None, "propositions": [], "actions": [],
                }),
            ]

            with patch("ai_trading_companion.__main__.ProviderBrokerClient", return_value=broker):
                result = run_chat(engine, store, portfolio, conversation["cycle_id"], batch_id, True)

            self.assertEqual("The prior semiconductor drawdown remains the relevant lesson.", result["answer"]["points"][0])
            requests = [call.args[0] for call in broker.invoke.call_args_list]
            self.assertEqual(["chat_research", "chat_research", "chat"], [request.stage for request in requests])
            self.assertEqual("test-snapshot-1", requests[0].packet["research_state"]["snapshot"]["snapshot_id"])
            self.assertIn("semiconductor drawdown lesson", requests[-1].packet["cognition_prompt"])
            with store.connection() as connection:
                saved = json.loads(connection.execute("SELECT result_json FROM companion_cognition_job").fetchone()[0])
            self.assertEqual("test-snapshot-1", saved["memory_research"]["snapshot"]["snapshot_id"])
            self.assertIn("test-episode-1", saved["memory_research"]["episode_ids"])

    def test_external_discovery_enters_chat_only_after_memoryhub_receipt(self) -> None:
        memory = _RecordingMemory()
        seen: list[dict[str, object]] = []

        def external(action: dict[str, object], _snapshot: dict[str, object]) -> list[dict[str, object]]:
            seen.append(action)
            receipt = memory.append(_episode("test-space", "web-1", "widely discussed risk", "2026-08-20T00:00:00Z"))
            return [{"episode_id": receipt["episode_id"], "text": "widely discussed risk", "memory_receipt": receipt}]

        decisions = iter([
            {"operation": "web_search", "query": "market risk rumor", "episode_id": None, "url": None},
            {"operation": "complete", "query": None, "episode_id": None, "url": None},
        ])
        result = AdaptiveMemoryResearch(memory, "test-space", lambda _state: next(decisions), discover_external=external).collect(
            "cycle", [{"message_id": "m", "body_text": "risk", "known_at": "2026-08-20T00:00:00Z"}], deadline=float("inf"),
        )

        self.assertEqual("market risk rumor", seen[0]["query"])
        self.assertEqual("test-episode-1", result.context[0]["episode_id"])

    def test_stable_source_reference_is_receipted_then_expanded_from_a_new_snapshot(self) -> None:
        memory = _RecordingMemory()
        engine = type("Engine", (), {"memory": memory, "memory_space_id": "test-space"})()
        action = {"operation": "markethub_quote", "source_reference": {
            "source_system": "markethub", "record_type": "stock_quote_1d", "date": "2026-08-20", "code": "600000",
        }}
        with patch("ai_trading_companion.__main__.WebAccessGatewayClient"):
            # The in-memory adapter accepts the protocol shape; production hydration is covered by MemoryHub itself.
            rows = _discover_chat_external_evidence(engine, action, {"snapshot_id": "s", "cycle_id": "cycle"})
        self.assertEqual("immutable_source_reference", rows[0]["authority"])
        self.assertIn("memory_snapshot_id", rows[0])

    def test_m1_snapshot_hides_same_cycle_h0_but_keeps_prior_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            hub = MemoryHub(Path(temporary) / "memory.sqlite3")
            for event, body, episode_type, stage in [("old", "prior lesson", "note", "m0_research"), ("h0", "current human h0", "h0", "h0")]:
                hub.append({"memory_space_id": "formal", "source_system": "test", "source_event_id": event,
                    "content_hash": "auto", "episode_type": episode_type, "body": body,
                    "occurred_at": "2026-08-20T00:00:00Z", "known_at": "2026-08-20T00:00:00Z", "submitted_at": "2026-08-20T00:00:00Z",
                    "authority": "test", "protocol_version": "memoryhub/v1", "metadata": {"cycle_id": "cycle", "stage": stage, "actor": "human" if event == "h0" else "ai"}})
            snapshot = hub.begin_snapshot("formal", as_of="2026-08-21T00:00:00Z", stage="m1_research", cycle_id="cycle")
            visible = hub.search(snapshot.snapshot_id, "", limit=20)
        self.assertEqual(["prior lesson"], [item["summary"] for item in visible])


def _response(result: dict[str, object]) -> BrokerResponse:
    return BrokerResponse(
        output_text=json.dumps(result), result=result, actual_model="test", provider="fake",
        intellect="smart", fulfilled_intellect="smart", request_id="request",
    )


if __name__ == "__main__":
    unittest.main()
