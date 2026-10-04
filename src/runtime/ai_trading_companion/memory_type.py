"""Runtime-owned MemoryTypeSpec/v1 for semantic MemoryHub records.

The envelope is stored inside an authoritative MemoryHub episode's metadata. It
classifies the record without creating a second ledger or turning projections
into facts.
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from .temporal_integrity import canonical_time


CONTRACT = "MemoryTypeSpec/v1"
VERSION = 1
REPLAY_CONTRACT = "MemoryTypeReplay/v1"
SEMANTIC_TYPES = frozenset({
    "user_fact", "observation", "judgment", "outcome", "lesson", "preference", "rule",
    "message", "evidence", "correction", "operational",
})

_REQUIRED = {"contract", "version", "semantic_type", "source", "temporal", "authority", "correction_of", "provenance"}
_SOURCE_REQUIRED = {"source_system", "source_event_id"}
_TEMPORAL_REQUIRED = {"occurred_at", "known_at", "submitted_at"}
_PROVENANCE_REQUIRED = {"episode_type"}


def _canonical_memory_time(value: Any) -> str:
    text = str(value)
    if len(text) == 10:
        text += "T00:00:00Z"
    return canonical_time(text)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def build_envelope(episode: dict[str, Any], *, semantic_type: str) -> dict[str, Any]:
    """Build a typed envelope from the fields already owned by MemoryHub."""
    if semantic_type not in SEMANTIC_TYPES:
        raise ValueError(f"unsupported MemoryType semantic type: {semantic_type}")
    source_system = str(episode.get("source_system") or "").strip()
    source_event_id = str(episode.get("source_event_id") or "").strip()
    authority = str(episode.get("authority") or "").strip()
    episode_type = str(episode.get("episode_type") or "").strip()
    if not source_system or not source_event_id or not authority or not episode_type:
        raise ValueError("MemoryType requires source, authority and episode type")
    temporal = {
        field: _canonical_memory_time(episode[field])
        for field in ("occurred_at", "known_at", "submitted_at")
    }
    value = {
        "contract": CONTRACT,
        "version": VERSION,
        "semantic_type": semantic_type,
        "source": {"source_system": source_system, "source_event_id": source_event_id},
        "temporal": temporal,
        "authority": authority,
        "correction_of": episode.get("corrects_episode_id"),
        "provenance": {"episode_type": episode_type},
    }
    validate(value)
    return value


def typed_episode(episode: dict[str, Any], *, semantic_type: str) -> dict[str, Any]:
    """Attach a versioned type envelope while preserving the existing episode shape."""
    value = copy.deepcopy(episode)
    metadata = dict(value.get("metadata") or {})
    existing = metadata.get("memory_type")
    envelope = build_envelope(value, semantic_type=semantic_type)
    if existing is not None:
        validate(existing)
        if existing != envelope:
            raise ValueError("existing MemoryType envelope does not match episode")
    metadata["memory_type"] = envelope
    value["metadata"] = metadata
    return value


def validate(value: dict[str, Any]) -> None:
    if not isinstance(value, dict) or value.get("contract") != CONTRACT:
        raise ValueError("unsupported MemoryType contract")
    missing = sorted(_REQUIRED - set(value))
    unknown = sorted(set(value) - _REQUIRED)
    if missing:
        raise ValueError("MemoryType envelope missing: " + ", ".join(missing))
    if unknown:
        raise ValueError("MemoryType envelope contains unsupported fields: " + ", ".join(unknown))
    if value.get("version") != VERSION or value.get("semantic_type") not in SEMANTIC_TYPES:
        raise ValueError("invalid MemoryType identity")
    source = value["source"]
    if not isinstance(source, dict) or set(source) != _SOURCE_REQUIRED:
        raise ValueError("MemoryType source identity is invalid")
    if any(not isinstance(source[field], str) or not source[field].strip() for field in _SOURCE_REQUIRED):
        raise ValueError("MemoryType source identity is required")
    temporal = value["temporal"]
    if not isinstance(temporal, dict) or set(temporal) != _TEMPORAL_REQUIRED:
        raise ValueError("MemoryType temporal envelope is invalid")
    for field in _TEMPORAL_REQUIRED:
        if _canonical_memory_time(temporal[field]) != temporal[field]:
            raise ValueError(f"MemoryType {field} must be canonical")
    if not isinstance(value["authority"], str) or not value["authority"].strip():
        raise ValueError("MemoryType authority is required")
    correction = value["correction_of"]
    if correction is not None and (not isinstance(correction, str) or not correction.strip()):
        raise ValueError("MemoryType correction_of must be a non-empty episode id or null")
    provenance = value["provenance"]
    if not isinstance(provenance, dict) or set(provenance) != _PROVENANCE_REQUIRED:
        raise ValueError("MemoryType provenance is invalid")
    if not isinstance(provenance["episode_type"], str) or not provenance["episode_type"].strip():
        raise ValueError("MemoryType provenance episode_type is required")


def frozen_replay(value: dict[str, Any]) -> dict[str, Any]:
    """Return a deterministic qualification projection without changing the envelope."""
    validate(value)
    source_hash = sha256(value)
    return {
        "contract": REPLAY_CONTRACT,
        "version": VERSION,
        "source_sha256": source_hash,
        "qualification": {
            "valid": True,
            "semantic_type": value["semantic_type"],
            "authority": value["authority"],
            "correction_bound": value["correction_of"] is not None,
            "dual_time_bound": all(value["temporal"].values()),
        },
        "evaluation_vector": {
            "delivery_speed": {"state": "not_measured_in_frozen_replay"},
            "qualification_probability": {"state": "not_estimated_in_frozen_replay"},
            "research_quality": {"semantic_type": value["semantic_type"]},
            "judgment_outcome": {"state": "not_measured_in_frozen_replay"},
            "safety_reliability": {
                "authoritative_envelope": True,
                "projection_separate": True,
                "correction_append_only": True,
            },
        },
    }


def install_qualification() -> dict[str, Any]:
    episode = {
        "source_system": "stock-advisor",
        "source_event_id": "memory-type-install",
        "episode_type": "personal_fact",
        "occurred_at": "2026-01-01T00:00:00Z",
        "known_at": "2026-01-01T00:01:00Z",
        "submitted_at": "2026-01-01T00:01:00Z",
        "authority": "user_private_fact",
        "corrects_episode_id": None,
    }
    envelope = build_envelope(episode, semantic_type="user_fact")
    first = frozen_replay(envelope)
    second = frozen_replay(copy.deepcopy(envelope))
    return {
        "contract": "MemoryTypeInstallQualification/v1",
        "qualified": first == second,
        "replay_sha256": sha256(first),
        "source_sha256": first["source_sha256"],
        "evaluation_vector": first["evaluation_vector"],
    }


if __name__ == "__main__":
    print(canonical_json(install_qualification()))
