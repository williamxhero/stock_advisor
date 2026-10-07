"""Read-only MemoryRetrievalSpec/v1 over snapshot-bound MemoryHub episodes.

MemoryHub owns the ledger and visibility. Runtime owns this deterministic
ranking projection; a high score never grants visibility or promotes a lesson.
"""
from __future__ import annotations

import copy
from datetime import datetime
import re
from typing import Any, Callable

from .memory_type import build_envelope, sha256, validate as validate_type
from .temporal_integrity import canonical_time

CONTRACT = "MemoryRetrievalSpec/v1"
VERSION = 1
POLICY_VERSION = "memory-retrieval-policy/v1"
PROFILE_CONTRACT = "MemoryRetrievalProfile/v1"
BLIND_STAGES = frozenset({"m1_research", "m1_judgment"})
HALF_LIFE_DAYS = {
    "observation": 2.0, "preference": 3650.0, "user_fact": 30.0,
    "judgment": 14.0, "outcome": 90.0, "lesson": 30.0, "rule": 365.0,
    "message": 7.0, "evidence": 7.0, "correction": 30.0, "operational": 1.0,
}


class MemoryIsolationError(ValueError):
    pass


def build_input(snapshot: dict[str, Any], query: str, *, limit: int = 20,
                context: dict[str, Any] | None = None) -> dict[str, Any]:
    context = context or {}
    value = {
        "contract": CONTRACT, "version": VERSION, "policy_version": POLICY_VERSION,
        "snapshot": copy.deepcopy(snapshot), "query": query, "limit": limit,
        "instruments": context.get("instruments", sorted(set(re.findall(r"(?<!\d)\d{6}(?!\d)", query)))),
        "market_state": context.get("market_state"),
        "permissions": {"write_permissions": []},
    }
    if set(context) - {"instruments", "market_state"}:
        raise ValueError("unsupported memory retrieval context")
    validate_input(value)
    return value


def _refs(value: Any) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError("memory retrieval references must be non-empty strings")
    return sorted(set(value))


def validate_input(value: dict[str, Any]) -> None:
    if set(value) != {"contract", "version", "policy_version", "snapshot", "query", "limit", "instruments", "market_state", "permissions"}:
        raise ValueError("invalid MemoryRetrieval input fields")
    if value["contract"] != CONTRACT or type(value["version"]) is not int or value["version"] != VERSION or value["policy_version"] != POLICY_VERSION:
        raise ValueError("unsupported MemoryRetrieval version")
    snapshot = value["snapshot"]
    for field in ("snapshot_id", "memory_space_id", "stage", "as_of", "policy_version", "protocol_version"):
        if not isinstance(snapshot.get(field), str) or not snapshot[field].strip():
            raise ValueError(f"memory retrieval requires snapshot {field}")
    canonical_time(snapshot["as_of"])
    if type(snapshot.get("watermark")) is not int or snapshot["watermark"] < 0:
        raise ValueError("memory retrieval requires a frozen watermark")
    if snapshot["stage"] in BLIND_STAGES and not snapshot.get("cycle_id"):
        raise ValueError("blind memory retrieval requires cycle identity")
    if not isinstance(value["query"], str) or type(value["limit"]) is not int or not 1 <= value["limit"] <= 100:
        raise ValueError("invalid memory retrieval query or limit")
    _refs(value["instruments"])
    if value["market_state"] is not None and (not isinstance(value["market_state"], str) or not value["market_state"].strip()):
        raise ValueError("invalid memory retrieval market state")
    if value["permissions"] != {"write_permissions": []}:
        raise ValueError("memory retrieval is read-only")


def build_profile(*, reliability: str = "unverified", outcome_support: str = "unknown",
                  lesson_state: str = "candidate", instruments: list[str] | None = None,
                  market_states: list[str] | None = None, parent_episode_ids: list[str] | None = None,
                  evidence_episode_ids: list[str] | None = None,
                  outcome_episode_ids: list[str] | None = None) -> dict[str, Any]:
    value = {
        "contract": PROFILE_CONTRACT, "version": VERSION, "reliability": reliability,
        "outcome_support": outcome_support, "lesson_state": lesson_state,
        "instruments": instruments or [], "market_states": market_states or [],
        "parent_episode_ids": parent_episode_ids or [],
        "evidence_episode_ids": evidence_episode_ids or [], "outcome_episode_ids": outcome_episode_ids or [],
    }
    validate_profile(value)
    return value


