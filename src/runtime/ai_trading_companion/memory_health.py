from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .fallback_spec import build_receipt, sha256


@dataclass(frozen=True)
class MemoryCapabilityDecision:
    app_available: bool
    history_readable: bool
    memory_tasks_available: bool
    derivation_available: bool
    blocked_sources: tuple[str, ...]
    allow_local_memory_fallback: bool = False
    fallback: dict[str, Any] = field(default_factory=dict)


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
        receipt = build_receipt(
            "MemoryHub", "capability_health",
            status="unavailable" if not ledger_ready else "degraded" if not (index_ready and derivation_ready) or blocked_sources else "succeeded",
            as_of=str(health.get("as_of") or "unspecified"), source_contract="memoryhub/v1",
            source_version=str(health.get("protocol_version") or "memoryhub/v1"), input_sha256=sha256(health),
        )
        return MemoryCapabilityDecision(
            app_available=receipt["continuation"] != "blocked",
            history_readable=ledger_ready,
            memory_tasks_available=ledger_ready and index_ready,
            derivation_available=ledger_ready and derivation_ready,
            blocked_sources=blocked_sources,
            fallback=receipt,
        )
