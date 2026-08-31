from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ai_trading_companion.engine import CompanionEngine
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
            engine.chat_ready(cycle["cycle_id"], "continued result")


if __name__ == "__main__":
    unittest.main()