def validate_profile(value: dict[str, Any]) -> None:
    required = {"contract", "version", "reliability", "outcome_support", "lesson_state", "instruments", "market_states", "parent_episode_ids", "evidence_episode_ids", "outcome_episode_ids"}
    if not isinstance(value, dict) or set(value) != required or value["contract"] != PROFILE_CONTRACT or type(value["version"]) is not int or value["version"] != VERSION:
        raise ValueError("invalid MemoryRetrieval profile")
    if value["reliability"] not in {"unverified", "verified", "conflicted", "rejected"} or value["outcome_support"] not in {"unknown", "supported", "contradicted"} or value["lesson_state"] not in {"candidate", "verified", "error"}:
        raise ValueError("invalid memory reliability, outcome support or lesson state")
    for key in ("instruments", "market_states", "parent_episode_ids", "evidence_episode_ids", "outcome_episode_ids"):
        _refs(value[key])
    if value["reliability"] == "verified" and not value["evidence_episode_ids"]:
        raise ValueError("verified reliability requires evidence references")
    if value["outcome_support"] != "unknown" and not value["outcome_episode_ids"]:
        raise ValueError("outcome support requires outcome references")
    if value["lesson_state"] == "verified" and (value["reliability"] != "verified" or value["outcome_support"] != "supported"):
        raise ValueError("verified lessons require reliable evidence and supported outcomes")


def _semantic_type(episode: dict[str, Any]) -> str:
    envelope = episode.get("metadata", {}).get("memory_type")
    if envelope is not None:
        validate_type(envelope)
        if envelope != build_envelope(episode, semantic_type=envelope["semantic_type"]):
            raise ValueError("memory type identity conflict")
        return envelope["semantic_type"]
    return {
        "personal_fact": "user_fact", "external_evidence": "evidence",
        "user_message": "message", "ai_message": "message", "judgment": "judgment",
        "outcome": "outcome", "lesson": "lesson", "preference": "preference",
    }.get(episode.get("episode_type"), "observation")


def qualify_episode(episode: dict[str, Any], snapshot: dict[str, Any],
                    resolve: Callable[[str], dict[str, Any]], *, ancestors: frozenset[str] = frozenset()) -> None:
    """Fail closed on missing/cyclic lineage, including across cycles and spaces."""
    episode_id = episode["episode_id"]
    if episode_id in ancestors or len(ancestors) >= 64:
        raise MemoryIsolationError("cyclic_or_unbounded_lineage")
    if episode.get("memory_space_id", snapshot["memory_space_id"]) != snapshot["memory_space_id"]:
        raise MemoryIsolationError("cross_space_lineage")
    if episode["sequence"] > snapshot["watermark"] or any(
        canonical_time(episode[field]) > canonical_time(snapshot["as_of"])
        for field in ("known_at", "submitted_at")
    ):
        raise MemoryIsolationError("future_knowledge")
    metadata = episode.get("metadata") or {}
    semantic_type = _semantic_type(episode)
    profile = metadata.get("memory_retrieval")
    if profile is not None:
        validate_profile(profile)
    if snapshot["stage"] in BLIND_STAGES:
        if (episode.get("episode_type") in {"h0", "h0_proposition", "h0_action", "user_message"}
            or metadata.get("stage") in {"h0", "premarket", "m2", "m2_synthesis", "chat", "conversation"}
            or metadata.get("origin_stage") == "h0" or metadata.get("h0_derived") is True
            or (metadata.get("actor") == "human" and semantic_type not in {"user_fact", "preference"})):
            raise MemoryIsolationError("h0_or_conversation_lineage")
        if profile is None and semantic_type not in {"user_fact", "preference", "evidence"} and metadata.get("stage") not in {"m0", "m0_research", "m1_research", "m1_judgment"}:
            raise MemoryIsolationError("unproven_blind_lineage")
    parents = set()
    for key in ("parent_episode_ids", "derived_from_episode_ids", "related_episode_ids"):
        parents.update(_refs(metadata.get(key, [])))
    if episode.get("corrects_episode_id"):
        parents.add(episode["corrects_episode_id"])
    if profile:
        for key in ("parent_episode_ids", "evidence_episode_ids", "outcome_episode_ids"):
            parents.update(profile[key])
    for parent in sorted(parents):
        source = resolve(parent)
        if source.get("episode_id") != parent:
            raise MemoryIsolationError("lineage_identity_conflict")
        qualify_episode(source, snapshot, resolve, ancestors=ancestors | {episode_id})
        if profile and parent in profile["outcome_episode_ids"] and _semantic_type(source) != "outcome":
            raise MemoryIsolationError("outcome_reference_type_conflict")
        if profile and parent in profile["evidence_episode_ids"] and _semantic_type(source) not in {"evidence", "observation"}:
            raise MemoryIsolationError("evidence_reference_type_conflict")


