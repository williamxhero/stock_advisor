"""Runtime-owned, immutable evidence snapshots.

The model produces an evidence result; Runtime turns that result and the
acquisition watermarks into this contract.  Snapshot identity therefore never
comes from model text and a later observation cannot mutate an earlier
judgment baseline.
"""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from datetime import datetime
from typing import Any


VERSION = "EvidenceSnapshotSpec/v1"
SCHEMA_VERSION = 1
_NAMESPACE = uuid.UUID("f3c8f1de-9a07-4d06-9da0-3d1ea2f7b8e1")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha256(value: Any) -> str:
    text = value if isinstance(value, str) else canonical_json(value)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _require_time(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return text


def _source_refs(baseline: dict[str, Any]) -> list[str]:
    refs = {
        str(item.get("evidence_ref"))
        for item in baseline.get("sources") or []
        if isinstance(item, dict) and str(item.get("evidence_ref") or "")
    }
    return sorted(refs)


def source_watermarks_from_observations(observations: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Return stable source watermarks from Runtime acquisition receipts."""
    result: dict[str, Any] = {}
    for observation in observations or []:
        if not isinstance(observation, dict):
            continue
        key = str(observation.get("backend") or observation.get("tool") or "unknown")
        watermark = {
            "observation_id": str(observation.get("observation_id") or ""),
            "result_sha256": str(observation.get("result_sha256") or observation.get("content_sha256") or ""),
            "acquired_at": str(observation.get("acquired_at") or ""),
        }
        # A repeated backend may have multiple independent observations.  The
        # sequence is explicit so replay does not depend on dictionary order.
        if key in result:
            previous = result[key]
            if not isinstance(previous, list):
                previous = [previous]
            previous.append(watermark)
            result[key] = previous
        else:
            result[key] = watermark
    return {key: result[key] for key in sorted(result)}


def _content(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "cycle_id": snapshot["cycle_id"],
        "as_of": snapshot["as_of"],
        "schema_version": snapshot["schema_version"],
        "source_watermarks": snapshot["source_watermarks"],
        "included_sources": snapshot["included_sources"],
        "baseline": snapshot["baseline"],
    }


def snapshot_content_hash(snapshot: dict[str, Any]) -> str:
    return sha256(_content(snapshot))


def snapshot_id_for(cycle_id: str, as_of: str, content_hash: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, f"{VERSION}|{cycle_id}|{as_of}|{content_hash}"))


def build_snapshot(
    *,
    cycle_id: str,
    as_of: str,
    evidence: dict[str, Any],
    source_watermarks: dict[str, Any] | None = None,
    parent_snapshot_id: str | None = None,
    version: int = 1,
) -> dict[str, Any]:
    """Build a deterministic snapshot candidate without persistence."""
    if not str(cycle_id).strip():
        raise ValueError("cycle_id is required")
    frozen_as_of = _require_time(as_of, "as_of")
    if not isinstance(evidence, dict):
        raise ValueError("evidence baseline must be an object")
    if not isinstance(source_watermarks, dict):
        raise ValueError("source_watermarks must be an object")
    if int(version) < 1:
        raise ValueError("snapshot version must be positive")
    baseline = copy.deepcopy(evidence)
    watermarks = copy.deepcopy(source_watermarks or {})
    candidate = {
        "contract": VERSION,
        "cycle_id": str(cycle_id),
        "as_of": frozen_as_of,
        "schema_version": SCHEMA_VERSION,
        "source_watermarks": watermarks,
        "included_sources": _source_refs(baseline),
        "baseline": baseline,
        "version": int(version),
    }
    candidate["content_hash"] = snapshot_content_hash(candidate)
    candidate["snapshot_id"] = snapshot_id_for(candidate["cycle_id"], candidate["as_of"], candidate["content_hash"])
    if parent_snapshot_id:
        candidate["parent_snapshot_id"] = str(parent_snapshot_id)
    validate_snapshot(candidate)
    return candidate


def validate_snapshot(snapshot: dict[str, Any]) -> None:
    if not isinstance(snapshot, dict) or snapshot.get("contract") != VERSION:
        raise ValueError("unsupported evidence snapshot contract")
    required = {
        "snapshot_id", "cycle_id", "as_of", "source_watermarks", "schema_version",
        "content_hash", "included_sources", "baseline", "version",
    }
    missing = sorted(required - set(snapshot))
    if missing:
        raise ValueError("evidence snapshot missing fields: " + ", ".join(missing))
    _require_time(snapshot["as_of"], "as_of")
    if snapshot["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported evidence snapshot schema version")
    if not isinstance(snapshot["source_watermarks"], dict):
        raise ValueError("source_watermarks must be an object")
    if not isinstance(snapshot["included_sources"], list) or any(not isinstance(item, str) for item in snapshot["included_sources"]):
        raise ValueError("included_sources must be a list of strings")
    if snapshot["included_sources"] != sorted(set(snapshot["included_sources"])):
        raise ValueError("included_sources must be sorted and unique")
    if not isinstance(snapshot["baseline"], dict):
        raise ValueError("baseline must be an object")
    if int(snapshot["version"]) < 1:
        raise ValueError("snapshot version must be positive")
    expected_hash = snapshot_content_hash(snapshot)
    if snapshot.get("content_hash") != expected_hash:
        raise ValueError("evidence snapshot content hash mismatch")
    expected_id = snapshot_id_for(str(snapshot["cycle_id"]), str(snapshot["as_of"]), expected_hash)
    if snapshot.get("snapshot_id") != expected_id:
        raise ValueError("evidence snapshot identity mismatch")
    if snapshot["included_sources"] != _source_refs(snapshot["baseline"]):
        raise ValueError("evidence snapshot source references mismatch")


def descriptor(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Expose only the verifiable identity needed by a stage packet."""
    validate_snapshot(snapshot)
    return {
        "contract": snapshot["contract"],
        "snapshot_id": snapshot["snapshot_id"],
        "cycle_id": snapshot["cycle_id"],
        "as_of": snapshot["as_of"],
        "schema_version": snapshot["schema_version"],
        "content_hash": snapshot["content_hash"],
        "source_watermarks": copy.deepcopy(snapshot["source_watermarks"]),
        "included_sources": list(snapshot["included_sources"]),
        "version": snapshot["version"],
        **({"parent_snapshot_id": snapshot["parent_snapshot_id"]} if snapshot.get("parent_snapshot_id") else {}),
    }


# Explicit aliases make the contract convenient for callers and replay tools.
validate = validate_snapshot
build = build_snapshot
