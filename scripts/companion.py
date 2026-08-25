#!/usr/bin/env python3
"""Headless entry point for the local AI Decision Center companion runtime."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from companion_runtime.codex_runner import CodexCliRunner, CodexResult
from companion_runtime.engine import CompanionEngine, iso
from companion_runtime.exchange import LocalExchange
from companion_runtime.governance import RouterGovernance, classify_regime
from companion_runtime.learning import JudgmentLifecycle, WorkflowEvolution
from companion_runtime.models import TASK_POLICIES
from companion_runtime.packet_builder import RuntimePacketBuilder
from companion_runtime.portfolio import PortfolioService, explicit_fixture_extraction, is_portfolio_statement
from companion_runtime.projection import LearningProjectionRenderer
from companion_runtime.scheduler import DAILY_SCHEDULES, SHANGHAI, run_daily_schedule
from companion_runtime.router import CognitiveRouter
from companion_runtime.store import CompanionStore


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = Path(os.environ.get("STOCK_ADVISOR_COMPANION_RUNTIME", str(ROOT / "data" / "runtime" / "companion")))
DB = RUNTIME / "companion.sqlite3"
SCHEMAS = ROOT / "automations" / "contracts"


def exchange_root() -> Path:
    value = os.environ.get("LOCALAPPDATA")
    if not value:
        raise RuntimeError("LOCALAPPDATA is required")
    return Path(value) / "AIDecisionCenter" / "exchange"


def runtime() -> tuple[CompanionEngine, CompanionStore, LocalExchange, PortfolioService]:
    store = CompanionStore(DB)
    store.initialize()
    store.risk_doctrine()
    engine = CompanionEngine(store)
    JudgmentLifecycle(store).backfill()
    exchange = LocalExchange(exchange_root())
    exchange.ensure()
    portfolio = PortfolioService(ROOT, store)
    portfolio.reconcile()
    return engine, store, exchange, portfolio


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
    return count


def render_learning(store: CompanionStore) -> None:
    LearningProjectionRenderer(ROOT, store).render()


def _call_stage(
    store: CompanionStore,
    cycle: dict[str, Any],
    stage: str,
    packet: dict[str, Any],
    schema_name: str,
    *,
    search: bool,
    timeout: int,
) -> tuple[dict[str, Any], CodexResult]:
    router = CognitiveRouter()
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
        model=decision.model, reasoning_effort=decision.reasoning_effort,
        search_enabled=decision.search, timeout_seconds=decision.timeout_seconds,
        routing_reason=decision.reason, route_decision_id=decision_id,
        runner_fingerprint="codex-cli-output-schema-v1",
    )
    workspace_kind = "public" if search else "local"
    run_dir = RUNTIME / "runs" / cycle["cycle_id"] / workspace_kind / f"{stage}-{attempt['attempt_number']}"
    output = run_dir / "output" / f"{stage}.json"
    try:
        result = CodexCliRunner().run(
            RuntimePacketBuilder.prompt(packet),
            run_dir,
            SCHEMAS / schema_name,
            output,
            timeout=decision.timeout_seconds,
            search=decision.search,
            model=decision.model,
            reasoning_effort=decision.reasoning_effort,
        )
        data = json.loads(result.text)
        verifier = router.verify(stage, packet, data)
        store.finish_attempt(
            attempt["attempt_id"],
            "succeeded",
            output_sha256=hashlib.sha256(result.text.encode("utf-8")).hexdigest(),
            usage=result.usage, verifier=verifier,
        )
        if plan.mode == "shadow" and plan.candidate and verifier["passed"]:
            store.queue_router_shadow(
                decision_id, cycle["cycle_id"], stage, packet, schema_name, plan.candidate.as_json(),
                priority=1 if plan.profile.major else 0,
            )
        return data, result
    except Exception as exc:
        status = "timed_out" if isinstance(exc, (subprocess.TimeoutExpired, TimeoutError)) else "failed"
        store.finish_attempt(attempt["attempt_id"], status, error=str(exc))
        raise


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
        model=candidate["model"], reasoning_effort=candidate["reasoning_effort"],
        search_enabled=bool(candidate["search"]), timeout_seconds=int(candidate["timeout_seconds"]),
        routing_reason=candidate["reason"], route_decision_id=job["decision_id"], is_shadow=True,
        runner_fingerprint="codex-cli-output-schema-v1",
    )
    run_dir = RUNTIME / "shadow-runs" / job["job_id"]
    output = run_dir / "output" / f"{job['stage']}.json"
    try:
        result = CodexCliRunner().run(
            RuntimePacketBuilder.prompt(packet), run_dir, SCHEMAS / job["schema_name"], output,
            timeout=int(candidate["timeout_seconds"]), search=bool(candidate["search"]),
            model=candidate["model"], reasoning_effort=candidate["reasoning_effort"],
        )
        data = json.loads(result.text)
        verifier = CognitiveRouter().verify(job["stage"], packet, data)
        store.finish_attempt(
            attempt["attempt_id"], "succeeded", output_sha256=hashlib.sha256(result.text.encode("utf-8")).hexdigest(),
            usage=result.usage, verifier=verifier,
        )
        store.finish_router_shadow(job["job_id"], output=data, verifier=verifier)
    except Exception as exc:
        status = "timed_out" if isinstance(exc, (subprocess.TimeoutExpired, TimeoutError)) else "failed"
        store.finish_attempt(attempt["attempt_id"], status, error=str(exc))
        store.finish_router_shadow(job["job_id"], error=str(exc))


def _latest_json_artifact(store: CompanionStore, cycle_id: str, kind: str) -> dict[str, Any] | None:
    artifact = store.latest_artifact(cycle_id, kind)
    if not artifact:
        return None
    try:
        return json.loads(artifact["body_markdown"])
    except json.JSONDecodeError:
        return None


def run_research(
    engine: CompanionEngine,
    store: CompanionStore,
    cycle: dict[str, Any],
    execute: bool,
    on_progress: Any = None,
) -> dict[str, Any]:
    cycle = engine.research_started(cycle["cycle_id"])
    if on_progress:
        on_progress()
    builder = RuntimePacketBuilder(ROOT, store)
    if not execute:
        evidence = {"as_of": cycle["as_of"], "spoken_summary": "Fixture 模式：等待真实公开信息搜索。", "sources": [], "critical_gaps": []}
        store.append_artifact(cycle["cycle_id"], "evidence", "model", json.dumps(evidence, ensure_ascii=False), cycle["as_of"])
        return engine.research_ready(cycle["cycle_id"], "Fixture 模式：这里会显示自然、无方向的 M0 客观观察。")

    policy = TASK_POLICIES[cycle["task_key"]]
    public_packet = builder.build(cycle, "m0_research")
    for number in range(1, 3):
        try:
            evidence, _ = _call_stage(
                store, cycle, "m0_research", public_packet, "companion-evidence-result-v1.schema.json",
                search=True, timeout=int(policy.research_timeout.total_seconds()),
            )
            store.record_evidence(cycle, "m0_research", evidence)
            store.append_artifact(
                cycle["cycle_id"], "evidence", "model", json.dumps(evidence, ensure_ascii=False),
                evidence.get("as_of") or cycle["as_of"], {"public_only": True},
            )
            local_packet = builder.build(cycle, "m0_compose", evidence=evidence)
            m0, local_result = _call_stage(
                store, cycle, "m0_compose", local_packet, "companion-m0-result-v1.schema.json",
                search=False, timeout=int(policy.m1_timeout.total_seconds()),
            )
            return engine.research_ready(
                cycle["cycle_id"], m0["m0_markdown"], session_id=local_result.session_id,
                packet_hash=local_packet["sha256"], evidence_as_of=evidence.get("as_of"),
            )
        except Exception as exc:
            if number < 2:
                engine.research_retrying(cycle["cycle_id"], str(exc), number + 1)
                if on_progress:
                    on_progress()
                time.sleep(2)
                continue
            engine.research_failed(cycle["cycle_id"], str(exc))
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
) -> dict[str, Any]:
    cycle = store.get_cycle(cycle_id)
    if cycle["state"] == "waiting_for_repair":
        cycle = engine.resume_m1_after_repair(cycle_id)
    if cycle["state"] not in {"researching_m1", "m1_retry_wait"}:
        raise RuntimeError(f"cycle is not waiting for M1: {cycle['state']}")
    builder = RuntimePacketBuilder(ROOT, store)
    if not execute:
        engine.m1_judgment_started(cycle_id)
        result = engine.m1_ready(cycle_id, "Fixture 模式：这里会显示与 H0 隔离的独立 M1 判断。")
        return result

    policy = TASK_POLICIES[cycle["task_key"]]
    prior_evidence = _latest_json_artifact(store, cycle_id, "evidence") or {}
    for number in range(1, 3):
        try:
            public_packet = builder.build(
                cycle, "m1_research", evidence=prior_evidence,
                as_of=iso(datetime.now(timezone.utc)),
            )
            evidence, _ = _call_stage(
                store, cycle, "m1_research", public_packet, "companion-evidence-result-v1.schema.json",
                search=True, timeout=_deadline_timeout(cycle, int(policy.m1_timeout.total_seconds())),
            )
            store.record_evidence(cycle, "m1_research", evidence)
            store.append_artifact(
                cycle_id, "m1_evidence", "model", json.dumps(evidence, ensure_ascii=False),
                evidence.get("as_of") or iso(datetime.now(timezone.utc)), {"public_only": True},
            )
            cycle = engine.m1_judgment_started(cycle_id)
            local_packet = builder.build(
                cycle, "m1_judgment", evidence=evidence,
                as_of=str(evidence.get("as_of") or iso(datetime.now(timezone.utc))),
            )
            judgment, _ = _call_stage(
                store, cycle, "m1_judgment", local_packet, "companion-m1-result-v1.schema.json",
                search=False, timeout=_deadline_timeout(cycle, int(policy.m1_timeout.total_seconds())),
            )
            return engine.m1_ready(
                cycle_id, judgment["m1_markdown"], as_of=evidence.get("as_of"),
                snapshot=judgment.get("snapshot"), qualified=bool(judgment.get("judgment_qualified")),
            )
        except Exception as exc:
            try:
                remaining = _deadline_timeout(store.get_cycle(cycle_id), 60)
            except TimeoutError:
                remaining = 0
            retryable = number < 2 and remaining >= 30
            engine.m1_failed(cycle_id, str(exc), retryable=retryable)
            if not retryable:
                raise
            cycle = store.get_cycle(cycle_id)
            time.sleep(2)
    raise RuntimeError("M1 attempts exhausted")


def run_m2(engine: CompanionEngine, store: CompanionStore, cycle_id: str, execute: bool) -> dict[str, Any]:
    cycle = store.get_cycle(cycle_id)
    if cycle["state"] not in {"synthesizing_m2", "m2_deferred"}:
        return cycle
    if not execute:
        return engine.m2_ready(cycle_id, "Fixture 模式：这里会显示 M0、H0和独立 M1的伴生综合 M2。")
    frozen_as_of = str(cycle.get("m1_completed_at") or cycle["as_of"])
    packet = RuntimePacketBuilder(ROOT, store).build(cycle, "m2", as_of=frozen_as_of)
    data, _ = _call_stage(
        store, cycle, "m2", packet, "companion-m2-result-v1.schema.json",
        search=False, timeout=int(TASK_POLICIES[cycle["task_key"]].m2_timeout.total_seconds()),
    )
    return engine.m2_ready(cycle_id, data["m2_markdown"], snapshot=data.get("snapshot"), as_of=frozen_as_of)


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
        packet = RuntimePacketBuilder(ROOT, store).build(
            cycle, "reflection", context={"checkpoint_id": checkpoint_id},
            as_of=iso(datetime.now(timezone.utc)),
        )
        data, _ = _call_stage(
            store, cycle, "reflection", packet, "companion-reflection-result-v1.schema.json",
            search=False, timeout=int(TASK_POLICIES[cycle["task_key"]].m1_timeout.total_seconds()),
        )
    artifact = store.append_artifact(
        cycle_id, "reflection", "model", data["reflection_markdown"],
        iso(datetime.now(timezone.utc)),
        {"checkpoint_id": checkpoint_id, "memory_tags": data.get("memory_tags") or []},
    )
    proposal = None
    if data.get("workflow_proposal"):
        proposal = WorkflowEvolution(store).propose(
            cycle_id, data["workflow_proposal"], source_artifact_id=artifact["artifact_id"],
        )
    engine.emit(cycle, "reflection.ready", {
        "cycle": cycle, "text": data["reflection_markdown"],
        "source_artifact_id": artifact["artifact_id"],
        "workflow_proposal_id": proposal["proposal_id"] if proposal else None,
    })
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
        packet = RuntimePacketBuilder(ROOT, store).build(
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
        verdict = governance.promote_if_qualified(cell_key)
        if verdict["action"] == "promote":
            engine.chat_ready(
                cycle["cycle_id"],
                "这段时间我把同一批判断用不同思考深度反复对照了。更深入的那条路径在不同市场状态下都更稳，"
                "我会把它作为这类判断的优先习惯；以后如果它不再经得起结果检验，我会主动退回并告诉你。",
            )
    engine.emit(cycle, "outcome.ready", {
        "cycle": cycle, "text": result["summary"], "horizon": checkpoint["horizon"],
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
        builder = RuntimePacketBuilder(ROOT, store)
        research_packet = builder.build(
            cycle, "chat_research", context=public_scope,
            as_of=iso(datetime.now(timezone.utc)),
        )
        evidence, _ = _call_stage(
            store, cycle, "chat_research", research_packet, "companion-evidence-result-v1.schema.json",
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
    if data and data.get("judgment_revision"):
        revision = data["judgment_revision"]
        engine.judgment_revision_ready(
            cycle["cycle_id"], str(revision["revision_markdown"]), str(revision["revises_artifact_id"]),
        )
    store.finish_research_job(job["job_id"])
    source_metadata = json.loads(source.get("metadata_json") or "{}")
    engine.chat_ready(cycle["cycle_id"], reply, reply_to_batch_id=source_metadata.get("batch_id"))
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
            packet = RuntimePacketBuilder(ROOT, store).build(
                cycle, "workflow_feedback", context={"source_artifact_id": h0["artifact_id"]},
                as_of=iso(datetime.now(timezone.utc)),
            )
            data, _ = _call_stage(
                store, cycle, "workflow_feedback", packet, "companion-reflection-result-v1.schema.json",
                search=False, timeout=int(TASK_POLICIES[cycle["task_key"]].m1_timeout.total_seconds()),
            )
        artifact = store.append_artifact(
            cycle["cycle_id"], "ai_chat", "model", data["reflection_markdown"],
            iso(datetime.now(timezone.utc)),
            {"workflow_feedback_source": h0["artifact_id"], "memory_tags": data.get("memory_tags") or []},
        )
        proposal = None
        if data.get("workflow_proposal"):
            proposal = WorkflowEvolution(store).propose(
                cycle["cycle_id"], data["workflow_proposal"], source_artifact_id=artifact["artifact_id"],
            )
        engine.emit(cycle, "chat.ready", {
            "cycle": cycle, "text": data["reflection_markdown"],
            "source_artifact_id": artifact["artifact_id"],
            "workflow_proposal_id": proposal["proposal_id"] if proposal else None,
        })
        return {"cycle_id": cycle["cycle_id"], "artifact_id": artifact["artifact_id"]}
    return None


def _foreground_busy(store: CompanionStore) -> bool:
    with store.connection() as connection:
        return bool(connection.execute(
            """SELECT 1 FROM companion_cycle
               WHERE state IN ('queued','researching_m0','h0_locked','researching_m1','judging_m1','m1_retry_wait')
               LIMIT 1"""
        ).fetchone())


def _seconds_until_next_daily_schedule(at: datetime | None = None) -> int:
    current = (at or datetime.now(timezone.utc)).astimezone(SHANGHAI)
    for day_offset in range(0, 8):
        day = current.date() + timedelta(days=day_offset)
        if day.weekday() >= 5:
            continue
        for item in DAILY_SCHEDULES:
            candidate = datetime.combine(day, item.at, SHANGHAI)
            if candidate > current:
                return max(0, int((candidate - current).total_seconds()))
    return 24 * 60 * 60


def run_background(
    engine: CompanionEngine,
    store: CompanionStore,
    execute: bool,
) -> dict[str, Any]:
    if _foreground_busy(store):
        return {"action": "deferred", "reason": "foreground_cycle_has_priority"}
    until_next = _seconds_until_next_daily_schedule()
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


def _interpret_portfolio(portfolio: PortfolioService, source: dict[str, Any], execute: bool) -> dict[str, Any]:
    if not execute:
        return explicit_fixture_extraction(source["body_markdown"])
    run_dir = RUNTIME / "portfolio-runs" / source["artifact_id"]
    prompt = (
        "只解释用户是否明确报告了已经发生的真实成交或当前持仓事实。不要搜索互联网，不给投资建议，不修改文件。"
        "计划、建议、条件句不是成交。只提取原文明确出现的股票、方向、股数、价格和时间；缺失字段必须为 null，并逐字段给出原文 evidence。\n\n"
        + json.dumps({"user_text": source["body_markdown"], "current_portfolio": portfolio.snapshot()["positions"]}, ensure_ascii=False, indent=2)
    )
    result = CodexCliRunner().run(
        prompt, run_dir, SCHEMAS / "portfolio-interpretation-result-v1.schema.json",
        run_dir / "output" / "interpretation.json", timeout=45, search=False,
        model=os.environ.get("STOCK_ADVISOR_CODEX_FAST_MODEL", "gpt-5.6-luna"),
        reasoning_effort="medium",
    )
    return json.loads(result.text)


def _process_portfolio_artifact(portfolio: PortfolioService, artifact: dict[str, Any] | None, execute: bool) -> None:
    if not artifact or not is_portfolio_statement(artifact["body_markdown"]):
        return
    portfolio.record_job(artifact["artifact_id"], artifact["cycle_id"], artifact["body_markdown"])
    try:
        portfolio.complete_job(artifact["artifact_id"], _interpret_portfolio(portfolio, artifact, execute))
    except Exception as exc:
        portfolio.fail_job(artifact["artifact_id"], str(exc))


def run_chat(
    engine: CompanionEngine,
    store: CompanionStore,
    portfolio: PortfolioService,
    cycle_id: str,
    batch_id: str,
    execute: bool,
) -> dict[str, Any]:
    cycle = store.get_cycle(cycle_id)
    source = store.latest_artifact(cycle_id, "chat_human")
    _process_portfolio_artifact(portfolio, source, execute)
    if not source:
        raise RuntimeError("chat batch artifact missing")
    if not execute:
        return engine.chat_ready(cycle_id, "Fixture 模式：已收到这批消息。", reply_to_batch_id=batch_id)
    packet = RuntimePacketBuilder(ROOT, store).build(cycle, "chat", message_batch=source["body_markdown"])
    data, _ = _call_stage(
        store, cycle, "chat", packet, "companion-chat-result-v1.schema.json",
        search=False, timeout=int(TASK_POLICIES[cycle["task_key"]].m1_timeout.total_seconds()),
    )
    if data.get("judgment_revision"):
        revision = data["judgment_revision"]
        engine.judgment_revision_ready(
            cycle_id, str(revision["revision_markdown"]), str(revision["revises_artifact_id"]),
        )
    evolution = WorkflowEvolution(store)
    source_artifact_id = source["artifact_id"]
    if data.get("needs_fresh_search") and data.get("public_search_request"):
        store.queue_research_job(cycle_id, source_artifact_id, data["public_search_request"])
        data["reply_markdown"] += "\n\n这部分需要当下公开信息，我已经把脱敏后的问题排进补查；不会把你的私人消息交给联网步骤。"
    if data.get("workflow_proposal"):
        proposal = evolution.propose(cycle_id, data["workflow_proposal"], source_artifact_id=source_artifact_id)
        data["reply_markdown"] += f"\n\n我已经把这个改进记成待审核提案（{proposal['proposal_id'][:8]}）。它不会自行改代码、权限或扩大调用。"
    if data.get("proposal_decision"):
        decision = data["proposal_decision"]
        try:
            if decision["action"] == "rollback":
                rolled_back = evolution.rollback()
                action = "回滚到上一版本" if rolled_back else "保持现状，因为没有可回滚版本"
            else:
                applied = evolution.decide(
                    str(decision["proposal_id"]), decision["action"] == "approve",
                    note=str(decision.get("note") or ""),
                )
                action = "采用并保留了可回滚版本" if applied["state"] == "applied" else "不采用"
            data["reply_markdown"] += f"\n\n这个工作流提案我已按你的意思{action}。"
        except ValueError as exc:
            data["reply_markdown"] += f"\n\n这个提案现在还不能启用：{exc}。我会继续收集结果，不会强行套用。"
    return engine.chat_ready(cycle_id, data["reply_markdown"], reply_to_batch_id=batch_id)


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


def consume(
    engine: CompanionEngine,
    store: CompanionStore,
    exchange: LocalExchange,
    portfolio: PortfolioService,
    execute: bool = False,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for path, command in exchange.receive("to-runtime"):
        try:
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
                    result = run_m1(engine, store, portfolio, cycle_id, execute)
                _process_portfolio_artifact(portfolio, store.latest_artifact(cycle_id, "h0"), execute)
            elif cycle_id and typ == "commit_chat_batch":
                result = run_chat(engine, store, portfolio, cycle_id, str(result.get("committed_batch_id") or ""), execute)
            results.append(result)
        except Exception as exc:
            exchange.reject("to-runtime", path, str(exc))
            results.append({"error": str(exc), "path": path.name})
    flush(store, exchange)
    if results:
        render_learning(store)
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    start = sub.add_parser("start")
    start.add_argument("--task-key", required=True)
    start.add_argument("--scheduled-for", required=True)
    start.add_argument("--as-of")
    start.add_argument("--execute", action="store_true")
    resume = sub.add_parser("synthesize")
    resume.add_argument("--cycle-id", required=True)
    resume.add_argument("--execute", action="store_true")
    due = sub.add_parser("run-due")
    due.add_argument("--at")
    due.add_argument("--execute", action="store_true")
    schedule = sub.add_parser("run-schedule")
    schedule.add_argument("--at")
    schedule.add_argument("--execute", action="store_true")
    consume_parser = sub.add_parser("consume-command")
    consume_parser.add_argument("--execute", action="store_true")
    background = sub.add_parser("run-background")
    background.add_argument("--execute", action="store_true")
    sub.add_parser("dispatch")
    sub.add_parser("status")
    sub.add_parser("probe")
    args = parser.parse_args()
    engine, store, exchange, portfolio = runtime()
    if args.cmd == "probe":
        print(json.dumps(CodexCliRunner().probe(), ensure_ascii=False, indent=2))
        return 0
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
            _process_portfolio_artifact(portfolio, store.latest_artifact(cycle_id, "h0"), args.execute)
        flush(store, exchange)
        if completed:
            render_learning(store)
        print(json.dumps(completed, ensure_ascii=False))
        return 0
    if args.cmd == "run-schedule":
        at = datetime.fromisoformat(args.at.replace("Z", "+00:00")) if args.at else datetime.now(timezone.utc)
        changed = run_daily_schedule(engine, store, at, lambda cycle: run_research(engine, store, cycle, args.execute, lambda: flush(store, exchange)))
        flush(store, exchange)
        print(json.dumps(changed, ensure_ascii=False))
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
