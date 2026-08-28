"""Cost-aware, route-owned Provider competition.

The broker is intentionally independent of business stages and persistence.
Its transport and audit callbacks make timing/concurrency behavior deterministic
in tests and keep future protocol adapters outside the routing state machine.
"""
from __future__ import annotations

import hashlib
import json
import math
import queue
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from .provider_client import ProviderClient, ProviderError, _message_content
from .provider_capabilities import capability_level, capability_policy, upgrade_reason
from .provider_routes import NEAR_COST_TOLERANCE, catalog_entry
from .secret_guard import assert_safe


@dataclass(frozen=True)
class StageRequest:
    stage: str
    packet: dict[str, Any]
    packet_sha256: str
    effort: str
    schema: dict[str, Any] | None = None
    mode: str = "race"
    required_capabilities: tuple[str, ...] = ("race",)
    visible_stream: bool = False
    on_delta: Callable[[str], None] | None = None
    absolute_deadline: float = math.inf
    route_timeout_seconds: float = 90.0
    output_token_allowance: int = 2_000
    verifier_name: str = "none/v1"
    verifier: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    h0_forbidden: bool = False
    candidate_judgments: tuple[dict[str, Any] | str, ...] = ()
    invocation_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def __post_init__(self) -> None:
        canonical = json.dumps(self.packet, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        actual = hashlib.sha256(canonical).hexdigest()
        if self.packet_sha256 != actual:
            raise ValueError("StageRequest packet hash does not match canonical frozen input")
        if self.h0_forbidden and _contains_h0(self.packet):
            raise ValueError("M1 packet contains H0 or human-message material")
        if self.candidate_judgments and self.mode != "arbitration":
            raise ValueError("Candidate judgments are only valid for arbitration requests")


@dataclass
class TransportResult:
    text: str
    model: str | None = None
    response_id: str | None = None
    request_id: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    actual_cost: float | None = None
    actual_currency: str | None = None


class ProviderTransport(Protocol):
    def probe(self, endpoint: dict[str, Any], timeout: float) -> dict[str, Any]: ...
    def complete(self, endpoint: dict[str, Any], route: dict[str, Any], payload: dict[str, Any],
                 timeout: float, on_delta: Callable[[str], None], cancel: threading.Event) -> TransportResult: ...


class ChatCompletionsTransport:
    """Production OpenAI-compatible transport; route selection stays in Broker."""

    def __init__(self, home: Path, research: dict[str, Any], *, retry: dict[str, Any] | None = None,
                 user_agent: str | None = None) -> None:
        self.home = Path(home)
        self.research = research
        self.retry = retry or {}
        self.user_agent = user_agent

    def _client(self, endpoint: dict[str, Any], route: dict[str, Any] | None = None) -> ProviderClient:
        provider = {**endpoint, "retry": self.retry}
        return ProviderClient(provider, self.research, self.home, user_agent=self.user_agent,
                              transport=str((route or {}).get("transport") or "chat_completions"))

    def probe(self, endpoint: dict[str, Any], timeout: float) -> dict[str, Any]:
        try:
            models = self._client(endpoint)._list_models_once(max(1, int(timeout)))
        except ProviderError as exc:
            if exc.status in {404, 405}:
                return {"status": f"unknown_http_{exc.status}", "models": []}
            raise
        return {
            "status": "available" if models else "empty",
            "models": models,
        }

    def complete(self, endpoint: dict[str, Any], route: dict[str, Any], payload: dict[str, Any],
                 timeout: float, on_delta: Callable[[str], None], cancel: threading.Event) -> TransportResult:
        _assert_tool_free_payload(payload)

        def guarded_delta(delta: str) -> None:
            if cancel.is_set():
                raise ProviderError("Provider request was cancelled", category="provider_cancelled")
            on_delta(delta)

        if cancel.is_set():
            raise ProviderError("Provider request was cancelled", category="provider_cancelled")
        client = self._client(endpoint, route)
        if payload.get("stream"):
            response, _ = client._request_stream_single(
                payload, max(1, int(timeout)), guarded_delta, retry_after_delta=False,
            )
        else:
            response = client._request_single(payload, max(1, int(timeout)))
        if cancel.is_set():
            raise ProviderError("Provider request was cancelled", category="provider_cancelled")
        content = _message_content(response)
        if not content:
            raise ProviderError("Provider completed without message content", category="invalid_response")
        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        actual_cost, currency = _provider_cost(response)
        return TransportResult(
            content, model=str(response.get("model") or "") or None,
            response_id=str(response.get("id") or "") or None,
            request_id=str(response.get("_request_id") or "") or None,
            usage=dict(usage), actual_cost=actual_cost, actual_currency=currency,
        )


@dataclass
class AttemptTrace:
    attempt_id: str
    route_id: str
    endpoint_id: str
    model: str
    model_family: str
    tier: int
    cost_mode: str
    preference: int
    delayed_start: bool
    started_at: float
    requested_level: str | None = None
    actual_level: str | None = None
    upgrade_reason: str | None = None
    runner_fingerprint: str = "provider-broker/chat-completions-v1"
    first_token_at: float | None = None
    completed_at: float | None = None
    protocol_success: bool = False
    product_success: bool = False
    terminal_error: str | None = None
    winner: bool = False
    cancellation_class: str | None = None
    response_id: str | None = None
    request_id: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    estimated_cost: float | None = None
    actual_cost: float | None = None
    currency: str | None = None
    multiplier: float | None = None
    base_price_calibrated: bool = False
    cost_basis: str = "relative_multiplier_only"
    effective_unit_price: dict[str, float] = field(default_factory=dict)
    quality_score: int | None = None
    cost_index: float | None = None
    verifier: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderOutcome:
    invocation_id: str
    packet_sha256: str
    result: dict[str, Any] | str | None
    winner_route: str | None
    endpoint_id: str | None
    model: str | None
    model_family: str | None
    attempts: list[AttemptTrace]
    probes: list[dict[str, Any]]
    requested_level: str | None = None
    actual_level: str | None = None
    upgrade_reason: str | None = None
    request_id: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    ttft_seconds: float | None = None
    estimated_cost: float | None = None
    actual_cost: float | None = None
    currency: str | None = None
    multiplier: float | None = None
    base_price_calibrated: bool = False
    cost_basis: str | None = None
    effective_unit_price: dict[str, float] = field(default_factory=dict)
    verifier: dict[str, Any] = field(default_factory=dict)
    cancellation_status: str | None = None
    visible_locked: bool = False
    visible_incomplete: bool = False
    duel: dict[str, Any] | None = None
    arbitration: dict[str, Any] | None = None

    def as_json(self) -> dict[str, Any]:
        value = asdict(self)
        return value


class ProviderBroker:
    def __init__(self, provider: dict[str, Any], transport: ProviderTransport, *,
                 hedge_seconds: float = 8.0, probe_seconds: float = 3.0,
                 history_score: Callable[[dict[str, Any], str], float] | None = None,
                 audit: Callable[[str, dict[str, Any]], None] | None = None) -> None:
        self.provider = provider
        self.transport = transport
        self.hedge_seconds = hedge_seconds
        self.probe_seconds = probe_seconds
        self.history_score = history_score or (lambda _route, _stage: 0.0)
        self.audit = audit or (lambda _kind, _payload: None)
        self._endpoints = {item["id"]: item for item in provider.get("endpoints", []) if item.get("enabled", True) and not item.get("archived", False)}

    def invoke(self, request: StageRequest) -> ProviderOutcome:
        self.audit("provider_invocation_started", {
            "invocation_id": request.invocation_id, "stage": request.stage, "mode": request.mode,
            "packet_sha256": request.packet_sha256, "absolute_deadline": request.absolute_deadline,
            "route_timeout_seconds": request.route_timeout_seconds, "verifier_name": request.verifier_name,
        })
        health_probes: list[dict[str, Any]] = []
        try:
            health_probes = self._health_gate(request)
            outcome = self._invoke_core(request)
        except Exception as exc:
            self.audit("provider_invocation_finished", {
                "invocation_id": request.invocation_id, "winner_route": None,
                "winner_endpoint": None, "winner_model": None, "winner_family": None,
                "product_disposition": getattr(exc, "category", "internal_error"),
                "attempt_count": 0, "probe_count": len(getattr(exc, "tool_trace", health_probes)),
            })
            raise
        outcome.probes = health_probes + outcome.probes
        self.audit("provider_invocation_finished", {
            "invocation_id": request.invocation_id, "winner_route": outcome.winner_route,
            "winner_endpoint": outcome.endpoint_id, "winner_model": outcome.model,
            "winner_family": outcome.model_family,
            "product_disposition": "qualified" if outcome.winner_route else
            (outcome.arbitration or {}).get("failure", "failed"),
            "attempt_count": len(outcome.attempts), "probe_count": len(outcome.probes),
        })
        return outcome

    def _health_gate(self, request: StageRequest) -> list[dict[str, Any]]:
        """Require two consecutive failed-majority inventory rounds before outage.

        HTTP 404/405 remains an unknown, unusable inventory rather than a
        fabricated model directory or a fleet-wide failure signal.
        """
        endpoint_ids = sorted(self._endpoints)
        if not endpoint_ids:
            return []
        all_records: list[dict[str, Any]] = []
        failed_majority: list[bool] = []
        for round_index in (1, 2):
            deadline = min(request.absolute_deadline, time.monotonic() + self.probe_seconds)
            records: list[dict[str, Any]] = []

            def one(endpoint_id: str) -> dict[str, Any]:
                started = time.monotonic()
                try:
                    raw = self.transport.probe(
                        self._endpoints[endpoint_id], max(0.01, deadline - started),
                    )
                    status, models = _normalized_probe(raw)
                except ProviderError as exc:
                    status = f"unknown_http_{exc.status}" if exc.status in {404, 405} else "definitive_failure"
                    models = set()
                except Exception:
                    status, models = "definitive_failure", set()
                return {
                    "invocation_id": request.invocation_id,
                    "endpoint_id": endpoint_id,
                    "status": status,
                    "models": sorted(models),
                    "model_count": len(models),
                    "probe_round": round_index,
                    "probe_scope": "health_gate",
                    "started_at": started,
                    "completed_at": time.monotonic(),
                }

            executor = ThreadPoolExecutor(
                max_workers=max(1, len(endpoint_ids)), thread_name_prefix="provider-health",
            )
            futures = {executor.submit(one, endpoint_id): endpoint_id for endpoint_id in endpoint_ids}
            try:
                for future in as_completed(futures, timeout=max(0.01, deadline - time.monotonic())):
                    records.append(future.result())
            except TimeoutError:
                pass
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
            observed = {item["endpoint_id"] for item in records}
            for endpoint_id in endpoint_ids:
                if endpoint_id not in observed:
                    records.append({
                        "invocation_id": request.invocation_id,
                        "endpoint_id": endpoint_id,
                        "status": "definitive_failure",
                        "models": [], "model_count": 0,
                        "probe_round": round_index, "probe_scope": "health_gate",
                        "started_at": None, "completed_at": None,
                    })
            records.sort(key=lambda item: item["endpoint_id"])
            for record in records:
                self.audit("provider_probe_attempt", record)
            all_records.extend(records)
            failures = sum(item["status"] in {"definitive_failure", "empty"} for item in records)
            failed_majority.append(failures > len(endpoint_ids) / 2)
        if failed_majority == [True, True]:
            raise ProviderError(
                "Enabled Provider inventory failed in two consecutive majority rounds",
                category="PROVIDER_OUTAGE", retry_attempts=2, tool_trace=all_records,
            )
        return all_records

    def _invoke_core(self, request: StageRequest) -> ProviderOutcome:
        if request.mode == "duel":
            return self._duel(request)
        policy = capability_policy(request.stage)
        routes = self._eligible(request)
        if not routes:
            return ProviderOutcome(request.invocation_id, request.packet_sha256, None, None, None, None, None, [], [], arbitration={"failure": "provider_family_unavailable", "family_mode": self.provider.get("routing", {}).get("family_mode", "auto")})
        attempts: list[AttemptTrace] = []
        probes: list[dict[str, Any]] = []
        attempted_requested = False
        for level in policy.allowed_levels:
            level_routes = [route for route in routes if capability_level(route.get("model")) == level]
            for tier in sorted({int(route["cost"]["tier"]) for route in level_routes}):
                if time.monotonic() >= request.absolute_deadline:
                    break
                members = [route for route in level_routes if int(route["cost"]["tier"]) == tier]
                current, tier_probes = self._probe(members, request)
                probes.extend(tier_probes)
                if not current:
                    continue
                reason = upgrade_reason(policy.requested_level, level, attempted_requested=attempted_requested)
                before = len(attempts)
                winner = self._race_tier(
                    current, request, attempts, requested_level=policy.requested_level,
                    actual_level=level, level_upgrade_reason=reason,
                )
                if level == policy.requested_level and len(attempts) > before:
                    attempted_requested = True
                if winner is not None:
                    trace, result, parsed, visible_incomplete = winner
                    trace.winner = trace.product_success
                    return ProviderOutcome(request.invocation_id, request.packet_sha256, parsed,
                        trace.route_id, trace.endpoint_id, trace.model, trace.model_family,
                        attempts, probes, requested_level=trace.requested_level,
                        actual_level=trace.actual_level, upgrade_reason=trace.upgrade_reason,
                        request_id=trace.request_id, usage=dict(trace.usage),
                        ttft_seconds=(trace.first_token_at - trace.started_at) if trace.first_token_at is not None else None,
                        estimated_cost=trace.estimated_cost, actual_cost=trace.actual_cost, currency=trace.currency,
                        multiplier=trace.multiplier, base_price_calibrated=trace.base_price_calibrated,
                        cost_basis=trace.cost_basis, effective_unit_price=dict(trace.effective_unit_price),
                        verifier=dict(trace.verifier), cancellation_status=trace.cancellation_class,
                        visible_locked=request.visible_stream and trace.first_token_at is not None,
                        visible_incomplete=visible_incomplete)
        return ProviderOutcome(request.invocation_id, request.packet_sha256, None, None, None, None, None, attempts, probes)

    def _eligible(self, request: StageRequest, *, family: str | None = None, exclude: set[str] | None = None) -> list[dict[str, Any]]:
        excluded = exclude or set()
        result = []
        for route in self.provider.get("routes", []):
            if not route.get("enabled", True) or route["endpoint"] not in self._endpoints or route["id"] in excluded:
                continue
            mode = str(self.provider.get("routing", {}).get("family_mode", "auto"))
            required_family = family or (mode if request.mode not in {"duel", "arbitration"} and mode in {"openai", "anthropic"} else None)
            if required_family and route.get("model_family") != required_family:
                continue
            if capability_level(route.get("model")) not in capability_policy(request.stage).allowed_levels:
                continue
            if not set(request.required_capabilities).issubset(set(route.get("capabilities", []))):
                continue
            result.append(route)
        return result

    def _probe(self, routes: list[dict[str, Any]], request: StageRequest) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        endpoint_ids = sorted({route["endpoint"] for route in routes})
        deadline = min(request.absolute_deadline, time.monotonic() + self.probe_seconds)
        status: dict[str, str] = {item: "definitive_failure" for item in endpoint_ids}
        model_directories: dict[str, set[str]] = {}
        records: list[dict[str, Any]] = []
        def one(endpoint_id: str) -> tuple[str, str, set[str], float, float]:
            started = time.monotonic()
            try:
                raw = self.transport.probe(self._endpoints[endpoint_id], max(0.01, deadline - started))
                value, models = _normalized_probe(raw)
            except ProviderError as exc:
                value = f"unknown_http_{exc.status}" if exc.status in {404, 405} else "definitive_failure"
                models = set()
            except Exception:
                value = "definitive_failure"
                models = set()
            return endpoint_id, value, models, started, time.monotonic()
        executor = ThreadPoolExecutor(max_workers=max(1, len(endpoint_ids)), thread_name_prefix="provider-probe")
        futures = [executor.submit(one, item) for item in endpoint_ids]
        try:
            for future in as_completed(futures, timeout=max(0.01, deadline - time.monotonic())):
                endpoint_id, value, models, started, completed = future.result()
                status[endpoint_id] = value
                if models:
                    model_directories[endpoint_id] = models
                record = {"invocation_id": request.invocation_id, "endpoint_id": endpoint_id, "status": value,
                          "models": sorted(models), "model_count": len(models), "probe_scope": "route",
                          "started_at": started, "completed_at": completed}
                records.append(record); self.audit("provider_probe_attempt", record)
        except TimeoutError:
            pass
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        for endpoint_id in endpoint_ids:
            if not any(item["endpoint_id"] == endpoint_id for item in records):
                record = {"invocation_id": request.invocation_id, "endpoint_id": endpoint_id, "status": "definitive_failure",
                          "model_count": 0, "started_at": None, "completed_at": None}
                records.append(record); self.audit("provider_probe_attempt", record)
        probe_rank = {"available": 0, "unknown_http_404": 1, "unknown_http_405": 1,
                      "empty": 2, "definitive_failure": 2}
        usable = [
            route for route in routes
            if status[route["endpoint"]] == "available"
            and _directory_contains(model_directories.get(route["endpoint"], set()), str(route.get("model") or ""))
        ]
        usable = self._sort_tier_routes(usable, request, status, probe_rank)
        return usable, records

    def _sort_tier_routes(self, routes: list[dict[str, Any]], request: StageRequest,
                          status: dict[str, str], probe_rank: dict[str, int]) -> list[dict[str, Any]]:
        """Within a tier, price bands dominate; calibrated quality breaks close costs."""
        tolerance = float(self.provider.get("routing", {}).get("near_cost_tolerance", NEAR_COST_TOLERANCE))
        ordered: list[dict[str, Any]] = []
        for rank in sorted({probe_rank[status[row["endpoint"]]] for row in routes}):
            candidates = [row for row in routes if probe_rank[status[row["endpoint"]]] == rank]
            candidates.sort(key=lambda row: self._pre_cost(row, request))
            group: list[dict[str, Any]] = []
            anchor: float | None = None
            for route in candidates:
                cost = self._pre_cost(route, request)
                if anchor is None or cost <= anchor * (1 + tolerance):
                    group.append(route)
                    if anchor is None: anchor = cost
                    continue
                ordered.extend(self._sort_cost_group(group, request))
                group = [route]; anchor = cost
            ordered.extend(self._sort_cost_group(group, request))
        return ordered

    def _sort_cost_group(self, routes: list[dict[str, Any]], request: StageRequest) -> list[dict[str, Any]]:
        return sorted(routes, key=lambda row: (
            -self._quality_score(row, request.stage), self._pre_cost(row, request),
            -int(row.get("preference", 0)), -self.history_score(row, request.stage), row["id"],
        ))

    def _quality_score(self, route: dict[str, Any], stage: str) -> int:
        entry = catalog_entry(self.provider, str(route.get("model_family") or ""),
                              str(route.get("catalog_model") or route.get("model") or ""))
        value = entry.get("quality", {}).get(_stage_slot(stage)) if entry else None
        return int(value) if isinstance(value, int) else 0

    def _race_tier(self, routes: list[dict[str, Any]], request: StageRequest,
                   attempts: list[AttemptTrace], *, requested_level: str,
                   actual_level: str, level_upgrade_reason: str | None) -> tuple[AttemptTrace, TransportResult, Any, bool] | None:
        events: queue.Queue[tuple[str, str, Any]] = queue.Queue()
        active: dict[str, tuple[dict[str, Any], AttemptTrace, threading.Event]] = {}
        remaining = list(routes)
        locked_id: str | None = None
        published = False

        def launch(route: dict[str, Any], delayed: bool) -> None:
            endpoint = self._endpoints[route["endpoint"]]
            estimate = self._cost_details(route, request, max(1, len(json.dumps(self._input_body(request), ensure_ascii=False)) // 4), request.output_token_allowance)
            trace = AttemptTrace(
                str(uuid.uuid4()), route["id"], endpoint["id"], route["model"], route["model_family"],
                int(route["cost"]["tier"]), str(route["cost"]["mode"]), int(route.get("preference", 0)),
                delayed, time.monotonic(), requested_level=requested_level, actual_level=actual_level,
                upgrade_reason=level_upgrade_reason,
                runner_fingerprint=(
                    "provider-broker/responses-sse-v1" if route.get("transport") == "responses"
                    else "provider-broker/chat-completions-sse-v1"
                ),
                estimated_cost=estimate["amount"], currency=estimate["currency"],
                multiplier=estimate["multiplier"], base_price_calibrated=estimate["calibrated"],
                cost_basis=estimate["basis"], effective_unit_price=estimate["unit_price"],
                quality_score=self._quality_score(route, request.stage),
                cost_index=float(route["cost_index"]) if isinstance(route.get("cost_index"), (int, float)) else None,
            )
            cancel = threading.Event(); attempts.append(trace); active[trace.attempt_id] = (route, trace, cancel)
            payload = self._payload(route, request)
            self.audit("llm_attempt_started", {**asdict(trace), "invocation_id": request.invocation_id,
                                                "packet_sha256": request.packet_sha256, "stage": request.stage,
                                                "verifier_name": request.verifier_name})
            def delta(text: str) -> None:
                nonlocal locked_id, published
                if "\ufffd" in text:
                    raise ProviderError("Provider stream contains replacement characters", category="invalid_encoding")
                assert_safe(text, boundary="Provider stream delta")
                now = time.monotonic()
                if trace.first_token_at is None:
                    trace.first_token_at = now
                    events.put(("first_token", trace.attempt_id, None))
                if request.visible_stream:
                    if locked_id is None:
                        locked_id = trace.attempt_id
                    if locked_id == trace.attempt_id:
                        if request.on_delta:
                            request.on_delta(text)
                        published = True
            def run() -> None:
                timeout = min(request.route_timeout_seconds, max(0.01, request.absolute_deadline - time.monotonic()))
                try:
                    result = self.transport.complete(endpoint, route, payload, timeout, delta, cancel)
                    events.put(("complete", trace.attempt_id, result))
                except Exception as exc:
                    events.put(("error", trace.attempt_id, exc))
            threading.Thread(target=run, daemon=True, name=f"provider-{route['id']}").start()

        def cancel_remaining(except_id: str | None, cancellation_class: str) -> None:
            for other_id, (_, other, other_cancel) in list(active.items()):
                if other_id == except_id:
                    continue
                other_cancel.set()
                other.cancellation_class = cancellation_class
                other.completed_at = other.completed_at or time.monotonic()
                self.audit("llm_attempt_finished", asdict(other))
                active.pop(other_id, None)

        launch(remaining.pop(0), False)
        first_started = time.monotonic()
        while active and time.monotonic() < request.absolute_deadline:
            no_active_first_token = all(trace.first_token_at is None for _, trace, _ in active.values())
            if (locked_id is None and no_active_first_token and len(active) < 2 and remaining
                    and time.monotonic() - first_started >= self.hedge_seconds):
                first_family = next(iter(active.values()))[0]["model_family"]
                index = next((i for i, route in enumerate(remaining) if route["model_family"] != first_family), 0)
                launch(remaining.pop(index), True)
            try:
                kind, attempt_id, value = events.get(timeout=min(0.05, max(0.01, request.absolute_deadline - time.monotonic())))
            except queue.Empty:
                continue
            route, trace, cancel = active.get(attempt_id, (None, None, None))
            if trace is None:
                continue
            if kind == "first_token":
                if locked_id == attempt_id:
                    cancel_remaining(attempt_id, "hedge_cancelled_maybe_billed")
                continue
            trace.completed_at = time.monotonic()
            active.pop(attempt_id, None)
            if kind == "error":
                trace.terminal_error = _error_category(value, trace.first_token_at is not None)
                trace.winner = locked_id == attempt_id
                self.audit("llm_attempt_finished", asdict(trace))
                if locked_id == attempt_id:
                    return trace, TransportResult(""), None, published
                if len(active) < 2 and remaining:
                    launch(remaining.pop(0), False)
                continue
            result: TransportResult = value
            if result.model:
                trace.model = result.model
            trace.protocol_success = True; trace.response_id = result.response_id; trace.request_id = result.request_id
            trace.usage = dict(result.usage); trace.actual_cost = result.actual_cost
            estimate = self._cost_details(route, request, int(result.usage.get("prompt_tokens") or result.usage.get("input_tokens") or 1), int(result.usage.get("completion_tokens") or result.usage.get("output_tokens") or request.output_token_allowance))
            trace.estimated_cost = estimate["amount"]
            trace.currency = result.actual_currency or estimate["currency"]
            trace.multiplier = estimate["multiplier"]; trace.base_price_calibrated = estimate["calibrated"]
            trace.cost_basis = "provider_actual" if result.actual_cost is not None else estimate["basis"]
            trace.effective_unit_price = estimate["unit_price"]
            parsed: Any = None
            try:
                if "\ufffd" in result.text:
                    raise ProviderError("Provider output contains replacement characters", category="invalid_encoding")
                assert_safe(result.text, boundary="Provider output")
            except ProviderError as exc:
                trace.terminal_error = _error_category(exc, trace.first_token_at is not None)
            except ValueError:
                trace.terminal_error = "secret_rejection"
            else:
                try:
                    parsed = _parse_structured_json(result.text) if request.schema is not None else result.text
                except json.JSONDecodeError:
                    trace.terminal_error = "invalid_output_json"
                    parsed = None
            if trace.terminal_error is None:
                try:
                    assert parsed is not None
                    schema_result = _validate_schema(parsed, request.schema) if request.schema is not None else {"passed": True, "problems": []}
                    verifier = request.verifier(parsed) if request.verifier else {"passed": True, "problems": []}
                    trace.verifier = {"schema": schema_result, "business": verifier, "name": request.verifier_name}
                    trace.product_success = bool(schema_result["passed"] and verifier.get("passed"))
                    if not trace.product_success:
                        trace.terminal_error = "schema_rejection" if not schema_result["passed"] else "business_rejection"
                except Exception:
                    trace.terminal_error = "business_rejection"; parsed = None
            trace.winner = bool(trace.product_success or (request.visible_stream and locked_id == attempt_id))
            self.audit("llm_attempt_finished", asdict(trace))
            if request.visible_stream and locked_id == attempt_id:
                if not trace.product_success:
                    return trace, result, parsed, published
                cancel_remaining(attempt_id, "hedge_cancelled_maybe_billed")
                return trace, result, parsed, False
            if trace.product_success:
                cancel_remaining(attempt_id, "hedge_cancelled_maybe_billed")
                return trace, result, parsed, False
            if len(active) < 2 and remaining:
                launch(remaining.pop(0), False)
        cancel_remaining(None, "deadline_cancelled_maybe_billed")
        return None

    def _duel(self, request: StageRequest) -> ProviderOutcome:
        missing = [family for family in ("openai", "anthropic") if not self._eligible(request, family=family)]
        if missing:
            return ProviderOutcome(request.invocation_id, request.packet_sha256, None, None, None, None, None, [], [], duel={"status": "missing_required_family", "missing_families": missing})
        legs: dict[str, ProviderOutcome] = {}
        def run_leg(family: str) -> tuple[str, ProviderOutcome]:
            # M1 has already selected exactly one required family for this leg.
            # Reset the normal-stage global filter so it cannot filter that
            # selected family a second time (for example, Claude-only mode must
            # never suppress the compulsory OpenAI blind leg).
            scoped = {
                **self.provider,
                "routing": {**self.provider.get("routing", {}), "family_mode": "auto"},
                "routes": self._eligible(request, family=family),
            }
            race_request = StageRequest(**{**request.__dict__, "mode": "race", "required_capabilities": ("duel",)})
            return family, ProviderBroker(scoped, self.transport, hedge_seconds=self.hedge_seconds,
                                           probe_seconds=self.probe_seconds, history_score=self.history_score,
                                           audit=self.audit)._invoke_core(race_request)
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="provider-duel") as executor:
            for family, outcome in executor.map(run_leg, ("openai", "anthropic")):
                legs[family] = outcome
        passing = [item for item in legs.values() if item.winner_route]
        attempts = [attempt for leg in legs.values() for attempt in leg.attempts]
        probes = [probe for leg in legs.values() for probe in leg.probes]
        if not passing:
            return ProviderOutcome(request.invocation_id, request.packet_sha256, None, None, None, None, None,
                                   attempts, probes, duel={"status": "both_failed"})
        if len(passing) == 1:
            failed = [family for family, leg in legs.items() if not leg.winner_route]
            return ProviderOutcome(
                request.invocation_id, request.packet_sha256, None, None, None, None, None,
                attempts, probes,
                duel={"status": "required_family_failed", "failed_families": failed},
                arbitration={"status": "failed", "failure": "required_m1_family_failed"},
            )
        consistent = _materially_consistent(passing[0].result, passing[1].result)
        if consistent:
            chosen = min(passing, key=lambda item: next(
                a.estimated_cost if a.estimated_cost is not None else math.inf
                for a in item.attempts if a.winner
            ))
            chosen.attempts = attempts; chosen.probes = probes
            chosen.duel = {"status": "consistent", "cross_confirmation_route": next(item.winner_route for item in passing if item is not chosen)}
            return chosen
        candidate_routes = {item.winner_route for item in passing if item.winner_route}
        arbitration_request = StageRequest(
            stage=request.stage, packet=request.packet,
            packet_sha256=request.packet_sha256, effort=request.effort,
            schema=request.schema, mode="arbitration", required_capabilities=("arbitration",),
            absolute_deadline=request.absolute_deadline, route_timeout_seconds=request.route_timeout_seconds,
            output_token_allowance=request.output_token_allowance, verifier_name=request.verifier_name,
            verifier=request.verifier, h0_forbidden=request.h0_forbidden,
            candidate_judgments=tuple(item.result for item in passing),
        )
        arbitration_routes = self._eligible(arbitration_request, exclude=candidate_routes)
        if arbitration_routes:
            participant_models = {item.model for item in passing if item.model}
            arbitration_routes.sort(key=lambda route: (route["model"] in participant_models, route["id"]))
            scoped = {**self.provider, "routes": arbitration_routes}
            arbitration = ProviderBroker(
                scoped, self.transport, hedge_seconds=self.hedge_seconds, probe_seconds=self.probe_seconds,
                history_score=self.history_score, audit=self.audit,
            ).invoke(arbitration_request)
            arbitration.attempts = attempts + arbitration.attempts
            arbitration.probes = probes + arbitration.probes
            arbitration.duel = {"status": "material_conflict", "candidates": [item.result for item in passing]}
            if arbitration.winner_route:
                child_invocation_id = arbitration.invocation_id
                arbitration.invocation_id = request.invocation_id
                arbitration.arbitration = {
                    "status": "resolved", "route": arbitration.winner_route,
                    "invocation_id": child_invocation_id,
                }
                return arbitration
            attempts = arbitration.attempts
            probes = arbitration.probes
        return ProviderOutcome(request.invocation_id, request.packet_sha256, None, None, None, None, None,
                               attempts, probes,
                               duel={"status": "material_conflict", "candidates": [item.result for item in passing]},
                               arbitration={"status": "failed", "failure": "model_judgment_conflict"})

    @staticmethod
    def _payload(route: dict[str, Any], request: StageRequest) -> dict[str, Any]:
        input_body: dict[str, Any] = request.packet
        if request.candidate_judgments:
            input_body = {
                "frozen_evidence": request.packet,
                "candidate_judgments": list(request.candidate_judgments),
            }
        text = json.dumps(input_body, ensure_ascii=False, sort_keys=True)
        if route.get("transport") == "responses":
            payload: dict[str, Any] = {
                "model": route["model"], "input": text,
                "reasoning": {"effort": request.effort}, "max_output_tokens": request.output_token_allowance,
                "stream": True,
            }
            if request.schema is not None:
                payload["text"] = {"format": {"type": "json_schema", "name": "stage_result", "strict": True, "schema": request.schema}}
        else:
            messages: list[dict[str, str]] = []
            if request.schema is not None:
                messages.append({
                    "role": "system",
                    "content": json.dumps({
                        "instruction": "Return only one JSON object matching output_schema. Do not add prose or Markdown fences.",
                        "output_schema": request.schema,
                    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                })
            messages.append({"role": "user", "content": text})
            payload = {
                "model": route["model"], "messages": messages,
                "reasoning_effort": request.effort, "max_completion_tokens": request.output_token_allowance,
                "stream": True,
            }
            payload["stream_options"] = {"include_usage": True}
            if request.schema is not None:
                payload["response_format"] = {"type": "json_schema", "json_schema": {"name": "stage_result", "strict": True, "schema": request.schema}}
        return payload

    def _pre_cost(self, route: dict[str, Any], request: StageRequest) -> float:
        tokens = max(1, len(json.dumps(self._input_body(request), ensure_ascii=False)) // 4)
        details = self._cost_details(route, request, tokens, request.output_token_allowance)
        return float(details["amount"] if details["amount"] is not None else details["sort_cost"])

    def _final_cost(self, route: dict[str, Any], usage: dict[str, Any], request: StageRequest) -> float:
        details = self._cost_details(route, request, int(usage.get("prompt_tokens") or usage.get("input_tokens") or 1), int(usage.get("completion_tokens") or usage.get("output_tokens") or request.output_token_allowance))
        return float(details["amount"] or 0.0)

    @staticmethod
    def _input_body(request: StageRequest) -> dict[str, Any]:
        if not request.candidate_judgments:
            return request.packet
        return {
            "frozen_evidence": request.packet,
            "candidate_judgments": list(request.candidate_judgments),
        }

    def _cost_details(self, route: dict[str, Any], request: StageRequest, input_tokens: int, output_tokens: int) -> dict[str, Any]:
        cost = route["cost"]
        if cost["mode"] == "token":
            amount = float(cost.get("fixed_request", 0)) + input_tokens * float(cost.get("input_per_million", 0)) / 1_000_000 + output_tokens * float(cost.get("output_per_million", 0)) / 1_000_000
            return {"amount": amount, "sort_cost": amount, "currency": cost.get("currency"), "multiplier": None, "calibrated": True, "basis": "route_token_price", "unit_price": {"input_per_million": float(cost.get("input_per_million", 0)), "output_per_million": float(cost.get("output_per_million", 0))}}
        endpoint = self._endpoints[route["endpoint"]]
        multiplier = float(endpoint.get("weight", cost.get("weight", self.provider.get("routing", {}).get("default_weight", .3))))
        entry = catalog_entry(self.provider, route["model_family"], str(route.get("catalog_model") or route["model"]))
        catalog = entry.get("price") if entry else self.provider.get("routing", {}).get("price_catalog", {}).get(route["model_family"], {}).get(route["model"])
        if not isinstance(catalog, dict):
            return {"amount": None, "sort_cost": multiplier, "currency": None, "multiplier": multiplier, "calibrated": False, "basis": "relative_multiplier_only", "unit_price": {}}
        input_price = float(catalog.get("input_per_million", 0)) * multiplier
        output_price = float(catalog.get("output_per_million", 0)) * multiplier
        cached_price = float(catalog.get("cached_input_per_million", catalog.get("input_per_million", 0))) * multiplier
        cached_tokens = int(request.packet.get("cached_input_tokens", 0)) if isinstance(request.packet, dict) else 0
        amount = (max(0, input_tokens - cached_tokens) * input_price + cached_tokens * cached_price + output_tokens * output_price) / 1_000_000
        return {"amount": amount, "sort_cost": amount, "currency": "USD", "multiplier": multiplier, "calibrated": True, "basis": "official_base_x_multiplier", "unit_price": {"input_per_million": input_price, "cached_input_per_million": cached_price, "output_per_million": output_price}}


def canonical_packet_hash(packet: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _directory_contains(models: set[str], requested: str) -> bool:
    normalized = {item.lower().replace("_", "-") for item in models}
    return requested.lower().replace("_", "-") in normalized


def _normalized_probe(raw: dict[str, Any]) -> tuple[str, set[str]]:
    models = {str(item) for item in raw.get("models", []) if str(item)}
    status = str(raw.get("status") or "definitive_failure")
    if status == "available" and models:
        return "available", models
    if status in {"unknown_http_404", "unknown_http_405"}:
        return status, set()
    if status in {"empty", "available"}:
        return "empty", set()
    return "definitive_failure", set()


def _parse_structured_json(text: str) -> Any:
    """Accept exact JSON or one complete JSON fence; reject surrounding prose."""
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as direct_error:
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.IGNORECASE | re.DOTALL)
        if fenced is None:
            raise direct_error
        return json.loads(fenced.group(1))


def _stage_slot(stage: str) -> str:
    if "research" in stage: return "research"
    if stage in {"m1_judgment", "m2", "reflection", "workflow_feedback", "judgment"}: return "judgment"
    return "fast"


def _contains_h0(value: Any) -> bool:
    forbidden = {"h0", "human_messages", "chat_human", "human_message", "h0_propositions", "h0_actions"}
    if isinstance(value, dict):
        return any(str(key).lower() in forbidden or _contains_h0(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_h0(item) for item in value)
    return False


def _error_category(exc: Exception, after_token: bool) -> str:
    if isinstance(exc, ValueError) and "secret guard blocked" in str(exc):
        return "secret_rejection"
    if isinstance(exc, ProviderError):
        mapping = {"provider_rate_limited": "rate_limited", "provider_auth": "authentication",
                   "provider_permission": "permission", "provider_network": "disconnect",
                   "model_not_found": "model_not_found", "invalid_response": "invalid_protocol_json",
                   "invalid_encoding": "replacement_characters",
                   "incomplete_response": "partial_stream", "provider_cancelled": "cancelled"}
        if exc.category == "provider_timeout":
            return "timeout_after_first_token" if after_token else "timeout_before_first_token"
        return mapping.get(exc.category, exc.category)
    return "interrupted_stream" if after_token else "disconnect"


def _validate_schema(value: Any, schema: dict[str, Any] | None, path: str = "$") -> dict[str, Any]:
    if schema is None: return {"passed": True, "problems": []}
    problems: list[str] = []
    expected = schema.get("type")
    valid = {"object": isinstance(value, dict), "array": isinstance(value, list), "string": isinstance(value, str),
             "boolean": isinstance(value, bool), "integer": isinstance(value, int) and not isinstance(value, bool),
             "number": isinstance(value, (int, float)) and not isinstance(value, bool), "null": value is None}
    expected_types = expected if isinstance(expected, list) else [expected]
    recognized_types = [item for item in expected_types if item in valid]
    if recognized_types and not any(valid[item] for item in recognized_types):
        label = " or ".join(recognized_types)
        return {"passed": False, "problems": [f"{path}: expected {label}"]}
    if isinstance(value, dict):
        required = schema.get("required", [])
        problems.extend(f"{path}.{key}: required" for key in required if key not in value)
        if schema.get("additionalProperties") is False:
            problems.extend(f"{path}.{key}: additional property" for key in value if key not in schema.get("properties", {}))
        for key, child in schema.get("properties", {}).items():
            if key in value:
                problems.extend(_validate_schema(value[key], child, f"{path}.{key}")["problems"])
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            problems.extend(_validate_schema(item, schema["items"], f"{path}[{index}]")["problems"])
    if "enum" in schema and value not in schema["enum"]: problems.append(f"{path}: not in enum")
    if "const" in schema and value != schema["const"]: problems.append(f"{path}: const mismatch")
    return {"passed": not problems, "problems": problems}


def _materially_consistent(left: Any, right: Any) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict): return left == right
    judgment_keys = {"judgment_qualified", "qualified", "direction", "triggers", "invalidations", "principal_risk"}
    left_snapshot = left.get("snapshot") if isinstance(left.get("snapshot"), dict) else left
    right_snapshot = right.get("snapshot") if isinstance(right.get("snapshot"), dict) else right
    if not (judgment_keys.intersection(left_snapshot) or judgment_keys.intersection(right_snapshot)):
        return json.dumps(left, ensure_ascii=False, sort_keys=True, separators=(",", ":")) == json.dumps(
            right, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    def projection(value: dict[str, Any]) -> tuple[Any, ...]:
        snapshot = value.get("snapshot") if isinstance(value.get("snapshot"), dict) else value
        return (bool(value.get("judgment_qualified", snapshot.get("qualified"))), snapshot.get("direction"),
                tuple(snapshot.get("triggers") or ()), tuple(snapshot.get("invalidations") or ()), snapshot.get("principal_risk"))
    return projection(left) == projection(right)


def _assert_tool_free_payload(payload: dict[str, Any]) -> None:
    forbidden = {"tools", "tool_choice", "parallel_tool_calls", "web_search", "browser", "computer"}
    if forbidden.intersection(payload):
        raise ValueError("Provider transport forbids native tools")
    for message in payload.get("messages", []):
        if isinstance(message, dict) and (message.get("role") == "tool" or message.get("tool_calls")):
            raise ValueError("Provider transport forbids tool messages")


def _provider_cost(response: dict[str, Any]) -> tuple[float | None, str | None]:
    raw = response.get("cost") or response.get("provider_cost") or response.get("_provider_cost")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw), str(response.get("currency") or "") or None
    if isinstance(raw, dict):
        amount = raw.get("amount")
        if isinstance(amount, (int, float)) and not isinstance(amount, bool):
            return float(amount), str(raw.get("currency") or "") or None
    return None, None
