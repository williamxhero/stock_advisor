from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

from ai_trading_companion.memory_port import InMemoryMemoryAdapter
from ai_trading_companion.memoryhub_migration import LegacyWorkspaceImporter, MemoryHubMigrator, run_shadow_comparison
from ai_trading_companion.store import CompanionStore


def _source(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript("""
        CREATE TABLE companion_message(message_id TEXT PRIMARY KEY,cycle_id TEXT,actor TEXT,state TEXT,phase TEXT,batch_id TEXT,body_text TEXT,staged_at TEXT,submitted_at TEXT,withdrawn_at TEXT,source_artifact_id TEXT,occurred_at TEXT,known_at TEXT);
        CREATE TABLE narrative_artifact(artifact_id TEXT PRIMARY KEY,cycle_id TEXT,kind TEXT,revision INTEGER,actor TEXT,body_markdown TEXT,body_sha256 TEXT,as_of TEXT,sealed_at TEXT,metadata_json TEXT,occurred_at TEXT,known_at TEXT);
        CREATE TABLE memory_proposition(proposition_id TEXT PRIMARY KEY,subject TEXT,predicate TEXT,object_json TEXT,proposition_kind TEXT,status TEXT,confidence REAL,source_message_id TEXT,source_start INTEGER,source_end INTEGER,source_quote TEXT,known_at TEXT,supersedes_id TEXT,tombstoned_at TEXT,created_at TEXT);
        CREATE TABLE evidence_ledger_entry(evidence_id TEXT PRIMARY KEY,trading_date TEXT,cycle_id TEXT,source_url TEXT,source_title TEXT,body_text TEXT,occurred_at TEXT,known_at TEXT,metadata_json TEXT,stage TEXT,content_sha256 TEXT,coverage_state TEXT);
        INSERT INTO companion_message VALUES('msg-1','cycle','human','submitted','chat',NULL,'完整问题','2026-08-01T00:00:00Z','2026-08-01T00:00:00Z',NULL,NULL,'2026-08-01T00:00:00Z','2026-08-01T00:00:00Z');
        INSERT INTO narrative_artifact VALUES('art-1','cycle','m1',1,'ai','完整判断','ignored','2026-08-01T01:00:00Z','2026-08-01T01:00:00Z','{}','2026-08-01T01:00:00Z',NULL);
        INSERT INTO memory_proposition VALUES('prop-1','user','risk_tolerance','"low"','personal_fact','active',1.0,'msg-1',0,4,'完整问题','2026-08-01T00:00:00Z',NULL,NULL,'2026-08-01T00:00:00Z');
        INSERT INTO evidence_ledger_entry VALUES('ev-1','2026-08-01','cycle',NULL,'公告','重大反证','2026-08-01T00:30:00Z','2026-08-01T00:30:00Z','{}','m0','ignored','observed');
        """)
    finally:
        connection.close()


def test_read_only_idempotent_migration_and_shadow_gate(tmp_path: Path) -> None:
    source = tmp_path / "companion.sqlite3"
    _source(source)
    memory = InMemoryMemoryAdapter()
    migrator = MemoryHubMigrator(source, memory, "partner-main", migrated_at="2026-09-01T00:00:00Z")

    first = migrator.run()
    second = migrator.run()

    assert first["counts"] == {"message": 1, "artifact": 1, "proposition": 1, "evidence": 1}
    assert second["imported"] == 0
    assert second["replayed"] == 4
    artifact = next(row for row in memory._episodes if row["source_event_id"] == "artifact:art-1")
    assert artifact["known_at"] == "2026-09-01T00:00:00Z"
    assert artifact["metadata"]["known_at_provenance"] == "migration_time"
    assert first["validation"]["hash_mismatches"] == ["artifact:art-1"]
    assert first["source_database_mode"] == "read_only"

    report = run_shadow_comparison(
        memory, "partner-main", [{"query": "反证", "as_of": "2026-09-01T00:00:00Z", "stage": "chat"}],
        legacy_search=lambda _query, _as_of: ["ev-1"],
        qualification_passed=True, recovery_drill_passed=True,
    )
    assert report["candidate_used_by_production"] is False
    assert set(report["metrics"]) == {"recall", "recall_misses", "false_associations", "major_counterevidence", "similar_failures", "future_leakage", "latency_ms", "faults"}
    assert report["switchable"] is True


def test_legacy_workspace_is_imported_once_then_never_read_as_runtime_input(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    portfolio = root / "portfolio" / "01_CURRENT_PORTFOLIO.md"
    portfolio.parent.mkdir(parents=True)
    portfolio.write_text(
        "# 当前持仓\n\n当前总资产：约 **100,000元**\n\n"
        "## 当前持仓\n\n| 代码 | 名称 | 股数 | 参考价 | 市值 | 成本价 | 浮动盈亏 | 仓位占总资产约 |\n"
        "|---|---|---:|---:|---:|---:|---:|---:|\n"
        "| 600000 | 浦发银行 | 100 | 10 | 1000 | 9 | +100 | 1% |\n\n"
        "## 最近清仓\n\n暂无\n",
        encoding="utf-8",
    )
    state = root / "state" / "10_THEME_STATE.csv"
    state.parent.mkdir(parents=True)
    state.write_text("theme,state\n机器人,active\n", encoding="utf-8")
    store = CompanionStore(tmp_path / "runtime.sqlite3")
    store.initialize()
    memory = InMemoryMemoryAdapter()
    importer = LegacyWorkspaceImporter(
        root, store, memory, "private", migrated_at="2026-09-01T08:00:00Z",
    )

    first = importer.run()
    portfolio.write_text("this must never be read after the marker", encoding="utf-8")
    with patch.object(Path, "rglob", side_effect=AssertionError("marker must precede directory enumeration")):
        second = importer.run()

    assert first["memoryhub_episodes"] == 2
    assert first["portfolio_positions"] == 1
    assert second["state"] == "already_imported"
    with store.connection() as connection:
        position = dict(connection.execute("SELECT * FROM portfolio_position").fetchone())
    assert position["code"] == "600000"
    assert position["shares"] == 100
    portfolio_episode = next(
        row for row in memory._episodes
        if row["metadata"].get("legacy_relative_path") == "portfolio/01_CURRENT_PORTFOLIO.md"
    )
    assert "600000" in portfolio_episode["body"]
    snapshot = memory.begin_snapshot({"memory_space_id": "private", "as_of": "2026-09-01T08:00:00Z", "stage": "chat", "cycle_id": "test"})
    assert memory.search(snapshot["snapshot_id"], "机器人")
