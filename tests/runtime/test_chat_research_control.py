from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ai_trading_companion.adaptive_memory import AdaptiveMemoryResearch, MemoryResearchError
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.memory_port import InMemoryMemoryAdapter
from ai_trading_companion.store import CompanionStore


class ChatResearchControlTests(unittest.TestCase):
    def test_terminate_is_idempotent_and_blocks_late_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = CompanionStore(Path(temporary) / "runtime.sqlite3")
            engine = CompanionEngine(store)
            cycle = store.ensure_daily_conversation("2026-09-01")
            first = engine.command({"command_id": "stop", "cycle_id": cycle["cycle_id"], "type": "terminate_chat_research"})
            second = engine.command({"command_id": "stop", "cycle_id": cycle["cycle_id"], "type": "terminate_chat_research"})
            self.assertEqual(first, second)
            with self.assertRaisesRegex(RuntimeError, "terminated"):
                engine.chat_ready(cycle["cycle_id"], "late result")
            resumed = engine.command({"command_id": "resume", "cycle_id": cycle["cycle_id"], "type": "continue_chat_research"})
            self.assertTrue(resumed["resumed_at"])
            self.assertTrue(resumed["continued_now"])
            self.assertFalse(engine.command({"command_id": "resume-again", "cycle_id": cycle["cycle_id"], "type": "continue_chat_research"})["continued_now"])
            engine.chat_ready(cycle["cycle_id"], "continued result")

    def test_continue_reuses_the_saved_snapshot_without_repeating_qualified_search(self) -> None:
        memory = InMemoryMemoryAdapter()
        memory.append({
            "memory_space_id": "control-test", "source_system": "test", "source_event_id": "lesson",
            "content_hash": "lesson", "episode_type": "note", "body": "Semiconductor lesson",
            "occurred_at": "2026-09-01T00:00:00Z", "known_at": "2026-09-01T00:00:00Z",
            "submitted_at": "2026-09-01T00:00:00Z", "authority": "test", "protocol_version": "memoryhub/v1",
        })
        messages = [{"message_id": "m", "body_text": "Recall the semiconductor lesson", "known_at": "2026-09-01T00:00:00Z"}]
        checkpoints: list[dict] = []
        actions = iter([{"operation": "search", "query": "semiconductor"}])
        with self.assertRaisesRegex(MemoryResearchError, "terminated"):
            AdaptiveMemoryResearch(memory, "control-test", lambda _state: next(actions)).collect(
                "cycle", messages, deadline=float("inf"), on_checkpoint=checkpoints.append,
                cancelled=lambda: bool(checkpoints and checkpoints[-1]["actions"]),
            )
        checkpoint = checkpoints[-1]
        resumed = AdaptiveMemoryResearch(memory, "control-test", lambda _state: {"operation": "complete"}).collect(
            "cycle", messages, deadline=float("inf"), resume=checkpoint,
        )
        self.assertEqual(1, len(memory._snapshots))
        self.assertEqual("search", resumed.actions[0]["operation"])
        self.assertEqual("complete", resumed.actions[-1]["operation"])

    def test_checkpoint_survives_terminate_and_continue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = CompanionStore(Path(temporary) / "runtime.sqlite3")
            cycle = store.ensure_daily_conversation("2026-09-01")
            store.save_chat_research_checkpoint(cycle["cycle_id"], "source", ["batch"], {
                "cycle_id": cycle["cycle_id"], "stage": "chat", "snapshot": {"snapshot_id": "snap"},
                "context": [{"episode_id": "evidence"}], "actions": [{"operation": "search", "state": "completed"}],
                "known_episode_ids": ["evidence"], "executed": [["search", "risk"]], "last_observation": None,
            })
            store.terminate_chat_research(cycle["cycle_id"])
            store.continue_chat_research(cycle["cycle_id"])
            restored = store.resumed_chat_research_checkpoint(cycle["cycle_id"], "source")
            self.assertEqual("snap", restored["checkpoint"]["snapshot"]["snapshot_id"])
            self.assertEqual("evidence", restored["checkpoint"]["context"][0]["episode_id"])


if __name__ == "__main__":
    unittest.main()
