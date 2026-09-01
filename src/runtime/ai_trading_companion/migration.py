"""Idempotent, local-only migration from predecessor applications.

Structured facts are merged into the runtime database and retired text files
are appended to MemoryHub. The predecessor data remains untouched for recovery.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .paths import RuntimePaths
from .observatory import EvaluationObservatory
from .store import CompanionStore
from .memory_port import MemoryPort
from .memoryhub_migration import LegacyWorkspaceImporter


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class LegacySources:
    companion_database: Path
    automation_database: Path
    decision_center_home: Path
    workspace: Path

    @classmethod
    def defaults(cls, install_root: Path) -> "LegacySources":
        local = Path(__import__("os").environ["LOCALAPPDATA"])
        legacy_data = install_root / "data"
        if not legacy_data.exists():
            legacy_data = install_root / "archive" / "local" / "legacy-runtime-data-2026-08-25"
        return cls(
            companion_database=legacy_data / "runtime" / "companion" / "companion.sqlite3",
            automation_database=legacy_data / "runtime" / "stock_advisor.sqlite3",
            decision_center_home=local / "AIDecisionCenter",
            workspace=legacy_data,
        )


class LegacyMigrator:
    def __init__(
        self, target: RuntimePaths, sources: LegacySources,
        *, memory: MemoryPort | None = None, memory_space_id: str = "ai-trading-companion",
    ) -> None:
        self.target = target
        self.sources = sources
        self.memory = memory
        self.memory_space_id = memory_space_id

    def run(self) -> dict[str, Any]:
        self.target.ensure()
        copied_database = self._copy_companion_database()
        store = CompanionStore(self.target.database)
        store.initialize()
        report = {
            "migrated_at": _now(),
            "target_home": str(self.target.home),
            "copied_companion_database": copied_database,
            "decision_center_messages": self._import_decision_center(store),
            "automation_messages": self._import_automation_history(store),
            "legacy_workspace": LegacyWorkspaceImporter(
                self.sources.workspace, store, self.memory, self.memory_space_id,
                migrated_at=_now(),
            ).run() if self.memory is not None else {"state": "deferred_until_memoryhub_available"},
            "ui_files": self._copy_ui_state(),
            "observatory_backfill": EvaluationObservatory(
                store, schedule_path=self.target.resources / "schedules" / "tasks.json",
            )._backfill_legacy(),
        }
        manifest = self.target.home / "migration" / "legacy-migration-manifest.json"
        manifest.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report

    def _copy_companion_database(self) -> str:
        source = self.sources.companion_database
        target = self.target.database
        if not source.exists():
            return "not_found"
        if target.exists():
            self._merge_companion_database(source, target)
            return "merged"
        target.parent.mkdir(parents=True, exist_ok=True)
        source_connection = sqlite3.connect(source)
        target_connection = sqlite3.connect(target)
        try:
            source_connection.backup(target_connection)
        finally:
            target_connection.close()
            source_connection.close()
        return "copied"

    @staticmethod
    def _merge_companion_database(source: Path, target: Path) -> None:
        """Merge an already-created target without losing cycles created during cutover."""
        source_connection = sqlite3.connect(source)
        target_connection = sqlite3.connect(target)
        try:
            target_connection.execute("PRAGMA foreign_keys = OFF")
            source_tables = {
                row[0] for row in source_connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            target_tables = {
                row[0] for row in target_connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            ordered = [
                "companion_cycle", "companion_schedule_claim", "narrative_artifact", "companion_message",
                "companion_command_receipt", "companion_outbox", "portfolio_position", "portfolio_transaction",
                "portfolio_change_proposal", "portfolio_interpretation_job", "portfolio_render_intent",
                "portfolio_meta", "portfolio_outbox", "knowledge_change_proposal", "llm_attempt",
                "companion_research_job", "evidence_ledger_entry", "judgment_snapshot", "outcome_checkpoint",
                "memory_backup", "workflow_policy", "timing_policy", "ai_risk_doctrine",
                "market_regime_snapshot", "router_policy_cell", "cognitive_route_decision", "router_shadow_job",
                "router_evaluation", "evolution_hypothesis", "legacy_import_record",
            ]
            for table in ordered:
                if table not in source_tables or table not in target_tables:
                    continue
                source_columns = [row[1] for row in source_connection.execute(f"PRAGMA table_info({table})")]
                target_columns = {row[1] for row in target_connection.execute(f"PRAGMA table_info({table})")}
                columns = [column for column in source_columns if column in target_columns]
                if not columns:
                    continue
                quoted = ",".join(f'"{column}"' for column in columns)
                rows = source_connection.execute(f"SELECT {quoted} FROM {table}").fetchall()
                if rows:
                    target_connection.executemany(
                        f"INSERT OR IGNORE INTO {table} ({quoted}) VALUES ({','.join('?' for _ in columns)})", rows
                    )
            # FTS is derived from immutable artifacts. Rebuild it so imported
            # history is searchable even when its source database used a
            # different FTS segment layout.
            target_connection.execute("DELETE FROM narrative_fts")
            target_connection.execute(
                "INSERT INTO narrative_fts(artifact_id,cycle_id,kind,body_markdown) "
                "SELECT artifact_id,cycle_id,kind,body_markdown FROM narrative_artifact"
            )
            target_connection.commit()
        finally:
            target_connection.close()
            source_connection.close()

    @staticmethod
    def _record_exists(store: CompanionStore, source_name: str, source_id: str) -> bool:
        with store.connection() as connection:
            return connection.execute(
                "SELECT 1 FROM legacy_import_record WHERE source_name=? AND source_id=?",
                (source_name, source_id),
            ).fetchone() is not None

    @staticmethod
    def _record(store: CompanionStore, source_name: str, source_id: str, artifact_id: str | None, detail: dict[str, Any]) -> None:
        with store.connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO legacy_import_record(source_name,source_id,imported_artifact_id,imported_at,detail_json) VALUES(?,?,?,?,?)",
                (source_name, source_id, artifact_id, _now(), json.dumps(detail, ensure_ascii=False, sort_keys=True)),
            )

    @staticmethod
    def _import_row(store: CompanionStore, source_name: str, source_id: str, row: dict[str, Any]) -> bool:
        if LegacyMigrator._record_exists(store, source_name, source_id):
            return False
        body = str(row.get("body_markdown") or "").strip()
        if not body:
            LegacyMigrator._record(store, source_name, source_id, None, {"status": "empty"})
            return False
        task_key = str(row.get("task_key") or "legacy.history")
        occurred_at = str(row.get("completed_at") or row.get("scheduled_for") or row.get("received_at") or _now())
        scheduled_for = str(row.get("scheduled_for") or occurred_at)
        cycle = store.create_cycle(task_key, scheduled_for, occurred_at)
        if cycle["state"] != "complete":
            store.transition(cycle["cycle_id"], "complete")
        metadata = {
            "legacy_source": source_name,
            "legacy_id": source_id,
            "slot": row.get("slot"),
            "task_type": row.get("task_type"),
            "status": row.get("status"),
            "summary": row.get("summary"),
        }
        try:
            artifact = store.append_artifact(
                cycle["cycle_id"], "legacy_message", "legacy_import", body, occurred_at, metadata,
                occurred_at=occurred_at, known_at=str(row.get("received_at") or occurred_at),
            )
        except ValueError as error:
            LegacyMigrator._record(store, source_name, source_id, None, {"status": "rejected", "reason": str(error)})
            return False
        LegacyMigrator._record(store, source_name, source_id, artifact["artifact_id"], {"status": "imported", "cycle_id": cycle["cycle_id"]})
        return True

    def _import_decision_center(self, store: CompanionStore) -> int:
        database = self.sources.decision_center_home / "decision-center.db"
        if not database.exists():
            return 0
        count = 0
        connection = sqlite3.connect(database)
        try:
            connection.row_factory = sqlite3.Row
            rows = connection.execute("SELECT * FROM task_messages ORDER BY received_at, id").fetchall()
        finally:
            connection.close()
        for row in rows:
            if self._import_row(store, "decision-center", str(row["id"]), dict(row)):
                count += 1
        return count

    def _import_automation_history(self, store: CompanionStore) -> int:
        database = self.sources.automation_database
        if not database.exists():
            return 0
        count = 0
        connection = sqlite3.connect(database)
        try:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """SELECT r.run_id,r.task_key,r.task_type,r.scheduled_for,r.completed_at,r.status,r.summary,
                          a.body_markdown,r.started_at AS received_at
                     FROM automation_run r JOIN automation_response a ON a.run_id=r.run_id
                    ORDER BY r.completed_at,r.run_id"""
            ).fetchall()
        finally:
            connection.close()
        for row in rows:
            if self._import_row(store, "codex-automation", str(row["run_id"]), dict(row)):
                count += 1
        return count

    def _copy_ui_state(self) -> int:
        copied = 0
        source = self.sources.decision_center_home
        legacy_cache = self.target.home / "ui" / "legacy-message-cache.sqlite3"
        decision_database = source / "decision-center.db"
        if decision_database.exists() and not legacy_cache.exists():
            legacy_cache.parent.mkdir(parents=True, exist_ok=True)
            source_connection = sqlite3.connect(decision_database)
            target_connection = sqlite3.connect(legacy_cache)
            try:
                source_connection.backup(target_connection)
            finally:
                target_connection.close()
                source_connection.close()
            copied += 1
        mapping = {
            "appsettings.json": "ui/settings.json",
            "window-state.json": "ui/window-state.json",
            "companion-drafts.json": "ui/drafts.json",
        }
        for legacy_name, target_relative in mapping.items():
            legacy = source / legacy_name
            target = self.target.home / target_relative
            if legacy.exists() and not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(legacy, target)
                copied += 1
        return copied


def source_digest(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
