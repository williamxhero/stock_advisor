from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from trading_memory_hub import MemoryHub, SourceUnavailable
from trading_memory_hub.backup import BackupIntegrityError, BackupManager


class StableSource:
    def hydrate(self, reference: dict[str, str]) -> dict[str, str]:
        return {"title": "公告", "body": "标题未写但正文包含机器人风险", "occurred_at": reference["date"] + "T00:00:00Z"}

    def health(self) -> dict[str, str]:
        return {"state": "ready"}


def test_backup_restores_ledger_and_rebuilds_source_index(tmp_path: Path) -> None:
    adapter = StableSource()
    hub = MemoryHub(tmp_path / "live.sqlite3", source_adapters={"8815": adapter})
    receipt = hub.append(
        {
            "memory_space_id": "partner-main", "source_system": "8815",
            "source_event_id": "notice-1", "content_hash": "auto",
            "episode_type": "external_evidence",
            "source_reference": {"source_system": "8815", "record_type": "cninfo_disclosure", "date": "2026-08-28", "code": "600000", "event_id": "notice-1"},
            "occurred_at": "2026-08-28T00:00:00Z", "known_at": "2026-08-31T01:00:00Z",
            "submitted_at": "2026-08-31T01:00:00Z", "authority": "immutable_public_source",
            "protocol_version": "memoryhub/v1",
        }
    )
    backup = BackupManager(hub).create(tmp_path / "backups")

    restored = BackupManager.restore(
        backup.database, tmp_path / "isolated" / "ledger.sqlite3",
        source_adapters={"8815": adapter}, rebuild=True,
    )
    snapshot = restored.begin_snapshot("partner-main", as_of="2026-08-31T02:00:00Z", stage="chat")

    assert restored.health()["ledger"]["episodes"] == 1
    assert restored.search(snapshot.snapshot_id, "机器人")[0]["episode_id"] == receipt.episode_id
    assert restored.expand(snapshot.snapshot_id, receipt.episode_id)["body"].endswith("机器人风险")
    assert backup.manifest["episodes"] == 1


def test_restore_rejects_tampered_backup_before_creating_destination(tmp_path: Path) -> None:
    hub = MemoryHub(tmp_path / "live.sqlite3")
    backup = BackupManager(hub).create(tmp_path / "backups")
    with backup.database.open("ab") as stream:
        stream.write(b"tampered")
    destination = tmp_path / "isolated" / "ledger.sqlite3"

    with pytest.raises(BackupIntegrityError):
        BackupManager.restore(backup.database, destination)

    assert not destination.exists()


def test_failed_source_rebuild_preserves_previous_search_projection(tmp_path: Path) -> None:
    source = StableSource()
    hub = MemoryHub(tmp_path / "live.sqlite3", source_adapters={"8815": source})
    hub.append(
        {
            "memory_space_id": "partner-main", "source_system": "8815",
            "source_event_id": "notice-1", "content_hash": "auto", "episode_type": "external_evidence",
            "source_reference": {"source_system": "8815", "record_type": "cninfo_disclosure", "date": "2026-08-28", "code": "600000", "event_id": "notice-1"},
            "occurred_at": "2026-08-28T00:00:00Z", "known_at": "2026-08-31T01:00:00Z",
            "submitted_at": "2026-08-31T01:00:00Z", "authority": "immutable_public_source",
            "protocol_version": "memoryhub/v1",
        }
    )
    snapshot = hub.begin_snapshot("partner-main", as_of="2026-08-31T02:00:00Z", stage="chat")

    source.hydrate = lambda reference: (_ for _ in ()).throw(SourceUnavailable("offline"))  # type: ignore[method-assign]
    with pytest.raises(SourceUnavailable):
        hub.rebuild_indexes()

    assert hub.search(snapshot.snapshot_id, "机器人")


def test_health_keeps_ledger_ready_when_projection_is_unavailable(tmp_path: Path) -> None:
    hub = MemoryHub(tmp_path / "ledger.sqlite3")
    with sqlite3.connect(hub.database) as connection:
        connection.execute("DROP TABLE source_search_index")

    health = hub.health()

    assert health["ledger"]["state"] == "ready"
    assert health["index"]["state"] == "unavailable"
