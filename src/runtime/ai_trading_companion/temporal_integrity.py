"""Runtime-owned temporal integrity and frozen replay contract."""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable

VERSION = "TemporalIntegritySpec/v1"
POLICY_VERSION = "TemporalIntegrityPolicy/v1"
STATES = frozenset({"eligible", "degraded", "rejected"})
USES = frozenset({"full", "degraded", "none"})

_OCCURRENCE_FIELDS: dict[str, tuple[str, ...]] = {
    "market_fact": ("market_time", "effective_time", "fact_as_of", "occurred_at", "published_at"),
    "news_disclosure": ("effective_time", "event_time", "fact_as_of", "occurred_at", "published_at"),
    "social_propagation": ("effective_time", "event_time", "fact_as_of", "occurred_at", "published_at"),
    "source_opinion": ("effective_time", "event_time", "fact_as_of", "occurred_at", "published_at"),
    "derived_calculation": ("calculation_time", "effective_time", "fact_as_of", "occurred_at"),
    "quant_research": ("research_as_of", "effective_time", "fact_as_of", "occurred_at", "published_at"),
    "ai_reasoning": ("occurred_at", "effective_time", "fact_as_of"),
    "ai_summary": ("occurred_at", "effective_time", "fact_as_of"),
    "ai_conclusion": ("occurred_at", "effective_time", "fact_as_of"),
}


