"""Runtime-owned evidence identity; source assertions never grant fact authority.

EvidenceSpec is distinct from the task's Evidence v4 coverage contract and the
model's Evidence v3 selection format. No model output constructs this envelope.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from .temporal_integrity import resolve_temporal, validate_temporal

VERSION = "EvidenceSpec/v1"
KINDS = frozenset({"market_fact", "derived_calculation", "news_disclosure",
                   "social_propagation", "source_opinion", "quant_research",
                   "ai_reasoning", "ai_summary", "ai_conclusion"})
AI_KINDS = frozenset({"ai_reasoning", "ai_summary", "ai_conclusion"})
TRUTH = frozenset({"verified", "unverified", "refuted", "conflicted", "unknown"})
PROPAGATION = frozenset({"observed", "not_observed", "unknown"})


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def timestamp(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("evidence timestamp requires timezone")
    return parsed.astimezone(timezone.utc)


def source_kind(item: dict[str, Any], observation: dict[str, Any] | None = None) -> str:
    declared = str(item.get("evidence_kind") or "")
    # Explicit generated provenance dominates even a contradictory fact label.
    if item.get("generated_by") in {"ai", "llm", "model"} or item.get("origin") == "ai":
        return declared if declared in AI_KINDS else "ai_reasoning"
    if declared in KINDS:
        return declared
    if observation and str(observation.get("backend") or "") == "market":
        return "market_fact"
    return "news_disclosure"


def from_observation(item: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    """Bind untrusted source claims to runtime observation identity and clocks."""
    kind = source_kind(item, observation)
    temporal = resolve_temporal({**item, "evidence_kind": kind}, observation)
    known = temporal["known_at"]
    occurred = temporal["occurred_at"]
    body = str(item.get("excerpt_text") or "")
    truth = str(item.get("factual_status") or "unknown")
    truth = truth if truth in TRUTH else "unknown"
    propagation = str(item.get("market_propagation") or "unknown")
    propagation = propagation if propagation in PROPAGATION else "unknown"
    impact = copy.deepcopy(item.get("propagation_impact") or {})
    reference = copy.deepcopy(item.get("source_reference") or {})
    if item.get("screenshot_only") is True:
        reference["screenshot_only"] = True
    if item.get("source_strength"):
        reference["source_strength"] = item["source_strength"]
    if propagation == "observed":
        impact.setdefault("evidence_refs", [str(item.get("evidence_ref") or "")])
        if item.get("propagation_observed_from"):
            impact["observed_from"] = item["propagation_observed_from"]
        if item.get("propagation_observed_to"):
            impact["observed_to"] = item["propagation_observed_to"]
    record = {
        "contract": VERSION, "kind": kind,
        "source": {"url": str(item.get("url") or ""), "title": str(item.get("title") or ""),
                   "identity": str(item.get("source_identity") or ""), "reference": reference},
        "occurred_at": occurred, "known_at": known, "published_at": temporal["published_at"],
        "temporal_integrity": temporal, "content": body,
        "truth_status": "unknown" if kind in AI_KINDS else truth,
        "market_propagation": {"status": propagation, "impact": impact},
        "provenance": {
            "origin": "ai" if kind in AI_KINDS else "deterministic" if kind == "derived_calculation" else "external_source",
            "attempt_id": str(observation.get("attempt_id") or ""),
            "observation_id": str(observation.get("observation_id") or ""),
            "evidence_ref": str(item.get("evidence_ref") or ""),
            "operation": str(observation.get("operation") or observation.get("tool") or ""),
            "backend": str(observation.get("backend") or ""),
            "original_source": str(item.get("original_source") or ""),
            "citation_chain": copy.deepcopy(item.get("citation_chain") or []),
            "content_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "derivation": copy.deepcopy(item.get("derivation") or {}),
        },
        "propositions": copy.deepcopy(item.get("claims") or []),
        "expires_at": item.get("expires_at"),
        # A source claim of truth is not Runtime verification. Qualification
        # below grants only the appropriate, explicitly limited use.
        "external_fact": kind == "market_fact" and truth == "verified",
    }
    record["record_id"] = fingerprint(record)
    return record


def validate(record: dict[str, Any]) -> None:
    if record.get("contract") != VERSION or record.get("kind") not in KINDS:
        raise ValueError("unsupported evidence contract or kind")
    required = (
        "record_id", "source", "occurred_at", "known_at", "content",
        "published_at", "temporal_integrity", "truth_status", "market_propagation", "provenance", "external_fact",
    )
    missing = [key for key in required if key not in record]
    if missing:
        raise ValueError("evidence record missing fields: " + ", ".join(missing))
    if record.get("record_id") != fingerprint({k: v for k, v in record.items() if k != "record_id"}):
        raise ValueError("evidence record integrity mismatch")
    source = record.get("source")
    provenance = record.get("provenance")
    propagation = record.get("market_propagation")
    temporal = record.get("temporal_integrity")
    validate_temporal(temporal)
    if temporal.get("occurred_at") != record.get("occurred_at") or temporal.get("known_at") != record.get("known_at"):
        raise ValueError("evidence temporal clocks do not match record")
    if temporal.get("published_at") != record.get("published_at"):
        raise ValueError("evidence publication clock does not match record")
    if not isinstance(source, dict) or not isinstance(provenance, dict):
        raise ValueError("evidence source and provenance required")
    if any(key not in source for key in ("url", "title", "identity", "reference")):
        raise ValueError("evidence source fields required")
    if not record.get("content") or not (
        source.get("url") or source.get("identity") or source.get("reference")
    ):
        raise ValueError("evidence content and source required")
    if not isinstance(propagation, dict) or not isinstance(propagation.get("impact"), dict):
        raise ValueError("evidence propagation details required")
    if not isinstance(record["external_fact"], bool):
        raise ValueError("evidence external_fact must be boolean")
    if any(key not in provenance for key in (
        "origin", "attempt_id", "observation_id", "evidence_ref", "content_sha256",
    )):
        raise ValueError("evidence provenance fields required")
    timestamp(record["known_at"])
    # Unknown occurrence stays explicit and cannot qualify as a dated fact.
    if record.get("occurred_at"):
        timestamp(record["occurred_at"])
    if record.get("expires_at"):
        timestamp(record["expires_at"])
    if record.get("truth_status") not in TRUTH or propagation.get("status") not in PROPAGATION:
        raise ValueError("invalid independent evidence dimensions")
    if provenance.get("content_sha256") != hashlib.sha256(record["content"].encode("utf-8")).hexdigest():
        raise ValueError("evidence content hash mismatch")
    if record.get("external_fact") and (
        record["kind"] != "market_fact" or record["truth_status"] != "verified"
        or provenance.get("origin") == "ai"
    ):
        raise ValueError("generated or unverified content is not an external fact")


def qualify(record: dict[str, Any], *, as_of: str | None = None) -> dict[str, Any]:
    """Pure replayable type qualification; coverage/freshness windows stay in EvidenceGate."""
    validate(record)
    reasons: list[str] = []
    state, use = "usable", "source_report"
    kind = record["kind"]
    if kind in AI_KINDS or record["provenance"].get("origin") == "ai":
        state, use = "rejected", "reasoning_only"
        reasons.append("ai_is_not_external_evidence")
    elif not record.get("occurred_at"):
        state = "degraded"
        reasons.append("occurrence_unknown")
    elif kind == "derived_calculation":
        use = "calculation"
        derivation = record["provenance"].get("derivation") or {}
        if not all(derivation.get(k) for k in ("formula", "version", "input_refs", "unit")):
            state = "rejected"
            reasons.append("calculation_provenance_missing")
    elif kind == "quant_research":
        use = "research_only"
        if not all(record["source"]["reference"].get(k) for k in ("package_id", "version")):
            state = "rejected"
            reasons.append("research_version_missing")
    elif record.get("external_fact"):
        use = "external_fact"
    if record["truth_status"] in {"unknown", "unverified", "refuted"} and state == "usable":
        state = "degraded"
        reasons.append("content_" + record["truth_status"])
    if record["truth_status"] == "conflicted" and state != "rejected":
        state = "conflicted"
        reasons.append("unresolved_content_conflict")
    if as_of:
        cutoff = timestamp(as_of)
        if timestamp(record["known_at"]) > cutoff or (record.get("occurred_at") and timestamp(record["occurred_at"]) > cutoff):
            state = "rejected"
            reasons.append("not_known_at_cutoff")
        elif record.get("expires_at") and timestamp(record["expires_at"]) <= cutoff and state != "rejected":
            state = "expired"
            reasons.append("expired_at_cutoff")
    return {"contract": VERSION, "record_id": record["record_id"], "state": state,
            "permitted_use": use, "reasons": reasons,
            "truth_status": record["truth_status"],
            "propagation_status": record["market_propagation"]["status"]}


def _normalized_refs(values: Iterable[str] | None) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = (values,)
    return sorted({str(value) for value in values if str(value)})


def frozen_replay(record: dict[str, Any], *, as_of: str | None = None,
                  original_artifact: dict[str, Any] | None = None,
                  source_refs: Iterable[str] = (),
                  source_conflict_refs: Iterable[str] = (),
                  memory_receipt: dict[str, Any] | None = None,
                  allow_post_cutoff_known_at: bool = False) -> dict[str, Any]:
    """Rebuild production qualification from an immutable input without rewriting history."""
    frozen = copy.deepcopy(record)
    validate(frozen)
    from .evidence_qualification import POLICY_VERSION, qualify_record

    normalized_source_refs = _normalized_refs(source_refs)
    normalized_conflict_refs = _normalized_refs(source_conflict_refs)
    qualification_inputs = {
        "evidence": frozen,
        "as_of": as_of,
        "qualification_policy_version": POLICY_VERSION,
        "source_refs": normalized_source_refs,
        "source_conflict_refs": normalized_conflict_refs,
        "memory_receipt": copy.deepcopy(memory_receipt),
        "allow_post_cutoff_known_at": bool(allow_post_cutoff_known_at),
    }
    result = qualify_record(
        frozen,
        as_of=as_of,
        source_refs=normalized_source_refs,
        source_conflict_refs=normalized_conflict_refs,
        memory_receipt=memory_receipt,
        allow_post_cutoff_known_at=allow_post_cutoff_known_at,
    )
    return {
        "contract": "EvidenceSpecReplay/v1",
        "input_sha256": fingerprint(qualification_inputs),
        "qualification_inputs": qualification_inputs,
        "evidence_contract": frozen["contract"],
        "evidence": frozen,
        "qualification": result,
        "original_artifact": copy.deepcopy(original_artifact),
    }


def install_qualification() -> dict[str, Any]:
    """Return deterministic install checks without claiming unavailable dimensions."""
    observation = {
        "attempt_id": "install-evidence-attempt",
        "observation_id": "obs_install_evidence",
        "operation": "install_smoke",
        "backend": "market",
        "known_at": "2026-09-20T02:00:00Z",
    }
    record = from_observation({
        "evidence_kind": "market_fact",
        "url": "https://example.test/install-evidence",
        "title": "安装回放固定事实",
        "excerpt_text": "2026-09-20 固定安装回放事实",
        "fact_as_of": "2026-09-20T01:00:00Z",
        "factual_status": "verified",
        "evidence_ref": "install-evidence-1",
    }, observation)
    cutoff = "2026-09-20T03:00:00Z"
    replay = frozen_replay(
        record,
        as_of=cutoff,
        original_artifact={"judgment": "qualified", "artifact_id": "install-evidence-artifact"},
    )

    from .local_research import LocalResearchChain, ReadOnlyResearchExecutor

    contract = {"version": 3, "as_of": cutoff, "requirements": [{
        "key": "install_source", "blocking": True, "allowed_coverage": ["covered"],
        "window": {"mode": "exact", "start": cutoff, "end": cutoff},
    }]}
    operation = {"requirement_key": "install_source", "backend": "gateway",
                 "operation": "web_read", "arguments": {"url": "https://example.test/unavailable"},
                 "fallback_backends": []}
    expected_call = [{"operation": "web_read", "url": "https://example.test/unavailable"}]
    calls: list[dict[str, Any]] = []

    def unavailable_backend(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append({"operation": name, "url": arguments.get("url")})
        raise OSError("source_unavailable")

    failed = LocalResearchChain(
        lambda *_: {"version": 1, "operations": [operation]},
        ReadOnlyResearchExecutor({"gateway": unavailable_backend}), max_repairs=0,
    ).run({"stage": "m0_research", "as_of": cutoff}, contract,
          attempt_id="install-evidence-unavailable-attempt")
    failure_observations = [
        observation for observation in failed.observations
        if observation.get("status") == "failed"
    ]
    failure_observation = failure_observations[0] if len(failure_observations) == 1 else {}
    items = [item for observation in failed.observations for item in observation.get("evidence_items") or []]
    successful_items = [
        item for observation in failed.observations if observation.get("status") == "succeeded"
        for item in observation.get("evidence_items") or []
    ]
    verifier_passed = failed.verifier.get("passed") is True
    unavailable_measurements = {
        "expected_backend_call": calls == expected_call,
        "single_failed_acquisition": (
            failure_observation.get("operation") == "web_read"
            and failure_observation.get("backend") == "gateway"
            and failure_observation.get("status") == "failed"
        ),
        "no_fabricated_evidence": not items and not successful_items,
        "no_qualified_fallback": failed.qualified is False and not verifier_passed,
    }
    unavailable = {
        "contract": "EvidenceSpecSourceAvailability/v1",
        "attempt_id": "install-evidence-unavailable-attempt",
        "observation_id": failure_observation.get("observation_id"),
        "status": failure_observation.get("status", "not_executed"),
        "available": bool(successful_items),
        "qualified": failed.qualified,
        "reason": "source_unavailable",
        "evidence_items": items,
        "backend_calls": calls,
        "verifier_passed": verifier_passed,
        "measurements": unavailable_measurements,
    }
    failure_safe = all(unavailable_measurements.values())
    replay_equal = replay == frozen_replay(
        record, as_of=cutoff, original_artifact=replay["original_artifact"],
    )
    evaluation_vector = {
        "delivery_speed": {
            "status": "not_measured",
            "reason": "install_smoke_has_no_live_latency_baseline",
            "reason_code": "no_live_latency_baseline",
            "measurements": {"measured": False, "sample_count": 0, "baseline_available": False},
        },
        "qualification_probability": {
            "status": "not_measured",
            "reason": "two_fixed_fixtures_are_not_a_population",
            "reason_code": "fixture_not_population",
            "measurements": {
                "measured": False, "sample_count": 2, "baseline_available": False,
                "qualified_fixtures": int(replay["qualification"]["state"] == "qualified"),
                "rejected_fixtures": int(failed.qualified is False),
            },
        },
        "research_quality": {
            "status": "not_measured",
            "reason": "install_smoke_has_no_research_quality_baseline",
            "reason_code": "no_research_quality_baseline",
            "measurements": {
                "measured": False, "sample_count": 1,
                "evidence_record_qualified": replay["qualification"]["state"] == "qualified",
                "baseline_available": False,
            },
        },
        "judgment_outcome": {
            "status": "not_measured",
            "reason": "install_smoke_has_no_trade_outcome",
            "reason_code": "no_observed_outcome",
            "measurements": {
                "measured": False, "declared_artifact": replay["original_artifact"] is not None,
                "outcome_observed": False, "baseline_available": False,
            },
        },
        "safety_reliability": {
            "status": "pass" if replay_equal and failure_safe else "fail",
            "scope": "deterministic_install_fixtures",
            "measurements": {
                "measured": True,
                "replay_equal": replay_equal,
                "unavailable_source_safe": failure_safe,
            },
        },
    }
    return {
        "contract": "EvidenceSpecInstallQualification/v1",
        "qualified": replay["qualification"]["state"] == "qualified" and replay_equal and failure_safe,
        "replay": replay,
        "source_unavailable_smoke": unavailable,
        "evaluation_vector": evaluation_vector,
    }


if __name__ == "__main__":
    print(json.dumps(install_qualification(), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
