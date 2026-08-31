from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator
import uuid


PROTOCOL_VERSION = "memoryhub/v1"


class MemoryHubError(RuntimeError):
    pass


class EpisodeConflict(MemoryHubError):
    pass


@dataclass(frozen=True)
class AppendReceipt:
    episode_id: str
    sequence: int
    content_hash: str
    protocol_version: str = PROTOCOL_VERSION

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class MemoryHub:
    """Authority for immutable Episodes behind the versioned MemoryHub interface."""

    _required = (
        "memory_space_id", "source_system", "source_event_id", "content_hash",
        "episode_type", "occurred_at", "known_at", "submitted_at", "authority",
        "protocol_version",
    )

    def __init__(self, database: Path | str) -> None:
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS episode (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    episode_id TEXT NOT NULL UNIQUE,
                    memory_space_id TEXT NOT NULL,
                    source_system TEXT NOT NULL,
                    source_event_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    episode_type TEXT NOT NULL,
                    body TEXT,
                    source_reference_json TEXT,
                    occurred_at TEXT NOT NULL,
                    known_at TEXT NOT NULL,
                    submitted_at TEXT NOT NULL,
                    authority TEXT NOT NULL,
                    original_span_json TEXT,
                    corrects_episode_id TEXT,
                    protocol_version TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    UNIQUE(memory_space_id, source_system, source_event_id),
                    FOREIGN KEY(corrects_episode_id) REFERENCES episode(episode_id)
                );
                CREATE INDEX IF NOT EXISTS ix_episode_space_sequence
                    ON episode(memory_space_id, sequence);
                CREATE INDEX IF NOT EXISTS ix_episode_space_known
                    ON episode(memory_space_id, known_at, sequence);
                """
            )

    def append(self, value: dict[str, Any]) -> AppendReceipt:
        missing = [name for name in self._required if value.get(name) in (None, "")]
        if missing:
            raise MemoryHubError("missing episode fields: " + ", ".join(missing))
        if value["protocol_version"] != PROTOCOL_VERSION:
            raise MemoryHubError(f"unsupported protocol: {value['protocol_version']}")
        if not value.get("body") and not value.get("source_reference"):
            raise MemoryHubError("episode requires body or source_reference")

        with self._connection() as connection:
            existing = connection.execute(
                """SELECT episode_id, sequence, content_hash, protocol_version
                   FROM episode WHERE memory_space_id=? AND source_system=? AND source_event_id=?""",
                (value["memory_space_id"], value["source_system"], value["source_event_id"]),
            ).fetchone()
            if existing:
                if existing["content_hash"] != value["content_hash"]:
                    raise EpisodeConflict("source event already exists with different content")
                return AppendReceipt(
                    existing["episode_id"], existing["sequence"],
                    existing["content_hash"], existing["protocol_version"],
                )

            correction = value.get("corrects_episode_id")
            if correction and not connection.execute(
                "SELECT 1 FROM episode WHERE episode_id=? AND memory_space_id=?",
                (correction, value["memory_space_id"]),
            ).fetchone():
                raise MemoryHubError("corrected episode does not exist in memory space")

            episode_id = str(value.get("episode_id") or uuid.uuid4())
            cursor = connection.execute(
                """INSERT INTO episode(
                       episode_id,memory_space_id,source_system,source_event_id,content_hash,
                       episode_type,body,source_reference_json,occurred_at,known_at,submitted_at,
                       authority,original_span_json,corrects_episode_id,protocol_version,metadata_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    episode_id, value["memory_space_id"], value["source_system"],
                    value["source_event_id"], value["content_hash"], value["episode_type"],
                    value.get("body"), _json(value.get("source_reference")),
                    value["occurred_at"], value["known_at"], value["submitted_at"],
                    value["authority"], _json(value.get("original_span")), correction,
                    PROTOCOL_VERSION, _json(value.get("metadata") or {}),
                ),
            )
            return AppendReceipt(episode_id, int(cursor.lastrowid), value["content_hash"])

    def health(self) -> dict[str, Any]:
        with self._connection() as connection:
            count = int(connection.execute("SELECT COUNT(*) FROM episode").fetchone()[0])
        return {
            "protocol_version": PROTOCOL_VERSION,
            "ledger": {"state": "ready", "episodes": count},
            "index": {"state": "not_configured"},
            "derivation": {"state": "not_configured"},
            "sources": {},
        }


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, sort_keys=True)

