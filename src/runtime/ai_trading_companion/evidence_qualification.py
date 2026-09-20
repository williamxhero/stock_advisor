"""Deterministic, replayable qualification facts for EvidenceSpec records."""
from __future__ import annotations

import copy
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from .evidence_spec import AI_KINDS, PROPAGATION, TRUTH, fingerprint, validate

VERSION = "EvidenceQualificationSpec/v1"
POLICY_VERSION = "EvidenceQualificationPolicy/v1"
STATES = frozenset({"qualified", "degraded", "conflicted", "expired", "rejected"})
USES = frozenset({
    "external_fact", "source_report", "context_only", "propagation_only",
    "calculation", "research_only", "reasoning_only", "none",
})


def _time(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("qualification timestamp requires timezone")
    return parsed.astimezone(timezone.utc)


def _weak_source(record: dict[str, Any]) -> bool:
    source = record.get("source") if isinstance(record.get("source"), dict) else {}
    reference = source.get("reference") if isinstance(source.get("reference"), dict) else {}
    provenance = record.get("provenance") if isinstance(record.get("provenance"), dict) else {}
    return bool(
        source.get("screenshot_only") is True
        or reference.get("screenshot_only") is True
        or reference.get("source_strength") in {"screenshot", "weak"}
        or provenance.get("screenshot_only") is True
    )


def qualify_record(
    record: dict[str, Any], *, as_of: str | None = None,
    source_refs: Iterable[str] = (),
    source_conflict_refs: Iterable[str] = (),
    memory_receipt: dict[str, Any] | None = None,
    allow_post_cutoff_known_at: bool = False,
) -> dict[str, Any]:
    """Return a complete qualification fact without changing the evidence record.

    Truth and market propagation remain separate.  An unverified or refuted
    record can therefore qualify as a propagation observation while never
    becoming an external fact.
    """
    validate(record)
    truth = str(record["truth_status"])
    propagation = str(record["market_propagation"]["status"])
    reasons: list[str] = []
    state = "qualified"
    permitted_use = "external_fact" if record.get("external_fact") else "source_report"
    kind = str(record["kind"])

    if kind in AI_KINDS or record["provenance"].get("origin") == "ai":
        state, permitted_use = "rejected", "reasoning_only"
        reasons.append("ai_is_not_external_evidence")
    elif not record.get("occurred_at"):
        state, permitted_use = "degraded", "context_only"
        reasons.append("occurrence_unknown")
    elif kind == "derived_calculation":
        permitted_use = "calculation"
        derivation = record["provenance"].get("derivation") or {}
        if not all(derivation.get(key) for key in ("formula", "version", "input_refs", "unit")):
            state, permitted_use = "rejected", "none"
            reasons.append("calculation_provenance_missing")
    elif kind == "quant_research":
        permitted_use = "research_only"
        if not all(record["source"]["reference"].get(key) for key in ("package_id", "version")):
            state, permitted_use = "rejected", "none"
            reasons.append("research_version_missing")

    if _weak_source(record) and state not in {"rejected"}:
        state, permitted_use = "degraded", "context_only"
        reasons.append("weak_source_screenshot_only")

    if truth in {"unknown", "unverified", "refuted"} and state == "qualified":
        state, permitted_use = "degraded", "context_only"
        reasons.append("content_" + truth)
    if truth == "conflicted" and state != "rejected":
        state, permitted_use = "conflicted", "context_only"
        reasons.append("unresolved_content_conflict")
    conflict_refs = sorted({str(ref) for ref in source_conflict_refs if str(ref)})
    if conflict_refs and state not in {"rejected"}:
        state, permitted_use = "conflicted", "context_only"
        reasons.append("source_conflict")

    if propagation == "observed":
        impact = record["market_propagation"].get("impact") or {}
        if not impact:
            state, permitted_use = "rejected", "none"
            reasons.append("observed_propagation_impact_missing")
        elif truth in {"unknown", "unverified", "refuted"}:
            if state not in {"rejected"}:
                state = "degraded"
            permitted_use = "propagation_only"
            reasons.append("market_propagation_observed_despite_unverified_content")

    if as_of:
        cutoff = _time(as_of)
        if (not allow_post_cutoff_known_at and _time(record["known_at"]) > cutoff) or (
            record.get("occurred_at") and _time(record["occurred_at"]) > cutoff
        ):
            state, permitted_use = "rejected", "none"
            reasons.append("not_known_at_cutoff")
        elif record.get("expires_at") and _time(record["expires_at"]) <= cutoff:
            state = "expired"
            permitted_use = "propagation_only" if propagation == "observed" else "none"
            reasons.append("expired_at_cutoff")

    reasons = list(dict.fromkeys(reasons))
    refs = [{"record_id": record["record_id"], "source_refs": sorted({str(ref) for ref in source_refs if str(ref)})}]
    if memory_receipt:
        refs[0]["memory_receipt"] = copy.deepcopy(memory_receipt)
    result = {
        "contract": VERSION,
        "qualification_id": "",
        "qualification_policy_version": POLICY_VERSION,
        "as_of": as_of,
        "input_record_refs": refs,
        "source_conflict_refs": conflict_refs,
        "state": state,
        "permitted_use": permitted_use,
        "reasons": reasons,
        "truth_status": truth,
        "propagation_status": propagation,
    }
    result["qualification_id"] = fingerprint({k: v for k, v in result.items() if k != "qualification_id"})
    validate_qualification(result)
    return result


def validate_qualification(value: dict[str, Any]) -> None:
    if value.get("contract") != VERSION:
        raise ValueError("unsupported evidence qualification contract")
    if value.get("qualification_policy_version") != POLICY_VERSION:
        raise ValueError("unsupported evidence qualification policy")
    if value.get("qualification_id") != fingerprint({k: v for k, v in value.items() if k != "qualification_id"}):
        raise ValueError("evidence qualification integrity mismatch")
    if value.get("state") not in STATES or value.get("permitted_use") not in USES:
        raise ValueError("invalid evidence qualification state or permitted use")
    if not isinstance(value.get("input_record_refs"), list) or not value["input_record_refs"]:
        raise ValueError("evidence qualification input references required")
    for ref in value["input_record_refs"]:
        if not isinstance(ref, dict) or not ref.get("record_id"):
            raise ValueError("evidence qualification record reference required")
    if value.get("truth_status") not in TRUTH or value.get("propagation_status") not in PROPAGATION:
        raise ValueError("invalid evidence qualification dimensions")
    if not isinstance(value.get("reasons"), list) or not all(isinstance(item, str) for item in value["reasons"]):
        raise ValueError("evidence qualification reasons required")
    if value.get("as_of") is not None:
        _time(value["as_of"])


def qualification_for_records(
    records: Iterable[dict[str, Any]], *, as_of: str | None = None,
    source_refs_by_record: dict[str, Iterable[str]] | None = None,
    conflict_refs_by_record: dict[str, Iterable[str]] | None = None,
) -> list[dict[str, Any]]:
    """Qualify a stable ordered set for frozen replay."""
    source_refs_by_record = source_refs_by_record or {}
    conflict_refs_by_record = conflict_refs_by_record or {}
    return [
        qualify_record(
            record, as_of=as_of,
            source_refs=source_refs_by_record.get(record["record_id"], ()),
            source_conflict_refs=conflict_refs_by_record.get(record["record_id"], ()),
        )
        for record in records
    ]
