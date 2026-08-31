from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MemoryCapabilityDecision:
    app_available: bool
    history_readable: bool
    memory_tasks_available: bool
    derivation_available: bool
    blocked_sources: tuple[str, ...]
    allow_local_memory_fallback: bool = False


class MemoryCapabilityPolicy:
    @staticmethod
    def evaluate(health: dict[str, Any]) -> MemoryCapabilityDecision:
        ledger_ready = (health.get("ledger") or {}).get("state") == "ready"
        index_ready = (health.get("index") or {}).get("state") == "ready"
        derivation_ready = (health.get("derivation") or {}).get("state") in {"ready", "degraded"}
        blocked_sources = tuple(
            sorted(
                name for name, value in (health.get("sources") or {}).items()
                if (value or {}).get("state") != "ready"
            )
        )
        return MemoryCapabilityDecision(
            app_available=ledger_ready,
            history_readable=ledger_ready,
            memory_tasks_available=ledger_ready and index_ready,
            derivation_available=ledger_ready and derivation_ready,
            blocked_sources=blocked_sources,
        )
