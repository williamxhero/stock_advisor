"""Runtime-owned, immutable evidence snapshots for a decision cycle.

The snapshot is a small, deterministic envelope around ledger references.  It
does not copy authority from an LLM response: the Runtime chooses the ledger
rows, their qualification receipts, and the source watermarks before making a
snapshot available to M0 or M1.
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable
from uuid import NAMESPACE_URL, uuid5

VERSION = "EvidenceSnapshotSpec/v1"
SCHEMA_VERSION = VERSION


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _timestamp(value: Any) -> str:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("evidence snapshot timestamp requires timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _content(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "contract": snapshot["contract"],
        "schema_version": snapshot["schema_version"],
        "cycle_id": snapshot["cycle_id"],
        "as_of": snapshot["as_of"],
        "source_watermarks": snapshot["source_watermarks"],
        "evidence_refs": snapshot["evidence_refs"],
    }


def content_hash(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(_content(snapshot)).encode("utf-8")).hexdigest()


def snapshot_id(cycle_id: str, snapshot_content_hash: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"{VERSION}:{cycle_id}:{snapshot_content_hash}"))


def _reference(item: Any) -> dict[str, Any]:
    if isinstance(item, str):
        return {"evidence_id": item}
    if not isinstance(item, dict):
        raise ValueError("evidence snapshot references must be objects")
    evidence_id = str(item.get("evidence_id") or item.get("record_id") or "")
    if not evidence_id:
        raise ValueError("evidence snapshot reference requires evidence_id")
    result = {"evidence_id": evidence_id}
    for key in ("content_sha256", "known_at", "occurred_at", "qualification_id"):
        if item.get(key) is not None:
            result[key] = item[key]
    return result


def build(
    cycle_id: str,
    as_of: str,
    evidence_refs: Iterable[Any],
    source_watermarks: dict[str, Any],
    *,
    version: int = 1,
    previous_snapshot_id: str | None = None,
) -> dict[str, Any]:
    refs = sorted((_reference(item) for item in evidence_refs), key=lambda item: canonical(item))
    snapshot: dict[str, Any] = {
        "contract": VERSION,
        "schema_version": SCHEMA_VERSION,
        "cycle_id": str(cycle_id),
        "as_of": _timestamp(as_of),
        "source_watermarks": copy.deepcopy(source_watermarks),
        "evidence_refs": refs,
        "version": int(version),
        "previous_snapshot_id": previous_snapshot_id,
    }
    snapshot["content_hash"] = content_hash(snapshot)
    snapshot["snapshot_id"] = snapshot_id(snapshot["cycle_id"], snapshot["content_hash"])
    validate(snapshot)
    return snapshot


def validate(snapshot: dict[str, Any]) -> None:
    if not isinstance(snapshot, dict) or snapshot.get("contract") != VERSION:
        raise ValueError("unsupported evidence snapshot contract")
    required = (
        "snapshot_id", "cycle_id", "as_of", "source_watermarks", "schema_version",
        "content_hash", "evidence_refs", "version", "previous_snapshot_id",
    )
    missing = [key for key in required if key not in snapshot]
    if missing:
        raise ValueError("evidence snapshot missing fields: " + ", ".join(missing))
    if snapshot["schema_version"] != SCHEMA_VERSION or not str(snapshot["cycle_id"]):
        raise ValueError("invalid evidence snapshot schema or cycle")
    _timestamp(snapshot["as_of"])
    if not isinstance(snapshot["source_watermarks"], dict):
        raise ValueError("evidence snapshot source watermarks must be an object")
    if not isinstance(snapshot["evidence_refs"], list):
        raise ValueError("evidence snapshot references must be a list")
    if int(snapshot["version"]) < 1:
        raise ValueError("evidence snapshot version must be positive")
    if snapshot["previous_snapshot_id"] is not None and not str(snapshot["previous_snapshot_id"]):
        raise ValueError("invalid previous evidence snapshot id")
    if snapshot["content_hash"] != content_hash(snapshot):
        raise ValueError("evidence snapshot content hash mismatch")
    if snapshot["snapshot_id"] != snapshot_id(str(snapshot["cycle_id"]), snapshot["content_hash"]):
        raise ValueError("evidence snapshot identity mismatch")
    for item in snapshot["evidence_refs"]:
        _reference(item)


# Explicit names make the Runtime API easy to discover while retaining the
# small functional style used by EvidenceSpec and EvidenceQualificationSpec.
create = build
validate_snapshot = validate
