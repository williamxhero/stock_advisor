"""Runtime-owned ObservabilitySpec/v1 event and evaluation contracts.

This module records lifecycle facts without turning them into one mutable status or
one composite score.  Runtime writes events; observatories and evaluators consume
and validate the immutable records.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from types import MappingProxyType

from .secret_guard import assert_safe
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

CONTRACT = "ObservabilitySpec/v1"
VERSION = 1
REPLAY_CONTRACT = "ObservabilitySpecReplay/v1"
EVALUATION_CONTRACT = "ObservabilityEvaluation/v1"
REPLAY_VERSION = 1
EVALUATION_VERSION = 1
RUNTIME_SOURCE = "runtime"
EVENT_TYPES = frozenset({"plan_started", "actual_started", "qualified_delivery", "failed"})
EVALUATION_DIMENSIONS = (
    "delivery_speed", "qualification_probability", "research_quality",
    "judgment_outcome", "safety_reliability",
)
_FORBIDDEN_AGGREGATES = frozenset({
    "aggregate", "aggregate_score", "composite", "composite_score", "overall",
    "overall_score", "score", "scores", "total_score", "weighted_score",
    "weighted_average", "single_score",
})
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_PRIVATE_FIELDS = frozenset({
    "chain_of_thought", "private_reasoning", "reasoning_content", "thinking",
    "thinking_blocks", "raw_packet", "input_packet", "private_context",
})


def _validate_payload(value: Any) -> None:
    """Bound telemetry before copying/hashing; never persist private reasoning."""
    nodes = 0

    def visit(item: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > 4096 or depth > 16:
            raise ObservabilityContractError("observation payload exceeds structural limits")
        if isinstance(item, Mapping):
            for key, child in item.items():
                if not isinstance(key, str) or len(key) > 512:
                    raise ObservabilityContractError("observation keys must be bounded strings")
                if key.strip().casefold().replace("-", "_") in _PRIVATE_FIELDS:
                    raise ObservabilityContractError("private content is forbidden in observability")
                visit(child, depth + 1)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child, depth + 1)
        elif isinstance(item, str):
            if len(item) > 8192:
                raise ObservabilityContractError("observation string exceeds size limit")
        elif item is not None and type(item) not in {bool, int, float}:
            raise ObservabilityContractError("observations must contain only JSON values")
        elif isinstance(item, float) and not math.isfinite(item):
            raise ObservabilityContractError("observation numbers must be finite")

    visit(value, 0)
    raw = canonical_json(value)
    if len(raw.encode("utf-8")) > 131072:
        raise ObservabilityContractError("observation payload exceeds size limit")
    assert_safe(raw, boundary="observability")


class ObservabilityContractError(ValueError):
    """Raised when a versioned observation is malformed or contradictory."""


class AggregateScoreError(ObservabilityContractError):
    """Raised when independent evaluation dimensions are collapsed."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _digest_body(value: Mapping[str, Any]) -> str:
    return sha256({key: item for key, item in value.items() if key != "sha256"})


def _text(value: Any, field: str, limit: int = 512) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ObservabilityContractError(f"{field} must be a non-empty string")
    result = value.strip()
    if len(result) > limit:
        raise ObservabilityContractError(f"{field} is too long")
    return result


def timestamp(value: Any, field: str) -> str:
    result = _text(value, field, 80)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ObservabilityContractError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ObservabilityContractError(f"{field} must be ISO-8601 with timezone")
    return result


def _timestamp_or_none(value: Any, field: str) -> str | None:
    return None if value is None else timestamp(value, field)


def _reject_aggregate_keys(value: Any, path: str = "evaluation") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).strip().casefold().replace("-", "_")
            if normalized in _FORBIDDEN_AGGREGATES:
                raise AggregateScoreError(f"ObservabilitySpec forbids aggregate score field at {path}.{key}")
            _reject_aggregate_keys(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_aggregate_keys(child, f"{path}[{index}]")


def _copy(value: Any) -> Any:
    return copy.deepcopy(value)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(child) for child in value)
    if isinstance(value, tuple):
        return tuple(_freeze(child) for child in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw(child) for child in value]
    return copy.deepcopy(value)


