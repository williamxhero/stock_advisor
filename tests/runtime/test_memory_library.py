from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ai_trading_companion.backup import BackupManager
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.config import RuntimeSettings
from ai_trading_companion.embedding import CloudEmbeddingGate
from ai_trading_companion.memory import MemoryIndexer, MemoryLibrary, MemoryRequest
from ai_trading_companion.packet_builder import RuntimePacketBuilder
from ai_trading_companion.secret_guard import assert_safe
from ai_trading_companion.store import CompanionStore


ROOT = Path(__file__).resolve().parents[2]


class MemoryLibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = CompanionStore(Path(self.temp.name) / "companion.sqlite3")
        self.engine = CompanionEngine(self.store)
        self.first = self.engine.start_cycle("daily.execution.0945", "2026-08-25T09:45:00+08:00", "2026-08-25T01:45:00Z")
        self.second = self.engine.start_cycle("daily.execution.1030", "2026-08-25T10:30:00+08:00", "2026-08-25T02:30:00Z")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_index_is_separate_rebuildable_and_retrieval_keeps_counterevidence(self) -> None:
        good = self.store.append_artifact(self.first["cycle_id"], "m1", "model", "机器人 603611 转强", "2026-08-25T02:00:00Z")
        bad = self.store.append_artifact(self.first["cycle_id"], "reflection", "model", "机器人 603611 的上一轮判断错误，反证来自量能衰竭", "2026-08-25T02:01:00Z")
        index = MemoryIndexer(self.store)
        self.assertEqual(2, index.sync())
        self.assertTrue(index.database.exists())
        bundle = MemoryLibrary(self.store, index).retrieve_bundle(MemoryRequest("daily.execution.1030", "2099-01-01T00:00:00Z", "m0_research", "603611 机器人"))
        self.assertIn(bad["artifact_id"], [card["artifact_id"] for card in bundle.cards])
        self.assertIn(good["artifact_id"], [card["artifact_id"] for card in bundle.cards])
        self.assertEqual("ready", index.health()["state"])

    def test_m1_excludes_current_h0_but_keeps_historical_user_reasoning(self) -> None:
        historical = "昨天我认为 603611 会转强"
        current = "本周期我认为 603611 会冲高回落"
        self.store.append_artifact(self.first["cycle_id"], "h0", "human", historical, "2026-08-25T02:00:00Z")
        self.store.append_artifact(self.second["cycle_id"], "h0", "human", current, "2026-08-25T03:00:00Z")
        packet = RuntimePacketBuilder(ROOT / "resources", ROOT / "data", self.store).build(self.second, "m1_research", evidence={"sources": [{"title": "603611"}]}, as_of="2099-01-01T00:00:00Z")
        serialized=json.dumps(packet,ensure_ascii=False)
        self.assertIn(historical, serialized)
        self.assertNotIn(current, serialized)

    def test_secret_guard_blocks_storage_and_cloud_packet(self) -> None:
        with self.assertRaisesRegex(ValueError, "secret guard"):
            fake_secret = "api_key=" + "sk-proj-" + "abcdefghijklmnopqrstuvwxyz"
            assert_safe(fake_secret, boundary="test")
        self.engine.research_started(self.first["cycle_id"])
        ready=self.engine.research_ready(self.first["cycle_id"], "M0")
        with self.assertRaisesRegex(ValueError, "secret guard"):
            self.engine._stage_message(ready, "Bearer abcdefghijklmnopqrstuvwxyz", None)

    def test_online_backup_restores_with_integrity_check(self) -> None:
        self.store.append_artifact(self.first["cycle_id"], "m0", "model", "可恢复事实", "2026-08-25T02:00:00Z")
        backup=BackupManager(self.store,Path(self.temp.name)/"runtime").create(reason="test")
        verified=BackupManager(self.store,Path(self.temp.name)/"runtime").verify_restore(backup["backup_id"])
        self.assertEqual("ok",verified["integrity"])
        self.assertEqual(1,verified["artifact_count"])

    def test_cloud_embedding_requires_evidence_gate_and_clean_historical_scan(self) -> None:
        settings=RuntimeSettings({}, {"enabled":True,"base_url":"https://example.invalid","model":"x","api_key":"not-a-real-key","max_concurrency":1,"timeout_seconds":1,"max_attempts_per_record":1}, {})
        gate=CloudEmbeddingGate(self.store,settings)
        self.assertFalse(gate.readiness(semantic_miss_cases=49,measured_material_gain=True)["ready"])
        # Simulate a legacy record predating the guard.  New writes are blocked
        # earlier; activation still has to refuse a contaminated old database.
        self.store.append_artifact(self.first["cycle_id"],"m0","model","历史正常记录","2026-08-25T02:00:00Z")
        with self.store.connection() as c:
            c.execute("UPDATE narrative_artifact SET body_markdown=? WHERE artifact_id=(SELECT artifact_id FROM narrative_artifact LIMIT 1)", ("token=abcdefghijklmnopqrstuvwxyz0123456789",))
        self.assertFalse(gate.readiness(semantic_miss_cases=50,measured_material_gain=True)["ready"])
