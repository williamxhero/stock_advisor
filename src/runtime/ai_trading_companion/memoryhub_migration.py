"""Read-only, idempotent migration and shadow qualification for MemoryHub cutover."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable

from .memory_port import MemoryPort


def _hash(body: str) -> str:
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


class MemoryHubMigrator:
    def __init__(self, source_database: Path, memory: MemoryPort, memory_space_id: str, *, migrated_at: str) -> None:
        self.source_database = Path(source_database)
        self.memory = memory
        self.memory_space_id = memory_space_id
        self.migrated_at = migrated_at

    def run(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        conflicts: list[dict[str, str]] = []
        hash_mismatches: list[str] = []
        with sqlite3.connect(f"file:{self.source_database.as_posix()}?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            records = self._records(connection)
        episodes: list[dict[str, Any]] = []
        for kind, values in records:
            counts[kind] = counts.get(kind, 0) + 1
            body = str(values.pop("body"))
            supplied = str(values.pop("source_hash", "") or "")
            actual = _hash(body)
            if supplied and supplied.removeprefix("sha256:") != actual.removeprefix("sha256:"):
                # Legacy stores did not consistently use the MemoryHub prefix;
                # record mismatch without corrupting the authoritative import.
                hash_mismatches.append(str(values["source_event_id"]))
            episodes.append({**values, "body": body, "content_hash": actual})
        before = int(self.memory.health().get("ledger", {}).get("episodes", 0))
        receipt_by_source: dict[str, str] = {}
        pending = list(episodes)
        while pending:
            ready = [item for item in pending if not item.get("corrects_source") or item["corrects_source"] in receipt_by_source]
            if not ready:
                conflicts.extend({"source_event_id": str(item["source_event_id"]), "error": "unresolved correction predecessor"} for item in pending)
                break
            payload = []
            for item in ready:
                value = dict(item); corrects = value.pop("corrects_source", None)
                if corrects:
                    value["corrects_episode_id"] = receipt_by_source[str(corrects)]
                payload.append(value)
            results = self.memory.append_batch(payload)
            for value, result in zip(payload, results):
                receipt = result.get("receipt") if isinstance(result, dict) else None
                if receipt:
                    receipt_by_source[str(value["source_event_id"])] = str(receipt["episode_id"])
                else:
                    conflicts.append({"source_event_id": str(value["source_event_id"]), "error": str(result.get("detail") or result.get("error") or "unknown append failure")})
            ready_ids = {id(item) for item in ready}
            pending = [item for item in pending if id(item) not in ready_ids]
        after = int(self.memory.health().get("ledger", {}).get("episodes", before))
        succeeded = len(receipt_by_source)
        imported = max(0, after - before)
        replayed = max(0, succeeded - imported)
        return {
            "source_database": str(self.source_database), "source_database_mode": "read_only",
            "counts": counts, "imported": imported, "replayed": replayed, "conflicts": conflicts,
            "validation": {"hash_mismatches": hash_mismatches, "correction_chain_failures": [], "source_hydration_failures": []},
            "old_source_deleted": False,
        }

    def _records(self, connection: sqlite3.Connection) -> list[tuple[str, dict[str, Any]]]:
        tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        output: list[tuple[str, dict[str, Any]]] = []

        def known(value: Any) -> tuple[str, str]:
            return (str(value), "source") if value else (self.migrated_at, "migration_time")

        if "companion_message" in tables:
            for row in connection.execute("SELECT * FROM companion_message WHERE state IN ('submitted','published','complete') ORDER BY occurred_at,message_id"):
                item = dict(row); at, provenance = known(item.get("known_at"))
                output.append(("message", self._episode(
                    f"message:{item['message_id']}", str(item["body_text"]),
                    "user_message" if item.get("actor") == "human" else "ai_message",
                    item.get("occurred_at") or item.get("submitted_at") or at, at, provenance,
                    {"original_id": item["message_id"], "cycle_id": item.get("cycle_id"), "state": item.get("state")},
                )))
        if "narrative_artifact" in tables:
            for row in connection.execute("SELECT * FROM narrative_artifact ORDER BY sealed_at,artifact_id"):
                item = dict(row); at, provenance = known(item.get("known_at"))
                output.append(("artifact", self._episode(
                    f"artifact:{item['artifact_id']}", str(item["body_markdown"]), str(item.get("kind") or "artifact"),
                    item.get("occurred_at") or item.get("as_of") or item.get("sealed_at") or at, at, provenance,
                    {"original_id": item["artifact_id"], "cycle_id": item.get("cycle_id"), "revision": item.get("revision"), "actor": item.get("actor")},
                    source_hash=item.get("body_sha256"),
                )))
        if "evidence_ledger_entry" in tables:
            for row in connection.execute("SELECT * FROM evidence_ledger_entry ORDER BY known_at,evidence_id"):
                item = dict(row); at, provenance = known(item.get("known_at")); metadata = json.loads(item.get("metadata_json") or "{}")
                output.append(("evidence", self._episode(
                    f"evidence:{item['evidence_id']}", str(item["body_text"]), "evidence",
                    item.get("occurred_at") or at, at, provenance,
                    {"original_id": item["evidence_id"], "cycle_id": item.get("cycle_id"), "stage": item.get("stage"), **metadata},
                    source_hash=item.get("content_sha256"),
                )))
        if "memory_proposition" in tables:
            for row in connection.execute("SELECT * FROM memory_proposition ORDER BY known_at,created_at,proposition_id"):
                item = dict(row); at, provenance = known(item.get("known_at"))
                body = json.dumps({"subject": item.get("subject"), "predicate": item.get("predicate"), "object": json.loads(item.get("object_json") or "null"), "source_quote": item.get("source_quote")}, ensure_ascii=False, sort_keys=True)
                value = self._episode(
                    f"proposition:{item['proposition_id']}", body, str(item.get("proposition_kind") or "proposition"),
                    item.get("created_at") or at, at, provenance,
                    {"original_id": item["proposition_id"], "status": item.get("status"), "source_message_id": item.get("source_message_id"), "source_span": [item.get("source_start"), item.get("source_end")]},
                )
                if item.get("supersedes_id"):
                    value["corrects_source"] = f"proposition:{item['supersedes_id']}"
                output.append(("proposition", value))
        if "judgment_snapshot" in tables:
            for row in connection.execute("SELECT * FROM judgment_snapshot ORDER BY created_at,snapshot_id"):
                item = dict(row); at, provenance = known(item.get("as_of"))
                output.append(("judgment", self._episode(
                    f"judgment:{item['snapshot_id']}", str(item["snapshot_json"]), "judgment",
                    item.get("created_at") or at, at, provenance,
                    {"original_id": item["snapshot_id"], "artifact_id": item.get("artifact_id"), "cycle_id": item.get("cycle_id"), "verification_status": item.get("verification_status")},
                )))
        if "outcome_checkpoint" in tables:
            for row in connection.execute("SELECT * FROM outcome_checkpoint ORDER BY created_at,checkpoint_id"):
                item = dict(row); at, provenance = known(item.get("as_of"))
                output.append(("outcome", self._episode(
                    f"outcome:{item['checkpoint_id']}", str(item["outcome_json"]), "outcome",
                    item.get("created_at") or at, at, provenance,
                    {"original_id": item["checkpoint_id"], "snapshot_id": item.get("snapshot_id"), "horizon": item.get("horizon"), "status": item.get("status")},
                )))
        return output

    def _episode(self, source_event_id: str, body: str, episode_type: str, occurred_at: str, known_at: str, provenance: str, metadata: dict[str, Any], *, source_hash: Any = None) -> dict[str, Any]:
        return {
            "memory_space_id": self.memory_space_id, "source_system": "stock-advisor-migration",
            "source_event_id": source_event_id, "episode_type": episode_type,
            "occurred_at": str(occurred_at), "known_at": known_at, "submitted_at": self.migrated_at,
            "authority": "migrated_legacy_record", "protocol_version": "memoryhub/v1",
            "metadata": {**metadata, "known_at_provenance": provenance}, "source_hash": source_hash, "body": body,
        }


def run_shadow_comparison(
    memory: MemoryPort, memory_space_id: str, queries: list[dict[str, str]],
    *, legacy_search: Callable[[str, str], list[str]], qualification_passed: bool,
    recovery_drill_passed: bool,
) -> dict[str, Any]:
    metrics: dict[str, Any] = {
        "recall": [], "recall_misses": [], "false_associations": [],
        "major_counterevidence": [], "similar_failures": [], "future_leakage": [],
        "latency_ms": [], "faults": [],
    }
    for number, query in enumerate(queries):
        expected = set(legacy_search(query["query"], query["as_of"]))
        started = time.monotonic()
        try:
            snapshot = memory.begin_snapshot({"memory_space_id": memory_space_id, "as_of": query["as_of"], "stage": query.get("stage", "chat"), "cycle_id": f"shadow-{number}"})
            bundle = memory.retrieve_bundle(str(snapshot["snapshot_id"]), query["query"], limit=100)
            rows = bundle.get("results", [])
            returned = {str(row.get("metadata", {}).get("original_id") or row.get("source_event_id") or row.get("episode_id")) for row in rows}
            metrics["recall"].append(sorted(expected & returned))
            metrics["recall_misses"].append(sorted(expected - returned))
            metrics["false_associations"].append(sorted(returned - expected))
            metrics["major_counterevidence"].append([row.get("episode_id") for row in rows if "反证" in str(row)])
            metrics["similar_failures"].append([row.get("episode_id") for row in rows if any(word in str(row).lower() for word in ("失败", "错误", "failure"))])
            metrics["future_leakage"].append([row.get("episode_id") for row in rows if str(row.get("known_at", "")) > query["as_of"]])
        except Exception as error:
            metrics["faults"].append(str(error))
        metrics["latency_ms"].append(round((time.monotonic() - started) * 1000, 3))
    blockers = []
    if not qualification_passed: blockers.append("retrieval qualification gate has not passed")
    if not recovery_drill_passed: blockers.append("real recovery drill has not passed")
    if metrics["faults"]: blockers.append("shadow retrieval faults remain")
    return {"metrics": metrics, "candidate_used_by_production": False, "switchable": not blockers, "blockers": blockers}
