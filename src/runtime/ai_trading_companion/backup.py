"""Online SQLite backups with deterministic integrity/restore checks."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import shutil
import sqlite3
import uuid
from typing import Any

from .store import now


class BackupManager:
    def __init__(self, store: Any, runtime_root: Path, settings: dict[str, Any] | None = None) -> None:
        self.store=store; self.runtime_root=Path(runtime_root); self.settings=settings or {}

    def create(self, *, reason: str) -> dict[str, Any]:
        directory=self.runtime_root/"backups"/datetime.now(timezone.utc).strftime("%Y-%m-%d")
        directory.mkdir(parents=True,exist_ok=True)
        target=directory/f"companion-{datetime.now(timezone.utc).strftime('%H%M%S')}-{reason}.sqlite3"
        # sqlite backup is safe while the service writes; never copy a live WAL
        # file as though it were a database snapshot.
        source=sqlite3.connect(self.store.database); destination=sqlite3.connect(target)
        try: source.backup(destination)
        finally: destination.close(); source.close()
        digest=hashlib.sha256(target.read_bytes()).hexdigest(); backup_id=str(uuid.uuid4())
        state="local_only"; external=self.settings.get("external_path")
        if external:
            try:
                external_target=Path(str(external))/target.name; external_target.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(target,external_target); state="mirrored"
            except OSError: state="external_unavailable"
        with self.store.connection() as c:c.execute("INSERT INTO memory_backup(backup_id,path,sha256,database_kind,state,created_at) VALUES(?,?,?,?,?,?)",(backup_id,str(target),digest,"facts",state,now()))
        return {"backup_id":backup_id,"path":str(target),"state":state,"sha256":digest}

    def ensure_daily(self) -> dict[str, Any] | None:
        today=datetime.now(timezone.utc).date().isoformat()
        with self.store.connection() as c:
            exists=c.execute("SELECT 1 FROM memory_backup WHERE database_kind='facts' AND substr(created_at,1,10)=? LIMIT 1",(today,)).fetchone()
        return None if exists else self.create(reason="daily")

    def verify_restore(self, backup_id: str) -> dict[str, Any]:
        with self.store.connection() as c: row=c.execute("SELECT * FROM memory_backup WHERE backup_id=?",(backup_id,)).fetchone()
        if not row: raise ValueError("unknown backup")
        path=Path(row["path"]); con=sqlite3.connect(path)
        try:
            integrity=con.execute("PRAGMA integrity_check").fetchone()[0]
            count=con.execute("SELECT COUNT(*) FROM narrative_artifact").fetchone()[0]
        finally: con.close()
        if integrity!="ok": raise RuntimeError("backup integrity check failed")
        with self.store.connection() as c:c.execute("UPDATE memory_backup SET verified_at=?,state='verified',error=NULL WHERE backup_id=?",(now(),backup_id))
        return {"backup_id":backup_id,"integrity":integrity,"artifact_count":count}