def rank_bundle(bundle: dict[str, Any], resolve: Callable[[str], dict[str, Any]], *,
                limit: int = 20, context: dict[str, Any] | None = None) -> dict[str, Any]:
    request = build_input(bundle["snapshot"], bundle["query"], limit=limit, context=context)
    snapshot = request["snapshot"]
    scored, excluded, inputs = [], [], []
    candidates = bundle["results"]
    for card in candidates:
        episode_id = card["episode_id"]
        try:
            episode = resolve(episode_id)
            if episode.get("episode_id") != episode_id:
                raise MemoryIsolationError("candidate_identity_conflict")
            qualify_episode(episode, snapshot, resolve)
        except (ValueError, KeyError, TypeError) as error:
            excluded.append({"episode_id": episode_id, "reason": str(error)})
            continue
        semantic_type = _semantic_type(episode)
        profile = episode.get("metadata", {}).get("memory_retrieval") or build_profile()
        reliability = {"unverified": 0.5, "verified": 1.0, "conflicted": 0.2, "rejected": 0.0}[profile["reliability"]]
        if semantic_type in {"user_fact", "preference"} and episode["authority"] == "user_private_fact":
            reliability = 1.0 if profile["reliability"] == "unverified" else reliability
        if reliability == 0:
            excluded.append({"episode_id": episode_id, "reason": "rejected_reliability"})
            continue
        terms = sorted(set(request["query"].casefold().split()))
        text = str(episode.get("body") or card.get("summary") or "").casefold()
        relevance = sum(term in text for term in terms) / len(terms) if terms else 1.0
        instruments = profile["instruments"] or sorted(set(re.findall(r"(?<!\d)\d{6}(?!\d)", text)))
        instrument_match = (1.0 if set(request["instruments"]) & set(instruments) else 0.25 if instruments else 0.5) if request["instruments"] else 1.0
        market_match = (1.0 if request["market_state"] in profile["market_states"] else 0.25 if profile["market_states"] else 0.5) if request["market_state"] else 1.0
        half_life = HALF_LIFE_DAYS[semantic_type]
        if semantic_type == "lesson":
            half_life = {"candidate": 30.0, "verified": 365.0, "error": 7.0}[profile["lesson_state"]]
        age = max(0.0, (datetime.fromisoformat(canonical_time(snapshot["as_of"])) - datetime.fromisoformat(canonical_time(episode["occurred_at"]))).total_seconds() / 86400)
        decay = 2 ** (-age / half_life)
        support = {"unknown": 0.5, "supported": 1.0, "contradicted": 0.2}[profile["outcome_support"]]
        if semantic_type == "lesson" and profile["lesson_state"] == "error":
            support = min(support, 0.2)
        score = round((0.7 * relevance + 0.3 * instrument_match) * reliability * decay * support * market_match, 12)
        if relevance == 0 and not (request["instruments"] and instrument_match == 1):
            excluded.append({"episode_id": episode_id, "reason": "irrelevant"})
            continue
        dimensions = {
            "relevance": relevance, "reliability": reliability, "decay": decay,
            "outcome_support": support, "instrument_match": instrument_match, "market_state_match": market_match,
        }
        source = {key: episode[key] for key in ("episode_id", "content_hash", "source_system", "source_event_id", "known_at")}
        inputs.append({"episode_id": episode_id, "sha256": sha256(episode)})
        # Use the original ledger text, not a potentially contaminated derived summary.
        result = {**card, "summary": str(episode.get("body") or card.get("summary") or "")[:500],
                  "metadata": copy.deepcopy(episode.get("metadata", {})),
                  "retrieval": {"contract": CONTRACT, "version": VERSION, "policy_version": POLICY_VERSION,
                                "semantic_type": semantic_type, "half_life_days": half_life,
                                "dimensions": dimensions, "score": score, "provenance": source}}
        result.pop("derived_summary", None)
        scored.append(result)
    scored.sort(key=lambda item: (-item["retrieval"]["score"], item["episode_id"]))
    receipt = {
        **request, "status": "degraded" if excluded else "qualified" if scored else "empty",
        "input_sha256": sha256(request), "candidate_episode_ids": [card["episode_id"] for card in candidates],
        "candidate_window_limit": 100, "candidate_window_saturated": len(candidates) >= 100,
        "accepted_inputs": inputs, "excluded": excluded,
        "provenance": {"bundle_id": bundle["bundle_id"], "audit_id": bundle["audit_id"], "versions": copy.deepcopy(bundle["versions"])},
    }
    return {**bundle, "results": scored[:limit], "retrieval": receipt}
