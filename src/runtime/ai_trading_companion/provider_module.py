"""The sole public Provider execution and management seam.

Business callers pass language, an intellect slot, and effort only.  Parsing JSON,
schema checks, evidence gates, and any decision verifier deliberately remain
outside this module so they can never rewrite an external call's outcome.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import time
from typing import Any, Callable

from .config import provider_management, refresh_all_provider_models, record_model_fulfillment
from .provider_broker import ProviderBroker, StageRequest, canonical_packet_hash
from .provider_routes import catalog_entry


TECHNICAL_STATUSES = frozenset({
    "completed", "unavailable", "transport_failed", "protocol_failed",
    "stream_incomplete", "timed_out", "cancelled", "not_started",
})


@dataclass(frozen=True)
class GenerateRequest:
    prompt: str
    intellect: str
    effort: str | None = None
    deadline: float = math.inf
    on_delta: Callable[[str], None] | None = None
    output_token_limit: int = 2_000

    def __post_init__(self) -> None:
        if self.intellect not in {"standard", "smart", "expert"}:
            raise ValueError("intellect must be standard, smart, or expert")
        if not self.prompt:
            raise ValueError("prompt is required")


@dataclass(frozen=True)
class GenerateResult:
    text: str | None
    status: str
    provider: str | None = None
    model: str | None = None
    model_family: str | None = None
    tier: int | None = None
    requested_intellect: str | None = None
    effective_intellect: str | None = None
    ttft_seconds: float | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    cost: float | None = None
    currency: str | None = None
    request_id: str | None = None
    requested_effort: str | None = None
    effective_effort: str | None = None
    attempts: tuple[dict[str, Any], ...] = ()


class ProviderModule:
    def __init__(self, broker: ProviderBroker, *, management_home: Any = None) -> None:
        self._broker, self._management_home = broker, management_home

    def generate(self, request: GenerateRequest) -> GenerateResult:
        packet = {"prompt": request.prompt}
        deadline = request.deadline
        if deadline == math.inf:
            deadline = time.monotonic() + 90
        target_level = {"standard": "L1", "smart": "L2", "expert": "L3"}[request.intellect]
        if deadline <= time.monotonic():
            return GenerateResult(None, "not_started", requested_intellect=request.intellect)
        outcomes = []
        levels = ("L1", "L2", "L3")
        for level in levels[levels.index(target_level):]:
            outcome = self._broker.invoke(StageRequest(
                intellect=request.intellect, packet=packet, packet_sha256=canonical_packet_hash(packet),
                effort=request.effort, visible_stream=request.on_delta is not None, on_delta=request.on_delta,
                absolute_deadline=deadline, output_token_allowance=request.output_token_limit,
                target_level=level,
            ))
            outcomes.append(outcome)
            if outcome.winner_route:
                target_level = level
                break
            if time.monotonic() >= deadline:
                break
        outcome = outcomes[-1]
        winner = next((item for item in outcome.attempts if item.winner), None)
        if outcome.winner_route and isinstance(outcome.result, str):
            if self._management_home is not None and outcome.endpoint_id and outcome.model and winner:
                record_model_fulfillment(self._management_home, outcome.endpoint_id, winner.requested_model or winner.model, outcome.model)
            return GenerateResult(outcome.result, "completed", outcome.endpoint_id, outcome.model,
                outcome.model_family, winner.tier if winner else None, request.intellect,
                _actual_intellect(self._broker.provider, outcome.model, outcome.model_family)
                or {"L1": "standard", "L2": "smart", "L3": "expert"}[target_level],
                outcome.ttft_seconds, dict(outcome.usage), outcome.actual_cost if outcome.actual_cost is not None else outcome.estimated_cost,
                outcome.currency, outcome.request_id, request.effort,
                winner.effective_effort if winner else None,
                tuple(item.__dict__.copy() for candidate in outcomes for item in candidate.attempts))
        status = _technical_status(outcome)
        return GenerateResult(None, status, requested_intellect=request.intellect,
            attempts=tuple(item.__dict__.copy() for candidate in outcomes for item in candidate.attempts))

    def manage(self, command: dict[str, Any]) -> dict[str, Any]:
        if self._management_home is None:
            raise RuntimeError("Provider management home is not configured")
        return provider_management(self._management_home, command)

    def refresh_models(self) -> dict[str, Any]:
        """Refresh all enabled Provider inventories before a new task starts."""
        if self._management_home is None:
            raise RuntimeError("Provider management home is not configured")
        return refresh_all_provider_models(self._management_home)


def _technical_status(outcome: Any) -> str:
    if not outcome.attempts:
        return "unavailable"
    errors = {item.terminal_error for item in outcome.attempts if item.terminal_error}
    if any("timeout" in item for item in errors): return "timed_out"
    if any("cancel" in item for item in errors): return "cancelled"
    if any("stream" in item for item in errors): return "stream_incomplete"
    if any(item.protocol_success for item in outcome.attempts): return "protocol_failed"
    return "transport_failed"


def _actual_intellect(provider: dict[str, Any], model: str | None, family: str | None) -> str | None:
    entry = catalog_entry(provider, str(family or ""), str(model or ""))
    return {"L1": "standard", "L2": "smart", "L3": "expert"}.get(str(entry.get("target_level") or "")) if entry else None
