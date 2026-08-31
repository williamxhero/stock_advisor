#!/usr/bin/env python3
"""Headless entry point for the local AI Trading Companion runtime."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import sqlite3
import time
import sys
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .backup import BackupManager
from .cognition import ReplyMarkdownStream, UnifiedCognition
from .adaptive_memory import AdaptiveMemoryResearch, MemoryResearchError
from .broker_client import BrokerError, BrokerRequest, BrokerResponse, ProviderBrokerClient, canonical_packet_hash
from .config import load_settings, remove_legacy_provider_settings, save_research_settings
from .engine import CompanionEngine, iso
from .evidence_gate import EvidenceGate, EvidenceInsufficient
from .effort_policy import CognitiveEffortPolicy
from .exchange import LocalExchange
from .governance import EvolutionGovernance, RouterGovernance, StrategyPolicyExecutor, classify_regime
from .gateway import RuntimeGateway, serve as serve_gateway
from .learning import JudgmentLifecycle, WorkflowEvolution
from .message_presentation import explicit_format_requested
from .memory_commands import handle_memory_command
from .memory_port import HttpMemoryAdapter, MemoryUnavailable
from .memory_health import MemoryCapabilityPolicy
from .memory_evidence import MemoryEvidenceRegistrar
from .migration import LegacyMigrator, LegacySources
from .models import TASK_POLICIES
from .observatory import EvaluationObservatory, EvaluationRequest, ExperimentRequest, ForecastRequest
from .packet_builder import RuntimePacketBuilder
from .paths import RuntimePaths
from .portfolio import PortfolioService
from .preview import approve_bundle, build_bundle, find_source_cycle, launch_preview, seal_bundle, write_bundle
from .projection import LearningProjectionRenderer
from .scheduler import SHANGHAI, conversation_auto_submit_at, ensure_registered_policy, run_registry_schedule
from .schedule_registry import ScheduleRegistry, _target_for_day
from .router import CognitiveRouter
from .runtime_strategy_policy import RuntimeStrategyControls, RuntimeStrategyPolicy
from .web_access_gateway import WebAccessGatewayClient
from .local_research import (
    BrokerResearchPlanner, DeterministicMarketBackend, LocalResearchChain,
    ReadOnlyResearchExecutor, WebAccessGatewayBackend,
)
from .store import CompanionStore
from .trading_calendar import XshgTradingCalendar


PATHS = RuntimePaths.discover()
INSTALL_ROOT = PATHS.install_root
WORKSPACE = PATHS.workspace
RUNTIME = Path(os.environ.get("AI_TRADING_COMPANION_RUNTIME", str(PATHS.runtime)))
DB = Path(os.environ.get("AI_TRADING_COMPANION_DATABASE", str(PATHS.database)))
SCHEMAS = PATHS.contracts


@dataclass(frozen=True)
class VerifiedStageResult:
    output: dict[str, Any]
    broker: BrokerResponse
    attempt_id: str
    packet_hash: str
    verifier: dict[str, Any]

    def __iter__(self):
        yield self.output
        yield self.broker


M1_MAX_JUDGMENT_ATTEMPTS = 4
M1_MIN_RETRY_WINDOW_SECONDS = 30


def _m1_should_retry(exc: Exception, *, attempt_number: int, remaining_seconds: int) -> bool:
    if attempt_number >= M1_MAX_JUDGMENT_ATTEMPTS or remaining_seconds < M1_MIN_RETRY_WINDOW_SECONDS:
        return False
    if isinstance(exc, EvidenceInsufficient):
        return False
    if isinstance(exc, TimeoutError):
        return True
    if not isinstance(exc, BrokerError):
        return False
    return exc.category not in {
        "broker_effort_unsupported",
        "broker_authentication",
        "broker_forbidden",
        "broker_secret_rejected",
    }


def _m1_retry_feedback(exc: Exception) -> dict[str, Any] | None:
    if not isinstance(exc, BrokerError) or not isinstance(exc.verifier, dict):
        return None
    schema = exc.verifier.get("schema") if isinstance(exc.verifier.get("schema"), dict) else {}
    business = exc.verifier.get("business") if isinstance(exc.verifier.get("business"), dict) else {}

    def problems(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item)[:500] for item in value[:20]]

    return {
        "category": exc.category,
        "schema_problems": problems(schema.get("problems")),
        "business_problems": problems(business.get("problems")),
    }


def resolve_stage_controls(
    store: CompanionStore,
    stage: str,
    *,
    timeout: int,
    search: bool,
    runtime_strategy_shadow_cell: str | None = None,
) -> RuntimeStrategyControls:
    """Resolve the controls that must be part of a stage's frozen input."""
    runtime_strategy = RuntimeStrategyPolicy(store)
    if runtime_strategy_shadow_cell:
        return runtime_strategy.shadow_controls(
            runtime_strategy_shadow_cell, stage, timeout_seconds=timeout, search=search,
        )
    return runtime_strategy.controls(stage, timeout_seconds=timeout, search=search)


def finalize_stage_packet(packet: dict[str, Any], controls: RuntimeStrategyControls) -> dict[str, Any]:
    """Bind runtime controls before deriving the sole hash for a stage invocation.

    The returned packet is a new immutable candidate.  Its ``sha256`` covers
    exactly the object later persisted, checkpointed, and sent to Broker.
    """
    final_packet = {
        key: value for key, value in packet.items()
        if key not in {"sha256", "runtime_strategy_controls", "allowed_research_backends"}
    }
    final_packet["runtime_strategy_controls"] = {
        "timeout_seconds": controls.timeout_seconds,
        "max_operations": controls.max_operations,
        "enabled_backends": list(controls.enabled_backends),
        "revisions": list(controls.revisions),
    }
    final_packet["allowed_research_backends"] = list(controls.enabled_backends)
    final_packet["sha256"] = canonical_packet_hash(final_packet)
    return final_packet


def _evidence_read_cutoff(packet: dict[str, Any], contract: dict[str, Any]) -> str | None:
    """Freeze external reads to the evidence contract, never the later worker start."""
    return str(contract.get("as_of") or packet.get("as_of") or "") or None


def _m1_research_as_of(evidence: dict[str, Any], frozen_as_of: str | None) -> str:
    """M1 judges the exact frozen M0 bundle and must preserve its timestamp."""
    return str(frozen_as_of or evidence.get("as_of") or iso(datetime.now(timezone.utc)))


def exchange_root() -> Path:
    return PATHS.exchange


def publish_observatory_evaluation(store: CompanionStore, cycle_id: str) -> None:
    cycle = store.get_cycle(cycle_id)
    if cycle["task_key"] != "daily.execution.0945":
        return
    try:
        EvaluationObservatory(store, exchange=LocalExchange(exchange_root()), schedule_path=PATHS.resources / "schedules" / "tasks.json").evaluate(
            EvaluationRequest(
                cycle_id=cycle_id, observed_at=str(cycle.get("updated_at") or iso(datetime.now(timezone.utc))),
                request_id=f"m0-terminal:{cycle_id}",
            ),
        )
        publish_observatory_forecast(store, cycle_id, trigger="m0_terminal")
    except Exception as exc:
        store.queue_event(cycle_id, "observatory.failed", {
            "cycle_id": cycle_id, "reason": str(exc)[:1000],
        })


def publish_observatory_forecast(store: CompanionStore, cycle_id: str, *, trigger: str) -> None:
    """Publish a new forecast only when the target's factual context changed.

    Stage and dependency owners call this seam after they append their fact.
    The Observatory derives a stable context fingerprint and suppresses a
    duplicate trigger without mutating a historical forecast.
    """
    cycle = store.get_cycle(cycle_id)
    if cycle["task_key"] != "daily.execution.0945":
        return
    try:
        EvaluationObservatory(store, exchange=LocalExchange(exchange_root()), schedule_path=PATHS.resources / "schedules" / "tasks.json").forecast(
            ForecastRequest(
                task_key=cycle["task_key"], cycle_id=cycle_id,
                observed_at=str(cycle.get("updated_at") or iso(datetime.now(timezone.utc))),
                trigger=trigger,
            ),
        )
    except Exception as exc:
        store.queue_event(cycle_id, "observatory.forecast_failed", {
            "cycle_id": cycle_id, "trigger": trigger, "reason": str(exc)[:1000],
        })


def handle_effort_capability_fault(
    store: CompanionStore, *, decision_id: str, cycle_id: str, fault_id: str, promoted: bool,
) -> None:
    """Reject an unsupported shadow candidate or roll back a promoted effort through governance."""
    try:
        cell_key = RouterGovernance(store).record_effort_capability_fault(
            decision_id, cycle_id, fault_id,
        )
        source_kind = "post_promotion_monitoring" if promoted else "shadow"
        assessment = EvaluationObservatory(
            store, exchange=LocalExchange(exchange_root()),
        ).assess_experiment(ExperimentRequest(
            cell_key, source_kind=source_kind,
            request_id=f"effort-capability:{source_kind}:{fault_id}",
        ))
        receipt = None
        if promoted:
            decision = EvolutionGovernance(store).decide(
                assessment.snapshot_id, "approve", approver="automatic-governance",
            )
            receipt = StrategyPolicyExecutor(store).apply(decision.decision_id)
        store.queue_event(cycle_id, "effort.capability_fault", {
            "cell_key": cell_key,
            "assessment_snapshot_id": assessment.snapshot_id,
            "decision": assessment.decision,
            "receipt_id": receipt.receipt_id if receipt else None,
        })
    except Exception as recovery_error:
        store.queue_event(cycle_id, "effort.capability_fault_recovery_failed", {
            "decision_id": decision_id, "fault_id": fault_id,
            "reason": str(recovery_error)[:1000],
        })


def runtime() -> tuple[CompanionEngine, CompanionStore, LocalExchange, PortfolioService]:
    PATHS.ensure()
    # The old direct-provider block can contain API keys.  Erase it before any
    # runtime work and never copy it into a backup or another configuration.
    remove_legacy_provider_settings(PATHS.home)
    _backup_before_schedule_migration()
    store = CompanionStore(DB)
    store.initialize()
    registry = _schedule_registry(store)
    registry.seed(json.loads((PATHS.resources / "schedules" / "tasks.json").read_text(encoding="utf-8")))
    registry.validate_or_repair()
    store.risk_doctrine()
    memory = HttpMemoryAdapter(os.environ.get("MEMORYHUB_URL", "http://yosef-server:8820"))
    capability = MemoryCapabilityPolicy.evaluate(memory.health())
    if not capability.memory_tasks_available:
        raise MemoryUnavailable("MemoryHub ledger or retrieval index is unavailable; ordinary chat cannot use a local-memory fallback")
    engine = CompanionEngine(
        store,
        memory=memory,
        memory_space_id=os.environ.get("MEMORYHUB_SPACE_ID", "ai-trading-companion"),
    )
    engine.recover_interrupted_streams()
    JudgmentLifecycle(store).backfill()
    exchange = LocalExchange(exchange_root())
    exchange.ensure()
    portfolio = PortfolioService(WORKSPACE, store)
    portfolio.reconcile()
    return engine, store, exchange, portfolio


def _backup_before_schedule_migration() -> None:
    """Schema upgrades never become the first destructive recovery boundary."""
    if not DB.exists():
        return
    source = sqlite3.connect(DB)
    try:
        version = source.execute("PRAGMA user_version").fetchone()[0]
        if version >= 13:
            return
        target = RUNTIME / "backups" / "migrations" / f"before-provider-broker-v13-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.sqlite3"
        target.parent.mkdir(parents=True, exist_ok=True)
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()


def _schedule_registry(store: CompanionStore) -> ScheduleRegistry:
    return ScheduleRegistry(store, XshgTradingCalendar(PATHS.home / "config" / "xshg-calendar-overrides.json"))


def flush(store: CompanionStore, exchange: LocalExchange) -> int:
    count = 0
    for event in store.pending_events():
        payload = {
            "contract": "companion-client-event/v1",
            "event_id": event["event_id"],
            "cycle_id": event["cycle_id"],
            "type": event["event_type"],
            "created_at": event["created_at"],
            "payload": json.loads(event["payload_json"]),
        }
        exchange.send("to-client", event["event_id"], payload)
        store.mark_event_delivered(event["event_id"])
        count += 1
    for event in store.pending_portfolio_events():
        payload = {
            "contract": "portfolio-client-event/v1",
            "event_id": event["event_id"],
            "type": event["event_type"],
            "created_at": event["created_at"],
            "payload": json.loads(event["payload_json"]),
        }
        exchange.send("to-client", event["event_id"], payload)
        store.mark_portfolio_event_delivered(event["event_id"])
        count += 1
    for event in store.pending_schedule_events():
        payload = {
            "contract": "schedule-client-event/v1", "event_id": event["event_id"],
            "type": event["event_type"], "created_at": event["created_at"],
            "payload": json.loads(event["payload_json"]),
        }
        exchange.send("to-client", event["event_id"], payload)
        store.mark_schedule_event_delivered(event["event_id"])
        count += 1
    return count