def timestamp(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("temporal timestamp requires timezone")
    return parsed.astimezone(timezone.utc)


def canonical_time(value: Any) -> str:
    return timestamp(value).isoformat().replace("+00:00", "Z")


def _candidate(item: dict[str, Any], fields: Iterable[str]) -> tuple[str | None, str | None, list[tuple[str, str]]]:
    values: list[tuple[str, str]] = []
    for field in fields:
        value = item.get(field)
        if value in (None, ""):
            continue
        values.append((field, canonical_time(value)))
    if not values:
        return None, None, []
    return values[0][1], values[0][0], values


def _kind(item: dict[str, Any], observation: dict[str, Any] | None) -> str:
    declared = str(item.get("evidence_kind") or item.get("kind") or "")
    if declared:
        return declared
    if observation and str(observation.get("backend") or "") == "market":
        return "market_fact"
    return "news_disclosure"


def resolve_temporal(item: dict[str, Any], observation: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolve source clocks into a runtime-owned temporal envelope."""
    observation = observation or {}
    kind = _kind(item, observation)
    occurrence, occurrence_source, occurrence_candidates = _candidate(
        item, _OCCURRENCE_FIELDS.get(kind, _OCCURRENCE_FIELDS["news_disclosure"]),
    )
    occurrence_precedence = list(_OCCURRENCE_FIELDS.get(kind, _OCCURRENCE_FIELDS["news_disclosure"]))
    publication, publication_source, _ = _candidate(
        item, ("published_at", "publish_time", "source_publication_time"),
    )
    runtime_known = observation.get("known_at") or observation.get("acquired_at")
    known_source = "runtime_observation.known_at" if observation.get("known_at") else "runtime_observation.acquired_at"
    if runtime_known in (None, ""):
        runtime_known = item.get("runtime_known_at") or item.get("ingest_time")
        known_source = "runtime.ingest_time" if item.get("ingest_time") else "runtime.runtime_known_at"
    if runtime_known in (None, ""):
        runtime_known = item.get("known_at")
        known_source = "legacy_record.known_at"
    known = canonical_time(runtime_known) if runtime_known not in (None, "") else None

    reasons: list[str] = []
    distinct_occurrences = {value for _, value in occurrence_candidates}
    if len(distinct_occurrences) > 1:
        reasons.append("occurred_time_conflict_precedence_applied")
    if occurrence is None:
        reasons.append("occurred_at_unknown")
    if known is None:
        reasons.append("known_at_unknown")
    if publication is None and kind in {"news_disclosure", "social_propagation", "source_opinion", "quant_research"}:
        reasons.append("published_at_unknown")

    degraded = occurrence is None or known is None or len(distinct_occurrences) > 1
    return {
        "contract": VERSION,
        "policy_version": POLICY_VERSION,
        "kind": kind,
        "as_of": None,
        "occurred_at": occurrence,
        "known_at": known,
        "published_at": publication,
        "occurred_at_source": occurrence_source,
        "known_at_source": known_source if known else None,
        "published_at_source": publication_source,
        "precedence": {
            "occurred_at": occurrence_precedence,
            "known_at": ["runtime_observation.known_at", "runtime_observation.acquired_at", "runtime.ingest_time", "legacy_record.known_at"],
            "published_at": ["published_at", "publish_time", "source_publication_time"],
        },
        "state": "degraded" if degraded else "eligible",
        "permitted_use": "degraded" if degraded else "full",
        "reasons": reasons,
    }


def qualify_temporal(
    envelope: dict[str, Any], *, as_of: str | None = None,
    allow_post_cutoff_known_at: bool = False,
) -> dict[str, Any]:
    """Apply an as_of cutoff without changing the frozen source envelope."""
    validate_temporal(envelope)
    result = copy.deepcopy(envelope)
    reasons = list(dict.fromkeys(str(reason) for reason in result.get("reasons") or []))
    state = "eligible" if result.get("occurred_at") and result.get("known_at") else "degraded"
    if result.get("state") == "degraded":
        state = "degraded"
    permitted_use = "full" if state == "eligible" else "degraded"
    if as_of:
        cutoff = timestamp(as_of)
        result["as_of"] = canonical_time(as_of)
        known = timestamp(result["known_at"]) if result.get("known_at") else None
        occurred = timestamp(result["occurred_at"]) if result.get("occurred_at") else None
        published = timestamp(result["published_at"]) if result.get("published_at") else None
        if known is None:
            state, permitted_use = "rejected", "none"
            reasons.append("known_at_required_for_historical_cutoff")
        elif known > cutoff and not allow_post_cutoff_known_at:
            state, permitted_use = "rejected", "none"
            reasons.append("known_at_after_as_of")
        if occurred and occurred > cutoff:
            state, permitted_use = "rejected", "none"
            reasons.append("occurred_at_after_as_of")
        if published and published > cutoff:
            state, permitted_use = "rejected", "none"
            reasons.append("published_at_after_as_of")
        if published and known and published > known:
            state, permitted_use = "rejected", "none"
            reasons.append("published_at_after_known_at")
        if published and occurred and published < occurred and state != "rejected":
            state, permitted_use = "degraded", "degraded"
            reasons.append("published_at_before_occurred_at")
    result["state"] = state
    result["permitted_use"] = permitted_use
    result["reasons"] = list(dict.fromkeys(reasons))
    validate_temporal(result)
    return result


def validate_temporal(value: dict[str, Any]) -> None:
    if not isinstance(value, dict) or value.get("contract") != VERSION:
        raise ValueError("unsupported temporal integrity contract")
    required = {
        "contract", "kind", "as_of", "occurred_at", "known_at", "published_at",
        "policy_version", "precedence",
        "occurred_at_source", "known_at_source", "published_at_source",
        "state", "permitted_use", "reasons",
    }
    missing = sorted(required - set(value))
    if missing:
        raise ValueError("temporal integrity missing fields: " + ", ".join(missing))
    for field in ("as_of", "occurred_at", "known_at", "published_at"):
        if value.get(field) not in (None, ""):
            timestamp(value[field])
    if value.get("state") not in STATES or value.get("permitted_use") not in USES:
        raise ValueError("invalid temporal integrity state")
    if value.get("policy_version") != POLICY_VERSION or not isinstance(value.get("precedence"), dict):
        raise ValueError("unsupported temporal integrity policy")
    if not isinstance(value.get("reasons"), list) or not all(isinstance(item, str) for item in value["reasons"]):
        raise ValueError("temporal integrity reasons required")


def replay_records(records: Iterable[dict[str, Any]], *, as_of: str) -> dict[str, Any]:
    """Replay frozen records in stable order and fail closed on future clocks."""
    frozen_as_of = canonical_time(as_of)
    envelopes = []
    for record in sorted(records, key=lambda row: str(row.get("record_id") or "")):
        envelope = record.get("temporal_integrity")
        if not isinstance(envelope, dict):
            envelope = resolve_temporal(record)
        envelopes.append(qualify_temporal(envelope, as_of=frozen_as_of))
    result = {
        "contract": VERSION,
        "as_of": frozen_as_of,
        "records": envelopes,
        "passed": all(row["state"] != "rejected" for row in envelopes),
    }
    result["replay_hash"] = hashlib.sha256(
        json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return result


def validate_replay(value: dict[str, Any]) -> None:
    if value.get("contract") != VERSION or not isinstance(value.get("records"), list):
        raise ValueError("invalid temporal replay")
    as_of = value.get("as_of")
    if not as_of:
        raise ValueError("temporal replay cutoff is required")
    canonical_time(as_of)
    for record in value["records"]:
        validate_temporal(record)
    if value.get("passed") != all(record["state"] != "rejected" for record in value["records"]):
        raise ValueError("temporal replay result mismatch")
    expected = hashlib.sha256(
        json.dumps({key: value[key] for key in ("contract", "as_of", "records", "passed")},
                   ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if value.get("replay_hash") != expected:
        raise ValueError("temporal replay hash mismatch")


resolve = resolve_temporal
qualify = qualify_temporal
replay = replay_records
validate = validate_temporal
