from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ai_trading_companion.cognition import UnifiedCognition
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.memory_port import InMemoryMemoryAdapter
from ai_trading_companion.portfolio import PortfolioService
from ai_trading_companion.store import CompanionStore


class UserLearningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = CompanionStore(Path(self.temp.name) / "companion.sqlite3")
        self.memory = InMemoryMemoryAdapter()
        self.engine = CompanionEngine(self.store, memory=self.memory)
        self.portfolio = PortfolioService(Path(self.temp.name), self.store)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def apply(self, text: str) -> None:
        cycle = self.engine.ensure_daily_conversation()
        message = self.store.stage_message(cycle["cycle_id"], text, "conversation")
        batch, messages = self.store.commit_staged_messages(cycle["cycle_id"], "conversation")
        source = self.store.append_artifact(cycle["cycle_id"], "chat_human", "human", text, cycle["as_of"], {"batch_id": batch})
        UnifiedCognition(self.store, self.portfolio, self.engine).apply(cycle, source, messages, "conversation", {
            "reply_markdown": "收到。", "needs_fresh_search": False, "public_search_request": None,
            "propositions": [], "actions": [],
        })

    def test_explicit_preferences_are_append_only_memoryhub_episodes(self) -> None:
        self.apply("以后少贴原文，发链接就好。")
        self.apply("这次请用列表展示。")
        self.apply("以后多贴原文。")

        self.assertEqual({"chat"}, {snapshot["stage"] for snapshot in self.memory._snapshots.values()})
        rows = [row for row in self.memory._episodes if "expression.material_density" in row["body"]]
        self.assertEqual(2, len(rows))
        self.assertIn("more_source_excerpt", rows[-1]["body"])
        self.assertEqual(rows[0]["metadata"]["proposition_id"], rows[-1]["metadata"]["supersedes_id"])

    def test_method_is_recorded_as_an_unverified_user_view(self) -> None:
        self.apply("我觉得这是诱多，因为高开后核心承接不住。")

        row = next(row for row in self.memory._episodes if "user.market_method" in row["body"])
        self.assertEqual("proposition", row["episode_type"])
        self.assertIn("unverified", row["body"])

    def test_length_and_tone_preferences_are_append_only_too(self) -> None:
        self.apply("以后简短点。")
        self.apply("以后详细点。")
        self.apply("以后直接点。")
        self.apply("以后叫我老周。")

        length = [row for row in self.memory._episodes if "expression.length" in row["body"]]
        tone = [row for row in self.memory._episodes if "expression.tone" in row["body"]]
        address = [row for row in self.memory._episodes if "expression.address" in row["body"]]
        self.assertEqual(2, len(length))
        self.assertEqual(length[0]["metadata"]["proposition_id"], length[1]["metadata"]["supersedes_id"])
        self.assertEqual(1, len(tone))
        self.assertEqual(1, len(address))


if __name__ == "__main__":
    unittest.main()