def render_learning(store: CompanionStore) -> None:
    LearningProjectionRenderer(WORKSPACE, store).render()


def broker_client(settings: Any | None = None) -> ProviderBrokerClient:
    """Create the only LLM transport from the local Broker base-URL setting."""
    active = settings if settings is not None else load_settings(PATHS.home)
    return ProviderBrokerClient(str(active.broker["url"]))


def _gateway_snapshot(engine: CompanionEngine, store: CompanionStore, portfolio: PortfolioService, kind: str, query: dict[str, str]) -> dict[str, Any]:
    if kind == "history":
        return store.history_page(before=query.get("before"), limit=int(query.get("limit", "31")), search=query.get("search", ""))
    if kind == "today":
        scheduled_date = query.get("date") or datetime.now(SHANGHAI).date().isoformat()
        registry = _schedule_registry(store)
        return {"contract": "companion-today/v1", "scheduled_date": scheduled_date,
                "is_trading_day": registry.calendar.is_trading_day(date.fromisoformat(scheduled_date)),
                "projections": [engine._projection(cycle) for cycle in store.latest_cycles_for_date(scheduled_date)]}
    if kind == "cycle":
        cycle_id = query.get("cycle_id")
        if not cycle_id:
            raise ValueError("cycle_id is required")
        return {"contract": "companion-cycle-projection/v1", "projection": engine._projection(store.get_cycle(cycle_id))}
    if kind == "portfolio":
        return {"contract": "portfolio-snapshot/v1", "snapshot": portfolio.snapshot()}
    if kind == "schedules":
        return {"contract": "schedule-list/v1", "schedules": _schedule_registry(store).list()}
    raise ValueError("unknown snapshot kind")


def run_gateway(execute: bool = False) -> None:
    """Serve desktop requests without granting the desktop database access."""
    engine, store, exchange, portfolio = runtime()
    def command(payload: dict[str, Any]) -> dict[str, Any]:
        contract = payload.get("contract")
        if contract == "schedule-user-command/v1":
            result = _schedule_command(store, payload)
        elif contract == "portfolio-user-command/v1":
            result = _portfolio_command(store, portfolio, payload)
        else:
            result = engine.command(payload)
        flush(store, exchange)
        return result
    def snapshot(kind: str, request: Any) -> dict[str, Any]:
        return _gateway_snapshot(engine, store, portfolio, kind, dict(request.query))
    def tick() -> None:
        run_schedules(engine, store, datetime.now(timezone.utc), execute, exchange, portfolio)
        for cycle in store.claim_scheduled_workers(limit=2):
            run_scheduled_cycle(engine, store, exchange, portfolio, cycle["cycle_id"], execute)
        for projection in engine.run_due():
            cycle_id = projection["cycle"]["cycle_id"]
            run_m1(engine, store, portfolio, cycle_id, execute)
            process_h0_cognition(engine, store, portfolio, cycle_id, execute)
        consume(engine, store, exchange, portfolio, execute)
        run_background(engine, store, execute)
        flush(store, exchange)
    import asyncio
    asyncio.run(serve_gateway(RuntimeGateway(PATHS.home, store, command, snapshot, tick)))


def _call_stage(
    store: CompanionStore,
    cycle: dict[str, Any],
    stage: str,
    packet: dict[str, Any],
    schema_name: str,
    *,
    search: bool,
    timeout: int,
    retry_model_slot: str | None = None,
    runtime_strategy_shadow_cell: str | None = None,
    frozen_controls: RuntimeStrategyControls | None = None,
) -> VerifiedStageResult:
    settings = load_settings(PATHS.home)
    runtime_strategy = RuntimeStrategyPolicy(store)
    controls = frozen_controls or resolve_stage_controls(
        store, stage, timeout=timeout, search=search,
        runtime_strategy_shadow_cell=runtime_strategy_shadow_cell,
    )
    timeout = controls.timeout_seconds
    search = bool(search and controls.max_operations > 0 and controls.enabled_backends)
    packet = finalize_stage_packet(packet, controls)
    router = CognitiveRouter(effort_policy=CognitiveEffortPolicy.load(store))
    preliminary = router.plan(stage, packet, timeout, search)
    cell = store.router_policy_cell(
        preliminary.profile.cell_key, preliminary.baseline.as_json(),
        preliminary.candidate.as_json() if preliminary.candidate else None,
    )
    plan = router.plan(stage, packet, timeout, search, str(cell["mode"]))
    decision = plan.selected
    decision_id = store.record_route_decision(
        cycle["cycle_id"], stage, plan.profile.cell_key, plan.mode, plan.profile.as_json(),
        plan.baseline.as_json(), plan.candidate.as_json() if plan.candidate else None, decision.as_json(),
    )
    attempt = store.begin_attempt(
        cycle["cycle_id"], stage, iso(datetime.now(timezone.utc)), packet.get("sha256"),
        model=None, reasoning_effort=decision.reasoning_effort,
        search_enabled=search, timeout_seconds=min(timeout, decision.timeout_seconds),
        routing_reason="Provider Broker intellect/effort routing", route_decision_id=decision_id,
        is_shadow=runtime_strategy_shadow_cell is not None,
        runner_fingerprint="provider-broker/v1",
        effort_policy_version=decision.effort_policy_version,
        effort_input_fingerprint=decision.effort_input_fingerprint,
        input_packet=packet,
    )
    attempt_finished = False
    tool_trace: list[dict[str, Any]] = []
    planner: BrokerResearchPlanner | None = None
    try:
        broker = broker_client(settings)
        deadline = time.monotonic() + max(1, min(timeout, decision.timeout_seconds))
        evidence_verifier: dict[str, Any] | None = None
        outcome: BrokerResponse | None = None
        request_packet = {key: value for key, value in packet.items() if key != "sha256"}
        request_hash = str(packet.get("sha256") or canonical_packet_hash(request_packet))

        if search:
            contract = packet.get("evidence_contract")
            if not isinstance(contract, dict):
                contract = {
                    "version": 3, "as_of": packet.get("as_of"),
                    "requirements": packet.get("evidence_requirements") or [],
                }
            planner = BrokerResearchPlanner(
                broker, deadline=lambda: deadline, intellect=decision.intellect, effort=decision.reasoning_effort,
            )
            web = WebAccessGatewayBackend(
                WebAccessGatewayClient(settings.research), as_of=_evidence_read_cutoff(packet, contract),
            )
            market_facts = packet.get("deterministic_market_facts")
            backends = {"gateway": web} if "gateway" in controls.enabled_backends else {}
            if "market" in controls.enabled_backends and isinstance(market_facts, dict) and market_facts:
                backends["market"] = DeterministicMarketBackend(market_facts)
            executor = ReadOnlyResearchExecutor(backends, max_operations=controls.max_operations)
            research = LocalResearchChain(planner, executor, max_repairs=2).run(
                packet, contract, attempt_id=attempt["attempt_id"],
            )
            tool_trace = [*research.observations, *_broker_call_trace(planner.outcomes)]
            evidence_verifier = research.verifier
            if not research.qualified:
                raise EvidenceInsufficient(research.verifier)
            if schema_name.startswith("companion-evidence-result-"):
                data = research.evidence
                outcome = planner.outcomes[-1] if planner.outcomes else None
                verifier = research.verifier
            else:
                request_packet = {
                    **request_packet,
                    "frozen_evidence": research.evidence,
                    "evidence_bundle_sha256": research.bundle_sha256,
                }
                request_hash = canonical_packet_hash(request_packet)

        if not search or not schema_name.startswith("companion-evidence-result-"):
            schema = json.loads((SCHEMAS / schema_name).read_text(encoding="utf-8"))
            request = BrokerRequest(
                stage=stage, packet=request_packet, packet_sha256=request_hash,
                intellect=decision.intellect, effort=decision.reasoning_effort, schema=schema,
                visible_stream=False, absolute_deadline=deadline,
                output_token_limit=6_000 if stage == "m1_judgment" else 4_000 if stage == "m2" else 2_000,
                verifier_name=f"cognitive-router/{stage}",
                verifier=lambda output: router.verify(stage, packet, output),
                h0_forbidden=stage == "m1_judgment",
            )
            outcome = broker.invoke(request)
            if not isinstance(outcome.result, dict):
                raise BrokerError(f"Broker produced no qualified result for {stage}", category="broker_output_invalid")
            data = outcome.result
            verifier = router.verify(stage, packet, data)
            if evidence_verifier is not None:
                verifier["evidence_gate"] = evidence_verifier
                verifier["passed"] = bool(verifier.get("passed")) and bool(evidence_verifier.get("passed"))

        if outcome is None:
            raise BrokerError("Broker produced no auditable outcome", category="broker_protocol")
        status = "succeeded" if verifier.get("passed") else "rejected"
        output_text = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        store.finish_attempt(
            attempt["attempt_id"],
            status,
            output_sha256=hashlib.sha256(output_text.encode("utf-8")).hexdigest(),
            output=data,
            usage=outcome.usage, verifier=verifier,
            broker_metadata=outcome.audit_metadata(),
            tool_trace=[*tool_trace, outcome.audit_metadata()], actual_model=outcome.actual_model,
        )
        attempt_finished = True
        if not verifier.get("passed"):
            raise EvidenceInsufficient(verifier)
        if runtime_strategy_shadow_cell is None:
            runtime_strategy.queue_shadows(
                cycle["cycle_id"], stage, packet, schema_name, attempt["attempt_id"],
            )
        if runtime_strategy_shadow_cell is None and not retry_model_slot and plan.mode == "shadow" and plan.candidate and verifier["passed"]:
            store.queue_router_shadow(
                decision_id, cycle["cycle_id"], stage, packet, schema_name, plan.candidate.as_json(),
                priority=1 if plan.profile.major else 0,
            )
        return VerifiedStageResult(data, outcome, attempt["attempt_id"], str(packet.get("sha256") or ""), verifier)
    except Exception as exc:
        if not attempt_finished:
            if planner is not None:
                known_requests = {
                    str(item.get("request_id")) for item in tool_trace
                    if isinstance(item, dict) and item.get("kind") == "broker_call"
                }
                tool_trace.extend(
                    item for item in _broker_call_trace(planner.outcomes)
                    if str(item.get("request_id")) not in known_requests
                )
            status = "timed_out" if isinstance(exc, TimeoutError) else "failed"
            store.finish_attempt(
                attempt["attempt_id"], status, error=str(exc),
                verifier=getattr(exc, "verifier", None),
                broker_metadata=exc.metadata or {"request_id": exc.request_id, "attempts": exc.attempts} if isinstance(exc, BrokerError) else None,
                actual_model=exc.metadata.get("actual_model") if isinstance(exc, BrokerError) else None,
                tool_trace=getattr(exc, "tool_trace", None) or tool_trace,
            )
        if (
            isinstance(exc, BrokerError) and exc.category == "broker_effort_unsupported"
            and plan.mode == "promoted"
        ):
            handle_effort_capability_fault(
                store, decision_id=decision_id, cycle_id=cycle["cycle_id"],
                fault_id=f"production:{attempt['attempt_id']}", promoted=True,
            )
        raise


def _broker_call_trace(outcomes: list[BrokerResponse]) -> list[dict[str, Any]]:
    """Bind each derived Broker request to the owning business-stage attempt."""
    return [outcome.audit_metadata() for outcome in outcomes]


