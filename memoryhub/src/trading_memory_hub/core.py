from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import json
import hashlib
from pathlib import Path
import sqlite3
from typing import Any, Iterator
import uuid
import time

from .secret_guard import assert_safe


PROTOCOL_VERSION = "memoryhub/v1"
ALLOWED_STAGES = {
    "chat", "chat_research", "m0_research", "m0_compose", "m1_research",
    "m1_judgment", "m2_synthesis", "reflection", "workflow_feedback",
}


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
                CREATE TABLE IF NOT EXISTS derivation_job (
                    episode_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    error TEXT,
                    extractor_version TEXT,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(episode_id) REFERENCES episode(episode_id)
                );
                CREATE TABLE IF NOT EXISTS derived_memory (
                    episode_id TEXT PRIMARY KEY,
                    extractor_version TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    FOREIGN KEY(episode_id) REFERENCES episode(episode_id)
                );
                CREATE TABLE IF NOT EXISTS event_link (
                    left_episode_id TEXT NOT NULL,
                    right_episode_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    PRIMARY KEY(left_episode_id,right_episode_id)
                );
                CREATE TABLE IF NOT EXISTS clear_request (
                    confirmation_token TEXT PRIMARY KEY,
                    memory_space_id TEXT NOT NULL,
                    export_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL,
                    result_json TEXT
                );
                CREATE TABLE IF NOT EXISTS retrieval_bundle (
                    bundle_id TEXT PRIMARY KEY,
                    snapshot_id TEXT NOT NULL,
                    audit_id TEXT NOT NULL UNIQUE,
                    value_json TEXT NOT NULL,
                    FOREIGN KEY(snapshot_id) REFERENCES memory_snapshot(snapshot_id)
                );
                CREATE TABLE IF NOT EXISTS retrieval_audit (
                    audit_id TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS frozen_query_set (
                    query_set_id TEXT PRIMARY KEY,
                    memory_space_id TEXT NOT NULL,
                    value_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS qualification_report (
                    report_id TEXT PRIMARY KEY,
                    qualified INTEGER NOT NULL,
                    value_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS retrieval_configuration (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    active_retriever TEXT NOT NULL
                );
                INSERT OR IGNORE INTO retrieval_configuration(singleton,active_retriever)
                    VALUES(1,'sqlite-lexical/v1');
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
        for name in ("occurred_at", "known_at", "submitted_at"):
            value[name] = _canonical_time(str(value[name]))
        if value.get("body"):
            actual_body_hash = _content_hash(str(value["body"]))
            if value["content_hash"] == "auto":
                value["content_hash"] = actual_body_hash
            elif value["content_hash"] != actual_body_hash:
                raise SourceIntegrityError("episode body does not match supplied hash")
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
            connection.execute(
                "INSERT INTO derivation_job(episode_id,state,error,extractor_version,updated_at) VALUES(?,'pending',NULL,NULL,?)",
                (episode_id, value["submitted_at"]),
            )
            return AppendReceipt(episode_id, int(cursor.lastrowid), value["content_hash"])

    def append_batch(self, values: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(values, list) or len(values) > 1000:
            raise MemoryHubError("append batch must contain at most 1000 episodes")
        results: list[dict[str, Any]] = []
        for value in values:
            try:
                results.append({"receipt": self.append(value).as_dict()})
            except Exception as error:
                results.append({"error": type(error).__name__, "detail": str(error)})
        return results

    def derive_pending(
        self, extractor: Any, *, extractor_version: str, limit: int = 20
    ) -> int:
        with self._connection() as connection:
            jobs = list(
                connection.execute(
                    """SELECT j.episode_id,e.body,s.searchable_text,e.submitted_at
                       FROM derivation_job j JOIN episode e ON e.episode_id=j.episode_id
                       LEFT JOIN source_search_index s ON s.episode_id=e.episode_id
                       WHERE j.state='pending' ORDER BY e.sequence LIMIT ?""",
                    (max(1, min(limit, 100)),),
                )
            )
        completed = 0
        for job in jobs:
            text = str(job["body"] or job["searchable_text"] or "")
            try:
                value = extractor(text)
                self._validate_derivation(text, value)
                with self._connection() as connection:
                    connection.execute(
                        """INSERT OR REPLACE INTO derived_memory(episode_id,extractor_version,value_json)
                           VALUES(?,?,?)""",
                        (job["episode_id"], extractor_version, _json(value)),
                    )
                    connection.execute(
                        """UPDATE derivation_job SET state='complete',error=NULL,extractor_version=?,updated_at=?
                           WHERE episode_id=?""",
                        (extractor_version, job["submitted_at"], job["episode_id"]),
                    )
                completed += 1
            except Exception as error:
                with self._connection() as connection:
                    connection.execute(
                        """UPDATE derivation_job SET state='failed',error=?,extractor_version=?,updated_at=?
                           WHERE episode_id=?""",
                        (str(error)[:1000], extractor_version, job["submitted_at"], job["episode_id"]),
                    )
        return completed

    def derived_memory(self, episode_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM derived_memory WHERE episode_id=?", (episode_id,)
            ).fetchone()
        if row is None:
            raise MemoryHubError("derived memory is unavailable")
        return {**json.loads(row["value_json"]), "extractor_version": row["extractor_version"], "episode_id": episode_id}

    def link_events(self, left_episode_id: str, right_episode_id: str) -> dict[str, str]:
        left, right = sorted((left_episode_id, right_episode_id))
        if left == right:
            return {"status": "confirmed", "reason": "same_episode"}
        records = self._event_records(left, right)
        status, reason = self._event_link_status(records[left], records[right])
        with self._connection() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO event_link(left_episode_id,right_episode_id,status,reason) VALUES(?,?,?,?)",
                (left, right, status, reason),
            )
        return {"status": status, "reason": reason}

    def independent_source_count(self, episode_ids: list[str]) -> int:
        parent = {episode_id: episode_id for episode_id in episode_ids}

        def root(value: str) -> str:
            while parent[value] != value:
                parent[value] = parent[parent[value]]
                value = parent[value]
            return value

        placeholders = ",".join("?" for _ in episode_ids)
        if not placeholders:
            return 0
        with self._connection() as connection:
            links = list(
                connection.execute(
                    f"""SELECT * FROM event_link WHERE status='confirmed'
                        AND left_episode_id IN ({placeholders}) AND right_episode_id IN ({placeholders})""",
                    [*episode_ids, *episode_ids],
                )
            )
        for link in links:
            left_root, right_root = root(link["left_episode_id"]), root(link["right_episode_id"])
            if left_root != right_root:
                parent[right_root] = left_root
        return len({root(value) for value in episode_ids})

    def rebuild_indexes(self) -> dict[str, int]:
        with self._connection() as connection:
            source_rows = list(
                connection.execute(
                    "SELECT episode_id,content_hash,source_reference_json FROM episode WHERE source_reference_json IS NOT NULL"
                )
            )
        rebuilt: list[tuple[str, str, str]] = []
        for row in source_rows:
            reference = _loads(row["source_reference_json"])
            hydrated = self._hydrate(reference)
            if _content_hash(hydrated["body"]) != row["content_hash"]:
                raise SourceIntegrityError("source changed during index rebuild")
            rebuilt.append((row["episode_id"], hydrated["title"], hydrated["body"]))
        with self._connection() as connection:
            connection.execute("DELETE FROM source_search_index")
            connection.execute("DELETE FROM derived_memory")
            connection.execute("DELETE FROM event_link")
            connection.execute(
                "UPDATE derivation_job SET state='pending',error=NULL,extractor_version=NULL"
            )
            connection.executemany(
                "INSERT INTO source_search_index(episode_id,title,searchable_text) VALUES(?,?,?)", rebuilt
            )
        return {"source_documents": len(rebuilt), "derivation_jobs": self.health()["ledger"]["episodes"]}

    def begin_snapshot(
        self, memory_space_id: str, *, as_of: str, stage: str, cycle_id: str | None = None
    ) -> SnapshotReceipt:
        if not memory_space_id or not as_of or not stage:
            raise MemoryHubError("snapshot requires memory_space_id, as_of and stage")
        if stage not in ALLOWED_STAGES:
            raise MemoryHubError(f"unsupported access-policy stage: {stage}")
        as_of = _canonical_time(as_of)
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
        selected = [row for _, row in scored[: max(1, min(limit, 100))]]
        derived = self._derived_summaries(row["episode_id"] for row in selected)
        cards = [self._card(row) for row in selected]
        for card in cards:
            if card["episode_id"] in derived:
                card["derived_summary"] = derived[card["episode_id"]]
        return cards

    def retrieve_bundle(self, snapshot_id: str, query: str, *, limit: int = 20) -> dict[str, Any]:
        """Return replayable retrieval output and persist operational audit separately from memory."""
        started = time.monotonic()
        snapshot = self._snapshot(snapshot_id)
        visible = self._visible_rows(snapshot)
        results = self.search(snapshot_id, query, limit=limit)
        result_ids = [str(item["episode_id"]) for item in results]
        with self._connection() as connection:
            future = [
                str(row["episode_id"]) for row in connection.execute(
                    """SELECT episode_id FROM episode
                       WHERE memory_space_id=? AND sequence<=? AND known_at>? ORDER BY sequence""",
                    (snapshot.memory_space_id, snapshot.watermark, snapshot.as_of),
                )
            ]
            active = str(connection.execute(
                "SELECT active_retriever FROM retrieval_configuration WHERE singleton=1"
            ).fetchone()[0])
        versions = {
            "policy": snapshot.policy_version, "retriever": active,
            "index": "source-search/v1", "extractor": "derived-memory/v1",
            "protocol": snapshot.protocol_version,
        }
        audit_id, bundle_id = str(uuid.uuid4()), str(uuid.uuid4())
        audit = {
            "audit_id": audit_id, "snapshot_id": snapshot_id, "query": query,
            "candidate_episode_ids": [str(row["episode_id"]) for row in visible],
            "final_episode_ids": result_ids,
            "excluded": {"future_knowledge": future},
            "expanded_original_episode_ids": [],
            "latency_ms": round((time.monotonic() - started) * 1000, 3), "fault": None,
            "versions": versions,
        }
        bundle = {
            "bundle_id": bundle_id, "audit_id": audit_id,
            "snapshot": snapshot.as_dict(), "versions": versions, "query": query,
            "results": results,
        }
        with self._connection() as connection:
            connection.execute("INSERT INTO retrieval_audit(audit_id,value_json) VALUES(?,?)", (audit_id, _json(audit)))
            connection.execute(
                "INSERT INTO retrieval_bundle(bundle_id,snapshot_id,audit_id,value_json) VALUES(?,?,?,?)",
                (bundle_id, snapshot_id, audit_id, _json(bundle)),
            )
        return bundle

    def retrieval_audit(self, audit_id: str) -> dict[str, Any]:
        return self._governance_value("retrieval_audit", "audit_id", audit_id)

    def replay_bundle(self, bundle_id: str) -> dict[str, Any]:
        return self._governance_value("retrieval_bundle", "bundle_id", bundle_id)

    def create_frozen_query_set(self, memory_space_id: str, queries: list[dict[str, Any]]) -> dict[str, Any]:
        if not queries:
            raise MemoryHubError("frozen query set requires queries")
        snapshots = [self._snapshot(str(item.get("snapshot_id") or "")) for item in queries]
        if any(item.memory_space_id != memory_space_id for item in snapshots):
            raise MemoryHubError("query snapshot belongs to another memory space")
        query_set_id = str(uuid.uuid4())
        value = {
            "query_set_id": query_set_id, "memory_space_id": memory_space_id,
            "queries": queries,
            "frozen_contexts": [
                {"snapshot_id": item.snapshot_id, "watermark": item.watermark,
                 "as_of": item.as_of, "policy_version": item.policy_version}
                for item in snapshots
            ],
        }
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO frozen_query_set(query_set_id,memory_space_id,value_json) VALUES(?,?,?)",
                (query_set_id, memory_space_id, _json(value)),
            )
        return value

    def evaluate_candidate(self, query_set_id: str, candidate: dict[str, Any]) -> dict[str, Any]:
        frozen = self._governance_value("frozen_query_set", "query_set_id", query_set_id)
        if candidate.get("adapter") not in {"graphiti", "mem0"}:
            raise MemoryHubError("candidate adapter must be graphiti or mem0")
        runs = candidate.get("runs")
        if not isinstance(runs, list) or len(runs) != len(frozen["queries"]):
            raise MemoryHubError("candidate must provide one run per frozen query")
        misses: list[str] = []
        false: list[str] = []
        counter_misses: list[str] = []
        future: list[str] = []
        latencies: list[float] = []
        faults: list[str] = []
        for query, context, run in zip(frozen["queries"], frozen["frozen_contexts"], runs):
            returned = {str(value) for value in run.get("episode_ids", [])}
            expected = {str(value) for value in query.get("expected_episode_ids", [])}
            counter = {str(value) for value in query.get("major_counterevidence_ids", [])}
            misses.extend(sorted(expected - returned))
            false.extend(sorted(returned - expected - counter))
            counter_misses.extend(sorted(counter - returned))
            with self._connection() as connection:
                if returned:
                    marks = ",".join("?" for _ in returned)
                    rows = connection.execute(
                        f"SELECT episode_id,sequence,known_at FROM episode WHERE episode_id IN ({marks})", tuple(returned)
                    )
                    future.extend(str(row["episode_id"]) for row in rows if row["sequence"] > context["watermark"] or row["known_at"] > context["as_of"])
            latencies.append(float(run.get("latency_ms") or 0))
            if run.get("fault"):
                faults.append(str(run["fault"]))
        metrics = {
            "recall_misses": sorted(set(misses)), "false_associations": sorted(set(false)),
            "major_counterevidence_misses": sorted(set(counter_misses)),
            "future_leakage": sorted(set(future)),
            "latency_ms": latencies, "faults": faults,
        }
        qualified = not any((misses, false, counter_misses, future, faults))
        report_id = str(uuid.uuid4())
        report = {
            "report_id": report_id, "query_set_id": query_set_id,
            "adapter": candidate["adapter"], "retriever_version": candidate.get("retriever_version"),
            "metrics": metrics, "qualified": qualified,
            "evidence_rule": "derived clusters, summaries and model propositions are not independent evidence",
        }
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO qualification_report(report_id,qualified,value_json) VALUES(?,?,?)",
                (report_id, int(qualified), _json(report)),
            )
        return report

    def promote_candidate(self, report_id: str) -> dict[str, Any]:
        report = self._governance_value("qualification_report", "report_id", report_id)
        if not report["qualified"]:
            raise ValueError("candidate is not qualified")
        version = str(report.get("retriever_version") or "")
        if not version:
            raise MemoryHubError("qualified candidate has no retriever version")
        with self._connection() as connection:
            connection.execute(
                "UPDATE retrieval_configuration SET active_retriever=? WHERE singleton=1", (version,)
            )
        return {"active_retriever": version, "report_id": report_id}

    def _governance_value(self, table: str, key: str, value: str) -> dict[str, Any]:
        allowed = {
            ("retrieval_audit", "audit_id"), ("retrieval_bundle", "bundle_id"),
            ("frozen_query_set", "query_set_id"), ("qualification_report", "report_id"),
        }
        if (table, key) not in allowed:
            raise MemoryHubError("unsupported governance record")
        with self._connection() as connection:
            row = connection.execute(f"SELECT value_json FROM {table} WHERE {key}=?", (value,)).fetchone()
        if row is None:
            raise MemoryHubError("governance record does not exist")
        return json.loads(row["value_json"])

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

    def timeline(self, memory_space_id: str, *, after_sequence: int = 0) -> list[dict[str, Any]]:
        if not memory_space_id:
            raise MemoryHubError("timeline requires memory_space_id")
        with self._connection() as connection:
            rows = list(
                connection.execute(
                    """SELECT * FROM episode
                       WHERE memory_space_id=? AND sequence>? AND episode_type IN ('user_message','ai_message')
                       ORDER BY sequence""",
                    (memory_space_id, max(0, int(after_sequence))),
                )
            )
        return [self._episode(row) for row in rows]

    def admin_memory_spaces(self) -> list[dict[str, Any]]:
        """Return read-only operator summaries without exposing storage layout."""
        with self._connection() as connection:
            rows = list(connection.execute(
                """SELECT memory_space_id,COUNT(*) AS episode_count,
                          MAX(sequence) AS latest_sequence,MAX(submitted_at) AS latest_submitted_at
                   FROM episode GROUP BY memory_space_id ORDER BY latest_sequence DESC"""
            ))
        return [dict(row) for row in rows]

    def admin_episodes(
        self, memory_space_id: str, *, cursor: int | None = None, limit: int = 50
    ) -> dict[str, Any]:
        if not memory_space_id:
            raise MemoryHubError("admin episode listing requires memory_space_id")
        page_size = max(1, min(int(limit), 200))
        clauses = ["memory_space_id=?"]
        values: list[Any] = [memory_space_id]
        if cursor is not None:
            clauses.append("sequence<?")
            values.append(max(1, int(cursor)))
        values.append(page_size + 1)
        with self._connection() as connection:
            rows = list(connection.execute(
                f"SELECT * FROM episode WHERE {' AND '.join(clauses)} ORDER BY sequence DESC LIMIT ?",
                values,
            ))
        has_more = len(rows) > page_size
        items = [self._episode(row) for row in rows[:page_size]]
        return {
            "items": items,
            "next_cursor": str(items[-1]["sequence"]) if has_more else None,
            "protocol_version": PROTOCOL_VERSION,
        }

    def export_space(self, memory_space_id: str) -> dict[str, Any]:
        if not memory_space_id:
            raise MemoryHubError("export requires memory_space_id")
        with self._connection() as connection:
            return self._space_export(connection, memory_space_id)

    def _space_export(self, connection: sqlite3.Connection, memory_space_id: str) -> dict[str, Any]:
        episodes = [
            self._episode(row) for row in connection.execute(
                "SELECT * FROM episode WHERE memory_space_id=? ORDER BY sequence",
                (memory_space_id,),
            )
        ]
        machine = {
            "memory_space_id": memory_space_id,
            "protocol_version": PROTOCOL_VERSION,
            "episodes": episodes,
        }
        canonical = json.dumps(machine, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        human_lines = [f"# MemoryHub export: {memory_space_id}", ""]
        for item in episodes:
            human_lines.extend([
                f"## {item['sequence']}. {item['episode_type']}",
                f"- occurred_at: {item['occurred_at']}",
                f"- known_at: {item['known_at']}",
                f"- source: {item['source_system']} / {item['source_event_id']}",
                "", str(item.get("body") or "[public source reference]"), "",
            ])
        return {
            **machine,
            "human_markdown": "\n".join(human_lines).rstrip() + "\n",
            "export_sha256": _content_hash(canonical),
        }

    def prepare_clear(self, memory_space_id: str, export_sha256: str) -> dict[str, Any]:
        current = self.export_space(memory_space_id)
        if current["export_sha256"] != export_sha256:
            raise MemoryHubError("clear requires a fresh successful export")
        token = str(uuid.uuid4())
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO clear_request(confirmation_token,memory_space_id,export_sha256,state) VALUES(?,?,?,'pending')",
                (token, memory_space_id, export_sha256),
            )
        return {
            "confirmation_token": token, "memory_space_id": memory_space_id,
            "export_sha256": export_sha256, "state": "confirmation_required",
        }

    def clear_space(self, memory_space_id: str, confirmation_token: str) -> dict[str, Any]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            request = connection.execute(
                "SELECT * FROM clear_request WHERE confirmation_token=? AND memory_space_id=?",
                (confirmation_token, memory_space_id),
            ).fetchone()
            if request is None:
                raise MemoryHubError("clear confirmation is invalid")
            if request["state"] == "applied":
                return json.loads(request["result_json"])
            if request["state"] != "pending":
                raise MemoryHubError("clear confirmation is no longer valid")
            current = self._space_export(connection, memory_space_id)
            if current["export_sha256"] != request["export_sha256"]:
                raise MemoryHubError("memory changed after export; export again before clearing")
            episode_ids = [item["episode_id"] for item in current["episodes"]]
            snapshot_rows = list(connection.execute(
                "SELECT snapshot_id FROM memory_snapshot WHERE memory_space_id=?", (memory_space_id,)
            ))
            snapshot_ids = [str(row["snapshot_id"]) for row in snapshot_rows]
            if snapshot_ids:
                snapshot_marks = ",".join("?" for _ in snapshot_ids)
                audits = [str(row["audit_id"]) for row in connection.execute(
                    f"SELECT audit_id FROM retrieval_bundle WHERE snapshot_id IN ({snapshot_marks})", snapshot_ids
                )]
                connection.execute(
                    f"DELETE FROM retrieval_bundle WHERE snapshot_id IN ({snapshot_marks})", snapshot_ids
                )
                if audits:
                    audit_marks = ",".join("?" for _ in audits)
                    connection.execute(f"DELETE FROM retrieval_audit WHERE audit_id IN ({audit_marks})", audits)
            if episode_ids:
                marks = ",".join("?" for _ in episode_ids)
                connection.execute(
                    f"DELETE FROM event_link WHERE left_episode_id IN ({marks}) OR right_episode_id IN ({marks})",
                    [*episode_ids, *episode_ids],
                )
                for table in ("derived_memory", "derivation_job", "source_search_index"):
                    connection.execute(f"DELETE FROM {table} WHERE episode_id IN ({marks})", episode_ids)
            connection.execute("DELETE FROM memory_snapshot WHERE memory_space_id=?", (memory_space_id,))
            connection.execute("DELETE FROM episode WHERE memory_space_id=?", (memory_space_id,))
            result = {
                "memory_space_id": memory_space_id, "state": "cleared",
                "deleted_episodes": len(episode_ids), "export_sha256": request["export_sha256"],
            }
            connection.execute(
                "UPDATE clear_request SET state='invalidated' WHERE memory_space_id=? AND state='pending'",
                (memory_space_id,),
            )
            connection.execute(
                "UPDATE clear_request SET state='applied',result_json=? WHERE confirmation_token=?",
                (json.dumps(result, ensure_ascii=False, sort_keys=True), confirmation_token),
            )
        return result

    def health(self) -> dict[str, Any]:
        with self._connection() as connection:
            count = int(connection.execute("SELECT COUNT(*) FROM episode").fetchone()[0])
            try:
                source_index_count = int(
                    connection.execute("SELECT COUNT(*) FROM source_search_index").fetchone()[0]
                )
                index_health = {
                    "state": "ready", "source_documents": source_index_count,
                    "ledger_documents": count,
                }
            except sqlite3.Error as error:
                index_health = {"state": "unavailable", "detail": str(error)}
            try:
                derivation = {
                    row["state"]: row["count"]
                    for row in connection.execute(
                        "SELECT state,COUNT(*) AS count FROM derivation_job GROUP BY state"
                    )
                }
            except sqlite3.Error:
                derivation = {"failed": 1}
        return {
            "protocol_version": PROTOCOL_VERSION,
            "ledger": {"state": "ready", "episodes": count},
            "index": index_health,
            "derivation": {
                "state": "degraded" if derivation.get("failed") else "ready",
                "pending": derivation.get("pending", 0),
                "complete": derivation.get("complete", 0),
                "failed": derivation.get("failed", 0),
            },
            "sources": {name: self._source_health(adapter) for name, adapter in self.source_adapters.items()},
        }

    @staticmethod
    def _source_health(adapter: Any) -> dict[str, Any]:
        try:
            return adapter.health()
        except Exception as error:
            return {"state": "unavailable", "detail": str(error)}

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

    def _derived_summaries(self, episode_ids: Any) -> dict[str, str]:
        values = list(episode_ids)
        if not values:
            return {}
        placeholders = ",".join("?" for _ in values)
        with self._connection() as connection:
            rows = list(connection.execute(
                f"SELECT episode_id,value_json FROM derived_memory WHERE episode_id IN ({placeholders})", values
            ))
        return {
            row["episode_id"]: str((json.loads(row["value_json"]) or {}).get("summary") or "")
            for row in rows
        }

    @staticmethod
    def _validate_derivation(text: str, value: Any) -> None:
        if not isinstance(value, dict):
            raise MemoryHubError("extractor result must be an object")
        for proposition in value.get("propositions") or []:
            span = proposition.get("span") if isinstance(proposition, dict) else None
            if not span or str(span) not in text:
                raise MemoryHubError("derived proposition must cite an exact source span")

    def _event_records(self, *episode_ids: str) -> dict[str, sqlite3.Row]:
        placeholders = ",".join("?" for _ in episode_ids)
        with self._connection() as connection:
            records = {
                row["episode_id"]: row
                for row in connection.execute(
                    f"SELECT * FROM episode WHERE episode_id IN ({placeholders})", episode_ids
                )
            }
        if len(records) != len(set(episode_ids)):
            raise MemoryHubError("event link episode does not exist")
        return records

    @staticmethod
    def _event_link_status(left: sqlite3.Row, right: sqlite3.Row) -> tuple[str, str]:
        if left["content_hash"] == right["content_hash"]:
            return "confirmed", "content_hash"
        left_metadata, right_metadata = _loads(left["metadata_json"]) or {}, _loads(right["metadata_json"]) or {}
        if left_metadata.get("original_url") and left_metadata.get("original_url") == right_metadata.get("original_url"):
            return "confirmed", "original_url"
        left_reference, right_reference = _loads(left["source_reference_json"]) or {}, _loads(right["source_reference_json"]) or {}
        if (
            left_reference.get("event_id")
            and left_reference.get("source_system") == right_reference.get("source_system")
            and left_reference.get("event_id") == right_reference.get("event_id")
        ):
            return "confirmed", "source_event_id"
        return "candidate", "semantic_similarity_only"

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
            or row["episode_type"] in {"user_message", "h0", "h0_proposition", "h0_action"}
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
            "original_span": _loads(row["original_span_json"]),
            "metadata": _loads(row["metadata_json"]) or {},
            "protocol_version": row["protocol_version"],
        }


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(value: str | None) -> Any:
    return None if value is None else json.loads(value)


def _content_hash(body: str) -> str:
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def _canonical_time(value: str) -> str:
    candidate = value.strip()
    try:
        if len(candidate) == 10:
            parsed = datetime.combine(date.fromisoformat(candidate), datetime.min.time(), tzinfo=timezone.utc)
        else:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError as error:
        raise MemoryHubError(f"invalid timestamp: {value}") from error
    if parsed.tzinfo is None:
        raise MemoryHubError(f"timestamp requires timezone: {value}")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
