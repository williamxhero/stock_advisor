from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from threading import Event, Thread
from typing import Any
import uuid


@dataclass(frozen=True)
class BackupArtifact:
    database: Path
    manifest_path: Path
    manifest: dict[str, Any]


class BackupManager:
    def __init__(self, hub: Any) -> None:
        self.hub = hub

    def create(self, directory: Path | str) -> BackupArtifact:
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        backup_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        database = root / f"memoryhub-{backup_id}.sqlite3"
        with sqlite3.connect(self.hub.database) as source, sqlite3.connect(database) as target:
            source.backup(target)
        with sqlite3.connect(database) as verified:
            episodes = int(verified.execute("SELECT COUNT(*) FROM episode").fetchone()[0])
        manifest = {
            "backup_id": backup_id,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "protocol_version": "memoryhub/v1",
            "episodes": episodes,
            "database_sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
        }
        manifest_path = database.with_suffix(".manifest.json")
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        return BackupArtifact(database, manifest_path, manifest)

    @staticmethod
    def restore(
        backup_database: Path | str, destination: Path | str, *,
        source_adapters: dict[str, Any] | None = None, rebuild: bool = True,
    ) -> Any:
        from .core import MemoryHub

        source_path, destination_path = Path(backup_database), Path(destination)
        if destination_path.exists():
            raise FileExistsError(f"restore destination already exists: {destination_path}")
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(source_path) as source, sqlite3.connect(destination_path) as target:
            source.backup(target)
        hub = MemoryHub(destination_path, source_adapters=source_adapters)
        if rebuild:
            hub.rebuild_indexes()
        return hub


class BackupWorker:
    def __init__(self, manager: BackupManager, directory: Path | str, *, interval_seconds: float) -> None:
        self.manager = manager
        self.directory = Path(directory)
        self.interval_seconds = max(60.0, interval_seconds)
        self._stop = Event()
        self._thread = Thread(target=self._run, name="memoryhub-backup", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.manager.create(self.directory)
            self._stop.wait(self.interval_seconds)