def _deadline_timeout(cycle: dict[str, Any], requested: int) -> int:
    deadline = cycle.get("m1_publish_deadline")
    if not deadline:
        return requested
    remaining = int((datetime.fromisoformat(deadline.replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds())
    if remaining <= 0:
        raise TimeoutError("M1 publish deadline has passed")
    return max(1, min(requested, remaining))


def run_router_shadow(store: CompanionStore, job: dict[str, Any], execute: bool) -> None:
    """Run an isolated candidate on the exact frozen packet; it never publishes UI output."""
    cycle = store.get_cycle(job["cycle_id"])
    packet = json.loads(job["packet_json"])
    candidate = json.loads(job["candidate_json"])
    if not execute:
        store.finish_router_shadow(job["job_id"], output={"fixture": True})
        return
    attempt = store.begin_attempt(
        cycle["cycle_id"], job["stage"], iso(datetime.now(timezone.utc)), job["packet_sha256"],
        model=None, reasoning_effort=candidate["reasoning_effort"],
        search_enabled=bool(candidate["search"]), timeout_seconds=int(candidate["timeout_seconds"]),
        routing_reason=candidate["reason"], route_decision_id=job["decision_id"], is_shadow=True,
        runner_fingerprint="provider-broker/v1",
        effort_policy_version=candidate.get("effort_policy_version"),
        effort_input_fingerprint=candidate.get("effort_input_fingerprint"),
    )
    try:
        request_packet = {key: value for key, value in packet.items() if key != "sha256"}
        router = CognitiveRouter()
        outcome = broker_client().invoke(BrokerRequest(
            stage=job["stage"], packet=request_packet, packet_sha256=canonical_packet_hash(request_packet),
            intellect=str(candidate["intellect"]), effort=str(candidate["reasoning_effort"]),
            schema=json.loads((SCHEMAS / job["schema_name"]).read_text(encoding="utf-8")),
            visible_stream=False,
            absolute_deadline=time.monotonic() + int(candidate["timeout_seconds"]),
            verifier_name=f"router-shadow/{job['stage']}",
            verifier=lambda output: router.verify(job["stage"], packet, output),
            h0_forbidden=job["stage"] == "m1_judgment",
        ))
        if not isinstance(outcome.result, dict):
            raise BrokerError("Broker produced no qualified shadow result", category="broker_output_invalid")
        data = outcome.result
        verifier = router.verify(job["stage"], packet, data)
        output_text = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        store.finish_attempt(
            attempt["attempt_id"], "succeeded", output_sha256=hashlib.sha256(output_text.encode("utf-8")).hexdigest(),
            usage=outcome.usage, verifier=verifier, broker_metadata=outcome.audit_metadata(),
            tool_trace=[outcome.audit_metadata()], actual_model=outcome.actual_model,
        )
        store.finish_router_shadow(job["job_id"], output=data, verifier=verifier)
    except Exception as exc:
        status = "timed_out" if isinstance(exc, TimeoutError) else "failed"
        store.finish_attempt(
            attempt["attempt_id"], status, error=str(exc),
            broker_metadata=exc.metadata if isinstance(exc, BrokerError) else None,
        )
        store.finish_router_shadow(job["job_id"], error=str(exc))
        if isinstance(exc, BrokerError) and exc.category == "broker_effort_unsupported":
            handle_effort_capability_fault(
                store, decision_id=job["decision_id"], cycle_id=cycle["cycle_id"],
                fault_id=f"shadow:{job['job_id']}", promoted=False,
            )


def run_runtime_strategy_shadow(store: CompanionStore, job: dict[str, Any], execute: bool) -> None:
    """Evaluate one reversible runtime control on a frozen packet without publishing it."""
    policy = RuntimeStrategyPolicy(store)
    if not execute:
        policy.finish_shadow(job["job_id"])
        return
    cycle = store.get_cycle(job["cycle_id"])
    packet = json.loads(job["packet_json"])
    frozen_controls = packet.get("runtime_strategy_controls") if isinstance(packet.get("runtime_strategy_controls"), dict) else {}
    requested_timeout = int(frozen_controls.get("timeout_seconds") or 300)
    search = bool(frozen_controls.get("enabled_backends"))
    try:
        candidate = _call_stage(
            store, cycle, job["stage"], packet, job["schema_name"],
            search=search, timeout=requested_timeout,
            runtime_strategy_shadow_cell=job["cell_key"],
        )
        attempts = {row["attempt_id"]: row for row in store.attempts(cycle["cycle_id"])}
        from .governance import _attempt_dimensions
        baseline_score = _attempt_dimensions(attempts.get(job["baseline_attempt_id"]))
        candidate_score = _attempt_dimensions(attempts.get(candidate.attempt_id))
        with store.connection() as connection:
            regime = connection.execute(
                "SELECT regime FROM market_regime_snapshot WHERE cycle_id=?", (cycle["cycle_id"],),
            ).fetchone()
        receipt = policy.record_evaluation(
            job["cell_key"], cycle["cycle_id"], f"stage:{job['stage']}",
            regime["regime"] if regime else "unknown", baseline_score, candidate_score,
        )
        policy.finish_shadow(job["job_id"], candidate_attempt_id=candidate.attempt_id)
        store.queue_event(cycle["cycle_id"], "runtime_strategy.shadow_completed", {
            "cell_key": job["cell_key"], "stage": job["stage"], "job_id": job["job_id"],
            "candidate_attempt_id": candidate.attempt_id,
            "application_receipt_id": receipt.receipt_id if receipt else None,
        })
    except Exception as exc:
        policy.finish_shadow(job["job_id"], error=str(exc))
        store.queue_event(cycle["cycle_id"], "runtime_strategy.shadow_failed", {
            "cell_key": job["cell_key"], "stage": job["stage"], "job_id": job["job_id"],
            "reason": str(exc)[:1000],
        })


def _latest_json_artifact(store: CompanionStore, cycle_id: str, kind: str) -> dict[str, Any] | None:
    artifact = store.latest_artifact(cycle_id, kind)
    if not artifact:
        return None
    try:
        return json.loads(artifact["body_markdown"])
    except json.JSONDecodeError:
        return None


def _fixture_attempt(store: CompanionStore, cycle_id: str, stage: str, packet_hash: str, output: dict[str, Any]) -> str:
    attempt = store.begin_attempt(cycle_id, stage, iso(datetime.now(timezone.utc)), packet_hash, runner_fingerprint="fixture-v1")
    store.finish_attempt(attempt["attempt_id"], "succeeded", output=output, verifier={"passed": True, "problems": [], "fixture": True})
    return attempt["attempt_id"]


def _reuse_m0_evidence_attempt(
    store: CompanionStore, cycle: dict[str, Any], packet: dict[str, Any], evidence: dict[str, Any],
) -> str:
    """Create an auditable M1 checkpoint without searching or calling a model.

    M1 must judge the exact M0 evidence bundle. The synthetic stage attempt
    preserves the existing publication/checkpoint contract while copying the
    original acquisition trace so EvidenceGate can re-verify provenance.
    """
    source_attempt = next((
        item for item in reversed(store.attempts(cycle["cycle_id"]))
        if item.get("stage") == "m0_research" and item.get("status") == "succeeded"
        and json.loads(item.get("output_json") or "null") == evidence
    ), None)
    if source_attempt is None:
        raise EvidenceInsufficient({
            "passed": False, "problems": ["frozen_m0_evidence_attempt_missing"],
            "missing_requirements": ["current_market_state", "material_events_and_counterevidence"],
        })
    tool_trace = json.loads(source_attempt.get("tool_trace_json") or "[]")
    contract = packet.get("evidence_contract") or packet.get("evidence_requirements") or []
    evidence_verifier = EvidenceGate().evaluate(
        evidence, contract, tool_trace, str(packet.get("as_of") or ""),
        attempt_id=str(source_attempt["attempt_id"]),
    )
    business_verifier = CognitiveRouter().verify("m1_research", packet, evidence)
    verifier = {
        **business_verifier,
        "passed": bool(business_verifier.get("passed")) and bool(evidence_verifier.get("passed")),
        "evidence_gate": evidence_verifier,
        "frozen_evidence_reuse": True,
        "source_attempt_id": str(source_attempt["attempt_id"]),
    }
    if not verifier["passed"]:
        verifier["problems"] = [
            *list(business_verifier.get("problems") or []),
            *list(evidence_verifier.get("problems") or []),
        ]
        raise EvidenceInsufficient(verifier)
    attempt = store.begin_attempt(
        cycle["cycle_id"], "m1_research", iso(datetime.now(timezone.utc)), packet.get("sha256"),
        search_enabled=False, routing_reason="Reuse frozen M0 evidence bundle",
        runner_fingerprint="runtime/frozen-evidence-reuse-v1", input_packet=packet,
    )
    output_text = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    store.finish_attempt(
        attempt["attempt_id"], "succeeded",
        output_sha256=hashlib.sha256(output_text.encode("utf-8")).hexdigest(),
        output=evidence, verifier=verifier, tool_trace=tool_trace,
    )
    return str(attempt["attempt_id"])


def run_research(
    engine: CompanionEngine,
    store: CompanionStore,
    cycle: dict[str, Any],
    execute: bool,
    on_progress: Any = None,
    frozen_as_of: str | None = None,
) -> dict[str, Any]:
    cycle = engine.research_started(cycle["cycle_id"], as_of=frozen_as_of)
    publish_observatory_forecast(store, cycle["cycle_id"], trigger="stage:m0_started")
    if on_progress:
        on_progress()
    builder = RuntimePacketBuilder(PATHS.resources, WORKSPACE, store)
    if not execute:
        evidence = {"as_of": cycle["as_of"], "spoken_summary": "Fixture 模式：等待真实公开信息搜索。", "sources": [], "critical_gaps": []}
        store.append_artifact(cycle["cycle_id"], "evidence", "model", json.dumps(evidence, ensure_ascii=False), cycle["as_of"])
        evidence_hash = "fixture-m0-research"
        compose_hash = "fixture-m0-compose"
        result = engine.research_ready(
            cycle["cycle_id"], "Fixture 模式：这里会显示自然、无方向的 M0 客观观察。",
            evidence_attempt_id=_fixture_attempt(store, cycle["cycle_id"], "m0_research", evidence_hash, evidence),
            compose_attempt_id=_fixture_attempt(store, cycle["cycle_id"], "m0_compose", compose_hash, {"m0_markdown": "Fixture 模式：这里会显示自然、无方向的 M0 客观观察。"}),
            evidence_packet_hash=evidence_hash, packet_hash=compose_hash,
        )
        publish_observatory_evaluation(store, cycle["cycle_id"])
        return result

    policy = TASK_POLICIES[cycle["task_key"]]
    research_timeout = int(policy.research_timeout.total_seconds())
    research_controls = resolve_stage_controls(
        store, "m0_research", timeout=research_timeout, search=True,
    )
    memory_research = _formal_adaptive_research(engine, store, cycle, "m0_research", cycle["as_of"], research_timeout)
    public_packet = finalize_stage_packet(builder.build(cycle, "m0_research", context=memory_research), research_controls)
    compose_timeout = int(policy.m1_timeout.total_seconds())
    compose_controls = resolve_stage_controls(
        store, "m0_compose", timeout=compose_timeout, search=False,
    )
    for number in range(1, 3):
        try:
            checkpoint = store.stage_checkpoint(cycle["cycle_id"], "m0_research", public_packet["sha256"])
            if checkpoint:
                evidence = checkpoint["output"]
                evidence_attempt_id = checkpoint["attempt_id"]
            else:
                evidence_stage = _call_stage(
                    store, cycle, "m0_research", public_packet, "companion-evidence-result-v3.schema.json",
                    search=True, timeout=research_timeout, frozen_controls=research_controls,
                    retry_model_slot="fast" if number > 1 else None,
                )
                evidence = evidence_stage.output
                evidence_attempt_id = evidence_stage.attempt_id
                store.save_stage_checkpoint(cycle["cycle_id"], "m0_research", public_packet["sha256"], evidence_attempt_id, evidence)
                store.record_evidence(cycle, "m0_research", evidence)
                store.append_artifact(
                    cycle["cycle_id"], "evidence", "model", json.dumps(evidence, ensure_ascii=False),
                    evidence.get("as_of") or cycle["as_of"], {"public_only": True, "attempt_id": evidence_attempt_id},
                )
            local_packet = finalize_stage_packet(
                builder.build(cycle, "m0_compose", evidence=evidence), compose_controls,
            )
            compose_checkpoint = store.stage_checkpoint(cycle["cycle_id"], "m0_compose", local_packet["sha256"])
            if compose_checkpoint:
                m0_output, compose_attempt_id = compose_checkpoint["output"], compose_checkpoint["attempt_id"]
            else:
                compose_stage = _call_stage(
                    store, cycle, "m0_compose", local_packet, "companion-m0-result-v1.schema.json",
                    search=False, timeout=compose_timeout, frozen_controls=compose_controls,
                )
                m0_output, compose_attempt_id = compose_stage.output, compose_stage.attempt_id
                store.save_stage_checkpoint(cycle["cycle_id"], "m0_compose", local_packet["sha256"], compose_attempt_id, m0_output)
            ready = engine.research_ready(
                cycle["cycle_id"], m0_output["m0_markdown"],
                evidence_attempt_id=evidence_attempt_id, compose_attempt_id=compose_attempt_id,
                evidence_packet_hash=public_packet["sha256"], packet_hash=local_packet["sha256"],
                evidence_as_of=evidence.get("as_of"),
            )
            publish_observatory_evaluation(store, cycle["cycle_id"])
            return ready
        except Exception as exc:
            if isinstance(exc, EvidenceInsufficient):
                engine.research_failed(cycle["cycle_id"], str(exc), details=exc.verifier)
                publish_observatory_evaluation(store, cycle["cycle_id"])
                if on_progress:
                    on_progress()
                raise
            if number < 2:
                engine.research_retrying(cycle["cycle_id"], str(exc), number + 1)
                publish_observatory_forecast(store, cycle["cycle_id"], trigger="stage:m0_retrying")
                if on_progress:
                    on_progress()
                time.sleep(2)
                continue
            engine.research_failed(cycle["cycle_id"], str(exc))
            publish_observatory_evaluation(store, cycle["cycle_id"])
            if on_progress:
                on_progress()
            raise
    raise RuntimeError("M0 attempts exhausted")


def run_m1(
    engine: CompanionEngine,
    store: CompanionStore,
    portfolio: PortfolioService,
    cycle_id: str,
    execute: bool,
    frozen_as_of: str | None = None,
) -> dict[str, Any]:
    cycle = store.get_cycle(cycle_id)
    if cycle["state"] == "waiting_for_repair":
        cycle = engine.resume_m1_after_repair(cycle_id)
    if cycle["state"] not in {"researching_m1", "m1_retry_wait"}:
        raise RuntimeError(f"cycle is not waiting for M1: {cycle['state']}")
    builder = RuntimePacketBuilder(PATHS.resources, WORKSPACE, store)
    if not execute:
        engine.m1_judgment_started(cycle_id)
        research_hash, judgment_hash = "fixture-m1-research", "fixture-m1-judgment"
        result = engine.m1_ready(
            cycle_id, "Fixture 模式：这里会显示与 H0 隔离的独立 M1 判断。",
            research_attempt_id=_fixture_attempt(store, cycle_id, "m1_research", research_hash, {}),
            judgment_attempt_id=_fixture_attempt(store, cycle_id, "m1_judgment", judgment_hash, {"m1_markdown": "Fixture 模式：这里会显示与 H0 隔离的独立 M1 判断。"}),
            research_packet_hash=research_hash, judgment_packet_hash=judgment_hash,
        )
        return result

    policy = TASK_POLICIES[cycle["task_key"]]
    prior_evidence = _latest_json_artifact(store, cycle_id, "evidence") or {}
    if not prior_evidence:
        raise EvidenceInsufficient({
            "passed": False, "problems": ["frozen_m0_evidence_missing"],
            "missing_requirements": ["current_market_state", "material_events_and_counterevidence"],
        })
    research_as_of = _m1_research_as_of(prior_evidence, frozen_as_of)
    research_timeout = int(policy.research_timeout.total_seconds())
    research_controls = resolve_stage_controls(
        store, "m1_research", timeout=research_timeout, search=True,
    )
    memory_research = _formal_adaptive_research(engine, store, cycle, "m1_research", research_as_of, research_timeout)
    public_packet = finalize_stage_packet(
        builder.build(cycle, "m1_research", evidence=prior_evidence, as_of=research_as_of, context=memory_research), research_controls,
    )
    checkpoint = store.stage_checkpoint(cycle_id, "m1_research", public_packet["sha256"])
    if checkpoint:
        evidence = checkpoint["output"]
        evidence_attempt_id = checkpoint["attempt_id"]
    else:
        evidence = prior_evidence
        evidence_attempt_id = _reuse_m0_evidence_attempt(store, cycle, public_packet, evidence)
        store.save_stage_checkpoint(
            cycle_id, "m1_research", public_packet["sha256"], evidence_attempt_id, evidence,
        )
        store.append_artifact(
            cycle_id, "m1_evidence", "runtime", json.dumps(evidence, ensure_ascii=False),
            str(evidence.get("as_of") or research_as_of),
            {"public_only": True, "attempt_id": evidence_attempt_id, "reused_from": "m0_research"},
        )
    verification_feedback: dict[str, Any] | None = None
    for number in range(1, M1_MAX_JUDGMENT_ATTEMPTS + 1):
        try:
            cycle = engine.m1_judgment_started(cycle_id)
            judgment_timeout = _deadline_timeout(cycle, int(policy.m1_timeout.total_seconds()))
            judgment_controls = resolve_stage_controls(
                store, "m1_judgment", timeout=judgment_timeout, search=False,
            )
            judgment_packet = builder.build(
                cycle, "m1_judgment", evidence=evidence,
                as_of=str(evidence.get("as_of") or iso(datetime.now(timezone.utc))),
            )
            if verification_feedback is not None:
                judgment_packet["verification_repair"] = {
                    **verification_feedback,
                    "attempt_number": number,
                    "instruction": (
                        "The previous candidate was not published. Correct every listed schema and business-verifier "
                        "problem while independently recomputing the judgment from the same frozen evidence."
                    ),
                }
            local_packet = finalize_stage_packet(
                judgment_packet, judgment_controls,
            )
            judgment_checkpoint = store.stage_checkpoint(cycle_id, "m1_judgment", local_packet["sha256"])
            if judgment_checkpoint:
                judgment, judgment_attempt_id = judgment_checkpoint["output"], judgment_checkpoint["attempt_id"]
            else:
                judgment_stage = _call_stage(
                    store, cycle, "m1_judgment", local_packet, "companion-m1-result-v1.schema.json",
                    search=False, timeout=judgment_timeout, frozen_controls=judgment_controls,
                )
                judgment, judgment_attempt_id = judgment_stage.output, judgment_stage.attempt_id
                store.save_stage_checkpoint(cycle_id, "m1_judgment", local_packet["sha256"], judgment_attempt_id, judgment)
            return engine.m1_ready(
                cycle_id, judgment["m1_markdown"], as_of=evidence.get("as_of"),
                research_attempt_id=evidence_attempt_id, judgment_attempt_id=judgment_attempt_id,
                research_packet_hash=public_packet["sha256"], judgment_packet_hash=local_packet["sha256"],
                snapshot=judgment.get("snapshot"), qualified=bool(judgment.get("judgment_qualified")),
            )
        except Exception as exc:
            try:
                remaining = _deadline_timeout(store.get_cycle(cycle_id), 60)
            except TimeoutError:
                remaining = 0
            retryable = _m1_should_retry(exc, attempt_number=number, remaining_seconds=remaining)
            details = getattr(exc, "verifier", None)
            engine.m1_failed(
                cycle_id, str(exc), retryable=retryable,
                details=details if isinstance(details, dict) else None,
            )
            if not retryable:
                raise
            verification_feedback = _m1_retry_feedback(exc)
            cycle = store.get_cycle(cycle_id)
            time.sleep(2)
    raise RuntimeError("M1 attempts exhausted")


def run_m2(engine: CompanionEngine, store: CompanionStore, cycle_id: str, execute: bool) -> dict[str, Any]:
    cycle = store.get_cycle(cycle_id)
    if cycle["state"] not in {"synthesizing_m2", "m2_deferred"}:
        return cycle
    if not execute:
        packet_hash = "fixture-m2"
        return engine.m2_ready(
            cycle_id, "Fixture 模式：这里会显示 M0、H0和独立 M1的伴生综合 M2。",
            attempt_id=_fixture_attempt(store, cycle_id, "m2", packet_hash, {"m2_markdown": "Fixture 模式：这里会显示 M0、H0和独立 M1的伴生综合 M2。"}), packet_hash=packet_hash,
        )
    frozen_as_of = str(cycle.get("m1_completed_at") or cycle["as_of"])
    timeout = int(TASK_POLICIES[cycle["task_key"]].m2_timeout.total_seconds())
    controls = resolve_stage_controls(store, "m2", timeout=timeout, search=False)
    packet = finalize_stage_packet(
        RuntimePacketBuilder(PATHS.resources, WORKSPACE, store).build(cycle, "m2", as_of=frozen_as_of), controls,
    )
    stage_result = _call_stage(
        store, cycle, "m2", packet, "companion-m2-result-v1.schema.json",
        search=False, timeout=timeout, frozen_controls=controls,
    )
    store.save_stage_checkpoint(cycle_id, "m2", packet["sha256"], stage_result.attempt_id, stage_result.output)
    return engine.m2_ready(
        cycle_id, stage_result.output["m2_markdown"], snapshot=stage_result.output.get("snapshot"), as_of=frozen_as_of,
        attempt_id=stage_result.attempt_id, packet_hash=packet["sha256"],
    )


def run_preview_worker(source_cycle_id: str, preview_id: str, known_at: str, bundle_path: Path) -> dict[str, Any]:
    engine, store, _exchange, portfolio = runtime()
    source = store.get_cycle(source_cycle_id)
    cycle = store.create_preview_cycle(source_cycle_id, known_at)
    failure = None
    try:
        frozen_as_of = str(source["as_of"])
        cycle = run_research(engine, store, cycle, True, frozen_as_of=frozen_as_of)
        preview_deadline = iso(datetime.now(timezone.utc) + timedelta(minutes=45))
        source_h0 = store.latest_artifact(source_cycle_id, "h0")
        if source_h0:
            metadata = json.loads(source_h0.get("metadata_json") or "{}")
            metadata.update({"preview_frozen_from": source_h0["artifact_id"], "source_cycle_id": source_cycle_id})
            h0 = store.append_artifact(
                cycle["cycle_id"], "h0", "human", source_h0["body_markdown"], source_h0["as_of"], metadata,
                occurred_at=source_h0.get("occurred_at"), known_at=source_h0.get("known_at"),
            )
            cycle = store.transition(
                cycle["cycle_id"], "researching_m1", h0_locked_at=known_at,
                h0_artifact_id=h0["artifact_id"], has_h0=1, m1_started_at=known_at,
                m1_publish_deadline=preview_deadline,
            )
        else:
            cycle = store.transition(
                cycle["cycle_id"], "researching_m1", h0_locked_at=known_at, has_h0=0,
                m1_started_at=known_at, m1_publish_deadline=preview_deadline,
            )
        cycle = run_m1(engine, store, portfolio, cycle["cycle_id"], True, frozen_as_of=frozen_as_of)
        if cycle["state"] in {"synthesizing_m2", "m2_deferred"}:
            cycle = run_m2(engine, store, cycle["cycle_id"], True)
    except Exception as exc:
        failure = {
            "category": getattr(exc, "category", "runtime_error"),
            "message": str(exc),
            "verifier": getattr(exc, "verifier", None),
        }
    bundle = build_bundle(store, cycle["cycle_id"], source_cycle_id, preview_id, known_at)
    if failure:
        bundle["preview_status"] = "failed"
        bundle["failure"] = failure
        seal_bundle(bundle)
    write_bundle(bundle, bundle_path)
    return bundle


def run_reflection(
    engine: CompanionEngine,
    store: CompanionStore,
    cycle_id: str,
    checkpoint_id: str,
    execute: bool,
) -> dict[str, Any] | None:
    cycle = store.get_cycle(cycle_id)
    if any(
        artifact["kind"] == "reflection" and checkpoint_id in artifact.get("metadata_json", "")
        for artifact in store.artifacts(cycle_id)
    ):
        return None
    if not execute:
        data = {"reflection_markdown": "Fixture 模式：结果已记录，等待真实复盘。", "memory_tags": ["fixture"], "workflow_proposal": None}
    else:
        packet = RuntimePacketBuilder(PATHS.resources, WORKSPACE, store).build(
            cycle, "reflection", context={"checkpoint_id": checkpoint_id},
            as_of=iso(datetime.now(timezone.utc)),
        )
        data, _ = _call_stage(
            store, cycle, "reflection", packet, "companion-reflection-result-v1.schema.json",
            search=False, timeout=int(TASK_POLICIES[cycle["task_key"]].m1_timeout.total_seconds()),
        )
    artifact = engine.publish_proactive_message(
        cycle_id, "reflection", data["reflection_markdown"],
        meaningful=not str(data["reflection_markdown"]).startswith("Fixture "),
        metadata={"checkpoint_id": checkpoint_id, "memory_tags": data.get("memory_tags") or []},
    )
    proposal = None
    if data.get("workflow_proposal") and artifact:
        proposal = WorkflowEvolution(store).propose(
            cycle_id, data["workflow_proposal"], source_artifact_id=artifact["artifact_id"],
        )
    return artifact


def run_outcome(
    engine: CompanionEngine,
    store: CompanionStore,
    checkpoint: dict[str, Any],
    execute: bool,
) -> dict[str, Any]:
    cycle = store.get_cycle(checkpoint["cycle_id"])
    if not execute:
        result = {
            "as_of": iso(datetime.now(timezone.utc)),
            "checkpoint_ready": False, "target_session_date": None,
            "next_check_at": iso(datetime.now(timezone.utc) + timedelta(hours=1)),
            "verification_status": "unverified",
            "summary": f"Fixture 模式：{checkpoint['horizon']} 结果等待真实行情验证。",
            "observations": [], "data_gaps": ["fixture mode"],
        }
    else:
        packet = RuntimePacketBuilder(PATHS.resources, WORKSPACE, store).build(
            cycle,
            "outcome_research",
            context={
                "checkpoint_id": checkpoint["checkpoint_id"],
                "horizon": checkpoint["horizon"],
                "judgment_as_of": checkpoint["judgment_as_of"],
                "judgment_snapshot": json.loads(checkpoint["snapshot_json"]),
                "judgment_text": checkpoint["judgment_text"],
            },
            as_of=iso(datetime.now(timezone.utc)),
        )
        result, _ = _call_stage(
            store, cycle, "outcome_research", packet, "companion-outcome-result-v1.schema.json",
            search=True, timeout=300,
        )
    if not result.get("checkpoint_ready"):
        next_check = str(result.get("next_check_at") or iso(datetime.now(timezone.utc) + timedelta(hours=1)))
        store.defer_outcome(checkpoint["checkpoint_id"], next_check, result["summary"])
        return result
    presented = engine.present_for_publication(str(result["summary"]), str(result.get("as_of") or cycle["as_of"]), "outcome")
    result = {**result, "summary": presented.markdown, "presentation": presented.metadata()["presentation"]}
    artifact = JudgmentLifecycle(store).record_outcome(checkpoint, result)
    regime_metrics = result.get("market_regime") if isinstance(result.get("market_regime"), dict) else {}
    regime = classify_regime(regime_metrics)
    store.save_market_regime(cycle["cycle_id"], str(result.get("as_of") or cycle["as_of"]), regime, regime_metrics, str(regime_metrics.get("data_quality") or "unknown"))
    governance = RouterGovernance(store)
    governance.evaluate_outcome(
        cycle["cycle_id"], checkpoint["horizon"], result.get("observations") or [],
        json.loads(checkpoint["snapshot_json"]), artifact["artifact_id"],
    )
    with store.connection() as connection:
        cells = [row[0] for row in connection.execute(
            "SELECT DISTINCT cell_key FROM router_evaluation WHERE cycle_id=? AND horizon=?", (cycle["cycle_id"], checkpoint["horizon"])
        )]
    for cell_key in cells:
        assessment = EvaluationObservatory(
            store, exchange=LocalExchange(exchange_root()),
        ).assess_experiment(ExperimentRequest(
            cell_key, request_id=f"experiment:{cell_key}:{checkpoint['checkpoint_id']}",
        ))
        if assessment.decision in {"recommend_promotion", "recommend_rollback"}:
            decision = EvolutionGovernance(store).decide(
                assessment.snapshot_id, "approve", approver="automatic-governance",
            )
            receipt = StrategyPolicyExecutor(store).apply(decision.decision_id)
        else:
            receipt = None
        if receipt and receipt.state == "applied":
            engine.chat_ready(
                cycle["cycle_id"],
                "这段时间我把同一批判断用不同思考深度反复对照了。更深入的那条路径在不同市场状态下都更稳，"
                "我会把它作为这类判断的优先习惯；以后如果它不再经得起结果检验，我会主动退回并告诉你。",
            )
        elif receipt and receipt.state == "rollback_applied":
            engine.chat_ready(
                cycle["cycle_id"],
                "晋升后的思考深度触发了保护维度故障，我已经按已批准的回滚决定恢复上一版本。",
            )
    engine.emit(cycle, "outcome.ready", {
        "cycle": cycle, "text": result["summary"], "horizon": checkpoint["horizon"],
        "presentation": presented.metadata()["presentation"],
        "verification_status": result["verification_status"],
        "source_artifact_id": artifact["artifact_id"],
    })
    run_reflection(engine, store, cycle["cycle_id"], checkpoint["checkpoint_id"], execute)
    return result


def run_chat_research(
    engine: CompanionEngine,
    store: CompanionStore,
    job: dict[str, Any],
    execute: bool,
) -> dict[str, Any]:
    cycle = store.get_cycle(job["cycle_id"])
    source = next(
        (artifact for artifact in store.artifacts(cycle["cycle_id"]) if artifact["artifact_id"] == job["source_artifact_id"]),
        None,
    )
    if not source:
        raise RuntimeError("chat research source artifact is missing")
    public_scope = json.loads(job["public_scope_json"])
    if not execute:
        evidence = {
            "as_of": iso(datetime.now(timezone.utc)), "spoken_summary": "Fixture 模式：公开补查尚未执行。",
            "sources": [], "critical_gaps": ["fixture mode"],
        }
        reply = "Fixture 模式：补查完成后，我会把新增信息继续发在这里。"
        data = None
    else:
        builder = RuntimePacketBuilder(PATHS.resources, WORKSPACE, store)
        research_packet = builder.build(
            cycle, "chat_research", context=public_scope,
            as_of=iso(datetime.now(timezone.utc)),
        )
        evidence, _ = _call_stage(
            store, cycle, "chat_research", research_packet, "companion-evidence-result-v3.schema.json",
            search=True, timeout=300,
        )
        store.record_evidence(cycle, "chat_research", evidence)
        local_packet = builder.build(
            cycle, "chat", evidence=evidence, message_batch=source["body_markdown"],
            context={"fresh_search_completed": True}, as_of=str(evidence.get("as_of") or iso(datetime.now(timezone.utc))),
        )
        data, _ = _call_stage(
            store, cycle, "chat_followup", local_packet, "companion-chat-result-v1.schema.json",
            search=False, timeout=int(TASK_POLICIES[cycle["task_key"]].m1_timeout.total_seconds()),
        )
        reply = data["reply_markdown"]
    # Ordinary conversation can inform later work, but never silently rewrites
    # a published M1/M2.  A formal rerun remains an explicit user action.
    store.finish_research_job(job["job_id"])
    source_metadata = json.loads(source.get("metadata_json") or "{}")
    reply_kind = "premarket_chat" if source["kind"] == "pre_m0_submission" else "ai_chat"
    engine.chat_ready(
        cycle["cycle_id"], reply, reply_to_batch_id=source_metadata.get("batch_id"), kind=reply_kind
    )
    return evidence


def run_pending_workflow_feedback(
    engine: CompanionEngine,
    store: CompanionStore,
    execute: bool,
) -> dict[str, Any] | None:
    keywords = ("挖掘", "搜索", "工作流", "流程", "信息不足", "信息缺失", "覆盖", "改进", "优化", "网络")
    with store.connection() as connection:
        cycles = [dict(row) for row in connection.execute(
            """SELECT * FROM companion_cycle WHERE has_h0=1 AND state IN ('complete','m2_deferred')
               ORDER BY h0_locked_at"""
        )]
    for cycle in cycles:
        h0 = store.latest_artifact(cycle["cycle_id"], "h0")
        if not h0 or not any(keyword in h0["body_markdown"] for keyword in keywords):
            continue
        if any(
            json.loads(artifact.get("metadata_json") or "{}").get("workflow_feedback_source") == h0["artifact_id"]
            for artifact in store.artifacts(cycle["cycle_id"])
        ):
            continue
        if not execute:
            data = {"reflection_markdown": "Fixture 模式：这条工作流反馈已记录。", "memory_tags": ["workflow_feedback"], "workflow_proposal": None}
        else:
            packet = RuntimePacketBuilder(PATHS.resources, WORKSPACE, store).build(
                cycle, "workflow_feedback", context={"source_artifact_id": h0["artifact_id"]},
                as_of=iso(datetime.now(timezone.utc)),
            )
            data, _ = _call_stage(
                store, cycle, "workflow_feedback", packet, "companion-reflection-result-v1.schema.json",
                search=False, timeout=int(TASK_POLICIES[cycle["task_key"]].m1_timeout.total_seconds()),
            )
        artifact = engine.publish_proactive_message(
            cycle["cycle_id"], "ai_chat", data["reflection_markdown"],
            meaningful=not str(data["reflection_markdown"]).startswith("Fixture "),
            metadata={"workflow_feedback_source": h0["artifact_id"], "memory_tags": data.get("memory_tags") or []},
        )
        proposal = None
        if data.get("workflow_proposal") and artifact:
            proposal = WorkflowEvolution(store).propose(
                cycle["cycle_id"], data["workflow_proposal"], source_artifact_id=artifact["artifact_id"],
            )
        return {"cycle_id": cycle["cycle_id"], "artifact_id": artifact["artifact_id"]} if artifact else {"cycle_id": cycle["cycle_id"], "action": "silent"}
    return None


def _foreground_busy(store: CompanionStore) -> bool:
    with store.connection() as connection:
        return bool(connection.execute(
            """SELECT 1 FROM companion_cycle
               WHERE state IN ('queued','researching_m0','h0_locked','researching_m1','judging_m1','m1_retry_wait')
               LIMIT 1"""
        ).fetchone())


def _seconds_until_next_schedule(at: datetime | None = None) -> int:
    current = (at or datetime.now(timezone.utc)).astimezone(SHANGHAI)
    _, store, _, _ = runtime()
    candidates = [datetime.fromisoformat(value.replace("Z", "+00:00")) for row in _schedule_registry(store).list(current) for value in row["next_targets"]]
    future = [candidate for candidate in candidates if candidate > current]
    return max(0, int((min(future) - current).total_seconds())) if future else 24 * 60 * 60


def run_schedules(engine: CompanionEngine, store: CompanionStore, at: datetime, execute: bool, exchange: LocalExchange, portfolio: PortfolioService) -> list[dict[str, Any]]:
    # Materialisation intentionally does not call an LLM.  The service launches
    # bounded workers from the resulting queued cycles, so later tasks are not
    # held behind a long earlier judgment.
    registry = _schedule_registry(store)
    results = run_registry_schedule(engine, store, registry, at)
    conversation = engine.ensure_daily_conversation(at)
    defaults = json.loads((PATHS.resources / "schedules" / "tasks.json").read_text(encoding="utf-8"))
    default_lead = int((defaults.get("conversation") or {}).get("auto_submit_lead_minutes", 20))
    local_at = at.astimezone(SHANGHAI)
    for row in registry.list(local_at, include_inactive=False):
        config = row["config"]
        target = _target_for_day(config, local_at.date())
        if target is None or target.date() != local_at.date():
            continue
        if config["trigger"]["type"] in {"trading_day_fixed", "market_relative"} and not registry.calendar.is_trading_day(local_at.date()):
            continue
        threshold = conversation_auto_submit_at(config, target, default_lead)
        if threshold is None:
            continue
        work_start = target - timedelta(minutes=max(0, int((config.get("trigger") or {}).get("lead_minutes", 0))))
        if local_at > work_start:
            continue
        if local_at < threshold:
            continue
        submitted = engine.auto_submit_conversation(conversation["cycle_id"], row["task_key"], target.isoformat(timespec="seconds"))
        if not submitted:
            continue
        flush(store, exchange)
        reply = run_chat(
            engine, store, portfolio, conversation["cycle_id"], submitted["committed_batch_id"], execute,
            on_progress=lambda: flush(store, exchange),
        )
        results.append({
            "task_key": row["task_key"], "scheduled_for": target.isoformat(timespec="seconds"),
            "action": "conversation_auto_submitted", "conversation_cycle_id": conversation["cycle_id"],
            "cognition_job_id": reply.get("job_id"),
        })
    return results


def run_scheduled_cycle(engine: CompanionEngine, store: CompanionStore, exchange: LocalExchange, portfolio: PortfolioService, cycle_id: str, execute: bool) -> dict[str, Any]:
    cycle = store.get_cycle(cycle_id)
    try:
        if cycle["state"] != "queued":
            return cycle
        snapshot = json.loads(cycle.get("schedule_snapshot_json") or "{}")
        if snapshot:
            ensure_registered_policy(cycle["task_key"], snapshot, datetime.fromisoformat(cycle["scheduled_for"]))
        result = run_research(engine, store, cycle, execute, lambda: flush(store, exchange))
        process_h0_cognition(engine, store, portfolio, cycle_id, execute)
        return result
    finally:
        store.finish_scheduled_worker(cycle_id)


def run_background(
    engine: CompanionEngine,
    store: CompanionStore,
    execute: bool,
) -> dict[str, Any]:
    # This is deterministic maintenance, not a cognition call.  It remains
    # local and does not disturb foreground cycles when the backup already ran.
    daily_backup = BackupManager(store, RUNTIME, load_settings(PATHS.home).backup).ensure_daily()
    if daily_backup and daily_backup["state"] == "external_unavailable":
        # Keep the local snapshot and surface a natural diagnostic through the
        # existing status path; never stop market research for a mirror outage.
        return {"action": "backup_warning", "reason": "外部备份位置不可用，已保留本机备份", "backup_id": daily_backup["backup_id"]}
    if _foreground_busy(store):
        return {"action": "deferred", "reason": "foreground_cycle_has_priority"}
    until_next = _seconds_until_next_schedule()
    research_jobs = store.pending_research_jobs(limit=1)
    if research_jobs and until_next >= 7 * 60:
        job = research_jobs[0]
        try:
            run_chat_research(engine, store, job, execute)
            return {"action": "chat_research", "job_id": job["job_id"]}
        except Exception as exc:
            store.finish_research_job(job["job_id"], error=str(exc), retry=job["attempt_count"] < 2)
            engine.background_failed(job["cycle_id"], "chat_research", str(exc))
            return {"action": "chat_research_failed", "job_id": job["job_id"]}
    with store.connection() as connection:
        candidates = [dict(row) for row in connection.execute(
            """SELECT * FROM companion_cycle WHERE state IN ('synthesizing_m2','m2_deferred')
               ORDER BY CASE state WHEN 'synthesizing_m2' THEN 0 ELSE 1 END,m2_started_at LIMIT 8"""
        )]
    for cycle in candidates:
        if until_next < 17 * 60:
            break
        attempts = [item for item in store.attempts(cycle["cycle_id"]) if item["stage"] == "m2"]
        if len(attempts) >= 3:
            continue
        if attempts and attempts[-1].get("completed_at"):
            completed = datetime.fromisoformat(attempts[-1]["completed_at"].replace("Z", "+00:00"))
            if datetime.now(timezone.utc) < completed + timedelta(minutes=5):
                continue
        try:
            result = run_m2(engine, store, cycle["cycle_id"], execute)
            return {"action": "m2", "cycle_id": cycle["cycle_id"], "state": result["state"]}
        except Exception as exc:
            engine.m2_deferred(cycle["cycle_id"], str(exc))
            return {"action": "m2_deferred", "cycle_id": cycle["cycle_id"]}
    if until_next >= 12 * 60:
        runtime_shadow = RuntimeStrategyPolicy(store).next_shadow()
        if runtime_shadow:
            try:
                run_runtime_strategy_shadow(store, runtime_shadow, execute)
                return {"action": "runtime_strategy_shadow", "job_id": runtime_shadow["job_id"]}
            except Exception as exc:
                return {"action": "runtime_strategy_shadow_failed", "job_id": runtime_shadow["job_id"], "error": str(exc)}
        shadow = store.next_router_shadow()
        if shadow:
            try:
                run_router_shadow(store, shadow, execute)
                return {"action": "router_shadow", "job_id": shadow["job_id"]}
            except Exception as exc:
                # The job itself is marked failed; candidate failures must never disturb formal research.
                return {"action": "router_shadow_failed", "job_id": shadow["job_id"], "error": str(exc)}
    feedback = run_pending_workflow_feedback(engine, store, execute) if until_next >= 7 * 60 else None
    if feedback:
        return {"action": "workflow_feedback", **feedback}
    due = store.due_outcomes(iso(datetime.now(timezone.utc)), limit=1) if until_next >= 12 * 60 else []
    if due:
        checkpoint = due[0]
        try:
            run_outcome(engine, store, checkpoint, execute)
            return {"action": "outcome", "checkpoint_id": checkpoint["checkpoint_id"]}
        except Exception as exc:
            store.fail_outcome(
                checkpoint["checkpoint_id"], str(exc), retry=checkpoint["attempt_count"] < 2,
                retry_at=iso(datetime.now(timezone.utc) + timedelta(minutes=15)),
            )
            engine.background_failed(checkpoint["cycle_id"], "outcome", str(exc))
            return {"action": "outcome_failed", "checkpoint_id": checkpoint["checkpoint_id"]}
    return {"action": "idle"}


def _receipt_safe_stream_prefix(text: str) -> str:
    """Do not expose a success assertion before deterministic action receipts exist."""
    lowered = text.lower()
    markers = (
        "task created", "task successfully created", "analysis created", "analysis request succeeded",
        "portfolio updated", "portfolio has been updated", "proposal registered", "proposal has been registered",
        "正式研判任务已创建", "任务已创建", "持仓已更新", "提案已登记",
    )
    indexes = [index for marker in markers if (index := lowered.find(marker.lower())) >= 0]
    return text[:min(indexes)].rstrip() if indexes else text


def _next_memory_research_action(
    store: CompanionStore, cycle: dict[str, Any], state: dict[str, Any], deadline: float,
) -> dict[str, Any]:
    """Ask the same conversation cognition to choose one MemoryHub operation.

    The runtime does not infer what to retrieve or when enough has been read:
    it merely gives the model the frozen snapshot and executes its declared,
    policy-filtered operation.
    """
    packet = {
        "purpose": (
            "Choose the next private-memory operation needed before replying to this "
            "conversation. You may search by a different entity, time, prior mistake, "
            "correction, counterexample, or related episode when the last result is insufficient. "
            "Choose complete only when the visible memory is enough for an honest reply. "
            "Do not answer the user, create actions, or expose this research process."
        ),
        "research_state": state,
        "task_key": cycle["task_key"],
    }
    timeout_seconds = max(1, int(deadline - time.monotonic()))
    decision = CognitiveRouter(
        effort_policy=CognitiveEffortPolicy.load(store),
    ).route("chat_research", packet, timeout_seconds, True)
    outcome = broker_client().invoke(BrokerRequest(
        stage="chat_research",
        packet=packet,
        packet_sha256=canonical_packet_hash(packet),
        intellect=decision.intellect,
        effort=decision.reasoning_effort,
        schema=json.loads((SCHEMAS / "memory-research-decision-v1.schema.json").read_text(encoding="utf-8")),
        absolute_deadline=deadline,
        verifier_name="memory-research-decision/v1",
        verifier=lambda _output: {"passed": True, "problems": []},
    ))
    if not isinstance(outcome.result, dict):
        raise MemoryResearchError("Broker produced no qualified memory research decision")
    return outcome.result


def _formal_adaptive_research(engine: CompanionEngine, store: CompanionStore, cycle: dict[str, Any], stage: str, as_of: str, timeout: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    result = AdaptiveMemoryResearch(
        engine.memory, engine.memory_space_id,
        lambda state: _next_memory_research_action(store, cycle, state, deadline),
        discover_external=lambda action, snapshot: _discover_chat_external_evidence(engine, action, snapshot),
    ).collect(cycle["cycle_id"], [{"message_id": stage, "body_text": f"Formal {stage} evidence gaps", "known_at": as_of}], deadline=deadline, stage=stage)
    return {"memoryhub_snapshot": result.snapshot, "adaptive_memory": list(result.context), "adaptive_actions": list(result.actions)}


def _formal_memory_snapshot(engine: CompanionEngine, cycle: dict[str, Any], stage: str, as_of: str) -> dict[str, Any]:
    """Freeze the policy-filtered MemoryHub view used by one formal stage."""
    if engine.memory is None:
        raise MemoryResearchError("MemoryHub is required for formal research")
    return engine.memory.begin_snapshot({
        "memory_space_id": engine.memory_space_id, "as_of": as_of,
        "stage": stage, "cycle_id": cycle["cycle_id"],
    })


def _discover_chat_external_evidence(
    engine: CompanionEngine, action: dict[str, Any], snapshot: dict[str, Any],
) -> list[dict[str, Any]]:
    """Acquire WAG material only through a MemoryHub receipt before context use."""
    client = WebAccessGatewayClient(load_settings(PATHS.home).research)
    operation = action["operation"]
    if operation == "web_search":
        response = client.search(str(action["query"]), "news")
    elif operation == "web_read":
        response = client.read(str(action["url"]), not_after=None)
    else:
        reference = dict(action["source_reference"] or {})
        now = iso(datetime.now(timezone.utc))
        receipt = engine.memory.append({
            "memory_space_id": engine.memory_space_id,
            "source_system": "markethub" if operation == "markethub_quote" else "8815",
            "source_event_id": "chat:" + str(snapshot["snapshot_id"]) + ":" + hashlib.sha256(json.dumps(reference, sort_keys=True).encode("utf-8")).hexdigest(),
            "content_hash": "auto", "episode_type": "external_evidence", "source_reference": reference,
            "occurred_at": str(reference.get("date") or now), "known_at": now, "submitted_at": now,
            "authority": "immutable_source_reference", "protocol_version": "memoryhub/v1",
        })
        receipt_snapshot = engine.memory.begin_snapshot({
            "memory_space_id": engine.memory_space_id, "as_of": now, "stage": "chat", "cycle_id": snapshot.get("cycle_id"),
        })
        expanded = engine.memory.expand(str(receipt_snapshot["snapshot_id"]), str(receipt["episode_id"]))
        return [{**expanded, "memory_snapshot_id": receipt_snapshot["snapshot_id"], "memory_receipt": receipt}]
    registered: list[dict[str, Any]] = []
    registrar = MemoryEvidenceRegistrar(engine.memory, clock=lambda: iso(datetime.now(timezone.utc)))
    for index, row in enumerate(response.get("results") or []):
        if not isinstance(row, dict):
            continue
        body = str(row.get("excerpt_text") or "")
        url = str(row.get("url") or "")
        if not body or not url:
            continue
        known = registrar.register_web_snapshot(
            memory_space_id=engine.memory_space_id,
            source_event_id=f"chat:{snapshot['snapshot_id']}:{operation}:{index}:{hashlib.sha256(url.encode('utf-8')).hexdigest()}",
            url=url, title=str(row.get("title") or url), body=body,
            occurred_at=str(row.get("published_at") or row.get("fact_as_of") or iso(datetime.now(timezone.utc))),
        )
        registered.append({
            **known.context, "episode_id": known.episode_id, "authority": "mutable_source_snapshot",
            "occurred_at": row.get("published_at") or row.get("fact_as_of"), "source_reference": {"url": url},
        })
    return registered


def run_unified_cognition(
    engine: CompanionEngine,
    store: CompanionStore,
    portfolio: PortfolioService,
    cycle_id: str,
    source: dict[str, Any],
    messages: list[dict[str, Any]],
    batch_ids: list[str],
    execute: bool,
    *,
    mode: str,
    reply_kind: str = "ai_chat",
    on_progress: Any = None,
    memory_context: list[dict[str, Any]] | None = None,
    memory_research: dict[str, Any] | None = None,
    absolute_deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Understand once, then execute allowlisted capabilities from receipts."""
    if not messages:
        raise RuntimeError("no submitted messages for cognition")
    cycle = store.get_cycle(cycle_id)
    cognition = UnifiedCognition(store, portfolio, engine)
    job = store.start_cognition_job(cycle_id, source["artifact_id"], mode, source["body_markdown"])
    if job["state"] != "completed":
        job = store.claim_cognition_job(job["job_id"])
        if not job["claimed"]:
            return {"cycle_id": cycle_id, "job_id": job["job_id"], "state": job["state"], "receipts": []}
    stream = None
    try:
        if cancelled and cancelled():
            raise MemoryResearchError("memory research was terminated by the user")
        if not execute:
            data = cognition.fixture_result(messages, mode)
        elif job["state"] == "completed" and job.get("result_json"):
            data = {"reply_markdown": None, "needs_fresh_search": False, "public_search_request": None, "propositions": [], "actions": []}
        else:
            # The ordinary chat entrypoint provides only a real, frozen
            # MemoryHub context.  Direct unit callers intentionally see no
            # implicit SQLite-memory substitute.
            memories = list(memory_context or [])
            stream = engine.chat_stream_started(cycle_id, batch_ids, reply_kind) if mode != "h0" else None
            reply_stream = ReplyMarkdownStream()
            streamed_reply = ""

            def on_delta(delta: str) -> None:
                nonlocal streamed_reply
                if cancelled and cancelled():
                    raise MemoryResearchError("memory research was terminated by the user")
                visible = reply_stream.feed(delta)
                streamed_reply += visible

            request_packet = {
                "mode": mode,
                "cognition_prompt": cognition.prompt(cycle, messages, mode, memories),
            }
            timeout_seconds = int(TASK_POLICIES.get(
                cycle["task_key"], TASK_POLICIES["daily.execution.0945"],
            ).m1_timeout.total_seconds())
            deadline = absolute_deadline if absolute_deadline is not None else time.monotonic() + timeout_seconds
            chat_decision = CognitiveRouter(
                effort_policy=CognitiveEffortPolicy.load(store),
            ).route("chat", {**request_packet, "task_key": cycle["task_key"]}, timeout_seconds, False)
            outcome = broker_client().invoke(BrokerRequest(
                stage="chat", packet=request_packet, packet_sha256=canonical_packet_hash(request_packet),
                intellect=chat_decision.intellect, effort=chat_decision.reasoning_effort,
                schema=json.loads((SCHEMAS / "companion-cognition-result-v1.schema.json").read_text(encoding="utf-8")),
                visible_stream=stream is not None,
                on_delta=on_delta if stream else None,
                absolute_deadline=deadline,
                verifier_name="unified-cognition/v1",
                verifier=lambda _output: {"passed": True, "problems": []},
            ))
            if not isinstance(outcome.result, dict):
                raise BrokerError("Broker produced no qualified cognition result", category="broker_output_invalid")
            data = outcome.result
        if cancelled and cancelled():
            raise MemoryResearchError("memory research was terminated by the user")
        outcome = cognition.apply(cycle, source, messages, mode, data, memory_research=memory_research)
    except Exception as exc:
        store.finish_cognition_job(job["job_id"], error=str(exc))
        if stream:
            if engine.store.chat_research_terminated(cycle_id):
                engine.emit(cycle, "chat.stream.cancelled", {"cycle": cycle, "stream_id": stream["stream_id"]})
            else:
                safe_prefix = _receipt_safe_stream_prefix(streamed_reply)
                if safe_prefix:
                    presented_prefix = engine.present_for_publication(
                        safe_prefix, cycle["as_of"], reply_kind,
                        allow_structured_format=any(explicit_format_requested(item["body_text"]) for item in messages),
                    )
                    engine.chat_stream_delta(cycle_id, stream["stream_id"], presented_prefix.markdown)
                engine.chat_stream_failed(cycle_id, stream["stream_id"], str(exc))
            if on_progress:
                on_progress()
        raise

    engine.emit(cycle, "cognition.receipts.ready", {
        "cycle": cycle, "job_id": outcome.job_id, "receipts": list(outcome.receipts),
        "propositions_recorded": outcome.propositions_recorded,
    })
    if outcome.needs_fresh_search and outcome.public_search_request:
        store.queue_research_job(cycle_id, source["artifact_id"], outcome.public_search_request)
    if outcome.reply_markdown:
        if cancelled and cancelled():
            raise MemoryResearchError("memory research was terminated by the user")
        allow_structured_format = any(explicit_format_requested(item["body_text"]) for item in messages)
        presented = engine.present_for_publication(
            outcome.reply_markdown, cycle["as_of"], reply_kind,
            allow_structured_format=allow_structured_format,
        )
        stream_id = None
        if stream:
            current = store.stream_message(stream["stream_id"])["text"]
            remainder = presented.markdown[len(current):] if presented.markdown.startswith(current) else presented.markdown if not current else ""
            if remainder:
                engine.chat_stream_delta(cycle_id, stream["stream_id"], remainder)
                if on_progress:
                    on_progress()
            store.finish_stream_message(stream["stream_id"])
            stream_id = stream["stream_id"]
        engine.chat_ready(
            cycle_id, presented.markdown,
            reply_to_batch_id=batch_ids[-1] if batch_ids else None,
            reply_to_batch_ids=batch_ids, stream_id=stream_id, kind=reply_kind,
            allow_structured_format=allow_structured_format, presented=presented,
        )
    elif mode == "h0":
        store.mark_batches_responded(batch_ids, source["artifact_id"])
    return {
        "cycle_id": cycle_id, "job_id": outcome.job_id, "receipts": list(outcome.receipts),
        "reply_markdown": outcome.reply_markdown,
    }


def run_chat(
    engine: CompanionEngine,
    store: CompanionStore,
    portfolio: PortfolioService,
    cycle_id: str,
    batch_id: str,
    execute: bool,
    *,
    source_kind: str = "chat_human",
    reply_kind: str = "ai_chat",
    on_progress: Any = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    cycle = store.get_cycle(cycle_id)
    phase = "pre_m0" if source_kind == "pre_m0_submission" else "conversation" if cycle.get("kind") == "daily_conversation" else "chat"
    batches = store.pending_message_batches(cycle_id, phase)
    batch_ids = [str(item["batch_id"]) for item in batches]
    messages = store.messages_for_batches(batch_ids)
    source = store.latest_artifact(cycle_id, source_kind)
    if not messages:
        raise RuntimeError("no unresponded submitted messages")
    if source is None:
        raise RuntimeError("submitted conversation artifact is missing")
    engine.record_submitted_messages(cycle_id, messages)
    memory_context: list[dict[str, Any]] | None = None
    memory_research: dict[str, Any] | None = None
    deadline: float | None = None
    if source_kind == "chat_human":
        if engine.memory is None:
            raise MemoryResearchError("MemoryHub is required for ordinary chat")
        timeout_seconds = int(TASK_POLICIES.get(
            cycle["task_key"], TASK_POLICIES["daily.execution.0945"],
        ).m1_timeout.total_seconds())
        deadline = time.monotonic() + timeout_seconds
        resumed = store.resumed_chat_research_checkpoint(cycle_id, source["artifact_id"])
        try:
            memory_result = AdaptiveMemoryResearch(
                engine.memory,
                engine.memory_space_id,
                lambda state: _next_memory_research_action(store, cycle, state, deadline),
                discover_external=lambda action, snapshot: _discover_chat_external_evidence(engine, action, snapshot),
            ).collect(
                cycle_id, messages, deadline=deadline,
                resume=resumed.get("checkpoint") if resumed else None,
                on_checkpoint=lambda checkpoint: store.save_chat_research_checkpoint(
                    cycle_id, source["artifact_id"], batch_ids, checkpoint,
                ),
                cancelled=cancelled,
            )
        except MemoryResearchError:
            if store.chat_research_terminated(cycle_id):
                return {"cycle_id": cycle_id, "state": "terminated"}
            raise
        memory_context = list(memory_result.context)
        memory_research = {
            "snapshot": memory_result.snapshot,
            "actions": list(memory_result.actions),
            "episode_ids": [str(item["episode_id"]) for item in memory_result.context if item.get("episode_id")],
        }
    try:
        result = run_unified_cognition(
            engine, store, portfolio, cycle_id, source, messages, batch_ids, execute,
            mode="conversation", reply_kind=reply_kind, on_progress=on_progress,
            memory_context=memory_context, memory_research=memory_research,
            absolute_deadline=deadline, cancelled=cancelled,
        )
    except MemoryResearchError:
        if store.chat_research_terminated(cycle_id):
            return {"cycle_id": cycle_id, "state": "terminated"}
        raise
    if on_progress:
        on_progress()
    return result


def process_h0_cognition(
    engine: CompanionEngine,
    store: CompanionStore,
    portfolio: PortfolioService,
    cycle_id: str,
    execute: bool,
) -> dict[str, Any] | None:
    source = store.latest_artifact(cycle_id, "h0")
    if source is None:
        return None
    metadata = json.loads(source.get("metadata_json") or "{}")
    batch_id = str(metadata.get("batch_id") or "")
    messages = store.messages_for_batches([batch_id]) if batch_id else []
    if not messages:
        return None
    return run_unified_cognition(
        engine, store, portfolio, cycle_id, source, messages, [batch_id], execute,
        mode="h0",
    )


def run_pending_premarket_reply(
    engine: CompanionEngine,
    store: CompanionStore,
    portfolio: PortfolioService | None,
    cycle_id: str,
    execute: bool,
) -> dict[str, Any]:
    source = store.latest_artifact(cycle_id, "pre_m0_submission")
    if not source:
        return {"action": "no_submission"}
    metadata = json.loads(source.get("metadata_json") or "{}")
    batch_id = str(metadata.get("batch_id") or "")
    for artifact in store.artifacts(cycle_id):
        if artifact["kind"] != "premarket_chat":
            continue
        reply_metadata = json.loads(artifact.get("metadata_json") or "{}")
        if reply_metadata.get("reply_to_batch_id") == batch_id:
            return {"action": "already_replied", "batch_id": batch_id}
    run_chat(
        engine, store, portfolio, cycle_id, batch_id, execute,
        source_kind="pre_m0_submission", reply_kind="premarket_chat",
    )
    return {"action": "replied", "batch_id": batch_id}


def _portfolio_command(store: CompanionStore, portfolio: PortfolioService, command: dict[str, Any]) -> dict[str, Any]:
    command_id = command.get("command_id")
    typ = command.get("type")
    if not command_id or not typ:
        raise ValueError("portfolio command requires command_id and type")
    previous = store.receipt(command_id, command)
    if previous is not None:
        return previous
    if typ == "request_snapshot":
        portfolio.emit_snapshot()
        result = {"accepted": True}
    elif typ == "revert_transaction":
        result = portfolio.revert_latest()
    elif typ == "cancel_pending_proposal":
        portfolio.cancel_pending(str(command.get("proposal_id") or ""))
        result = {"accepted": True}
    else:
        raise ValueError(f"unsupported portfolio command: {typ}")
    store.save_receipt(command_id, None, typ, command, result)
    return result


def _schedule_command(store: CompanionStore, command: dict[str, Any]) -> dict[str, Any]:
    """Exchange adapter only; validation and persistence stay in ScheduleRegistry."""
    command_id = str(command.get("command_id") or "")
    typ = str(command.get("type") or "")
    if not command_id or not typ:
        raise ValueError("任务命令需要 command_id 和 type")
    existing = store.receipt(command_id, command)
    if existing is not None:
        return existing
    registry = _schedule_registry(store)
    if typ == "schedule.list":
        result = {"schedules": registry.list()}
    elif typ == "schedule.preview":
        result = registry.preview(dict(command.get("config") or {}))
    elif typ == "schedule.create":
        result = {"schedule": registry.create(dict(command.get("config") or {}))}
    elif typ == "schedule.update":
        result = {"schedule": registry.update(str(command.get("schedule_id") or ""), int(command.get("expected_version")), dict(command.get("config") or {}))}
    elif typ == "schedule.pause":
        result = {"schedule": registry.pause(str(command.get("schedule_id") or ""), int(command.get("expected_version")))}
    elif typ == "schedule.resume":
        result = {"schedule": registry.resume(str(command.get("schedule_id") or ""), int(command.get("expected_version")))}
    elif typ == "schedule.archive":
        result = {"schedule": registry.archive(str(command.get("schedule_id") or ""), int(command.get("expected_version")))}
    elif typ == "schedule.history":
        result = {"history": store.schedule_history(str(command.get("schedule_id") or ""))}
    elif typ == "schedule.restore_defaults":
        registry.seed(json.loads((PATHS.resources / "schedules" / "tasks.json").read_text(encoding="utf-8")))
        result = {"schedules": registry.list(), "action": "defaults_restored"}
    else:
        raise ValueError("不支持的任务命令")
    store.save_receipt(command_id, None, typ, command, result)
    store.queue_schedule_event("schedule.result", {"command_id": command_id, **result})
    return result


def consume(
    engine: CompanionEngine,
    store: CompanionStore,
    exchange: LocalExchange,
    portfolio: PortfolioService,
    execute: bool = False,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    def interrupt_chat_research(cycle_id: str) -> bool:
        """Process only this chat's stop command while its worker is active."""
        interrupted = False
        for control_path, control in exchange.receive_matching(
            "to-runtime",
            lambda value: value.get("contract") == "companion-user-command/v1"
            and value.get("type") == "terminate_chat_research"
            and value.get("cycle_id") == cycle_id,
        ):
            engine.command(control)
            exchange.acknowledge("to-runtime", control_path)
            interrupted = True
        if interrupted:
            flush(store, exchange)
        return store.chat_research_terminated(cycle_id)

    for path, command in exchange.receive("to-runtime"):
        try:
            if command.get("contract") == "memory-user-command/v1":
                command_id = str(command.get("command_id") or "")
                command_type = str(command.get("type") or "")
                result = store.receipt(command_id, command)
                if result is None:
                    result = handle_memory_command(
                        engine.memory, engine.memory_space_id, command, PATHS.home / "exports" / "memory",
                    )
                    store.save_receipt(command_id, None, command_type, command, result)
                exchange.send("to-client", f"memory-{command_id}", {
                    "contract": "memory-command-result/v1", "command_id": command_id,
                    "type": command_type, "result": result,
                })
                exchange.acknowledge("to-runtime", path)
                results.append(result)
                continue
            if command.get("contract") == "schedule-user-command/v1":
                result = _schedule_command(store, command)
                exchange.acknowledge("to-runtime", path)
                results.append(result)
                continue
            if command.get("contract") == "portfolio-user-command/v1":
                result = _portfolio_command(store, portfolio, command)
                exchange.acknowledge("to-runtime", path)
                results.append(result)
                continue
            result = engine.command(command)
            exchange.acknowledge("to-runtime", path)
            # Publish deterministic state changes before a potentially long LLM call.
            # This keeps the UI responsive and makes H0 locking observable immediately.
            flush(store, exchange)
            cycle_id = command.get("cycle_id")
            typ = command.get("type")
            if cycle_id and typ in {"commit_h0", "skip_h0", "submit_h0", "submit_voice_h0"}:
                if store.get_cycle(cycle_id)["state"] in {"researching_m1", "m1_retry_wait"}:
                    # Both branches receive the same immutable H0.  M1 reads
                    # the pre-H0 private snapshot; cognition may update facts,
                    # and neither branch waits for the other to begin.
                    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="h0-fanout") as pool:
                        m1_future = pool.submit(run_m1, engine, store, portfolio, cycle_id, execute)
                        cognition_future = pool.submit(process_h0_cognition, engine, store, portfolio, cycle_id, execute)
                        cognition_error = None
                        try:
                            cognition_future.result()
                        except Exception as exc:
                            cognition_error = exc
                            engine.background_failed(cycle_id, "cognition", str(exc))
                        result = m1_future.result()
                        if cognition_error:
                            result = {**result, "cognition_error": str(cognition_error)}
                else:
                    process_h0_cognition(engine, store, portfolio, cycle_id, execute)
            elif cycle_id and typ == "commit_pre_m0":
                run_pending_premarket_reply(engine, store, portfolio, cycle_id, execute)
            elif cycle_id and typ in {"commit_chat_batch", "commit_conversation_batch"}:
                result = run_chat(
                    engine, store, portfolio, cycle_id, str(result.get("committed_batch_id") or ""), execute,
                    on_progress=lambda: flush(store, exchange),
                    cancelled=lambda: interrupt_chat_research(str(cycle_id)),
                )
            elif cycle_id and typ == "continue_chat_research" and result.get("continued_now"):
                result = {
                    **result,
                    "continuation": run_chat(
                        engine, store, portfolio, cycle_id, "", execute,
                        on_progress=lambda: flush(store, exchange),
                        cancelled=lambda: interrupt_chat_research(str(cycle_id)),
                    ),
                }
            results.append(result)
        except Exception as exc:
            exchange.reject("to-runtime", path, str(exc))
            results.append({"error": str(exc), "path": path.name})
    flush(store, exchange)
    if results:
        render_learning(store)
    return results


def main() -> int:
    # Windows installations commonly inherit a GBK console even though every
    # runtime artifact is UTF-8 JSON. A completed preview must not be reported
    # as failed because a safe summary contains ordinary Unicode punctuation.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    start = sub.add_parser("start")
    start.add_argument("--task-key", required=True)
    start.add_argument("--scheduled-for", required=True)
    start.add_argument("--as-of")
    start.add_argument("--execute", action="store_true")
    gateway = sub.add_parser("serve-gateway")
    gateway.add_argument("--execute", action="store_true")
    resume = sub.add_parser("synthesize")
    resume.add_argument("--cycle-id", required=True)
    resume.add_argument("--execute", action="store_true")
    due = sub.add_parser("run-due")
    due.add_argument("--at")
    due.add_argument("--execute", action="store_true")
    schedule = sub.add_parser("run-schedule")
    schedule.add_argument("--at")
    schedule.add_argument("--execute", action="store_true")
    scheduled_cycle = sub.add_parser("run-scheduled-cycle")
    scheduled_cycle.add_argument("--cycle-id", required=True)
    scheduled_cycle.add_argument("--execute", action="store_true")
    diagnostic_rerun = sub.add_parser("diagnostic-rerun")
    diagnostic_rerun.add_argument("--cycle-id", required=True)
    diagnostic_rerun.add_argument("--execute", action="store_true")
    preview_rerun = sub.add_parser("preview-rerun")
    preview_rerun.add_argument("--date", required=True)
    preview_rerun.add_argument("--cycle-id")
    preview_rerun.add_argument("--known-at", help="ISO-8601 cutoff; defaults to the selected cycle's 15:20 Shanghai time")
    approve_preview = sub.add_parser("approve-preview")
    approve_preview.add_argument("--preview-id", required=True)
    preview_worker = sub.add_parser("_preview-worker")
    preview_worker.add_argument("--source-cycle-id", required=True)
    preview_worker.add_argument("--preview-id", required=True)
    preview_worker.add_argument("--known-at", required=True)
    preview_worker.add_argument("--bundle-path", type=Path, required=True)
    sub.add_parser("claim-scheduled-workers")
    consume_parser = sub.add_parser("consume-command")
    consume_parser.add_argument("--execute", action="store_true")
    background = sub.add_parser("run-background")
    premarket_reply = sub.add_parser("reply-premarket")
    premarket_reply.add_argument("--cycle-id", required=True)
    premarket_reply.add_argument("--execute", action="store_true")
    backup = sub.add_parser("backup")
    backup.add_argument("--verify", action="store_true")
    migration = sub.add_parser("migrate-legacy")
    migration.add_argument("--legacy-root", type=Path)
    background.add_argument("--execute", action="store_true")
    sub.add_parser("dispatch")
    sub.add_parser("status")
    gateway_config = sub.add_parser("configure-web-access-gateway")
    gateway_config.add_argument("--mcp-url")
    gateway_config.add_argument("--token-stdin", action="store_true")
    args = parser.parse_args()
    if args.cmd == "preview-rerun":
        source = {"cycle_id": args.cycle_id} if args.cycle_id else find_source_cycle(DB, args.date)
        preview_id = f"{args.date.replace('-', '')}-1520-{uuid.uuid4().hex[:10]}"
        # A historical preview must preserve the requested cycle's information
        # cutoff.  Using the current clock would admit later evidence and make
        # both the bundle and its approval fingerprint non-reproducible.
        known_at = args.known_at or f"{args.date}T15:20:00+08:00"
        bundle = launch_preview(PATHS.home, DB, INSTALL_ROOT, source["cycle_id"], preview_id, known_at)
        print(json.dumps({
            "preview_id": bundle["preview_id"], "bundle_sha256": bundle["bundle_sha256"],
            "source_cycle_id": bundle["source_cycle_id"], "cycle_state": bundle["cycle_state"],
            "preview_status": bundle["preview_status"], "failure": bundle.get("failure"),
            "known_at": bundle["known_at"],
            "bundle_path": str(PATHS.home / "runtime" / "previews" / preview_id / "bundle.json"),
        }, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "_preview-worker":
        bundle = run_preview_worker(args.source_cycle_id, args.preview_id, args.known_at, args.bundle_path)
        print(json.dumps({"preview_id": bundle["preview_id"], "bundle_sha256": bundle["bundle_sha256"]}, ensure_ascii=False))
        return 0
    if args.cmd == "migrate-legacy":
        _backup_before_schedule_migration()
        sources = LegacySources.defaults(INSTALL_ROOT)
        if args.legacy_root:
            legacy_root = args.legacy_root.resolve()
            legacy_data = legacy_root / "data"
            if not legacy_data.exists():
                legacy_data = legacy_root / "archive" / "local" / "legacy-runtime-data-2026-08-25"
            sources = LegacySources(
                companion_database=legacy_data / "runtime" / "companion" / "companion.sqlite3",
                automation_database=legacy_data / "runtime" / "stock_advisor.sqlite3",
                decision_center_home=sources.decision_center_home,
                workspace=legacy_data,
            )
        print(json.dumps(LegacyMigrator(PATHS, sources).run(), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "configure-web-access-gateway":
        settings = load_settings(PATHS.home)
        research = json.loads(json.dumps(settings.research))
        gateway = research.setdefault("web_access_gateway", {})
        if args.mcp_url:
            gateway["mcp_url"] = args.mcp_url.rstrip("/")
        if args.token_stdin:
            token = sys.stdin.read().strip()
            if not token:
                raise ValueError("Gateway token stdin was empty")
            gateway["token"] = token
        save_research_settings(PATHS.home, research)
        print(json.dumps({"configured": True, "mcp_url": gateway.get("mcp_url"), "token_configured": bool(gateway.get("token"))}, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "serve-gateway":
        run_gateway(args.execute)
        return 0
    engine, store, exchange, portfolio = runtime()
    if args.cmd == "approve-preview":
        bundle_path = PATHS.home / "runtime" / "previews" / args.preview_id / "bundle.json"
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        approved = approve_bundle(store, bundle)
        flush(store, exchange)
        print(json.dumps(approved, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "backup":
        _, store, _, _ = runtime()
        created = BackupManager(store, RUNTIME, load_settings(PATHS.home).backup).create(reason="manual")
        result = BackupManager(store, RUNTIME, load_settings(PATHS.home).backup).verify_restore(created["backup_id"]) if args.verify else created
        print(json.dumps(result, ensure_ascii=False, indent=2)); return 0
    if args.cmd == "start":
        cycle = engine.start_cycle(args.task_key, args.scheduled_for, args.as_of or iso(datetime.now(timezone.utc)))
        cycle = run_research(engine, store, cycle, args.execute, lambda: flush(store, exchange))
        flush(store, exchange)
        print(json.dumps(cycle, ensure_ascii=False))
        return 0
    if args.cmd == "synthesize":
        cycle = store.get_cycle(args.cycle_id)
        if cycle["state"] in {"researching_m1", "m1_retry_wait", "waiting_for_repair"}:
            cycle = run_m1(engine, store, portfolio, args.cycle_id, args.execute)
        else:
            cycle = run_m2(engine, store, args.cycle_id, args.execute)
        flush(store, exchange)
        render_learning(store)
        print(json.dumps(cycle, ensure_ascii=False))
        return 0
    if args.cmd == "run-due":
        at = datetime.fromisoformat(args.at.replace("Z", "+00:00")) if args.at else None
        changed = engine.run_due(at)
        completed = []
        for projection in changed:
            cycle_id = projection["cycle"]["cycle_id"]
            completed.append(run_m1(engine, store, portfolio, cycle_id, args.execute))
            process_h0_cognition(engine, store, portfolio, cycle_id, args.execute)
        flush(store, exchange)
        if completed:
            render_learning(store)
        print(json.dumps(completed, ensure_ascii=False))
        return 0
    if args.cmd == "run-schedule":
        at = datetime.fromisoformat(args.at.replace("Z", "+00:00")) if args.at else datetime.now(timezone.utc)
        changed = run_schedules(engine, store, at, args.execute, exchange, portfolio)
        flush(store, exchange)
        print(json.dumps(changed, ensure_ascii=False))
        return 0
    if args.cmd == "run-scheduled-cycle":
        changed = run_scheduled_cycle(engine, store, exchange, portfolio, args.cycle_id, args.execute)
        flush(store, exchange)
        render_learning(store)
        print(json.dumps(changed, ensure_ascii=False))
        return 0
    if args.cmd == "diagnostic-rerun":
        changed = engine.start_diagnostic_rerun(args.cycle_id)
        try:
            if args.execute:
                changed = run_m1(engine, store, portfolio, changed["cycle_id"], True)
        except Exception as exc:
            failed_cycle = store.get_cycle(changed["cycle_id"])
            flush(store, exchange)
            render_learning(store)
            error_category = exc.category if isinstance(exc, BrokerError) else "runtime_error"
            print(json.dumps({
                "status": "failed",
                "cycle": failed_cycle,
                "error_category": error_category,
                "diagnostic_code": CompanionEngine._diagnostic_code(str(exc)),
            }, ensure_ascii=False))
            return 2
        flush(store, exchange)
        render_learning(store)
        print(json.dumps(changed, ensure_ascii=False))
        return 0
    if args.cmd == "claim-scheduled-workers":
        print(json.dumps({"cycles": [row["cycle_id"] for row in store.claim_scheduled_workers(limit=2)]}, ensure_ascii=False))
        return 0
    if args.cmd == "consume-command":
        print(json.dumps(consume(engine, store, exchange, portfolio, args.execute), ensure_ascii=False))
        return 0
    if args.cmd == "run-background":
        result = run_background(engine, store, args.execute)
        flush(store, exchange)
        if result.get("action") not in {"idle", "deferred"}:
            render_learning(store)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if args.cmd == "reply-premarket":
        result = run_pending_premarket_reply(
            engine, store, portfolio, args.cycle_id, args.execute
        )
        flush(store, exchange)
        if result.get("action") == "replied":
            render_learning(store)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if args.cmd == "dispatch":
        print(json.dumps({"delivered": flush(store, exchange)}, ensure_ascii=False))
        return 0
    if args.cmd == "status":
        with store.connection() as connection:
            rows = [dict(row) for row in connection.execute(
                """SELECT cycle_id,task_key,state,revision,h0_auto_submit_at,m1_publish_deadline,
                          h0_locked_at,m1_started_at,m1_completed_at,m2_started_at,m2_completed_at,updated_at
                   FROM companion_cycle ORDER BY updated_at DESC"""
            )]
        print(json.dumps({"cycles": rows, "portfolio": portfolio.snapshot()}, ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
