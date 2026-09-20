"""Immutable, replayable evidence snapshots owned by the runtime.

The snapshot is a public evidence boundary.  It is deliberately separate from
the model's Evidence v3 output and from judgment snapshots: models may produce
the former, while the runtime freezes the latter for every consumer in a
decision cycle.
"""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from .cycle_contract import validate_m1_blind_packet

VERSION = "EvidenceSnapshotSpec/v1"
SCHEMA_VERSION = 1


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _timestamp(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    normalized = parsed.astimezone(timezone.utc)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _normalized_watermarks(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("source_watermarks must be an object")
    # Deep-copying and sorting keys makes the frozen envelope independent of
    # caller mutation and stable across Python versions.
    return {str(key): copy.deepcopy(value[key]) for key in sorted(value, key=str)}


def derive_source_watermarks(
    evidence: dict[str, Any], observations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Derive deterministic source clocks without asking an LLM to name them."""
    explicit = evidence.get("source_watermarks")
    result = _normalized_watermarks(explicit)
    for observation in observations or []:
        if not isinstance(observation, dict):
            continue
        source = str(
            observation.get("source")
            or observation.get("backend")
            or observation.get("operation")
            or ""
        ).strip()
        watermark = (
            observation.get("source_watermark")
            or observation.get("watermark")
            or observation.get("acquired_at")
            or observation.get("observed_at")
        )
        if source and watermark is not None:
            current = result.get(source)
            if current is None or str(watermark) > str(current):
                result[source] = copy.deepcopy(watermark)
    if not result:
        for source in evidence.get("sources") or []:
            if not isinstance(source, dict):
                continue
            key = str(source.get("evidence_ref") or source.get("url") or "").strip()
            watermark = source.get("known_at") or source.get("published_at") or source.get("fact_as_of")
            if key and watermark is not None:
                result[key] = copy.deepcopy(watermark)
    return {key: result[key] for key in sorted(result)}


def _identity(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "contract": snapshot["contract"],
        "schema_version": snapshot["schema_version"],
        "cycle_id": snapshot["cycle_id"],
        "as_of": snapshot["as_of"],
        "source_watermarks": snapshot["source_watermarks"],
        "evidence": snapshot["evidence"],
        "parent_snapshot_id": snapshot.get("parent_snapshot_id"),
        "decision_id": snapshot.get("decision_id"),
    }


def build_snapshot(
    *, cycle_id: str, as_of: str, source_watermarks: dict[str, Any] | None,
    evidence: dict[str, Any], parent_snapshot_id: str | None = None,
    decision_id: str | None = None,
) -> dict[str, Any]:
    if not str(cycle_id or "").strip():
        raise ValueError("cycle_id is required")
    if not isinstance(evidence, dict):
        raise ValueError("snapshot evidence must be an object")
    # A snapshot may be consumed by M1, so the public evidence envelope must
    # retain the same blind boundary as the durable M1 packet.
    validate_m1_blind_packet(evidence)
    snapshot: dict[str, Any] = {
        "contract": VERSION,
        "schema_version": SCHEMA_VERSION,
        "cycle_id": str(cycle_id),
        "as_of": _timestamp(as_of, "as_of"),
        "source_watermarks": _normalized_watermarks(source_watermarks),
        "evidence": copy.deepcopy(evidence),
        "parent_snapshot_id": str(parent_snapshot_id) if parent_snapshot_id else None,
        "decision_id": str(decision_id) if decision_id else None,
    }
    snapshot["content_hash"] = content_hash(_identity(snapshot))
    snapshot["snapshot_id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{VERSION}:{snapshot['content_hash']}"))
    validate_snapshot(snapshot)
    return snapshot


create_snapshot = build_snapshot


def validate_snapshot(snapshot: dict[str, Any]) -> None:
    if not isinstance(snapshot, dict) or snapshot.get("contract") != VERSION:
        raise ValueError("unsupported evidence snapshot contract")
    if snapshot.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported evidence snapshot schema version")
    for field in ("snapshot_id", "cycle_id", "as_of", "content_hash", "evidence"):
        if not snapshot.get(field):
            raise ValueError(f"evidence snapshot {field} is required")
    if not isinstance(snapshot.get("source_watermarks"), dict):
        raise ValueError("evidence snapshot source watermarks are required")
    _timestamp(snapshot["as_of"], "as_of")
    if not isinstance(snapshot["evidence"], dict):
        raise ValueError("evidence snapshot evidence must be an object")
    validate_m1_blind_packet(snapshot["evidence"])
    expected_hash = content_hash(_identity(snapshot))
    if snapshot.get("content_hash") != expected_hash:
        raise ValueError("evidence snapshot content hash mismatch")
    expected_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{VERSION}:{expected_hash}"))
    if snapshot.get("snapshot_id") != expected_id:
        raise ValueError("evidence snapshot identity mismatch")


def replay_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Verify and return a detached snapshot suitable for deterministic replay."""
    replay = copy.deepcopy(snapshot)
    validate_snapshot(replay)
    return replay


def shared_baseline(*snapshots: dict[str, Any]) -> str:
    """Validate that all stages cite the exact same frozen evidence version."""
    if not snapshots:
        raise ValueError("at least one evidence snapshot is required")
    for snapshot in snapshots:
        validate_snapshot(snapshot)
    ids = {snapshot["snapshot_id"] for snapshot in snapshots}
    if len(ids) != 1:
        raise ValueError("M0 and M1 must share one evidence snapshot")
    return next(iter(ids))


assert_shared_baseline = shared_baseline
