"""Runtime LessonPromotionSpec/v1 over the sole MemoryHub Episode Ledger.

Candidates, paired attempts and governance decisions are immutable episodes.
Bounded frozen market evidence can qualify professional lesson retrieval offline;
model metrics never become factual scores. This grants no production-strategy or
schedule authority. Failed/inconclusive attempts do not replace qualified lessons.
"""
from __future__ import annotations

import copy
import hashlib
import json
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
    if alpha / 2 == 0:
        return [0.0, 1.0]
    # The lower tail avoids cancellation to exactly 1 for old/high-sequence
    # ledgers, where the summable hypothesis allocation becomes very small.
    z = -NormalDist().inv_cdf(alpha / 2)
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

    def _recover(self, request_id: str, request: dict[str, Any], episodes: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
        for episode in episodes.values():
            if episode["source_event_id"] != f"lesson:{request_id}":
                continue
            event = episode["metadata"]["lesson_promotion"]
            validate(event)
            expected = event["provenance"].get("request_sha256")
            # Pre-recovery candidates retain sufficient immutable inputs to
            # recover without re-freezing their original ledger watermark.
            if expected is None and event["kind"] == "candidate":
                expected = sha256({"hypothesis": event["hypothesis"], "market_states": event["market_states"],
                                   **{key: event["provenance"][key] for key in ("evidence_episode_ids", "counterevidence_episode_ids")},
                                   "parent_episode_ids": sorted(set(event["payload"]["parent_episode_ids"])), "as_of": event["as_of"]})
            if expected is None and event["kind"] == "rollback":
                expected = sha256({"revision_episode_id": event["provenance"].get("revision_episode_id", event["provenance"].get("promotion_episode_id")),
                                   "reason": event["payload"]["reason"], "as_of": event["as_of"]})
            if expected != sha256(request):
                raise MemoryUnavailable("immutable lesson request conflict")
            return {**{key: episode[key] for key in ("episode_id", "sequence", "content_hash", "protocol_version")},
                    "decision": copy.deepcopy(event)}
        return None

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
        request = {"hypothesis": hypothesis, "market_states": sorted(set(market_states)),
                   "evidence_episode_ids": sorted(set(evidence_episode_ids)),
                   "counterevidence_episode_ids": sorted(set(counterevidence_episode_ids)),
                   "parent_episode_ids": sorted(set(parent_episode_ids or [])), "as_of": as_of}
        recovered = self._recover(f"candidate:{request_id}", request, episodes)
        if recovered is not None:
            return recovered
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
            "provenance": {"request_sha256": sha256(request), "evidence_episode_ids": sorted(set(evidence_episode_ids)), "counterevidence_episode_ids": sorted(set(counterevidence_episode_ids))},
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
        reasons = ["frozen_factual_window_required"]
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
        # Different cycle labels do not make overlapping factual windows
        # independent. Merge connected overlaps conservatively across subjects.
        roots = {trial["cycle_id"]: trial["cycle_id"] for trial in trials}
        def root(cycle: str) -> str:
            while roots[cycle] != cycle:
                roots[cycle] = roots[roots[cycle]]
                cycle = roots[cycle]
            return cycle
        windows = sorted((memory_timestamp(trial["window"]["reference_at"]), memory_timestamp(trial["window"]["window_end"]), trial["cycle_id"])
                         for trial in trials if trial.get("window") and trial["quality_passed"])
        segment_end, segment_cycle = None, None
        for start, end, cycle in windows:
            if segment_end is not None and start < segment_end:
                roots[root(cycle)] = root(segment_cycle)
                segment_end = max(segment_end, end)
            else:
                segment_end, segment_cycle = end, cycle
        attempted_cycles = {root(trial["cycle_id"]) for trial in trials}
        units: dict[str, dict[str, Any]] = {}
        for trial in trials:
            # Failed factual outcomes are evidence; unresolved requests are not.
            # Keep both in the ledger, but never turn missing facts into harm.
            if trial.get("evaluation_kind") != "frozen_facts" or not trial["quality_passed"]:
                continue
            cycle = root(trial["cycle_id"])
            prior = units.get(cycle)
            if prior is None:
                units[cycle] = dict(trial)
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
        # Absolute catastrophic excursion remains a hard safety boundary.
        # Ordinary paired harm is governed by the conservative interval above,
        # not an irreversible veto from one adverse observation.
        catastrophic = any(trial["quality_passed"] and
                           trial.get("metrics", {}).get("adverse_excursion", 0) > 0.15 for trial in trials)
        safety = bool(units) and not catastrophic
        quality = bool(units) and all(row["market_state"] != "conflicted" for row in units.values())
        mature &= safety and quality
        failed = catastrophic or any(
            item["support_interval"][1] < item["baseline_interval"][0]
            or item["harm_interval"][0] > POLICY["noninferiority_tolerance"] for item in strata.values())
        blockers = [] if mature else sorted({reason for trial in trials for reason in trial.get("reasons", [])})
        if not candidate["provenance"].get("counterevidence_episode_ids"):
            blockers.append("counterevidence_required")
        if not candidate["market_states"]:
            blockers.append("applicable_market_states_required")
        if not trials:
            blockers.append("paired_frozen_evidence_required")
        return {"state": "promoted" if mature else "failed" if failed else "inconclusive",
                "blockers": blockers,
                "maturity": {"independent_cycles": len(attempted_cycles), "qualified_cycles": len(units),
                             "adverse_cycles": sum(not row["safety_passed"] for row in units.values()),
                             "hypothesis_sequence": hypothesis_sequence,
                             "hypothesis_alpha": POLICY["alpha"] / multiplicity,
                             "strata": strata, "statistically_qualified": bool(mature),
                             "safety_passed": safety, "quality_passed": quality},
                "policy": copy.deepcopy(POLICY)}

    def _trials(self, candidate: dict[str, Any], episodes: dict[str, dict[str, Any]], snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        trials = []
        for episode in sorted(episodes.values(), key=lambda row: row["sequence"]):
            event = episode.get("metadata", {}).get("lesson_promotion")
            if not event or event.get("kind") != "attempt" or event.get("candidate_id") != candidate["candidate_id"]:
                continue
            validate(event)
            if episode["body"] != canonical_json(event) or episode["authority"] != "runtime_learning":
                raise ValueError("lesson_contract_integrity_conflict")
            payload = event["payload"]
            if payload.get("frozen_pair"):
                boundary = {**snapshot, **payload.get("input_cutoff", {"watermark": episode["sequence"] - 1, "as_of": event["as_of"]})}
                trial = self._frozen_trial(candidate, payload["frozen_pair"], episodes, boundary)
            else:
                old = payload["trial"]
                trial = self._trial(candidate, episodes[old["outcome_episode_id"]], episodes[old["baseline_episode_id"]], old["subject"], old["market_state"])
            trials.append(trial)
        return trials

    def _governed(self, candidate: dict[str, Any], episodes: dict[str, dict[str, Any]], assessment: dict[str, Any]) -> dict[str, Any]:
        governance = [episode for episode in episodes.values()
                      if (event := episode.get("metadata", {}).get("lesson_promotion"))
                      and event["candidate_id"] == candidate["candidate_id"]
                      and event["kind"] in {"rollback", "supersede"}]
        if governance:
            assessment["state"] = max(governance, key=lambda episode: episode["sequence"])["metadata"]["lesson_promotion"]["state"]
        return assessment

    def assess(self, candidate_episode_id: str, *, as_of: str) -> dict[str, Any]:
        snapshot, episodes = self._read(as_of)
        candidate = self._candidate(candidate_episode_id, episodes)
        trials = self._trials(candidate, episodes, snapshot)
        assessment = self._assessment(candidate, trials, hypothesis_sequence=episodes[candidate_episode_id]["sequence"])
        return self._governed(candidate, episodes, assessment)

    def trace(self, candidate_episode_id: str, *, as_of: str) -> dict[str, Any]:
        _, episodes = self._read(as_of)
        candidate = self._candidate(candidate_episode_id, episodes)
        revisions = [{**{key: episode[key] for key in ("episode_id", "sequence", "content_hash")}, "decision": copy.deepcopy(event)}
                     for episode in sorted(episodes.values(), key=lambda row: row["sequence"])
                     if (event := episode.get("metadata", {}).get("lesson_promotion")) and event["candidate_id"] == candidate["candidate_id"]]
        return {"contract": CONTRACT, "version": VERSION, "candidate_episode_id": candidate_episode_id,
                "effective_state": self.assess(candidate_episode_id, as_of=as_of)["state"], "revisions": revisions}

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
        trials = self._trials(candidate, episodes, snapshot)
        assessment = self._governed(candidate, episodes, self._assessment(candidate, [*trials, trial], hypothesis_sequence=episodes[candidate_episode_id]["sequence"]))
        if assessment["state"] == "promoted":
            parents = sorted(set([candidate_episode_id, *(ref for item in [*trials, trial]
                                  for ref in (item["outcome_episode_id"], item["baseline_episode_id"]))]))
        event = {**candidate, "kind": "attempt", "state": assessment["state"], "as_of": as_of,
                 "provenance": {"request_sha256": sha256(request), "input_episodes": [
                     {"episode_id": ref, "content_hash": episodes[ref]["content_hash"]} for ref in parents]},
                 "payload": {"trial": trial, **assessment, "request": copy.deepcopy(request),
                             "input_cutoff": {"watermark": snapshot["watermark"], "as_of": as_of},
                             "evidence_episode_ids": candidate["provenance"]["evidence_episode_ids"],
                             "outcome_episode_ids": sorted(set(ref for item in [*trials, trial]
                                 for ref in (item["outcome_episode_id"], item["baseline_episode_id"])))}}
        return self._append(f"attempt:{request_id}", event, parents)

    def _frozen_trial(self, candidate: dict[str, Any], pair: dict[str, Any], episodes: dict[str, dict[str, Any]],
                      snapshot: dict[str, Any]) -> dict[str, Any]:
        from .governance import classify_regime
        import re
        trial = {"cycle_id": "unresolved:" + sha256(pair), "market_state": "unknown", "subject": pair["subject"],
                 "outcome_episode_id": pair["outcome_episode_id"], "baseline_episode_id": pair["baseline_episode_id"],
                 "reported_verification_status": None, "support": 0, "baseline_support": 0,
                 "quality_passed": False, "safety_passed": False, "reasons": []}
        trial["evaluation_kind"] = "frozen_facts"
        trial["evaluator_version"] = "lesson-frozen-evaluator/v1"
        trial["evaluator_policy"] = {"maximum_adverse_excursion": 0.15}
        try:
            if any(pair[key] not in episodes for key in ("outcome_episode_id", "baseline_episode_id", "window_episode_id")):
                raise ValueError("missing_frozen_input")
            good, baseline = (episodes[pair[key]] for key in ("outcome_episode_id", "baseline_episode_id"))
            validated = self._trial(candidate, good, baseline, pair["subject"], good["metadata"].get("market_state", "unknown"))
            trial.update({**validated, "reasons": []})
            cutoff = candidate["payload"].get("proposal_cutoff")
            if not cutoff or any(episodes[ref]["sequence"] <= cutoff["watermark"] or
                                 memory_timestamp(episodes[ref]["known_at"]) <= memory_timestamp(cutoff["as_of"])
                                 for ref in (pair["outcome_episode_id"], pair["baseline_episode_id"], pair["window_episode_id"])):
                raise ValueError("outcome_known_before_proposal")
            for ref in (pair["outcome_episode_id"], pair["baseline_episode_id"], pair["window_episode_id"]):
                qualify_episode(episodes[ref], {**snapshot, "stage": "m1_judgment", "cycle_id": trial["cycle_id"]},
                                episodes.__getitem__, ancestors=frozenset({"lesson-governance"}))
            source = episodes[pair["window_episode_id"]]
            if source.get("authority") not in {"immutable_source_reference", "mutable_source_snapshot"} or source["metadata"]["memory_type"]["semantic_type"] != "evidence":
                raise ValueError("frozen_window_requires_external_evidence")
            if source["content_hash"] != "sha256:" + hashlib.sha256(source["body"].encode("utf-8")).hexdigest():
                raise ValueError("frozen_window_integrity_conflict")
            window = json.loads(source["body"])
            required = {"contract", "version", "cycle_id", "subject", "horizon", "reference_at", "window_end", "reference_price", "reference_benchmark_price", "benchmark", "market_regime", "bars"}
            if set(window) != required or window["contract"] != "LessonFrozenWindow/v1" or type(window["version"]) is not int or window["version"] != 1:
                raise ValueError("unsupported_frozen_window_contract")
            def positive(value: Any) -> bool:
                return type(value) in (int, float) and math.isfinite(value) and value > 0
            if not positive(window["reference_price"]) or not positive(window["reference_benchmark_price"]):
                raise ValueError("invalid_frozen_reference_prices")
            start, end = memory_timestamp(window["reference_at"]), memory_timestamp(window["window_end"])
            if start >= end or memory_timestamp(source["known_at"]) < end:
                raise ValueError("incomplete_frozen_window")
            regime = window["market_regime"]
            if not isinstance(regime, dict) or set(regime) != {"index_trend", "breadth", "turnover_change", "volatility"} or any(type(value) not in (int, float) or not math.isfinite(value) for value in regime.values()) or not 0 <= regime["breadth"] <= 1 or regime["volatility"] < 0:
                raise ValueError("invalid_frozen_market_regime")
            state = classify_regime(regime)
            trial["market_state"] = state
            if state not in candidate["market_states"] or any(row["metadata"].get("market_state") != state for row in (good, baseline)):
                raise ValueError("unqualified_market_state")
            directions = []
            for row in (good, baseline):
                metadata = row["metadata"]
                frozen = metadata["judgment_snapshot"]
                if row["episode_id"] == good["episode_id"] and frozen.get("lesson_candidate_id") != candidate["candidate_id"]:
                    raise ValueError("frozen_candidate_identity_conflict")
                expected = {"result": metadata["outcome_result"], "reflection": metadata["reflection"],
                            "judgment_snapshot": frozen, "parent_episode_ids": metadata["parent_episode_ids"]}
                if row["body"] != canonical_json(expected):
                    raise ValueError("frozen_outcome_integrity_conflict")
                if window["cycle_id"] != metadata["cycle_id"] or window["horizon"] != metadata["horizon"] or window["subject"] != pair["subject"] or pair["subject"] not in frozen["subjects"]:
                    raise ValueError("frozen_pair_context_conflict")
                if frozen.get("qualified") is not True or memory_timestamp(frozen["reference_at"]) != start or memory_timestamp(frozen["window_end"]) != end:
                    raise ValueError("frozen_judgment_window_conflict")
                if not isinstance(window["benchmark"], str) or not window["benchmark"].strip() or frozen.get("benchmark") != window["benchmark"]:
                    raise ValueError("frozen_benchmark_conflict")
                text = frozen["original_judgment_text"]
                parents = [episodes[ref] for ref in metadata["parent_episode_ids"]]
                if not any(parent.get("authority") == "published_ai_message" and parent["body"] == text and
                           memory_timestamp(parent["known_at"]) <= start and memory_timestamp(parent["submitted_at"]) <= start for parent in parents):
                    raise ValueError("unbound_frozen_judgment")
                # Only a complete unconditional claim has a factual direction.
                # Substring inference would turn negation/conditions into support.
                claim = re.fullmatch(r"\s*" + re.escape(pair["subject"]) + r"\s*(上涨|下跌)\s*[。.!！]?\s*", text)
                direction = ("bullish" if claim[1] == "上涨" else "bearish") if claim else None
                if (direction is None or direction != frozen["direction"] or
                    frozen["subjects"] != [pair["subject"]] or frozen.get("triggers") or frozen.get("invalidations")):
                    raise ValueError("unsupported_frozen_direction")
                directions.append(1 if direction == "bullish" else -1)
            cursor = start
            bars = window["bars"]
            if not isinstance(bars, list) or not bars:
                raise ValueError("incomplete_frozen_window")
            for bar in bars:
                if set(bar) != {"start_at", "end_at", "close", "benchmark_close", "low", "high"} or any(not positive(bar[key]) for key in ("close", "benchmark_close", "low", "high")):
                    raise ValueError("invalid_frozen_bar")
                if memory_timestamp(bar["start_at"]) != cursor or not cursor < memory_timestamp(bar["end_at"]) <= end or not bar["low"] <= bar["close"] <= bar["high"]:
                    raise ValueError("incomplete_or_conflicted_frozen_bars")
                cursor = memory_timestamp(bar["end_at"])
            if cursor != end:
                raise ValueError("incomplete_frozen_window")
            excess = bars[-1]["close"] / window["reference_price"] - bars[-1]["benchmark_close"] / window["reference_benchmark_price"]
            adverse = [max(0.0, 1 - min(bar["low"] for bar in bars) / window["reference_price"]) if direction == 1 else
                       max(0.0, max(bar["high"] for bar in bars) / window["reference_price"] - 1) for direction in directions]
            trial.update({"support": int(directions[0] * excess > 0), "baseline_support": int(directions[1] * excess > 0),
                          "quality_passed": True, "safety_passed": adverse[0] <= 0.15 and adverse[0] <= adverse[1],
                          "window": {"reference_at": window["reference_at"], "window_end": window["window_end"]},
                          "metrics": {"excess_return": excess, "adverse_excursion": adverse[0], "baseline_adverse_excursion": adverse[1]}})
            if excess == 0:
                raise ValueError("directional_outcome_inconclusive")
            trial["state"] = "supported" if trial["support"] and trial["safety_passed"] else "failed"
            trial["window_episode_id"] = pair["window_episode_id"]
            if trial["reported_verification_status"] == "superseded" or baseline["metadata"]["outcome_result"].get("verification_status") == "superseded":
                raise ValueError("superseded_frozen_judgment")
        except (ValueError, KeyError, TypeError) as error:
            trial.update({"state": "superseded" if str(error) == "superseded_frozen_judgment" else "inconclusive",
                          "support": 0, "baseline_support": 0, "quality_passed": False, "safety_passed": False,
                          "reasons": [str(error)]})
        return trial

    def observe_frozen_pair(self, request_id: str, candidate_episode_id: str, pair: dict[str, Any], *, as_of: str) -> dict[str, Any]:
        required = {"contract", "version", "subject", "outcome_episode_id", "baseline_episode_id", "window_episode_id"}
        if not isinstance(pair, dict) or set(pair) != required or pair["contract"] != "LessonFrozenPair/v1" or type(pair["version"]) is not int or pair["version"] != 1 or any(not isinstance(pair[key], str) or not pair[key].strip() for key in required - {"version"}):
            raise ValueError("invalid versioned LessonFrozenPair request")
        snapshot, episodes = self._read(as_of)
        request = {"candidate_episode_id": candidate_episode_id, "pair": pair, "as_of": as_of}
        recovered = self._recover(f"attempt:{request_id}", request, episodes)
        if recovered is not None:
            return recovered
        candidate = self._candidate(candidate_episode_id, episodes)
        trial = self._frozen_trial(candidate, pair, episodes, snapshot)
        trials = self._trials(candidate, episodes, snapshot)
        assessment = self._governed(candidate, episodes, self._assessment(candidate, [*trials, trial], hypothesis_sequence=episodes[candidate_episode_id]["sequence"]))
        references = {candidate_episode_id, pair["outcome_episode_id"], pair["baseline_episode_id"], pair["window_episode_id"]}
        parents = sorted(references & episodes.keys())
        window_refs = sorted({item["window_episode_id"] for item in [*trials, trial] if item.get("window_episode_id")})
        if assessment["state"] == "promoted":
            parents = sorted({*parents, *window_refs, *(ref for item in [*trials, trial] for ref in (item["outcome_episode_id"], item["baseline_episode_id"]))})
        event = {**candidate, "kind": "attempt", "state": assessment["state"], "as_of": as_of,
                 "provenance": {"request_sha256": sha256(request), "unavailable_episode_ids": sorted(references - episodes.keys()), "input_episodes": [
                     {"episode_id": ref, "content_hash": episodes[ref]["content_hash"]} for ref in parents]},
                 "payload": {"trial": trial, **assessment, "frozen_pair": copy.deepcopy(pair), "request": copy.deepcopy(request),
                             "input_cutoff": {"watermark": snapshot["watermark"], "as_of": as_of},
                             "evidence_episode_ids": sorted({*candidate["provenance"]["evidence_episode_ids"], *window_refs}),
                             "outcome_episode_ids": sorted({ref for item in [*trials, trial] for ref in (item["outcome_episode_id"], item["baseline_episode_id"])})}}
        return self._append(f"attempt:{request_id}", event, parents)

    def consume_trial(self, value: dict[str, Any], outcome_episode_id: str, *, request_id: str,
                      market_state: str, as_of: str) -> dict[str, Any]:
        required = {"contract", "version", "candidate_episode_id", "baseline_episode_id", "subject"}
        if not isinstance(value, dict) or set(value) not in (required, required | {"window_episode_id"}) or value.get("contract") != CONTRACT or type(value.get("version")) is not int or value["version"] != VERSION:
            raise ValueError("invalid versioned LessonPromotion trial request")
        if "window_episode_id" in value:
            pair = {"contract": "LessonFrozenPair/v1", "version": 1, "subject": value["subject"],
                    "outcome_episode_id": outcome_episode_id, "baseline_episode_id": value["baseline_episode_id"],
                    "window_episode_id": value["window_episode_id"]}
            return self.observe_frozen_pair(request_id, value["candidate_episode_id"], pair, as_of=as_of)
        return self.observe(request_id, value["candidate_episode_id"], outcome_episode_id, value["baseline_episode_id"],
                            subject=value["subject"], market_state=market_state, as_of=as_of)

    def rollback(self, request_id: str, promotion_episode_id: str, *, reason: str, as_of: str) -> dict[str, Any]:
        snapshot, episodes = self._read(as_of)
        request = {"revision_episode_id": promotion_episode_id, "reason": reason, "as_of": as_of}
        recovered = self._recover(f"rollback:{request_id}", request, episodes)
        if recovered is not None:
            return recovered
        promotion = episodes[promotion_episode_id]["metadata"]["lesson_promotion"]
        validate(promotion)
        qualify_episode(episodes[promotion_episode_id], snapshot, episodes.__getitem__, ancestors=frozenset({"lesson-governance"}))
        if not reason.strip():
            raise ValueError("rollback requires a lesson revision and reason")
        event = {**promotion, "kind": "rollback", "state": "rolled_back", "as_of": as_of,
                 "provenance": {"request_sha256": sha256(request), "revision_episode_id": promotion_episode_id,
                                "promotion_episode_id": promotion_episode_id, "content_hash": episodes[promotion_episode_id]["content_hash"]},
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
