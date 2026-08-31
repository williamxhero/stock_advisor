from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ai_trading_companion.cognition import UnifiedCognition
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.portfolio import PortfolioService
from ai_trading_companion.store import CompanionStore


class UserLearningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = CompanionStore(Path(self.temp.name) / "companion.sqlite3")
        self.engine = CompanionEngine(self.store)
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

    def test_explicit_preference_is_append_only_and_current_format_is_not_long_term(self):
        self.apply("以后少贴原文，发链接就好。")
        self.apply("这次请用列表展示。")
        self.apply("以后多贴原文。")

        rows = self.store.current_propositions("2099-01-01T00:00:00Z")
        current = [row for row in rows if row["predicate"] == "expression.material_density"]
        self.assertEqual(1, len(current))
        self.assertIn("more_source_excerpt", current[0]["object_json"])
        with self.store.connection() as connection:
            history = connection.execute("SELECT status FROM memory_proposition WHERE predicate='expression.material_density'").fetchall()
        self.assertEqual({"active", "superseded"}, {row[0] for row in history})

    def test_method_is_recorded_as_an_unverified_user_view(self):
        self.apply("我觉得这是诱多，因为高开后核心承接不住。")

        row = next(row for row in self.store.current_propositions("2099-01-01T00:00:00Z") if row["subject"] == "user.market_method")
        self.assertEqual("user_view", row["proposition_kind"])
        self.assertIn("unverified", row["object_json"])
        self.assertLess(row["confidence"], 1.0)


if __name__ == "__main__":
    unittest.main()
