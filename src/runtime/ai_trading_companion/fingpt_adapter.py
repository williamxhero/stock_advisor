"""Runtime-owned FinGPTAdapterSpec/v1 for bounded financial NLP.

The adapter may classify and extract from Runtime-frozen public evidence.  It
cannot create investment advice, write MemoryHub or portfolio state, or replace
M0/M1/M2 ownership.  Provider output is always wrapped in a versioned receipt
whose source, model/data versions, confidence state and input digest are
replayable.  The local implementation is deliberately small so FinGPT or
another provider is optional at installation time.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime
from typing import Any, Callable

from .adapter_contract import AdapterDefinition

CONTRACT = "FinGPTAdapterSpec/v1"
VERSION = 1
PROVIDER_OUTPUT_CONTRACT = "FinGPTAdapterProviderOutput/v1"
RESULT_CONTRACT = "FinGPTAdapterResult/v1"
REPLAY_CONTRACT = "FinGPTAdapterReplay/v1"
EXECUTION_CONTRACT = "FinGPTAdapterExecution/v1"
CODE_VERSION = "runtime-fingpt-nlp/v1"

CAPABILITIES = frozenset({
    "financial_sentiment",
    "headline_classification",
    "entity_recognition",
    "relation_extraction",
})
STAGES = frozenset({
    "m0_research", "m0_compose", "m1_research", "m1_judgment", "m2",
    "chat_research", "chat", "reflection", "outcome_research",
})
DOCUMENT_SOURCES = frozenset({"public_evidence", "markethub", "deterministic_computation"})
CONFIDENCE_STATES = frozenset({"reported", "recomputed"})
PROVIDERS = frozenset({"fingpt", "swappable_provider", "controlled_llm"})
_PERMISSIONS = {"write_permissions": []}
_QUANTRESEARCH = {"access": "read_only", "write_permissions": []}
_FORBIDDEN_KEYS = frozenset({
    "memoryhub", "memory", "portfolio", "positions", "orders", "schedule",
    "production_strategy", "final_judgment", "judgment", "task_state",
    "write_permissions", "memoryhub_write", "memory_write", "portfolio_write",
    "positions_write", "orders_write", "schedule_write", "exchange_write",
    "h0", "m1_output", "m2_output", "private_reasoning", "chain_of_thought",
})
_FORBIDDEN_ADVICE = re.compile(
    r"(?:\b(?:buy|sell|hold|long|short)\b|\b(?:add|reduce|clear)\s+position\b|"
    r"建议买入|建议卖出|建议持有|建议加仓|建议减仓|建议清仓|买入|卖出|持有|加仓|减仓|清仓|做多|做空|目标价)",
    re.IGNORECASE,
)
_FORBIDDEN_ADVICE_KEYS = frozenset({
    "recommendation", "advice", "action", "order", "target_price", "position_size",
})


def _reject_final_advice(value: Any, path: str = "fingpt") -> None:
    """Reject advice expressed either as text or as a structured field."""
    pending = [(value, path, 0)]
    visited = 0
    while pending:
        child, child_path, depth = pending.pop()
        visited += 1
        if depth > 64 or visited > 100_000:
            raise ValueError("FinGPT payload limits exceeded")
        if isinstance(child, dict):
            for key, item in child.items():
                normalized = str(key).strip().casefold().replace("-", "_")
                if normalized in _FORBIDDEN_ADVICE_KEYS:
                    raise ValueError("FinGPT output cannot contain final investment advice")
                pending.append((item, f"{child_path}.{key}", depth + 1))
        elif isinstance(child, (list, tuple)):
            pending.extend((item, f"{child_path}[{index}]", depth + 1) for index, item in enumerate(child))
        elif isinstance(child, str) and _FORBIDDEN_ADVICE.search(child):
            raise ValueError("FinGPT output cannot contain final investment advice")




def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: Any, field: str, *, maximum: int = 500) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > maximum:
        raise ValueError(f"FinGPT {field} must be bounded text")
    return value


def _timestamp(value: Any, field: str) -> str:
    text = _text(value, field, maximum=64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"FinGPT {field} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"FinGPT {field} must be timezone-aware")
    return text


def _bounded_version(value: Any, field: str) -> str:
    return _text(value, field, maximum=120)


def _score(value: Any, field: str = "confidence.score") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"FinGPT {field} must be numeric")
    value = float(value)
    if value != value or value in (float("inf"), float("-inf")) or not 0 <= value <= 1:
        raise ValueError(f"FinGPT {field} must be between zero and one")
    return value


def _walk_forbidden(value: Any, path: str = "fingpt") -> None:
    pending = [(value, path, 0)]
    visited = 0
    while pending:
        child, child_path, depth = pending.pop()
        visited += 1
        if depth > 64 or visited > 100_000:
            raise ValueError("FinGPT payload limits exceeded")
        if isinstance(child, dict):
            for key, item in child.items():
                normalized = str(key).strip().casefold().replace("-", "_")
                # Empty permission declarations are explicit read-only grants,
                # not an attempt to pass state authority through the adapter.
                empty_permission = normalized == "write_permissions" and item == []
                if normalized in _FORBIDDEN_KEYS and not empty_permission:
                    raise ValueError(f"FinGPT contract forbids protected field at {child_path}.{key}")
                pending.append((item, f"{child_path}.{key}", depth + 1))
        elif isinstance(child, (list, tuple)):
            pending.extend((item, f"{child_path}[{index}]", depth + 1) for index, item in enumerate(child))


def _validate_document(value: Any, cutoff: str) -> dict[str, Any]:
    required = {"document_id", "text", "source", "source_ref", "as_of", "known_at", "language"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("FinGPT document fields are not exact")
    document_id = _text(value["document_id"], "document.document_id", maximum=160)
    text = _text(value["text"], "document.text", maximum=16_000)
    source = _text(value["source"], "document.source", maximum=80)
    if source not in DOCUMENT_SOURCES:
        raise ValueError("FinGPT documents must come from a permitted evidence source")
    source_ref = _text(value["source_ref"], "document.source_ref", maximum=500)
    as_of = _timestamp(value["as_of"], "document.as_of")
    known_at = _timestamp(value["known_at"], "document.known_at")
    cutoff_dt = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
    as_of_dt = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    known_at_dt = datetime.fromisoformat(known_at.replace("Z", "+00:00"))
    if as_of_dt > known_at_dt or known_at_dt > cutoff_dt:
        raise ValueError("FinGPT document is unavailable at the frozen cutoff")
    language = _text(value["language"], "document.language", maximum=32)
    return {
        "document_id": document_id, "text": text, "source": source,
        "source_ref": source_ref, "as_of": as_of, "known_at": known_at,
        "language": language,
    }


def build_input(
    documents: list[dict[str, Any]], *, capability: str, as_of: str,
    stage: str = "m0_compose", model_version: str = "provider-unspecified/v1",
    data_version: str = "runtime-evidence/v1", cycle_id: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    value = {
        "contract": CONTRACT, "version": VERSION, "capability": capability,
        "stage": stage, "documents": copy.deepcopy(documents),
        "model_version": model_version, "data_version": data_version,
        "permissions": copy.deepcopy(_PERMISSIONS), "quantresearch": copy.deepcopy(_QUANTRESEARCH),
        "provenance": {"source": "runtime", "as_of": as_of, "cycle_id": cycle_id, "request_id": request_id},
    }
    value["sha256"] = sha256(value)
    return validate_input(value)


def validate_input(value: dict[str, Any]) -> dict[str, Any]:
    required = {
        "contract", "version", "capability", "stage", "documents", "model_version",
        "data_version", "permissions", "quantresearch", "provenance", "sha256",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("FinGPT input fields are not exact")
    if value["contract"] != CONTRACT or type(value["version"]) is not int or value["version"] != VERSION:
        raise ValueError("unsupported FinGPT input identity")
    if value["capability"] not in CAPABILITIES or value["stage"] not in STAGES:
        raise ValueError("FinGPT capability or stage is outside the allowlist")
    # Inspect document payloads before exact document validation so protected
    # state cannot be smuggled in under an otherwise malformed document.
    _walk_forbidden(value["documents"], "fingpt.input.documents")
    _bounded_version(value["model_version"], "model_version")
    _bounded_version(value["data_version"], "data_version")
    if not isinstance(value["documents"], list) or not value["documents"] or len(value["documents"]) > 200:
        raise ValueError("FinGPT documents must be a bounded non-empty list")
    provenance = value["provenance"]
    if not isinstance(provenance, dict) or set(provenance) != {"source", "as_of", "cycle_id", "request_id"}:
        raise ValueError("FinGPT provenance fields are not exact")
    if provenance["source"] != "runtime":
        raise ValueError("FinGPT input provenance must be Runtime-owned")
    cutoff = _timestamp(provenance["as_of"], "provenance.as_of")
    for field in ("cycle_id", "request_id"):
        if provenance[field] is not None:
            _text(provenance[field], f"provenance.{field}", maximum=200)
    documents = [_validate_document(item, cutoff) for item in value["documents"]]
    if len({item["document_id"] for item in documents}) != len(documents):
        raise ValueError("FinGPT document identifiers must be unique")
    if value["permissions"] != _PERMISSIONS or value["quantresearch"] != _QUANTRESEARCH:
        raise ValueError("FinGPT permissions must be read_only")
    _walk_forbidden(value, "fingpt.input")
    if value["sha256"] != sha256({key: item for key, item in value.items() if key != "sha256"}):
        raise ValueError("FinGPT input digest mismatch")
    return value


def _validate_annotation(value: Any, *, document_ids: set[str]) -> dict[str, Any]:
    required = {"document_id", "value", "evidence_ref", "confidence"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("FinGPT annotation fields are not exact")
    document_id = _text(value["document_id"], "annotation.document_id", maximum=160)
    if document_id not in document_ids:
        raise ValueError("FinGPT annotation document is not in the input")
    evidence_ref = _text(value["evidence_ref"], "annotation.evidence_ref", maximum=500)
    confidence = value["confidence"]
    if not isinstance(confidence, dict) or set(confidence) != {"score", "state", "method"}:
        raise ValueError("FinGPT annotation confidence is not auditable")
    _score(confidence["score"], "annotation.confidence.score")
    if confidence["state"] not in CONFIDENCE_STATES:
        raise ValueError("FinGPT annotation confidence state is unsupported")
    _text(confidence["method"], "annotation.confidence.method", maximum=160)
    _reject_final_advice(value["value"], "fingpt.annotation.value")
    _walk_forbidden(value, "fingpt.annotation")
    return copy.deepcopy(value)


def _validate_provider_output(value: dict[str, Any], input_contract: dict[str, Any] | None = None) -> dict[str, Any]:
    required = {"contract", "version", "capability", "annotations", "model_version", "data_version", "provider", "confidence"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("FinGPT provider output fields are not exact")
    if value["contract"] != PROVIDER_OUTPUT_CONTRACT or type(value["version"]) is not int or value["version"] != VERSION:
        raise ValueError("unsupported FinGPT provider output identity")
    if value["capability"] not in CAPABILITIES or value["provider"] not in PROVIDERS:
        raise ValueError("FinGPT provider capability or identity is unsupported")
    _bounded_version(value["model_version"], "provider.model_version")
    _bounded_version(value["data_version"], "provider.data_version")
    if not isinstance(value["annotations"], list) or len(value["annotations"]) > 1_000:
        raise ValueError("FinGPT annotations must be a bounded list")
    document_ids = set()
    if input_contract is not None:
        input_contract = validate_input(input_contract)
        if value["capability"] != input_contract["capability"]:
            raise ValueError("FinGPT provider capability does not match input")
        document_ids = {item["document_id"] for item in input_contract["documents"]}
    for annotation in value["annotations"]:
        _validate_annotation(annotation, document_ids=document_ids or {annotation.get("document_id")})
    confidence = value["confidence"]
    if not isinstance(confidence, dict) or set(confidence) != {"score", "state", "method", "basis"}:
        raise ValueError("FinGPT output confidence envelope is incomplete")
    _score(confidence["score"])
    if confidence["state"] not in CONFIDENCE_STATES:
        raise ValueError("FinGPT output confidence state is unsupported")
    _text(confidence["method"], "confidence.method", maximum=160)
    if not isinstance(confidence["basis"], dict) or set(confidence["basis"]) != {"annotation_count", "document_count"}:
        raise ValueError("FinGPT confidence basis is not auditable")
    if type(confidence["basis"]["annotation_count"]) is not int or type(confidence["basis"]["document_count"]) is not int:
        raise ValueError("FinGPT confidence basis counts are invalid")
    if confidence["basis"]["annotation_count"] != len(value["annotations"]):
        raise ValueError("FinGPT confidence annotation count mismatch")
    if input_contract is not None and confidence["basis"]["document_count"] != len(input_contract["documents"]):
        raise ValueError("FinGPT confidence document count mismatch")
    _walk_forbidden(value, "fingpt.provider_output")
    return value


def _confidence_for_annotations(annotations: list[dict[str, Any]], document_count: int, *, recomputed: bool) -> dict[str, Any]:
    scores = [float(item["confidence"]["score"]) for item in annotations]
    coverage = len({item["document_id"] for item in annotations}) / max(document_count, 1)
    score = round(min(scores) if scores else 0.0, 6)
    if recomputed:
        # A fallback never inherits provider confidence.  Its score is derived
        # from observed coverage and is capped below a qualified provider.
        score = round(min(0.65, 0.25 + 0.40 * coverage), 6)
    return {
        "score": score, "state": "recomputed" if recomputed else "reported",
        "method": "controlled-degradation-v1" if recomputed else "provider-reported-v1",
        "basis": {"annotation_count": len(annotations), "document_count": document_count},
    }


def build_output(
    input_contract: dict[str, Any], provider_output: dict[str, Any], *,
    provider: str | None = None, degraded: bool = False,
    source_output_sha256: str | None = None,
) -> dict[str, Any]:
    input_contract = validate_input(copy.deepcopy(input_contract))
    provider_output = _validate_provider_output(copy.deepcopy(provider_output), input_contract)
    resolved_provider = provider or provider_output["provider"]
    if resolved_provider not in PROVIDERS:
        raise ValueError("FinGPT provider is unsupported")
    annotations = copy.deepcopy(provider_output["annotations"])
    if degraded:
        # Recompute every annotation confidence rather than carrying stale
        # FinGPT/provider confidence into the controlled LLM path.
        fallback_score = _confidence_for_annotations(annotations, len(input_contract["documents"]), recomputed=True)["score"]
        for annotation in annotations:
            annotation["confidence"] = {
                "score": fallback_score, "state": "recomputed", "method": "controlled-degradation-v1",
            }
    confidence = _confidence_for_annotations(annotations, len(input_contract["documents"]), recomputed=degraded)
    provenance = {
        "source": "controlled_llm" if degraded else "provider",
        "provider": resolved_provider,
        "input_sha256": input_contract["sha256"],
        "output_sha256": sha256(provider_output),
        "model_version": provider_output["model_version"],
        "data_version": provider_output["data_version"],
        "code_version": CODE_VERSION,
        "source_refs": sorted({item["evidence_ref"] for item in annotations}),
        "confidence_recomputed": degraded,
        "source_output_sha256": source_output_sha256 if degraded else None,
    }
    receipt = {
        "contract": RESULT_CONTRACT, "version": VERSION, "spec_contract": CONTRACT,
        "state": "degraded" if degraded else "qualified",
        "capability": input_contract["capability"], "annotations": annotations,
        "model_version": provider_output["model_version"], "data_version": provider_output["data_version"],
        "confidence": confidence, "input": input_contract, "provider_output": provider_output,
        "permissions": copy.deepcopy(_PERMISSIONS), "quantresearch": copy.deepcopy(_QUANTRESEARCH),
        "provenance": provenance,
        "degradation": {
            "used": degraded,
            "path": "controlled_llm_cognition" if degraded else None,
            "reason": "primary_provider_failed" if degraded else None,
        },
    }
    receipt["sha256"] = sha256(receipt)
    return validate_output(receipt)


def validate_output(value: dict[str, Any]) -> dict[str, Any]:
    required = {
        "contract", "version", "spec_contract", "state", "capability", "annotations",
        "model_version", "data_version", "confidence", "input", "provider_output",
        "permissions", "quantresearch", "provenance", "degradation", "sha256",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("FinGPT result fields are not exact")
    if value["contract"] != RESULT_CONTRACT or value["version"] != VERSION or value["spec_contract"] != CONTRACT:
        raise ValueError("unsupported FinGPT result identity")
    if value["sha256"] != sha256({key: item for key, item in value.items() if key != "sha256"}):
        raise ValueError("FinGPT result digest mismatch")
    if value["state"] not in {"qualified", "degraded"} or value["capability"] not in CAPABILITIES:
        raise ValueError("FinGPT result state or capability is unsupported")
    input_contract = validate_input(value["input"])
    provider_output = _validate_provider_output(value["provider_output"], input_contract)
    if value["capability"] != input_contract["capability"] or value["annotations"] != provider_output["annotations"] and not value["degradation"]["used"]:
        raise ValueError("FinGPT result is not bound to provider output")
    _bounded_version(value["model_version"], "result.model_version")
    _bounded_version(value["data_version"], "result.data_version")
    if value["permissions"] != _PERMISSIONS or value["quantresearch"] != _QUANTRESEARCH:
        raise ValueError("FinGPT result permissions must be read_only")
    confidence = value["confidence"]
    if not isinstance(confidence, dict) or set(confidence) != {"score", "state", "method", "basis"}:
        raise ValueError("FinGPT result confidence is incomplete")
    _score(confidence["score"])
    if confidence["state"] != ("recomputed" if value["degradation"]["used"] else "reported"):
        raise ValueError("FinGPT result confidence state does not match degradation")
    _text(confidence["method"], "result.confidence.method", maximum=160)
    if not isinstance(confidence["basis"], dict):
        raise ValueError("FinGPT result confidence basis is invalid")
    provenance = value["provenance"]
    required_provenance = {
        "source", "provider", "input_sha256", "output_sha256", "model_version",
        "data_version", "code_version", "source_refs", "confidence_recomputed", "source_output_sha256",
    }
    if not isinstance(provenance, dict) or set(provenance) != required_provenance:
        raise ValueError("FinGPT result provenance is incomplete")
    if provenance["input_sha256"] != input_contract["sha256"] or provenance["model_version"] != value["model_version"] or provenance["data_version"] != value["data_version"]:
        raise ValueError("FinGPT result provenance mismatch")
    if provenance["confidence_recomputed"] != value["degradation"]["used"]:
        raise ValueError("FinGPT confidence provenance mismatch")
    if not isinstance(provenance["source_refs"], list) or any(not isinstance(item, str) for item in provenance["source_refs"]):
        raise ValueError("FinGPT source references are invalid")
    degradation = value["degradation"]
    if not isinstance(degradation, dict) or set(degradation) != {"used", "path", "reason"}:
        raise ValueError("FinGPT degradation envelope is incomplete")
    if degradation["used"] is not (value["state"] == "degraded"):
        raise ValueError("FinGPT degradation state mismatch")
    if degradation["used"] and (degradation["path"] != "controlled_llm_cognition" or not degradation["reason"]):
        raise ValueError("FinGPT degraded result lacks controlled path provenance")
    if not degradation["used"] and (degradation["path"] is not None or degradation["reason"] is not None):
        raise ValueError("FinGPT qualified result contains degradation claims")
    _walk_forbidden(value, "fingpt.result")
    if value["sha256"] != sha256({key: item for key, item in value.items() if key != "sha256"}):
        raise ValueError("FinGPT result digest mismatch")
    return value


def controlled_degradation(
    input_contract: dict[str, Any], fallback_output: dict[str, Any], *,
    failed_output: dict[str, Any] | None = None, provider: str = "controlled_llm",
) -> dict[str, Any]:
    """Build a fallback receipt with fresh provenance and confidence.

    ``fallback_output`` is validated as a new provider-shaped output.  No
    confidence field from a failed primary output is reused; the original
    output digest is retained only as diagnostic provenance.
    """
    validate_input(input_contract)
    source_digest = sha256(failed_output) if failed_output is not None else None
    return build_output(
        input_contract, fallback_output, provider=provider, degraded=True,
        source_output_sha256=source_digest,
    )


def frozen_replay(input_contract: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
    input_contract = validate_input(copy.deepcopy(input_contract))
    output = validate_output(copy.deepcopy(output))
    if output["input"]["sha256"] != input_contract["sha256"]:
        raise ValueError("FinGPT replay input binding mismatch")
    replay = {
        "contract": REPLAY_CONTRACT, "version": VERSION,
        "source_input": input_contract, "source_output": output,
        "source_input_sha256": input_contract["sha256"], "source_output_sha256": output["sha256"],
        "qualification": {
            "valid": True, "read_only": True, "capability_allowlisted": True,
            "advice_rejected": True, "degradation_provenance_recomputed": output["degradation"]["used"],
        },
        "evaluation_vector": {
            "delivery_speed": {"state": "not_measured_in_frozen_replay"},
            "qualification_probability": {"state": "not_estimated_in_frozen_replay"},
            "research_quality": {"capability": output["capability"], "model_version": output["model_version"]},
            "judgment_outcome": {"state": "adapter_not_a_judgment"},
            "safety_reliability": {"write_permissions": [], "m0_h0_m1_m2_isolation": True, "quantresearch_read_only": True},
        },
    }
    replay["sha256"] = sha256(replay)
    return replay


def validate_replay(value: dict[str, Any]) -> dict[str, Any]:
    required = {"contract", "version", "source_input", "source_output", "source_input_sha256", "source_output_sha256", "qualification", "evaluation_vector", "sha256"}
    if not isinstance(value, dict) or set(value) != required or value["contract"] != REPLAY_CONTRACT or value["version"] != VERSION:
        raise ValueError("FinGPT replay fields are not exact")
    source_input = validate_input(value["source_input"])
    source_output = validate_output(value["source_output"])
    if value["source_input_sha256"] != source_input["sha256"] or value["source_output_sha256"] != source_output["sha256"]:
        raise ValueError("FinGPT replay provenance mismatch")
    if value["sha256"] != sha256({key: item for key, item in value.items() if key != "sha256"}):
        raise ValueError("FinGPT replay digest mismatch")
    if value["qualification"].get("valid") is not True or value["qualification"].get("read_only") is not True:
        raise ValueError("FinGPT replay qualification mismatch")
    return value


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z0-9_.-]{1,30}|[一-鿿]{2,12}", text)


def _local_annotations(input_contract: dict[str, Any], *, provider: str) -> dict[str, Any]:
    annotations: list[dict[str, Any]] = []
    capability = input_contract["capability"]
    for document in input_contract["documents"]:
        text = document["text"]
        lower = text.casefold()
        if capability == "financial_sentiment":
            positive = sum(token in lower for token in ("增长", "盈利", "上调", "利好", "strong", "profit", "beat"))
            negative = sum(token in lower for token in ("下降", "亏损", "下调", "风险", "weak", "loss", "miss"))
            label = "positive" if positive > negative else "negative" if negative > positive else "neutral"
            value: Any = {"label": label, "positive_hits": positive, "negative_hits": negative}
        elif capability == "headline_classification":
            labels = (("earnings", ("业绩", "盈利", "revenue", "earnings")), ("regulatory", ("监管", "处罚", "regulator")), ("policy", ("政策", "央行", "policy")), ("corporate_action", ("收购", "回购", "merger")), ("macro", ("通胀", "利率", "inflation")))
            value = {"label": next((name for name, terms in labels if any(term.casefold() in lower for term in terms)), "other")}
        elif capability == "entity_recognition":
            value = {"entities": sorted(set(_words(text)))[:50]}
        else:
            relation_terms = (("partnership", ("合作", "partner", "供应")), ("acquisition", ("收购", "并购", "acquire")), ("competition", ("竞争", "competitor")))
            matched = next(((name, term) for name, terms in relation_terms for term in terms if term.casefold() in lower), None)
            value = {"relations": ([{"type": matched[0], "trigger": matched[1]}] if matched else [])}
        annotations.append({
            "document_id": document["document_id"], "value": value,
            "evidence_ref": document["source_ref"],
            "confidence": {"score": 0.75 if value else 0.0, "state": "reported", "method": "runtime-baseline-v1"},
        })
    result = {
        "contract": PROVIDER_OUTPUT_CONTRACT, "version": VERSION,
        "capability": capability, "annotations": annotations,
        "model_version": input_contract["model_version"], "data_version": input_contract["data_version"],
        "provider": provider,
        "confidence": _confidence_for_annotations(annotations, len(input_contract["documents"]), recomputed=False),
    }
    return _validate_provider_output(result, input_contract)


def execute_deterministic(data: dict[str, Any]) -> dict[str, Any]:
    input_contract = validate_input(data)
    return _local_annotations(input_contract, provider="fingpt")


def execute_controlled_llm(data: dict[str, Any]) -> dict[str, Any]:
    input_contract = validate_input(data)
    return _local_annotations(input_contract, provider="controlled_llm")


def validate_provider_input(data: dict[str, Any]) -> None:
    validate_input(data)


def validate_provider_output(data: dict[str, Any]) -> None:
    _validate_provider_output(data)


def qualify_provider_output(data: dict[str, Any]) -> dict[str, Any]:
    _validate_provider_output(data)
    return {
        "passed": True, "read_only": True, "capability_allowlisted": True,
        "final_advice": False, "write_permissions": [],
    }


def build_adapter(*, adapter_id: str = "deterministic-fingpt-nlp-v1", fallback: bool = False) -> AdapterDefinition:
    return AdapterDefinition(
        adapter_id, "v1", CONTRACT, PROVIDER_OUTPUT_CONTRACT,
        "deterministic" if not fallback else "probabilistic",
        execute_controlled_llm if fallback else execute_deterministic,
        validate_provider_input, validate_provider_output, qualify_provider_output,
        provider="controlled_llm" if fallback else "fingpt",
        capabilities=tuple(sorted(CAPABILITIES)),
        state_permissions=("read:evidence",),
    )


def install_qualification() -> dict[str, Any]:
    as_of = "2026-10-06T01:45:00Z"
    documents = [{
        "document_id": "install-1", "text": "Company reported revenue growth.",
        "source": "public_evidence", "source_ref": "evidence:install:1",
        "as_of": as_of, "known_at": as_of, "language": "en",
    }]
    request = build_input(documents, capability="financial_sentiment", as_of=as_of, cycle_id="fingpt-install")
    provider = execute_deterministic(request)
    result = build_output(request, provider)
    replay = frozen_replay(request, result)
    checks = {
        "qualified": result["state"] == "qualified", "read_only": result["permissions"] == _PERMISSIONS,
        "versioned": result["model_version"] and result["data_version"], "replay": replay == frozen_replay(copy.deepcopy(request), copy.deepcopy(result)),
    }
    return {
        "contract": "FinGPTAdapterInstallQualification/v1", "qualified": all(checks.values()),
        "checks": checks, "replay_sha256": replay["sha256"], "evaluation_vector": replay["evaluation_vector"],
    }


# Descriptive aliases used by callers that do not need generic names.
build_fingpt_input = build_input
build_fingpt_output = build_output
validate_fingpt_input = validate_input
validate_fingpt_output = validate_output
degrade_with_controlled_llm = controlled_degradation
replay = frozen_replay


if __name__ == "__main__":
    print(canonical_json(install_qualification()))
