"""The versioned, runtime-owned contract for one decision cycle.

This module is deliberately deterministic.  It describes identity and state
semantics; it does not make market judgments and it never owns business facts.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


SPEC_VERSION = "CompanionDecisionCycleSpec/v1"

STAGES = (
    "evidence",
    "m0",
    "h0",
    "m1",
    "m2",
    "outcome",
    "reflection",
)

# These are the states used by the current runtime.  Keeping the complete set
# here makes migrations and replay validation reject typos without imposing a
# second state machine on the older execution paths.
STATES = frozenset(
    {
        "queued",
        "open",
        "researching",
        "researching_m0",
        "m0_retry_wait",
        "awaiting_h0",
        "voice_grace",
        "h0_locked",
        "researching_m1",
        "judging_m1",
        "m1_retry_wait",
        "m1_ready",
        "synthesizing_m2",
        "m2_deferred",
        "waiting_for_repair",
        "outcome_pending",
        "outcome_ready",
        "reflecting",
        "model_only_ready",
        "complete",
        "reflected",
        "failed",
        "skipped",
        "missed",
        "closed",
    }
)

_M1_FORBIDDEN_KEYS = frozenset(
    {
        "h0",
        "h0_text",
        "h0_raw",
        "h0_message",
        "h0_artifact",
        "h0_artifact_id",
        "h0_signal",
        "h0_signals",
        "h0_action",
        "h0_action_result",
        "human_text",
        "human_message",
        "human_signal",
        "human_signals",
        "derived_signal",
        "derived_signals",
        "action_result",
    }
)


def _require_time(value: str, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return text


def validate_m1_blind_packet(value: Any, *, path: str = "packet") -> None:
    """Reject H0 raw text and H0-derived signals at the durable LLM boundary."""
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().lower()
            if normalized in _M1_FORBIDDEN_KEYS:
                raise ValueError(f"M1 packet exposes H0 or a derived H0 signal at {path}.{key}")
            validate_m1_blind_packet(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            validate_m1_blind_packet(child, path=f"{path}[{index}]")


@dataclass(frozen=True)
class CompanionDecisionCycleSpec:
    """Immutable identity and provenance captured when a cycle is created."""

    cycle_id: str
    task_key: str
    as_of: str
    scheduled_for: str
    schedule_id: str | None
    schedule_revision: int | None
    schedule_snapshot: dict[str, Any] | None
    version: str = SPEC_VERSION

    def __post_init__(self) -> None:
        if not str(self.cycle_id).strip():
            raise ValueError("cycle_id is required")
        if not str(self.task_key).strip():
            raise ValueError("task_key is required")
        _require_time(self.as_of, "as_of")
        _require_time(self.scheduled_for, "scheduled_for")
        if self.schedule_revision is not None and int(self.schedule_revision) < 1:
            raise ValueError("schedule_revision must be positive")
        if self.version != SPEC_VERSION:
            raise ValueError(f"unsupported cycle contract: {self.version}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract": self.version,
            "cycle_id": self.cycle_id,
            "task_key": self.task_key,
            "as_of": self.as_of,
            "scheduled_for": self.scheduled_for,
            "schedule_id": self.schedule_id,
            "schedule_revision": self.schedule_revision,
            "schedule_snapshot": self.schedule_snapshot,
        }


def validate_state(state: str) -> str:
    normalized = str(state or "").strip()
    if normalized not in STATES:
        raise ValueError(f"unsupported cycle state: {normalized}")
    return normalized


def memory_boundary(cycle: dict[str, Any], stage: str, as_of: str) -> tuple[str, str]:
    """Keep diagnostic M1 under the original cycle's MemoryHub exclusion."""
    import json

    if not stage.startswith("m1"):
        return cycle["cycle_id"], as_of
    snapshot = json.loads(cycle.get("schedule_snapshot_json") or "{}")
    cycle_id = snapshot.get("blind_source_cycle_id") or cycle["cycle_id"]
    cutoff = cycle.get("private_context_frozen_at") or cycle.get("h0_locked_at")
    if cutoff and datetime.fromisoformat(cutoff.replace("Z", "+00:00")) < datetime.fromisoformat(as_of.replace("Z", "+00:00")):
        as_of = cutoff
    return str(cycle_id), as_of
