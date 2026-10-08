"""Runtime LessonPromotionSpec/v1 over the sole MemoryHub Episode Ledger.

Candidates, paired attempts and governance decisions are immutable episodes.
Promotion is fail-closed until authoritative outcome verification and prospective
pair registration exist. Stored model metrics are never treated as factual scores.
Failed/inconclusive attempts do not replace an archived qualified lesson revision.
"""
from __future__ import annotations

import copy
import math
from statistics import NormalDist
from typing import Any

from .memory_port import MemoryPort, MemoryUnavailable
from .memory_retrieval import build_profile, memory_timestamp, qualify_episode
from .memory_type import sha256
from .memory_write import canonical_json, write_memory

CONTRACT = "LessonPromotionSpec/v1"
VERSION = 1
POLICY = {
    "version": "lesson-maturity/v1", "alpha": 0.05,
    "minimum_support_lower_bound": 0.7,
    "noninferiority_tolerance": 0.15,
}
STATES = frozenset({"candidate", "promoted", "failed", "inconclusive", "superseded", "rolled_back"})


def validate(value: dict[str, Any]) -> None:
    required = {"contract", "version", "policy_version", "kind", "candidate_id", "hypothesis", "market_states", "as_of", "state", "provenance", "payload"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("invalid LessonPromotion fields")
    if value["contract"] != CONTRACT or type(value["version"]) is not int or value["version"] != VERSION or value["policy_version"] != POLICY["version"]:
        raise ValueError("unsupported LessonPromotion version")
    if value["state"] not in STATES or value["kind"] not in {"candidate", "attempt", "rollback", "supersede"}:
        raise ValueError("invalid LessonPromotion state")
    for field in ("candidate_id", "hypothesis"):
        if not isinstance(value[field], str) or not value[field].strip():
            raise ValueError(f"LessonPromotion requires {field}")
    if not isinstance(value["market_states"], list) or any(not isinstance(state, str) or not state.strip() for state in value["market_states"]):
        raise ValueError("LessonPromotion requires applicable market states")
    memory_timestamp(value["as_of"])
    if not isinstance(value["payload"], dict) or not isinstance(value["provenance"], dict):
        raise ValueError("LessonPromotion requires payload and provenance")


def _interval(successes: int, total: int, strata: int) -> list[float]:
    if total == 0:
        return [0.0, 1.0]
    # Alpha spending across successive assessments, strata and three protected
    # dimensions. This prevents repeated peeking from becoming a count gate.
    alpha = POLICY["alpha"] / (3 * strata * total * (total + 1))
    z = NormalDist().inv_cdf(1 - alpha / 2)
    p, z2 = successes / total, z * z
    center = (p + z2 / (2 * total)) / (1 + z2 / total)
    radius = z * math.sqrt(p * (1 - p) / total + z2 / (4 * total * total)) / (1 + z2 / total)
    return [max(0.0, center - radius), min(1.0, center + radius)]


class LessonPromotion:
    def __init__(self, memory: MemoryPort | None, memory_space_id: str) -> None:
        if memory is None:
            raise MemoryUnavailable("MemoryHub is required for lesson promotion")
        self.memory, self.space = memory, memory_space_id

    def _read(self, as_of: str) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        snapshot = self.memory.begin_snapshot({"memory_space_id": self.space, "stage": "reflection", "as_of": as_of, "cycle_id": None})
        # Export contains original ledger episodes, not a search's truncated or
        # relevance-ranked window. Apply the frozen watermark and all clocks.
        episodes = {
            item["episode_id"]: item for item in self.memory.export_space(self.space)["episodes"]
            if item["sequence"] <= snapshot["watermark"] and all(
                memory_timestamp(item[field]) <= memory_timestamp(as_of)
                for field in ("occurred_at", "known_at", "submitted_at")
            )
        }
        return snapshot, episodes

    def _candidate(self, episode_id: str, episodes: dict[str, dict[str, Any]]) -> dict[str, Any]:
        event = episodes[episode_id].get("metadata", {}).get("lesson_promotion")
        validate(event)
        if event["kind"] != "candidate":
            raise ValueError("lesson identity must reference a candidate episode")
        return event

    def _append(self, request_id: str, event: dict[str, Any], parents: list[str]) -> dict[str, Any]:
        validate(event)
        metadata = {"lesson_promotion": event, "parent_episode_ids": sorted(set(parents))}
        if event["state"] == "promoted":
            metadata["memory_retrieval"] = build_profile(
                reliability="verified", outcome_support="supported", lesson_state="verified",
                market_states=event["market_states"],
                evidence_episode_ids=event["payload"]["evidence_episode_ids"],
                outcome_episode_ids=event["payload"]["outcome_episode_ids"],
            )
        receipt = write_memory(self.memory, "learning", {
            "memory_space_id": self.space, "source_system": "stock-advisor",
            "source_event_id": f"lesson:{request_id}", "episode_type": "lesson",
            "authority": "runtime_learning", "protocol_version": "memoryhub/v1", "content_hash": "auto",
            "body": canonical_json(event), "occurred_at": event["as_of"], "known_at": event["as_of"], "submitted_at": event["as_of"],
            "metadata": metadata,
        }, semantic_type="lesson")
        return {**receipt, "decision": copy.deepcopy(event)}

    def propose(self, request_id: str, hypothesis: str, *, market_states: list[str],
                evidence_episode_ids: list[str], counterevidence_episode_ids: list[str], as_of: str,
                parent_episode_ids: list[str] | None = None) -> dict[str, Any]:
        snapshot, episodes = self._read(as_of)
        parents = sorted(set([*evidence_episode_ids, *counterevidence_episode_ids, *(parent_episode_ids or [])]))
        if not evidence_episode_ids:
            raise ValueError("lesson candidates require evidence references")
        for ref in parents:
            qualify_episode(episodes[ref], snapshot, episodes.__getitem__, ancestors=frozenset({"lesson-governance"}))
        for ref in [*evidence_episode_ids, *counterevidence_episode_ids]:
            envelope = episodes[ref].get("metadata", {}).get("memory_type", {})
            if envelope.get("semantic_type") != "evidence":
                raise ValueError("lesson evidence must reference authoritative evidence episodes")
        event = {
            "contract": CONTRACT, "version": VERSION, "policy_version": POLICY["version"],
            "kind": "candidate", "candidate_id": request_id, "hypothesis": hypothesis,
            "market_states": sorted(set(market_states)), "as_of": as_of, "state": "candidate",
            "provenance": {"evidence_episode_ids": sorted(set(evidence_episode_ids)), "counterevidence_episode_ids": sorted(set(counterevidence_episode_ids))},
            "payload": {"parent_episode_ids": parent_episode_ids or [],
                        "proposal_cutoff": {"as_of": as_of, "watermark": snapshot["watermark"]}},
        }
        return self._append(f"candidate:{request_id}", event, parents)

    def _trial(self, candidate: dict[str, Any], good: dict[str, Any], baseline: dict[str, Any],
               subject: str, market_state: str) -> dict[str, Any]:
        for episode in (good, baseline):
            if episode.get("authority") != "runtime_learning" or episode.get("metadata", {}).get("memory_type", {}).get("semantic_type") != "outcome":
                raise ValueError("lesson trials require Runtime-owned outcome episodes")
        gm, bm = good["metadata"], baseline["metadata"]
        if good["episode_id"] == baseline["episode_id"] or not gm.get("cycle_id") or gm.get("cycle_id") != bm.get("cycle_id") or gm.get("horizon") != bm.get("horizon"):
            raise ValueError("lesson trials require distinct paired outcomes in one cycle and horizon")
        reasons = ["authoritative_outcome_verification_unavailable", "prospective_pair_registration_unavailable"]
        if market_state == "unknown" or market_state not in candidate["market_states"] or gm.get("market_state") != market_state or bm.get("market_state") != market_state:
            reasons.append("unqualified_market_state")
        # Runtime ownership of an append is not verification of its model payload.
        # The current fact seam does not bind a complete judgment window, benchmark,
        # adverse excursion or market-state classification to these observations.
        # Keep the attempt, but never use its claimed numeric metrics as facts.
        status = gm["outcome_result"].get("verification_status")
        return {"cycle_id": gm["cycle_id"], "market_state": market_state, "subject": subject,
                "outcome_episode_id": good["episode_id"], "baseline_episode_id": baseline["episode_id"],
                "state": "superseded" if status == "superseded" else "inconclusive",
                "reported_verification_status": status, "support": 0, "baseline_support": 0,
                "quality_passed": False, "safety_passed": False,
                "reasons": reasons}

    def _assessment(self, candidate: dict[str, Any], trials: list[dict[str, Any]], *, hypothesis_sequence: int) -> dict[str, Any]:
        # All horizons/subjects/duplicate requests from a cycle form one unit.
        # Select the worst result, never the most flattering repeated hit.
        units: dict[str, dict[str, Any]] = {}
        for trial in trials:
            prior = units.get(trial["cycle_id"])
            if prior is None:
                units[trial["cycle_id"]] = dict(trial)
            else:
                prior["support"] = min(prior["support"], trial["support"])
                prior["baseline_support"] = max(prior["baseline_support"], trial["baseline_support"])
                prior["quality_passed"] &= trial["quality_passed"]
                prior["safety_passed"] &= trial["safety_passed"]
                if prior["market_state"] != trial["market_state"]:
                    prior["market_state"] = "conflicted"
        # The ledger sequence is unique even for concurrent proposals. Spend a
        # summable allocation per hypothesis, including failed/superseded ones;
        # deleting competitors from consideration must never replenish alpha.
        multiplicity = hypothesis_sequence * (hypothesis_sequence + 1)
        strata, mature = {}, bool(candidate["market_states"] and candidate["provenance"].get("counterevidence_episode_ids"))
        for state in candidate["market_states"]:
            rows = [trial for trial in units.values() if trial["market_state"] == state]
            n = len(rows)
            support = _interval(sum(row["support"] for row in rows), n, len(candidate["market_states"]) * multiplicity)
            baseline = _interval(sum(row["baseline_support"] for row in rows), n, len(candidate["market_states"]) * multiplicity)
            harm = _interval(sum(not row["quality_passed"] or not row["safety_passed"] for row in rows), n, len(candidate["market_states"]) * multiplicity)
            qualified = bool(rows) and support[0] >= POLICY["minimum_support_lower_bound"] and support[0] > baseline[1] and harm[1] <= POLICY["noninferiority_tolerance"]
            mature &= qualified
            strata[state] = {"support_interval": support, "baseline_interval": baseline, "harm_interval": harm, "qualified": qualified}
        safety = bool(units) and all(row["safety_passed"] for row in units.values())
        quality = bool(units) and all(row["quality_passed"] for row in units.values())
        mature &= safety and quality and all(row["market_state"] != "conflicted" for row in units.values())
        # Do not let old attempt records, caller-supplied flags, or enough model
        # hits bypass missing factual verification and prospective registration.
        return {"state": "inconclusive",
                "blockers": ["authoritative_outcome_verification_unavailable",
                             "prospective_pair_registration_unavailable"],
                "maturity": {"independent_cycles": len(units), "hypothesis_sequence": hypothesis_sequence,
                             "hypothesis_alpha": POLICY["alpha"] / multiplicity,
                             "strata": strata, "statistically_qualified": bool(mature),
                             "safety_passed": safety, "quality_passed": quality},
                "policy": copy.deepcopy(POLICY)}

    def assess(self, candidate_episode_id: str, *, as_of: str) -> dict[str, Any]:
        _, episodes = self._read(as_of)
        candidate = self._candidate(candidate_episode_id, episodes)
        trials = [event["payload"]["trial"] for episode in episodes.values()
                  if (event := episode.get("metadata", {}).get("lesson_promotion"))
                  and event.get("kind") == "attempt" and event.get("candidate_id") == candidate["candidate_id"]]
        return self._assessment(candidate, trials, hypothesis_sequence=episodes[candidate_episode_id]["sequence"])

    def observe(self, request_id: str, candidate_episode_id: str, outcome_episode_id: str, baseline_episode_id: str,
                *, subject: str, market_state: str, as_of: str) -> dict[str, Any]:
        snapshot, episodes = self._read(as_of)
        candidate = self._candidate(candidate_episode_id, episodes)
        request = {"candidate_episode_id": candidate_episode_id, "outcome_episode_id": outcome_episode_id,
                   "baseline_episode_id": baseline_episode_id, "subject": subject, "market_state": market_state, "as_of": as_of}
        for episode in episodes.values():
            if episode["source_event_id"] == f"lesson:attempt:{request_id}":
                event = episode["metadata"]["lesson_promotion"]
                if event["provenance"]["request_sha256"] != sha256(request):
                    raise MemoryUnavailable("immutable lesson request conflict")
                return {**{key: episode[key] for key in ("episode_id", "sequence", "content_hash", "protocol_version")}, "decision": copy.deepcopy(event)}
        parents = [candidate_episode_id, outcome_episode_id, baseline_episode_id]
        for ref in parents:
            qualify_episode(episodes[ref], snapshot, episodes.__getitem__, ancestors=frozenset({"lesson-governance"}))
        trial = self._trial(candidate, episodes[outcome_episode_id], episodes[baseline_episode_id], subject, market_state)
        cutoff = candidate["payload"].get("proposal_cutoff")
        if not cutoff:
            trial["reasons"].append("proposal_cutoff_unavailable")
        elif any(episodes[ref]["sequence"] <= cutoff["watermark"] or
                 memory_timestamp(episodes[ref]["known_at"]) <= memory_timestamp(cutoff["as_of"])
                 for ref in (outcome_episode_id, baseline_episode_id)):
            trial["reasons"].append("outcome_known_before_proposal")
        trials = [event["payload"]["trial"] for episode in episodes.values()
                  if (event := episode.get("metadata", {}).get("lesson_promotion"))
                  and event.get("kind") == "attempt" and event.get("candidate_id") == candidate["candidate_id"]]
        assessment = self._assessment(candidate, [*trials, trial], hypothesis_sequence=episodes[candidate_episode_id]["sequence"])
        if assessment["state"] == "promoted":
            parents = sorted(set([candidate_episode_id, *(ref for item in [*trials, trial]
                                  for ref in (item["outcome_episode_id"], item["baseline_episode_id"]))]))
        event = {**candidate, "kind": "attempt", "state": assessment["state"], "as_of": as_of,
                 "provenance": {"request_sha256": sha256(request), "input_episodes": [
                     {"episode_id": ref, "content_hash": episodes[ref]["content_hash"]} for ref in parents]},
                 "payload": {"trial": trial, **assessment,
                             "evidence_episode_ids": candidate["provenance"]["evidence_episode_ids"],
                             "outcome_episode_ids": sorted(set(ref for item in [*trials, trial]
                                 for ref in (item["outcome_episode_id"], item["baseline_episode_id"])))}}
        return self._append(f"attempt:{request_id}", event, parents)

    def consume_trial(self, value: dict[str, Any], outcome_episode_id: str, *, request_id: str,
                      market_state: str, as_of: str) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != {"contract", "version", "candidate_episode_id", "baseline_episode_id", "subject"} or value.get("contract") != CONTRACT or type(value.get("version")) is not int or value["version"] != VERSION:
            raise ValueError("invalid versioned LessonPromotion trial request")
        return self.observe(request_id, value["candidate_episode_id"], outcome_episode_id, value["baseline_episode_id"],
                            subject=value["subject"], market_state=market_state, as_of=as_of)

    def rollback(self, request_id: str, promotion_episode_id: str, *, reason: str, as_of: str) -> dict[str, Any]:
        _, episodes = self._read(as_of)
        promotion = episodes[promotion_episode_id]["metadata"]["lesson_promotion"]
        validate(promotion)
        if promotion["state"] != "promoted" or not reason.strip():
            raise ValueError("rollback requires a promoted revision and reason")
        event = {**promotion, "kind": "rollback", "state": "rolled_back", "as_of": as_of,
                 "provenance": {"promotion_episode_id": promotion_episode_id, "content_hash": episodes[promotion_episode_id]["content_hash"]},
                 "payload": {"reason": reason, "rollback_target": "candidate"}}
        return self._append(f"rollback:{request_id}", event, [promotion_episode_id])

    def supersede(self, request_id: str, candidate_episode_id: str, replacement_episode_id: str,
                  *, reason: str, as_of: str) -> dict[str, Any]:
        _, episodes = self._read(as_of)
        candidate = self._candidate(candidate_episode_id, episodes)
        replacement = episodes[replacement_episode_id]["metadata"]["lesson_promotion"]
        validate(replacement)
        if replacement["state"] != "promoted" or replacement["candidate_id"] == candidate["candidate_id"] or not reason.strip():
            raise ValueError("supersession requires a different promoted replacement and reason")
        event = {**candidate, "kind": "supersede", "state": "superseded", "as_of": as_of,
                 "provenance": {"candidate_episode_id": candidate_episode_id, "replacement_episode_id": replacement_episode_id},
                 "payload": {"reason": reason}}
        return self._append(f"supersede:{request_id}", event, [candidate_episode_id, replacement_episode_id])
