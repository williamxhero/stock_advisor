from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import json
import hashlib
from pathlib import Path
import sqlite3
from typing import Any, Iterator
import uuid

from .secret_guard import assert_safe


PROTOCOL_VERSION = "memoryhub/v1"


class MemoryHubError(RuntimeError):
    pass


class EpisodeConflict(MemoryHubError):
    pass


class SourceIntegrityError(MemoryHubError):
    pass


@dataclass(frozen=True)
class AppendReceipt:
    episode_id: str
    sequence: int
    content_hash: str
    protocol_version: str = PROTOCOL_VERSION

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SnapshotReceipt:
    snapshot_id: str
    memory_space_id: str
    watermark: int
    as_of: str
    stage: str
    cycle_id: str | None
    policy_version: str = "memory-policy/v1"
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

    def __init__(self, database: Path | str, *, source_adapters: dict[str, Any] | None = None) -> None:
        self.database = Path(database)
        self.source_adapters = dict(source_adapters or {})
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
                CREATE TABLE IF NOT EXISTS memory_snapshot (
                    snapshot_id TEXT PRIMARY KEY,
                    memory_space_id TEXT NOT NULL,
                    watermark INTEGER NOT NULL,
                    as_of TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    cycle_id TEXT,
                    policy_version TEXT NOT NULL,
                    protocol_version TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS source_search_index (
                    episode_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    searchable_text TEXT NOT NULL,
                    FOREIGN KEY(episode_id) REFERENCES episode(episode_id)
                );
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
        if value.get("body"):
            assert_safe(str(value["body"]))

        value = dict(value)
        if value.get("body") and value["content_hash"] == "auto":
            value["content_hash"] = _content_hash(str(value["body"]))
        with self._connection() as connection:
            existing = connection.execute(
                """SELECT episode_id, sequence, content_hash, protocol_version
                   FROM episode WHERE memory_space_id=? AND source_system=? AND source_event_id=?""",
                (value["memory_space_id"], value["source_system"], value["source_event_id"]),
            ).fetchone()
            if existing:
                if value["content_hash"] != "auto" and existing["content_hash"] != value["content_hash"]:
                    raise EpisodeConflict("source event already exists with different content")
                return AppendReceipt(
                    existing["episode_id"], existing["sequence"],
                    existing["content_hash"], existing["protocol_version"],
                )

            hydrated: dict[str, str] | None = None
            if value.get("source_reference") and not value.get("body"):
                hydrated = self._hydrate(value["source_reference"])
                actual_hash = _content_hash(hydrated["body"])
                if value["content_hash"] not in {"auto", actual_hash}:
                    raise SourceIntegrityError("source content does not match supplied hash")
                value["content_hash"] = actual_hash

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
            if hydrated is not None:
                connection.execute(
                    "INSERT INTO source_search_index(episode_id,title,searchable_text) VALUES(?,?,?)",
                    (episode_id, hydrated["title"], hydrated["body"]),
                )
            return AppendReceipt(episode_id, int(cursor.lastrowid), value["content_hash"])

    def begin_snapshot(
        self, memory_space_id: str, *, as_of: str, stage: str, cycle_id: str | None = None
    ) -> SnapshotReceipt:
        if not memory_space_id or not as_of or not stage:
            raise MemoryHubError("snapshot requires memory_space_id, as_of and stage")
        snapshot = SnapshotReceipt(
            snapshot_id=str(uuid.uuid4()),
            memory_space_id=memory_space_id,
            watermark=self._watermark(memory_space_id),
            as_of=as_of,
            stage=stage,
            cycle_id=cycle_id,
        )
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO memory_snapshot(
                       snapshot_id,memory_space_id,watermark,as_of,stage,cycle_id,
                       policy_version,protocol_version) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    snapshot.snapshot_id, snapshot.memory_space_id, snapshot.watermark,
                    snapshot.as_of, snapshot.stage, snapshot.cycle_id,
                    snapshot.policy_version, snapshot.protocol_version,
                ),
            )
        return snapshot

    def search(self, snapshot_id: str, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        snapshot = self._snapshot(snapshot_id)
        terms = [term.casefold() for term in query.split() if term]
        rows = self._visible_rows(snapshot)
        source_text = self._source_search_text(row["episode_id"] for row in rows)
        scored: list[tuple[int, sqlite3.Row]] = []
        for row in rows:
            searchable = " ".join(
                (row["body"] or "", row["metadata_json"] or "", row["source_reference_json"] or "")
            ).casefold() + " " + source_text.get(row["episode_id"], "").casefold()
            score = sum(searchable.count(term) for term in terms) if terms else 1
            if score:
                scored.append((score, row))
        scored.sort(key=lambda item: (item[0], item[1]["sequence"]), reverse=True)
        return [self._card(row) for _, row in scored[: max(1, min(limit, 100))]]

    def expand(self, snapshot_id: str, episode_id: str) -> dict[str, Any]:
        snapshot = self._snapshot(snapshot_id)
        row = next((item for item in self._visible_rows(snapshot) if item["episode_id"] == episode_id), None)
        if row is None:
            raise MemoryHubError("episode is not visible in snapshot")
        result = self._episode(row)
        if result["body"] is None and result["source_reference"]:
            hydrated = self._hydrate(result["source_reference"])
            if _content_hash(hydrated["body"]) != result["content_hash"]:
                raise SourceIntegrityError("immutable source returned different content")
            result["title"] = hydrated["title"]
            result["body"] = hydrated["body"]
        return result

    def related(self, snapshot_id: str, episode_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        snapshot = self._snapshot(snapshot_id)
        rows = self._visible_rows(snapshot)
        target = next((row for row in rows if row["episode_id"] == episode_id), None)
        if target is None:
            raise MemoryHubError("episode is not visible in snapshot")
        target_metadata = json.loads(target["metadata_json"] or "{}")
        target_links = set(target_metadata.get("related_episode_ids") or [])
        result = []
        for row in rows:
            metadata = json.loads(row["metadata_json"] or "{}")
            linked = set(metadata.get("related_episode_ids") or [])
            if (
                row["corrects_episode_id"] == episode_id
                or target["corrects_episode_id"] == row["episode_id"]
                or row["episode_id"] in target_links
                or episode_id in linked
            ):
                result.append(self._card(row))
        return result[: max(1, min(limit, 100))]

    def health(self) -> dict[str, Any]:
        with self._connection() as connection:
            count = int(connection.execute("SELECT COUNT(*) FROM episode").fetchone()[0])
        return {
            "protocol_version": PROTOCOL_VERSION,
            "ledger": {"state": "ready", "episodes": count},
            "index": {"state": "not_configured"},
            "derivation": {"state": "not_configured"},
            "sources": {name: adapter.health() for name, adapter in self.source_adapters.items()},
        }

    def _hydrate(self, reference: dict[str, str]) -> dict[str, str]:
        source_system = reference.get("source_system", "")
        self._validate_reference(reference)
        adapter = self.source_adapters.get(source_system)
        if adapter is None:
            raise MemoryHubError(f"source adapter unavailable: {source_system}")
        return adapter.hydrate(reference)

    @staticmethod
    def _validate_reference(reference: dict[str, str]) -> None:
        common = ("source_system", "record_type", "date", "code")
        missing = [name for name in common if not reference.get(name)]
        if reference.get("source_system") == "8815" and not reference.get("event_id"):
            missing.append("event_id")
        if reference.get("source_system") not in {"markethub", "8815"}:
            missing.append("supported source_system")
        if missing:
            raise MemoryHubError("invalid immutable source reference: " + ", ".join(missing))

    def _source_search_text(self, episode_ids: Any) -> dict[str, str]:
        values = list(episode_ids)
        if not values:
            return {}
        placeholders = ",".join("?" for _ in values)
        with self._connection() as connection:
            return {
                row["episode_id"]: row["title"] + " " + row["searchable_text"]
                for row in connection.execute(
                    f"SELECT * FROM source_search_index WHERE episode_id IN ({placeholders})", values
                )
            }

    def _watermark(self, memory_space_id: str) -> int:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM episode WHERE memory_space_id=?",
                (memory_space_id,),
            ).fetchone()
        return int(row[0])

    def _snapshot(self, snapshot_id: str) -> SnapshotReceipt:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM memory_snapshot WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
        if row is None:
            raise MemoryHubError("snapshot does not exist")
        return SnapshotReceipt(
            row["snapshot_id"], row["memory_space_id"], row["watermark"], row["as_of"],
            row["stage"], row["cycle_id"], row["policy_version"], row["protocol_version"],
        )

    def _visible_rows(self, snapshot: SnapshotReceipt) -> list[sqlite3.Row]:
        with self._connection() as connection:
            rows = list(
                connection.execute(
                    """SELECT * FROM episode WHERE memory_space_id=? AND sequence<=? AND known_at<=?
                       ORDER BY sequence DESC""",
                    (snapshot.memory_space_id, snapshot.watermark, snapshot.as_of),
                )
            )
        return [row for row in rows if self._policy_allows(snapshot, row)]

    @staticmethod
    def _policy_allows(snapshot: SnapshotReceipt, row: sqlite3.Row) -> bool:
        if snapshot.stage not in {"m1_research", "m1_judgment"}:
            return True
        metadata = json.loads(row["metadata_json"] or "{}")
        if metadata.get("cycle_id") != snapshot.cycle_id:
            return True
        return not (
            metadata.get("stage") in {"h0", "premarket"}
            or metadata.get("actor") == "human"
            or row["episode_type"] in {"h0", "h0_proposition", "h0_action"}
        )

    @staticmethod
    def _card(row: sqlite3.Row) -> dict[str, Any]:
        body = row["body"] or ""
        return {
            "episode_id": row["episode_id"],
            "sequence": row["sequence"],
            "episode_type": row["episode_type"],
            "summary": body[:500],
            "occurred_at": row["occurred_at"],
            "known_at": row["known_at"],
            "authority": row["authority"],
            "source_reference": _loads(row["source_reference_json"]),
            "corrects_episode_id": row["corrects_episode_id"],
        }

    @staticmethod
    def _episode(row: sqlite3.Row) -> dict[str, Any]:
        return {
            **MemoryHub._card(row),
            "body": row["body"],
            "content_hash": row["content_hash"],
            "source_system": row["source_system"],
            "source_event_id": row["source_event_id"],
            "submitted_at": row["submitted_at"],
            "metadata": _loads(row["metadata_json"]) or {},
            "protocol_version": row["protocol_version"],
        }


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(value: str | None) -> Any:
    return None if value is None else json.loads(value)


def _content_hash(body: str) -> str:
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
