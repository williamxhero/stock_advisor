"""Canonical Provider capability levels and upgrade-only stage policies."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


LEVEL_ORDER = {"L1": 1, "L2": 2, "L3": 3}


@dataclass(frozen=True)
class CapabilityPolicy:
    requested_level: str
    allowed_levels: tuple[str, ...]


def normalize_model_id(value: Any) -> str:
    return re.sub(r"[_. ]+", "-", str(value or "").strip().casefold())


def capability_level(model: Any) -> str | None:
    value = normalize_model_id(model)
    if "luna" in value:
        return "L1"
    if "terra" in value or "sonnet" in value:
        return "L2"
    if re.search(r"(?:^|-)sol(?:-|$)", value) or "opus" in value:
        return "L3"
    return None


def capability_policy(stage: str) -> CapabilityPolicy:
    value = str(stage or "").casefold()
    if value in {"judgment", "m1_judgment", "m2", "reflection", "workflow_feedback"}:
        return CapabilityPolicy("L3", ("L3",))
    if "research" in value:
        return CapabilityPolicy("L2", ("L2", "L3"))
    return CapabilityPolicy("L1", ("L1", "L2", "L3"))


def upgrade_reason(requested_level: str, actual_level: str, *, attempted_requested: bool) -> str | None:
    if requested_level == actual_level:
        return None
    return (
        f"{requested_level}_CANDIDATES_EXHAUSTED"
        if attempted_requested else f"{requested_level}_UNAVAILABLE_IN_REAL_INVENTORY"
    )
