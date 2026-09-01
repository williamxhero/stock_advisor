from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from ai_trading_companion.migration import LegacyMigrator, LegacySources
from ai_trading_companion.memory_port import InMemoryMemoryAdapter
from ai_trading_companion.paths import RuntimePaths
from ai_trading_companion.store import CompanionStore


class LegacyMigrationTests(unittest.TestCase):
    def test_migration_copies_runtime_and_imports_each_legacy_message_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_runtime = root / "old" / "data" / "runtime" / "companion" / "companion.sqlite3"
            old_store = CompanionStore(old_runtime)
            cycle = old_store.create_cycle("daily.execution.0945", "2026-08-25T09:45:00+08:00", "2026-08-25T09:45:00+08:00")
            old_store.append_artifact(cycle["cycle_id"], "m0", "ai", "旧伴生消息", "2026-08-25T09:45:00+08:00")

            decision_home = root / "decision-center"
            decision_home.mkdir(parents=True)
            connection = sqlite3.connect(decision_home / "decision-center.db")
            try:
                connection.executescript("""
                    CREATE TABLE task_messages (
                      id INTEGER PRIMARY KEY, task_key TEXT, slot TEXT, task_type TEXT,
                      scheduled_for TEXT, completed_at TEXT, received_at TEXT, status TEXT,
                      summary TEXT, body_markdown TEXT);
                    INSERT INTO task_messages VALUES(1,'daily.execution.1030','10:30','趋势确认',
                      '2026-08-25T10:30:00+08:00','2026-08-25T10:35:00+08:00',
                      '2026-08-25T10:35:00+08:00','complete','旧任务','旧 Decision Center 消息');
                """)
            finally:
                connection.close()

            automation = root / "old" / "data" / "runtime" / "stock_advisor.sqlite3"
            automation.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(automation)
            try:
                connection.executescript("""
                    CREATE TABLE automation_run (run_id TEXT PRIMARY KEY, task_key TEXT, task_type TEXT,
                      scheduled_for TEXT, completed_at TEXT, status TEXT, summary TEXT, started_at TEXT);
                    CREATE TABLE automation_response (run_id TEXT PRIMARY KEY, body_markdown TEXT);
                    INSERT INTO automation_run VALUES('run-1','daily.review.1520','收盘复盘',
                      '2026-08-25T15:20:00+08:00','2026-08-25T15:25:00+08:00','complete','旧自动化',
                      '2026-08-25T15:20:00+08:00');
                    INSERT INTO automation_response VALUES('run-1','旧自动化消息');
                """)
            finally:
                connection.close()

            workspace = root / "old" / "data"
            (workspace / "portfolio").mkdir(parents=True, exist_ok=True)
            (workspace / "portfolio" / "01_CURRENT_PORTFOLIO.md").write_text("# 持仓\n", encoding="utf-8")
            (decision_home / "window-state.json").write_text("{}", encoding="utf-8")
            target = RuntimePaths(root / "install", root / "new-home")
            sources = LegacySources(old_runtime, automation, decision_home, workspace)

            memory = InMemoryMemoryAdapter()
            first = LegacyMigrator(target, sources, memory=memory).run()
            second = LegacyMigrator(target, sources, memory=memory).run()

            self.assertEqual(first["copied_companion_database"], "copied")
            self.assertEqual(first["decision_center_messages"], 1)
            self.assertEqual(first["automation_messages"], 1)
            self.assertEqual(second["decision_center_messages"], 0)
            self.assertEqual(second["automation_messages"], 0)
            self.assertEqual(second["copied_companion_database"], "merged")
            self.assertEqual("imported", first["legacy_workspace"]["state"])
            self.assertEqual("already_imported", second["legacy_workspace"]["state"])
            self.assertFalse((target.home / "workspace").exists())
            self.assertTrue(any(item["episode_type"] == "legacy_workspace_document" for item in memory._episodes))
            self.assertTrue((target.home / "ui" / "window-state.json").exists())
            self.assertTrue((target.home / "ui" / "legacy-message-cache.sqlite3").exists())
            with CompanionStore(target.database).connection() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM narrative_artifact").fetchone()[0], 3)


if __name__ == "__main__":
    unittest.main()