def _normalise_attribution(value: Any, *, event_type: str) -> dict[str, Any]:
    raw = {} if value is None else value
    if not isinstance(raw, Mapping):
        raise ObservabilityContractError("attribution must be an object")
    unknown = set(raw) - {"user_wait", "workload", "upstream"}
    if unknown:
        raise ObservabilityContractError(f"unknown attribution fields: {sorted(unknown)}")
    wait = raw.get("user_wait", {})
    workload = raw.get("workload", {})
    upstream = raw.get("upstream", {})
    if not all(isinstance(item, Mapping) for item in (wait, workload, upstream)):
        raise ObservabilityContractError("attribution sections must be objects")
    wait_result = {
        "started_at": _timestamp_or_none(wait.get("started_at"), "attribution.user_wait.started_at"),
        "ended_at": _timestamp_or_none(wait.get("ended_at"), "attribution.user_wait.ended_at"),
        "duration_ms": wait.get("duration_ms"),
    }
    if wait_result["duration_ms"] is not None:
        if type(wait_result["duration_ms"]) is not int or wait_result["duration_ms"] < 0:
            raise ObservabilityContractError("attribution.user_wait.duration_ms must be a non-negative integer")
    if set(wait) - {"started_at", "ended_at", "duration_ms"}:
        raise ObservabilityContractError("unknown user wait fields")
    if wait_result["started_at"] and wait_result["ended_at"]:
        duration = int((_parse(wait_result["ended_at"]) - _parse(wait_result["started_at"])).total_seconds() * 1000)
        if duration < 0:
            raise ObservabilityContractError("user wait ended_at cannot precede started_at")
        if wait_result["duration_ms"] is not None and wait_result["duration_ms"] != duration:
            raise ObservabilityContractError("user wait duration does not match its boundaries")
        wait_result["duration_ms"] = duration
    elif wait_result["ended_at"] and not wait_result["started_at"]:
        raise ObservabilityContractError("user wait cannot end before it starts")
    if wait_result["duration_ms"] is not None and not (wait_result["started_at"] and wait_result["ended_at"]):
        raise ObservabilityContractError("user wait duration requires both boundaries")
    if event_type == "plan_started" and any(value is not None for value in wait_result.values()):
        raise ObservabilityContractError("planning cannot start user wait")
    if event_type == "actual_started" and wait_result["ended_at"] is not None:
        raise ObservabilityContractError("actual start cannot close user wait")
    if event_type == "qualified_delivery" and wait_result["started_at"] and not wait_result["ended_at"]:
        raise ObservabilityContractError("qualified delivery must close user wait")
    workload_result = _copy(dict(workload))
    if "duration_ms" in workload_result and (
        type(workload_result["duration_ms"]) is not int or workload_result["duration_ms"] < 0
    ):
        raise ObservabilityContractError("attribution.workload.duration_ms must be a non-negative integer")
    upstream_result = _copy(dict(upstream))
    if upstream_result:
        for key in ("source_cycle_id", "source_task_key", "source_stage", "source_event_id"):
            if key in upstream_result and upstream_result[key] is not None:
                _text(upstream_result[key], f"attribution.upstream.{key}")
        for key in ("known_at", "reused_at"):
            if key in upstream_result and upstream_result[key] is not None:
                upstream_result[key] = timestamp(upstream_result[key], f"attribution.upstream.{key}")
        if upstream_result.get("known_at") and upstream_result.get("reused_at"):
            if _parse(upstream_result["known_at"]) > _parse(upstream_result["reused_at"]):
                raise ObservabilityContractError("upstream known_at cannot be after reused_at")
    return {"user_wait": wait_result, "workload": workload_result, "upstream": upstream_result}


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _normalise_source(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ObservabilityContractError("source must be an object")
    result = dict(value)
    source = _text(result.get("source"), "source.source")
    if source != RUNTIME_SOURCE:
        raise PermissionError("Observability execution facts must be Runtime-owned")
    _text(result.get("component"), "source.component")
    _text(result.get("writer"), "source.writer")
    result["source"] = source
    return result


def _body(event: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _copy(value) for key, value in event.items() if key != "sha256"}


def build_event(
    *, event_id: str, event_type: str, cycle_id: str, task_key: str, occurred_at: str,
    known_at: str | None = None, recorded_at: str | None = None, stage: str | None = None,
    stage_run_id: str | None = None, attempt_id: str | None = None,
    status: str | None = None, source: Mapping[str, Any] | None = None,
    attribution: Mapping[str, Any] | None = None, observations: Mapping[str, Any] | None = None,
    provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a deterministic envelope; callers must supply Runtime-owned identity."""
    _validate_payload({"source": source, "attribution": attribution, "observations": observations, "provenance": provenance})
    if event_type not in EVENT_TYPES:
        raise ObservabilityContractError(f"unsupported observation event_type: {event_type}")
    event = {
        "contract": CONTRACT, "contract_version": VERSION,
        "event_id": _text(event_id, "event_id"), "event_type": event_type,
        "cycle_id": _text(cycle_id, "cycle_id"), "task_key": _text(task_key, "task_key"),
        "stage": None if stage is None else _text(stage, "stage"),
        "stage_run_id": None if stage_run_id is None else _text(stage_run_id, "stage_run_id"),
        "attempt_id": None if attempt_id is None else _text(attempt_id, "attempt_id"),
        "occurred_at": timestamp(occurred_at, "occurred_at"),
        "known_at": timestamp(known_at or occurred_at, "known_at"),
        "recorded_at": timestamp(recorded_at or known_at or occurred_at, "recorded_at"),
        "status": _text(status or event_type, "status"),
        "source": _normalise_source(source or {"source": RUNTIME_SOURCE, "component": "runtime", "writer": "runtime"}),
        "attribution": _normalise_attribution(attribution, event_type=event_type),
        "observations": _copy(dict(observations or {})),
        "provenance": _copy(dict(provenance or {})),
    }
    if event_type == "actual_started" and (attribution is None or "user_wait" not in attribution):
        event["attribution"]["user_wait"]["started_at"] = event["occurred_at"]
    validate_event(event, verify_digest=False)
    event["sha256"] = _digest_body(event)
    return event


def validate_event(event: Mapping[str, Any], *, verify_digest: bool = True) -> dict[str, Any]:
    if not isinstance(event, Mapping):
        raise ObservabilityContractError("observation event must be an object")
    _validate_payload(event)
    required = {
        "contract", "contract_version", "event_id", "event_type", "cycle_id", "task_key",
        "stage", "stage_run_id", "attempt_id", "occurred_at", "known_at", "recorded_at",
        "status", "source", "attribution", "observations", "provenance", "sha256",
    }
    expected = required if verify_digest else required - {"sha256"}
    if set(event) != expected:
        raise ObservabilityContractError(f"observation event fields are not exact: {sorted(set(event) ^ expected)}")
    if event["contract"] != CONTRACT or type(event["contract_version"]) is not int or event["contract_version"] != VERSION:
        raise ObservabilityContractError("unsupported ObservabilitySpec contract")
    if event["event_type"] not in EVENT_TYPES:
        raise ObservabilityContractError("unsupported observation event type")
    for field in ("event_id", "cycle_id", "task_key", "status"):
        _text(event[field], field)
    for field in ("stage", "stage_run_id", "attempt_id"):
        if event[field] is not None:
            _text(event[field], field)
    for field in ("occurred_at", "known_at", "recorded_at"):
        timestamp(event[field], field)
    if _parse(event["known_at"]) < _parse(event["occurred_at"]):
        raise ObservabilityContractError("known_at cannot precede occurred_at")
    if _parse(event["recorded_at"]) < _parse(event["known_at"]):
        raise ObservabilityContractError("recorded_at cannot precede known_at")
    _normalise_source(event["source"])
    _normalise_attribution(event["attribution"], event_type=event["event_type"])
    if not isinstance(event["observations"], Mapping) or not isinstance(event["provenance"], Mapping):
        raise ObservabilityContractError("observations and provenance must be objects")
    _reject_aggregate_keys(event["observations"], "observations")
    _reject_aggregate_keys(event["provenance"], "provenance")
    if verify_digest:
        if not isinstance(event["sha256"], str) or not _HEX64.fullmatch(event["sha256"]):
            raise ObservabilityContractError("sha256 must be a SHA-256 digest")
        if event["sha256"] != _digest_body(event):
            raise ObservabilityContractError("observation event digest mismatch")
    return _copy(dict(event))


# Explicit aliases make the contract convenient for callers using either noun.
make_observation_event = build_event
validate_observation_event = validate_event
observation_event_digest = sha256


def compute_wait_workload(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Derive wait and workload independently from immutable lifecycle events."""
    validated = [validate_event(row) for row in events]
    if not validated:
        return {
            "user_wait": {"started_at": None, "ended_at": None, "duration_ms": None},
            "workload": {"direct_event_count": 0, "retry_count": 0, "upstream_duration_ms": 0},
            "upstream": [],
        }
    cycle_ids = {row["cycle_id"] for row in validated}
    stages = {row["stage"] for row in validated}
    if len(cycle_ids) != 1 or len(stages) != 1:
        raise ObservabilityContractError("wait/workload events must share one cycle and stage")
    starts = [
        row for row in validated
        if row["event_type"] == "actual_started"
        and row["attribution"]["user_wait"].get("started_at") is not None
    ]
    deliveries = [row for row in validated if row["event_type"] == "qualified_delivery"]
    start = min(starts, key=lambda row: row["occurred_at"]) if starts else None
    delivery = min(deliveries, key=lambda row: row["occurred_at"]) if deliveries else None
    wait: dict[str, Any] = {"started_at": None, "ended_at": None, "duration_ms": None}
    if start:
        wait["started_at"] = start["occurred_at"]
    if delivery:
        wait["ended_at"] = delivery["occurred_at"]
    if wait["started_at"] and wait["ended_at"]:
        duration_ms = int((_parse(wait["ended_at"]) - _parse(wait["started_at"])).total_seconds() * 1000)
        if duration_ms < 0:
            raise ObservabilityContractError("qualified delivery cannot precede actual start")
        wait["duration_ms"] = duration_ms
    workload = {"direct_event_count": len(validated), "retry_count": 0, "upstream_duration_ms": 0}
    upstream: list[dict[str, Any]] = []
    for row in validated:
        metrics = row["observations"].get("workload")
        if isinstance(metrics, Mapping):
            workload["retry_count"] += int(metrics.get("retry_count") or 0)
        source = row["attribution"].get("upstream")
        if source:
            upstream.append(_copy(dict(source)))
            workload["upstream_duration_ms"] += int((row["attribution"].get("workload") or {}).get("duration_ms") or 0)
    return {"user_wait": wait, "workload": workload, "upstream": upstream}


def build_evaluation_vector(dimensions: Mapping[str, Any], *, provenance: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build an independent multidimensional vector with no aggregate score."""
    result = {
        "contract": EVALUATION_CONTRACT, "contract_version": EVALUATION_VERSION,
        "dimensions": _copy(dict(dimensions)), "provenance": _copy(dict(provenance or {})),
    }
    validate_evaluation_vector(result, verify_digest=False)
    result["sha256"] = _digest_body(result)
    return result


def validate_evaluation_vector(value: Mapping[str, Any], *, verify_digest: bool = True) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ObservabilityContractError("evaluation vector must be an object")
    required = {"contract", "contract_version", "dimensions", "provenance", "sha256"}
    expected = required if verify_digest else required - {"sha256"}
    if set(value) != expected:
        raise ObservabilityContractError("evaluation vector fields are not exact")
    if (
        value["contract"] != EVALUATION_CONTRACT
        or type(value["contract_version"]) is not int
        or value["contract_version"] != EVALUATION_VERSION
        or not isinstance(value["dimensions"], Mapping)
        or not isinstance(value["provenance"], Mapping)
    ):
        raise ObservabilityContractError("invalid evaluation vector contract")
    dimension_keys = set(value["dimensions"])
    missing = set(EVALUATION_DIMENSIONS) - dimension_keys
    if missing:
        raise ObservabilityContractError(f"evaluation vector missing dimensions: {sorted(missing)}")
    unknown = dimension_keys - set(EVALUATION_DIMENSIONS)
    if unknown:
        raise ObservabilityContractError(f"evaluation vector has unknown dimensions: {sorted(unknown)}")
    _reject_aggregate_keys(value, "evaluation")
    if verify_digest:
        if not isinstance(value["sha256"], str) or not _HEX64.fullmatch(value["sha256"]):
            raise ObservabilityContractError("evaluation vector digest is invalid")
        if value["sha256"] != _digest_body(value):
            raise ObservabilityContractError("evaluation vector digest mismatch")
    return _copy(dict(value))


def frozen_replay(event: Mapping[str, Any] | None = None) -> dict[str, Any]:
    source = validate_event(event or {})
    replay = {
        "contract": REPLAY_CONTRACT, "contract_version": REPLAY_VERSION,
        "source_sha256": source["sha256"], "source": source,
    }
    replay["sha256"] = sha256(replay)
    return replay


def validate_replay(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"contract", "contract_version", "source_sha256", "source", "sha256"}:
        raise ObservabilityContractError("replay fields are not exact")
    if (
        value["contract"] != REPLAY_CONTRACT
        or type(value["contract_version"]) is not int
        or value["contract_version"] != REPLAY_VERSION
        or not isinstance(value["source"], Mapping)
        or value["source_sha256"] != value["source"].get("sha256")
    ):
        raise ObservabilityContractError("replay source digest mismatch")
    validate_event(value["source"])
    if value["sha256"] != sha256({key: value[key] for key in value if key != "sha256"}):
        raise ObservabilityContractError("replay digest mismatch")
    return _copy(dict(value))


def event_type_for_runtime_event(event_type: str) -> str | None:
    """Map existing Runtime event vocabulary to independent observation states."""
    value = str(event_type or "")
    if value == "cycle.created" or value.endswith(".planned"):
        return "plan_started"
    if value == "stage.started":
        return "actual_started"
    if value in {
        "m0.started", "m1.started", "m1.judging", "m2.started",
        "chat.stream.started", "analysis.request.created",
    }:
        return "actual_started"
    if value in {
        "stage.succeeded", "m0.ready", "m1.ready", "m2.ready", "m1.published", "m2.published",
        "chat.ready", "premarket.reply.ready", "outcome.ready", "reflection.ready",
        "chat.stream.completed", "chat.stream.finished",
    }:
        return "qualified_delivery"
    if value.endswith(".failed") or value in {
        "cycle.missed", "research.retrying", "research.retry_waiting", "m1.retrying",
        "m2.deferred", "stage.retry_waiting", "stage.skipped", "stage.rolled_back",
        "attempt.failed", "attempt.rejected", "attempt.timed_out",
    }:
        return "failed"
    if value == "attempt.started":
        return "actual_started"
    return None


@dataclass(frozen=True)
class ObservationEvent:
    """Small typed facade over a validated immutable event."""

    value: Mapping[str, Any]

    def __post_init__(self) -> None:
        validated = validate_event(self.value)
        object.__setattr__(self, "value", _freeze(validated))

    def to_dict(self) -> dict[str, Any]:
        return _thaw(self.value)
