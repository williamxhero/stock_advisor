"""Versioned transition-condition semantics shared by judgment paths."""
from __future__ import annotations

from typing import Any

TRANSITION_CONDITION_VERSION = 2


def is_event_condition(condition: dict[str, Any]) -> bool:
    return str(condition.get("kind") or "").strip() == "event"


def is_valid_condition(condition: dict[str, Any]) -> bool:
    if not isinstance(condition, dict):
        return False
    if str(condition.get("outcome") or "").strip() not in {"upgrade", "downgrade"}:
        return False
    if is_event_condition(condition):
        return bool(str(condition.get("event") or "").strip()) and bool(
            [ref for ref in condition.get("evidence_refs") or [] if str(ref).strip()]
        )
    return all(str(condition.get(key) or "").strip() for key in ("price", "breadth", "persistence"))


def condition_text(condition: dict[str, Any]) -> str:
    if is_event_condition(condition):
        return str(condition.get("event") or "").strip().rstrip("。！？；，.!?; ")
    return "，".join(
        str(condition.get(key) or "").strip().rstrip("。！？；，.!?; ")
        for key in ("price", "breadth", "persistence")
        if str(condition.get(key) or "").strip()
    )
