"""Versioned contract and invariants for one companion decision cycle.

The Runtime owns the lifecycle, while this module keeps the vocabulary and the
blindness rule in one place.  The contract is intentionally small: detailed
model attempts remain in ``llm_attempt`` and published prose remains in the
append-only narrative ledger.
"""

from __future__ import annotations

import json
from typing import Any, Iterable


DECISION_CYCLE_CONTRACT = "companion-decision-cycle/v1"
DECISION_CYCLE_VERSION = 1
DECISION_CYCLE_STAGES = ("m0", "h0", "m1", "m2", "result", "reflection", "memory")
STAGE_STATES = frozenset({
    "pending", "running", "retry_wait", "succeeded", "failed", "skipped", "rolled_back",
})

def validate_stage(stage: str) -> str:
    value = str(stage or "").strip()
    if value not in DECISION_CYCLE_STAGES:
        raise ValueError(f"unsupported decision-cycle stage: {value}")
    return value


def validate_stage_state(state: str) -> str:
    value = str(state or "").strip()
    if value not in STAGE_STATES:
        raise ValueError(f"unsupported decision-cycle stage state: {value}")
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def assert_m1_blind(packet: dict[str, Any], *, human_texts: Iterable[str] = ()) -> None:
    """Reject H0 text or H0-derived material before an M1 request is sent.

    M1 may receive the frozen pre-H0 private fact snapshot.  It may not receive
    a current H0 artifact, a cognition result, or a signal named as a human
    input.  The packet builder performs the same check against current-cycle
    artifacts; this helper is the shared defense used by retries and replay.
    """
    forbidden_keys = {
        "h0", "h0_text", "h0_raw", "h0_artifact", "h0_artifact_id",
        "h0_derived", "h0_signal", "derived_h0_signal", "cognition_result",
        "h0_action_result", "h0_action_receipt",
    }

    def walk(value: Any, path: tuple[str, ...] = ()) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).casefold() in forbidden_keys:
                    raise ValueError("M1 packet contains H0 or H0-derived input")
                walk(child, (*path, str(key)))
        elif isinstance(value, list):
            for child in value:
                walk(child, path)

    walk(packet)
    serialized = canonical_json(packet)
    for text in human_texts:
        if text and text in serialized:
            raise ValueError("M1 packet contains current-cycle human content")


def cycle_contract(cycle: dict[str, Any], stages: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the stable, serializable input/output contract projection."""
    try:
        provenance = json.loads(str(cycle.get("cycle_provenance_json") or "{}"))
    except json.JSONDecodeError:
        provenance = {}
    try:
        schedule_snapshot = json.loads(str(cycle.get("schedule_snapshot_json") or "null"))
    except json.JSONDecodeError:
        schedule_snapshot = None
    return {
        "contract": DECISION_CYCLE_CONTRACT,
        "contract_version": DECISION_CYCLE_VERSION,
        "cycle_id": str(cycle["cycle_id"]),
        "task_key": str(cycle["task_key"]),
        "as_of": str(cycle["as_of"]),
        "scheduled_for": str(cycle["scheduled_for"]),
        "schedule": {
            "schedule_id": cycle.get("schedule_id"),
            "revision": cycle.get("schedule_revision"),
            "snapshot": schedule_snapshot,
        },
        "state": str(cycle["state"]),
        "revision": int(cycle.get("revision") or 0),
        "stages": stages,
        "provenance": provenance,
    }
