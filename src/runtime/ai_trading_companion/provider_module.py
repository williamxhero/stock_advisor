"""Small public seam for Provider generation and management.

Callers choose language, an intellect level, and optional effort. Business
schemas, evidence gates, and verifiers remain outside this module, so complete
Provider content is never reclassified as a transport failure.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import time
from typing import Any, Callable

from .config import provider_management, record_model_fulfillment, refresh_all_provider_models
from .provider_broker import ProviderBroker, StageRequest, canonical_packet_hash
from .provider_routes import catalog_entry


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
    """Facade over Provider execution, inventory, and management interfaces."""

    def __init__(self, broker: ProviderBroker, *, management_home: Any = None) -> None:
        self._broker = broker
        self._management_home = management_home

    def generate(self, request: GenerateRequest) -> GenerateResult:
        deadline = request.deadline
        if deadline == math.inf:
            deadline = time.monotonic() + 90
        if deadline <= time.monotonic():
            return GenerateResult(
                None, "not_started", requested_intellect=request.intellect,
                requested_effort=request.effort,
            )

        packet = {"prompt": request.prompt}
        outcome = self._broker.invoke(StageRequest(
            stage=None,
            intellect=request.intellect,
            packet=packet,
            packet_sha256=canonical_packet_hash(packet),
            effort=request.effort,
            mode="race",
            required_capabilities=("race",),
            visible_stream=request.on_delta is not None,
            on_delta=request.on_delta,
            absolute_deadline=deadline,
            output_token_allowance=request.output_token_limit,
        ))
        winner = next((item for item in outcome.attempts if item.winner), None)
        attempts = tuple(item.__dict__.copy() for item in outcome.attempts)
        if outcome.winner_route and isinstance(outcome.result, str):
            if self._management_home is not None and outcome.endpoint_id and outcome.model and winner:
                record_model_fulfillment(
                    self._management_home,
                    outcome.endpoint_id,
                    winner.requested_model or winner.model,
                    outcome.model,
                )
            return GenerateResult(
                text=outcome.result,
                status="completed",
                provider=outcome.endpoint_id,
                model=outcome.model,
                model_family=outcome.model_family,
                tier=winner.tier if winner else None,
                requested_intellect=request.intellect,
                effective_intellect=_actual_intellect(
                    self._broker.provider, outcome.model, outcome.model_family,
                ) or _level_intellect(outcome.actual_level) or request.intellect,
                ttft_seconds=outcome.ttft_seconds,
                usage=dict(outcome.usage),
                cost=outcome.actual_cost if outcome.actual_cost is not None else outcome.estimated_cost,
                currency=outcome.currency,
                request_id=outcome.request_id,
                requested_effort=request.effort,
                effective_effort=winner.effective_effort if winner else request.effort,
                attempts=attempts,
            )
        return GenerateResult(
            None,
            _technical_status(outcome),
            requested_intellect=request.intellect,
            requested_effort=request.effort,
            attempts=attempts,
        )

    def manage(self, command: dict[str, Any]) -> dict[str, Any]:
        if self._management_home is None:
            raise RuntimeError("Provider management home is not configured")
        return provider_management(self._management_home, command)

    def refresh_models(self) -> dict[str, Any]:
        if self._management_home is None:
            raise RuntimeError("Provider management home is not configured")
        return refresh_all_provider_models(self._management_home)


def _technical_status(outcome: Any) -> str:
    if not outcome.attempts:
        failure = str((outcome.arbitration or {}).get("failure") or "")
        return "not_started" if failure == "stage_deadline_exhausted" else "unavailable"
    errors = {str(item.terminal_error or "") for item in outcome.attempts}
    if any("timeout" in item for item in errors):
        return "timed_out"
    if any("cancel" in item for item in errors):
        return "cancelled"
    if any("stream" in item for item in errors):
        return "stream_incomplete"
    if any(item.protocol_success for item in outcome.attempts):
        return "protocol_failed"
    return "transport_failed"


def _actual_intellect(provider: dict[str, Any], model: str | None, family: str | None) -> str | None:
    entry = catalog_entry(provider, str(family or ""), str(model or ""))
    return _level_intellect(str((entry or {}).get("target_level") or ""))


def _level_intellect(level: str | None) -> str | None:
    return {"L1": "standard", "L2": "smart", "L3": "expert"}.get(str(level or ""))
