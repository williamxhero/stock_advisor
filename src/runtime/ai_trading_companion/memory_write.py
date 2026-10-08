"""Runtime-owned MemoryWriteSpec/v1: the only path that writes MemoryHub.

Authoritative records (messages, judgments, outcomes, lessons, user facts,
evidence and corrections) are appended once through the versioned MemoryHub
interface. Summaries, indexes, entities, relations and exports are rebuildable
projections and are never written as episodes. The writer exposes no update or
delete operation and never falls back to a local memory store.
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from .memory_port import InMemoryMemoryAdapter, MemoryPort, MemoryUnavailable
from .memory_type import SEMANTIC_TYPES, sha256, typed_episode
from .secret_guard import assert_safe

CONTRACT = "MemoryWriteSpec/v1"
VERSION = 1
REPLAY_CONTRACT = "MemoryWriteReplay/v1"

# kind -> allowed episode types, authorities and semantic types. A kind absent
# from this table is not a formal memory record and cannot be written.
WRITE_POLICY: dict[str, dict[str, frozenset[str]]] = {
    "user_message": {
        "episode_types": frozenset({"user_message", "test_utterance"}),
        "authorities": frozenset({"user_private_fact"}),
        "semantic_types": frozenset({"message"}),
    },
    "ai_message": {
        "episode_types": frozenset({"ai_message"}),
        "authorities": frozenset({"published_ai_message"}),
        "semantic_types": frozenset({"message", "judgment", "outcome", "lesson"}),
    },
    "user_fact": {
        "episode_types": frozenset({"personal_fact", "proposition"}),
        "authorities": frozenset({"user_private_fact"}),
        "semantic_types": frozenset({"user_fact", "preference", "rule", "observation"}),
    },
    "evidence": {
        "episode_types": frozenset({"external_evidence"}),
        "authorities": frozenset({"mutable_source_snapshot", "immutable_source_reference"}),
        "semantic_types": frozenset({"evidence", "observation"}),
    },
    "correction": {
        "episode_types": frozenset({"correction"}),
        "authorities": frozenset({"user_private_fact", "published_ai_message", "recorded_observation"}),
        "semantic_types": frozenset({"correction"}),
    },
    "learning": {
        "episode_types": frozenset({"outcome", "lesson"}),
        "authorities": frozenset({"runtime_learning"}),
        "semantic_types": frozenset({"outcome", "lesson"}),
    },
    "migrated": {
        "episode_types": frozenset(),  # any legacy episode type
        "authorities": frozenset({"migrated_legacy_record"}),
        "semantic_types": SEMANTIC_TYPES,
    },
}
# Rebuildable derivations. They may be cached by MemoryHub but are not memory.
PROJECTIONS = frozenset({
    "summary", "lexical_index", "entity", "relation", "event_cluster",
    "retrieval_bundle", "retrieval_audit", "markdown_export",
})
FORBIDDEN_OPERATIONS = ("update", "delete", "overwrite", "replace", "remove")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _content_hash(body: str) -> str:
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


class MemoryWriter:
    """Policy-checked, append-only writer over the MemoryHub interface."""

    def __init__(self, memory: MemoryPort | None) -> None:
        if memory is None:
            raise MemoryUnavailable("MemoryHub is required for formal memory writes")
        self._memory = memory

    def prepare(self, kind: str, episode: dict[str, Any], *, semantic_type: str | None = None) -> dict[str, Any]:
        """Return the exact episode that would be appended, or raise before any write."""
        if kind in PROJECTIONS:
            raise ValueError(f"{kind} is a rebuildable projection, not authoritative memory")
        policy = WRITE_POLICY.get(kind)
        if policy is None:
            raise ValueError(f"unsupported memory write kind: {kind}")
        value = copy.deepcopy(episode)
        existing = (value.get("metadata") or {}).get("memory_type")
        resolved = semantic_type or (existing or {}).get("semantic_type")
        if resolved is None:
            raise ValueError("memory write requires a semantic type")
        if resolved not in policy["semantic_types"]:
            raise ValueError(f"semantic type {resolved} is not allowed for {kind}")
        if policy["episode_types"] and value.get("episode_type") not in policy["episode_types"]:
            raise ValueError(f"episode type {value.get('episode_type')} is not allowed for {kind}")
        if value.get("authority") not in policy["authorities"]:
            raise ValueError(f"authority {value.get('authority')} is not allowed for {kind}")
        if kind == "correction" and not value.get("corrects_episode_id"):
            raise ValueError("a correction must append against an existing episode")
        # Authentication secrets stop here, before any adapter or log sees them.
        assert_safe(
            canonical_json({
                "body": value.get("body"), "metadata": value.get("metadata"),
                "source_reference": value.get("source_reference"),
            }),
            boundary="MemoryHub episode",
        )
        if value.get("body") and value.get("content_hash") in (None, "", "auto"):
            value["content_hash"] = _content_hash(str(value["body"]))
        return typed_episode(value, semantic_type=resolved)

    def write(self, kind: str, episode: dict[str, Any], *, semantic_type: str | None = None) -> dict[str, Any]:
        return self._memory.append(self.prepare(kind, episode, semantic_type=semantic_type))

    def correct(
        self, original_episode_id: str, episode: dict[str, Any], *, semantic_type: str = "correction",
    ) -> dict[str, Any]:
        """Append a correction that references, and never rewrites, its target."""
        value = {**episode, "episode_type": "correction", "corrects_episode_id": original_episode_id}
        return self.write("correction", value, semantic_type=semantic_type)

    def write_batch(self, kind: str, episodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        prepared = [self.prepare(kind, episode) for episode in episodes]
        return self._memory.append_batch(prepared)


def write_memory(
    memory: MemoryPort | None, kind: str, episode: dict[str, Any], *, semantic_type: str,
) -> dict[str, Any]:
    return MemoryWriter(memory).write(kind, episode, semantic_type=semantic_type)


def plan(kind: str, episode: dict[str, Any], *, semantic_type: str) -> dict[str, Any]:
    """Deterministic, side-effect-free description of one authoritative write."""
    prepared = MemoryWriter(InMemoryMemoryAdapter()).prepare(kind, episode, semantic_type=semantic_type)
    return {
        "contract": CONTRACT,
        "version": VERSION,
        "kind": kind,
        "mode": "authoritative_append",
        "append_only": True,
        "semantic_type": prepared["metadata"]["memory_type"]["semantic_type"],
        "episode_type": prepared["episode_type"],
        "authority": prepared["authority"],
        "idempotency_key": [prepared["memory_space_id"], prepared["source_system"], prepared["source_event_id"]],
        "content_hash": prepared.get("content_hash"),
        "corrects_episode_id": prepared.get("corrects_episode_id"),
        "projection_kinds": sorted(PROJECTIONS),
    }


def validate_plan(value: dict[str, Any]) -> None:
    if not isinstance(value, dict) or value.get("contract") != CONTRACT or value.get("version") != VERSION:
        raise ValueError("unsupported MemoryWrite plan")
    if value.get("kind") not in WRITE_POLICY or value.get("mode") != "authoritative_append":
        raise ValueError("invalid MemoryWrite plan kind or mode")
    if value.get("append_only") is not True:
        raise ValueError("MemoryWrite plans must be append-only")
    if value.get("semantic_type") not in SEMANTIC_TYPES:
        raise ValueError("invalid MemoryWrite plan semantic type")
    key = value.get("idempotency_key")
    if not isinstance(key, list) or len(key) != 3 or any(not isinstance(item, str) or not item for item in key):
        raise ValueError("MemoryWrite plan requires a three-part idempotency key")
    if set(value.get("projection_kinds") or []) != PROJECTIONS:
        raise ValueError("MemoryWrite plan must declare the rebuildable projections")


def frozen_replay(value: dict[str, Any]) -> dict[str, Any]:
    validate_plan(value)
    return {
        "contract": REPLAY_CONTRACT,
        "version": VERSION,
        "source_sha256": sha256(value),
        "qualification": {
            "valid": True, "kind": value["kind"], "semantic_type": value["semantic_type"],
            "append_only": True, "correction_bound": value["corrects_episode_id"] is not None,
        },
        "evaluation_vector": {
            "delivery_speed": {"state": "not_measured_in_frozen_replay"},
            "qualification_probability": {"state": "not_estimated_in_frozen_replay"},
            "research_quality": {"state": "not_applicable_to_write_policy"},
            "judgment_outcome": {"state": "not_measured_in_frozen_replay"},
            "safety_reliability": {
                "no_update_or_delete_operation": True,
                "projection_never_written": True,
                "secret_blocked_before_write": True,
            },
        },
    }


def _install_checks() -> dict[str, bool]:
    memory = InMemoryMemoryAdapter()
    writer = MemoryWriter(memory)
    episode = {
        "memory_space_id": "install", "source_system": "stock-advisor",
        "source_event_id": "memory-write-install", "content_hash": "auto",
        "episode_type": "user_message", "body": "install fixture",
        "occurred_at": "2026-01-01T00:00:00Z", "known_at": "2026-01-01T00:00:00Z",
        "submitted_at": "2026-01-01T00:00:00Z", "authority": "user_private_fact",
        "protocol_version": "memoryhub/v1",
    }
    first = writer.write("user_message", episode, semantic_type="message")
    replay = writer.write("user_message", episode, semantic_type="message")
    checks = {"idempotent_replay": replay == first and len(memory._episodes) == 1}
    try:
        writer.write("user_message", {**episode, "body": "rewritten history"}, semantic_type="message")
        checks["rewrite_rejected"] = False
    except MemoryUnavailable:
        checks["rewrite_rejected"] = len(memory._episodes) == 1
    secret = {**episode, "source_event_id": "secret", "body": "token=abcdefgh12345678"}
    try:
        writer.write("user_message", secret, semantic_type="message")
        checks["secret_blocked_before_write"] = False
    except ValueError:
        checks["secret_blocked_before_write"] = len(memory._episodes) == 1
    try:
        writer.prepare("summary", episode, semantic_type="message")
        checks["projection_rejected"] = False
    except ValueError:
        checks["projection_rejected"] = True
    try:
        MemoryWriter(None)
        checks["memoryhub_required"] = False
    except MemoryUnavailable:
        checks["memoryhub_required"] = True
    checks["no_mutation_api"] = not any(
        any(word in name for word in FORBIDDEN_OPERATIONS) for name in dir(MemoryWriter) if not name.startswith("_")
    )
    return checks


def install_qualification() -> dict[str, Any]:
    episode = {
        "memory_space_id": "install", "source_system": "stock-advisor",
        "source_event_id": "memory-write-install", "content_hash": "auto",
        "episode_type": "user_message", "body": "install fixture",
        "occurred_at": "2026-01-01T00:00:00Z", "known_at": "2026-01-01T00:00:00Z",
        "submitted_at": "2026-01-01T00:00:00Z", "authority": "user_private_fact",
        "protocol_version": "memoryhub/v1",
    }
    value = plan("user_message", episode, semantic_type="message")
    first = frozen_replay(value)
    second = frozen_replay(copy.deepcopy(value))
    checks = _install_checks()
    return {
        "contract": "MemoryWriteInstallQualification/v1",
        "qualified": first == second and all(checks.values()),
        "checks": checks,
        "replay_sha256": sha256(first),
        "source_sha256": first["source_sha256"],
        "evaluation_vector": first["evaluation_vector"],
    }


if __name__ == "__main__":
    print(canonical_json(install_qualification()))
