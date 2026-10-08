"""Offline LessonPromotion replay; never write back to the canonical MemoryHub.

Original episodes remain immutable source facts. Recomputed qualification is a
separate projection, not a claim of live profitability or production approval.
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from .lesson_promotion import CONTRACT as LESSON_CONTRACT, POLICY, LessonPromotion, validate as validate_lesson
from .memory_port import InMemoryMemoryAdapter, MemoryPort, MemoryUnavailable
from .memory_retrieval import memory_timestamp, qualify_episode
from .memory_type import sha256, typed_episode
from .memory_write import canonical_json, write_memory

CONTRACT = "LessonPromotionReplay/v1"
VERSION = 1


class _ReplayMemory:
    """Ephemeral evaluator only; deliberately holds no canonical MemoryPort."""

    def __init__(self, source: dict[str, Any]) -> None:
        self.snapshot = copy.deepcopy(source["snapshot"])
        self.episodes: list[dict[str, Any]] = []
        self.target: dict[str, Any] | None = None

    def begin_snapshot(self, request: dict[str, Any]) -> dict[str, Any]:
        cutoff = (self.target or {}).get("metadata", {}).get("lesson_promotion", {}).get("payload", {}).get("input_cutoff")
        # Preserve the original ledger-wide watermark even when another space
        # occupied a sequence absent from this space-scoped frozen export.
        watermark = cutoff["watermark"] if cutoff else max((item["sequence"] for item in self.episodes), default=0)
        return {**self.snapshot, **request, "watermark": watermark}

    def export_space(self, memory_space_id: str) -> dict[str, Any]:
        if memory_space_id != self.snapshot["memory_space_id"]:
            raise MemoryUnavailable("replay memory space mismatch")
        return {"episodes": copy.deepcopy(self.episodes)}

    def append(self, episode: dict[str, Any]) -> dict[str, Any]:
        if self.target is None:
            raise MemoryUnavailable("replay evaluator has no active reconstruction")
        receipt = {key: self.target[key] for key in ("episode_id", "sequence", "protocol_version")}
        receipt["content_hash"] = episode["content_hash"]
        self.episodes.append(copy.deepcopy({**episode, **receipt}))
        return receipt


def freeze_lessons(memory: MemoryPort, memory_space_id: str, *, as_of: str) -> dict[str, Any]:
    """Freeze original ledger episodes at all three clocks and one watermark."""
    cutoff = memory_timestamp(as_of)
    snapshot = memory.begin_snapshot({"memory_space_id": memory_space_id, "stage": "reflection", "as_of": as_of, "cycle_id": None})
    exported = memory.export_space(memory_space_id)
    episodes = [copy.deepcopy(item) for item in exported["episodes"]
                if item["sequence"] <= snapshot["watermark"] and all(
                    memory_timestamp(item[field]) <= cutoff
                    for field in ("occurred_at", "known_at", "submitted_at"))]
    source = {"snapshot": copy.deepcopy(snapshot), "episodes": episodes,
              "versions": {"lesson": LESSON_CONTRACT, "policy": POLICY["version"], "memory": snapshot["protocol_version"]},
              "export_sha256": exported["export_sha256"],
              "episode_sha256": {item["episode_id"]: sha256(item) for item in episodes}}
    return {"contract": CONTRACT, "version": VERSION, "source": source, "source_sha256": sha256(source)}


def _validate_source(source: dict[str, Any]) -> None:
    snapshot = source["snapshot"]
    if source["versions"] != {"lesson": LESSON_CONTRACT, "policy": POLICY["version"], "memory": "memoryhub/v1"}:
        raise ValueError("unsupported original lesson replay contracts")
    if snapshot["protocol_version"] != "memoryhub/v1" or snapshot["stage"] != "reflection":
        raise ValueError("unsupported lesson replay snapshot")
    cutoff = memory_timestamp(snapshot["as_of"])
    identities, sequences = set(), set()
    for episode in source["episodes"]:
        identity, sequence = episode["episode_id"], episode["sequence"]
        if identity in identities or sequence in sequences or type(sequence) is not int or sequence < 1:
            raise ValueError("duplicate or invalid lesson replay episode identity")
        identities.add(identity)
        sequences.add(sequence)
        # MemoryHub/v1 exports bind the space on the enclosing export, not
        # necessarily each episode. Do not backfill fields into original facts.
        if episode.get("memory_space_id", snapshot["memory_space_id"]) != snapshot["memory_space_id"] or episode["protocol_version"] != "memoryhub/v1":
            raise ValueError("lesson replay episode space or protocol mismatch")
        if sequence > snapshot["watermark"] or any(memory_timestamp(episode[field]) > cutoff
                                                  for field in ("occurred_at", "known_at", "submitted_at")):
            raise ValueError("lesson replay episode outside frozen boundary")
        if source["episode_sha256"].get(identity) != sha256(episode):
            raise ValueError("lesson replay original episode hash mismatch")
        expected_hash = "sha256:" + hashlib.sha256(episode["body"].encode("utf-8")).hexdigest()
        if episode["content_hash"] != expected_hash:
            raise ValueError("lesson replay original content hash mismatch")
        metadata = episode.get("metadata", {})
        envelope = metadata.get("memory_type")
        if envelope is not None:
            typed_episode(episode, semantic_type=envelope["semantic_type"])
        event = metadata.get("lesson_promotion")
        if event is not None:
            validate_lesson(event)
            if episode["body"] != canonical_json(event) or episode["authority"] != "runtime_learning":
                raise ValueError("lesson replay governance contract integrity mismatch")
    if set(source["episode_sha256"]) != identities:
        raise ValueError("lesson replay episode hash inventory mismatch")
    originals = {item["episode_id"]: item for item in source["episodes"]}
    for episode in source["episodes"]:
        event = episode.get("metadata", {}).get("lesson_promotion")
        if event is None:
            continue
        input_cutoff = event["payload"].get("input_cutoff")
        if input_cutoff is not None and (set(input_cutoff) != {"watermark", "as_of"} or
                type(input_cutoff["watermark"]) is not int or not 0 <= input_cutoff["watermark"] < episode["sequence"] or
                memory_timestamp(input_cutoff["as_of"]) != memory_timestamp(event["as_of"])):
            raise ValueError("lesson replay invalid original input cutoff")
        boundary = {**snapshot, "watermark": episode["sequence"], "as_of": event["as_of"]}
        if memory_timestamp(event["as_of"]) > cutoff:
            raise ValueError("lesson replay event outside frozen boundary")
        try:
            qualify_episode(episode, boundary, originals.__getitem__, ancestors=frozenset({"lesson-replay"}))
            for reference in event["provenance"].get("input_episodes", []):
                if originals[reference["episode_id"]]["content_hash"] != reference["content_hash"]:
                    raise ValueError("original input hash mismatch")
        except (KeyError, ValueError) as error:
            raise ValueError(f"lesson replay invalid original lineage: {error}") from error


def replay_lessons(frozen: dict[str, Any]) -> dict[str, Any]:
    """Reconstruct qualification without providers or any canonical memory writes."""
    if frozen.get("contract") != CONTRACT or type(frozen.get("version")) is not int or frozen["version"] != VERSION:
        raise ValueError("unsupported lesson replay version")
    if sha256(frozen.get("source")) != frozen.get("source_sha256"):
        raise ValueError("lesson replay integrity mismatch")
    source = copy.deepcopy(frozen["source"])
    _validate_source(source)
    memory = _ReplayMemory(source)
    service = LessonPromotion(memory, source["snapshot"]["memory_space_id"])
    candidates, trace, identities = [], [], {}
    for episode in sorted(source["episodes"], key=lambda item: item["sequence"]):
        event = episode.get("metadata", {}).get("lesson_promotion")
        if not event or event["kind"] not in {"attempt", "rollback"}:
            memory.episodes.append(copy.deepcopy(episode))
        if not event:
            continue
        row = {"episode_id": episode["episode_id"], "sequence": episode["sequence"],
               "content_hash": episode["content_hash"], "kind": event["kind"],
               "recorded_state": event["state"], "provenance": copy.deepcopy(event["provenance"]),
               "recorded_decision": copy.deepcopy(event)}
        if event["kind"] == "candidate":
            if event["candidate_id"] in identities:
                raise ValueError("ambiguous lesson candidate identity")
            identities[event["candidate_id"]] = episode["episode_id"]
        elif event["kind"] == "attempt":
            candidate_id = identities[event["candidate_id"]]
            trial = event["payload"]["trial"]
            memory.target = episode
            prefix = "lesson:attempt:"
            request_id = episode["source_event_id"].removeprefix(prefix)
            if "frozen_pair" in event["payload"]:
                decision = service.observe_frozen_pair(request_id, candidate_id,
                                                       event["payload"]["frozen_pair"], as_of=event["as_of"])["decision"]
            else:
                decision = service.observe(request_id, candidate_id, trial["outcome_episode_id"], trial["baseline_episode_id"],
                                           subject=trial["subject"], market_state=trial["market_state"], as_of=event["as_of"])["decision"]
            memory.target = None
            row["recomputed_decision"] = decision
            row["matches_recorded"] = decision == event
        elif event["kind"] == "rollback":
            memory.target = episode
            revision = event["provenance"].get("revision_episode_id", event["provenance"].get("promotion_episode_id"))
            decision = service.rollback(episode["source_event_id"].removeprefix("lesson:rollback:"), revision,
                                        reason=event["payload"]["reason"], as_of=event["as_of"])["decision"]
            memory.target = None
            row["recomputed_decision"] = decision
            row["matches_recorded"] = decision == event
        trace.append(row)
    for candidate_id in identities.values():
        candidates.append({"episode_id": candidate_id,
                           "assessment": service.assess(candidate_id, as_of=source["snapshot"]["as_of"])})
    return {"contract": CONTRACT, "version": VERSION, "source_sha256": frozen["source_sha256"], "source": source,
            "trace": trace, "qualification": {"candidates": candidates, "scope": "offline_frozen_replay_only", "production_strategy_approved": False},
            "evaluation_vector": {
                "delivery_speed": {"status": "not_measured", "measurements": {"measured": False}, "reason": "offline_replay_has_no_live_delivery_clock"},
                "qualification_probability": {"status": "not_measured", "measurements": {"measured": False}, "reason": "frozen_examples_do_not_estimate_live_probability"},
                "research_quality": {"status": "not_measured", "measurements": {"measured": False}, "reason": "replay_does_not_grade_research"},
                "judgment_outcome": {"status": "not_measured", "measurements": {"measured": False}, "reason": "no_live_profitability_evidence"},
                "safety_reliability": {"status": "pass", "measurements": {"measured": True, "read_only": True}},
            }}


def install_qualification() -> dict[str, Any]:
    """Measured contract checks over isolated fixtures, not live investment axes."""
    memory = InMemoryMemoryAdapter()
    space, proposed_at, observed_at = "install-lesson-replay", "2026-09-21T08:10:00Z", "2026-09-22T08:10:00Z"

    def append(event_id: str, kind: str, body: str, at: str, metadata: dict[str, Any] | None = None) -> str:
        write_kind, episode_type, authority = {
            "evidence": ("evidence", "external_evidence", "immutable_source_reference"),
            "outcome": ("learning", "outcome", "runtime_learning"),
            "judgment": ("ai_message", "ai_message", "published_ai_message"),
        }[kind]
        return write_memory(memory, write_kind, {
            "memory_space_id": space, "source_system": "stock-advisor", "source_event_id": event_id,
            "episode_type": episode_type, "authority": authority,
            "body": body, "content_hash": "auto", "protocol_version": "memoryhub/v1",
            "occurred_at": at, "known_at": at, "submitted_at": at, "metadata": metadata or {},
        }, semantic_type=kind)["episode_id"]

    evidence = append("evidence", "evidence", "冻结证据与反证；不是实盘收益证明。", proposed_at)
    service = LessonPromotion(memory, space)
    candidate = service.propose("install-candidate", "Apply only in independently tested market states",
                                market_states=["trend_expansion"], evidence_episode_ids=[evidence],
                                counterevidence_episode_ids=[evidence], as_of=proposed_at)
    early = freeze_lessons(memory, space, as_of=proposed_at)
    early_receipt = replay_lessons(early)
    outcomes = []
    for label, status, direction, text in (("candidate", "incorrect", "bullish", "600519上涨"),
                                           ("baseline", "correct", "bearish", "600519下跌")):
        judgment = append(f"judgment:{label}", "judgment", text, proposed_at, {"stage": "m1_judgment"})
        snapshot = {"subjects": ["600519"], "direction": direction, "qualified": True, "benchmark": "000300",
                    "reference_at": proposed_at, "window_end": observed_at, "original_judgment_text": text}
        result = {"verification_status": status, "observations": [{"subject": "600519", "mae": 0.0, "excess_return": 99.0}]}
        body = {"result": result, "reflection": {}, "judgment_snapshot": snapshot, "parent_episode_ids": [judgment]}
        outcomes.append(append(label, "outcome", canonical_json(body), observed_at, {
            "cycle_id": "install-cycle", "horizon": "T+1", "market_state": "trend_expansion", "outcome_result": result,
            "reflection": {}, "judgment_snapshot": snapshot, "parent_episode_ids": [judgment], "stage": "m1_judgment",
        }))
    service.observe("install-model-attempt", candidate["episode_id"], *outcomes,
                    subject="600519", market_state="trend_expansion", as_of=observed_at)
    window = append("window", "evidence", canonical_json({
        "contract": "LessonFrozenWindow/v1", "version": 1, "cycle_id": "install-cycle", "subject": "600519", "horizon": "T+1", "benchmark": "000300",
        "reference_at": proposed_at, "window_end": observed_at, "reference_price": 100, "reference_benchmark_price": 100,
        "market_regime": {"index_trend": 0.1, "breadth": 0.7, "turnover_change": 0.1, "volatility": 0.2},
        "bars": [{"start_at": proposed_at, "end_at": observed_at, "close": 110, "benchmark_close": 101, "low": 99, "high": 111}],
    }), observed_at)
    factual = service.observe_frozen_pair("install-factual-attempt", candidate["episode_id"], {
        "contract": "LessonFrozenPair/v1", "version": 1, "subject": "600519",
        "outcome_episode_id": outcomes[0], "baseline_episode_id": outcomes[1], "window_episode_id": window,
    }, as_of=observed_at)
    service.rollback("install-rollback", candidate["episode_id"], reason="Counterexample invalidates applicability", as_of=observed_at)
    frozen = freeze_lessons(memory, space, as_of=observed_at)
    original, history = copy.deepcopy(frozen), copy.deepcopy(memory.export_space(space))
    first, second = replay_lessons(frozen), replay_lessons(copy.deepcopy(frozen))
    checks = {"frozen_replay": first == second, "source_unchanged": frozen == original,
              "canonical_memory_unchanged": memory.export_space(space) == history,
              "earlier_snapshot_unchanged": replay_lessons(early) == early_receipt,
              "model_metrics_not_facts": first["trace"][1]["recomputed_decision"]["payload"]["trial"]["support"] == 0,
              "factual_pair_reconstructed": first["trace"][2]["recomputed_decision"] == factual["decision"] and
                  first["trace"][2]["recomputed_decision"]["payload"]["trial"]["support"] == 1,
              "rollback_reconstructed": first["qualification"]["candidates"][0]["assessment"]["state"] == "rolled_back"}
    damaged = copy.deepcopy(frozen)
    damaged["source"]["episodes"][0]["body"] = "rewritten evidence"
    damaged["source_sha256"] = sha256(damaged["source"])
    try:
        replay_lessons(damaged)
        checks["episode_integrity_rejected"] = False
    except ValueError:
        checks["episode_integrity_rejected"] = True
    return {"contract": "LessonPromotionInstallQualification/v1", "version": VERSION,
            "qualified": all(checks.values()), "checks": checks,
            "source_sha256": first["source_sha256"], "replay_sha256": sha256(first),
            "scope": "offline_frozen_replay_only", "production_strategy_approved": False,
            "evaluation_vector": first["evaluation_vector"]}


if __name__ == "__main__":
    # ASCII JSON escapes preserve original Unicode through Windows PowerShell's
    # native stdout decoding without silently changing hashed evidence text.
    print(json.dumps(install_qualification(), ensure_ascii=True, sort_keys=True, separators=(",", ":")))
