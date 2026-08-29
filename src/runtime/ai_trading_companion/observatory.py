from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must be timezone-aware: {value}")
    return parsed


@dataclass(frozen=True)
class EvaluationRequest:
    cycle_id: str
    observed_at: str
    request_id: str | None = None
    legacy_incomplete: bool = False


@dataclass(frozen=True)
class ForecastRequest:
    task_key: str
    stage: str = "m0"
    cycle_id: str | None = None
    market_regime: str | None = None
    observed_at: str | None = None
    request_id: str | None = None
    trigger: str = "manual"


@dataclass(frozen=True)
class ExperimentRequest:
    experiment_key: str
    source_kind: str = "live_paired_shadow"
    request_id: str | None = None


@dataclass(frozen=True)
class SnapshotQuery:
    snapshot_kind: str | None = None
    task_key: str | None = None
    cycle_id: str | None = None
    limit: int = 100


@dataclass(frozen=True)
class ObservatorySnapshot:
    snapshot_id: str
    snapshot_kind: str
    created_at: str
    source_fingerprint: str


@dataclass(frozen=True)
class TimelineEvent:
    event_id: str
    phase: str
    event_type: str
    occurred_at: str
    status: str
    source_kind: str
    duration_seconds: int | None = None


@dataclass(frozen=True)
class ResearchQuality:
    evidence_coverage: float | None
    max_freshness_age_seconds: int | None
    independent_source_groups: int
    conflict_count: int
    factual_error_count: int
    evidence_gate_passed: bool | None
    rejection_reasons: tuple[str, ...]
    completeness: tuple[str, ...]


@dataclass(frozen=True)
class JudgmentOutcome:
    snapshot_id: str
    checkpoint_id: str
    horizon: str
    original_direction: str
    original_triggers: tuple[str, ...]
    original_invalidations: tuple[str, ...]
    original_market_regime: str
    original_judgment_text: str | None
    original_policy_version: str | None
    checkpoint_status: str
    verification_status: str | None
    outcome_summary: str | None
    data_gaps: tuple[str, ...]
    due_at: str | None
    outcome_as_of: str | None


@dataclass(frozen=True)
class SharedEvidenceAttribution:
    evidence_id: str
    source_cycle_id: str
    source_task_key: str
    source_stage: str | None
    evidence_known_at: str
    reused_at: str
    age_at_m0_start_seconds: int | None


@dataclass(frozen=True)
class ScheduleStartRecommendation:
    action: str
    rationale: tuple[str, ...]
    proposed_start_at: str | None
    schedule_sha256: str | None
    json_patch: tuple[dict[str, Any], ...]
    requires_explicit_apply: bool


@dataclass(frozen=True)
class EvaluationSnapshot(ObservatorySnapshot):
    cycle_id: str
    task_key: str
    stage: str
    observed_at: str
    planned_start_at: str
    actual_start_at: str | None
    value_window_end: str
    qualified_published_at: str | None
    delivery_state: str
    qualified_duration_seconds: int | None
    failure_category: str | None
    failure_reason: str | None
    market_regime: str
    legacy_incomplete: bool
    completeness: tuple[str, ...]
    timeline: tuple[TimelineEvent, ...]
    research_quality: ResearchQuality
    shared_evidence: tuple[SharedEvidenceAttribution, ...]
    judgment_outcomes: tuple[JudgmentOutcome, ...]


@dataclass(frozen=True)
class ForecastSnapshot(ObservatorySnapshot):
    task_key: str
    stage: str
    target_cycle_id: str | None
    observed_at: str | None
    trigger: str
    target_context_fingerprint: str | None
    market_regime: str | None
    fallback_level: str
    sample_size: int
    qualified_deliveries: int
    qualified_probability: float | None
    wilson_90_low: float | None
    wilson_90_high: float | None
    no_qualified_delivery_risk: float | None
    min_seconds: int | None
    p50_seconds: int | None
    p90_seconds: int | None
    p95_seconds: int | None
    max_seconds: int | None
    maturity: str
    interval_width: float | None
    data_completeness: float
    applicable_market_regimes: tuple[str, ...]
    tail_precision: str
    schedule_start_recommendation: ScheduleStartRecommendation


@dataclass(frozen=True)
class ForecastCalibrationSnapshot(ObservatorySnapshot):
    forecast_snapshot_id: str
    evaluation_snapshot_id: str
    cycle_id: str
    predicted_probability: float | None
    observed_qualified: int
    probability_hit: bool | None
    interval_covered: bool | None
    absolute_calibration_error: float | None


@dataclass(frozen=True)
class DimensionComparison:
    name: str
    direction: str
    pair_count: int
    baseline_mean: float | None
    candidate_mean: float | None
    mean_delta: float | None
    deltas: tuple[float, ...]


@dataclass(frozen=True)
class EvidenceMaturity:
    mature: bool
    effective_weight: float
    confidence_interval_width: float | None
    data_completeness: float
    market_regime_coverage: float
    protection_dimensions_stable: bool


@dataclass(frozen=True)
class ExperimentAssessment(ObservatorySnapshot):
    experiment_key: str
    source_kind: str
    paired_runs: int
    delivery_speed: DimensionComparison
    qualification: DimensionComparison
    research_quality: DimensionComparison
    judgment_outcome: DimensionComparison
    cost: DimensionComparison
    stability: DimensionComparison
    data_completeness: float
    market_regimes: tuple[str, ...]
    evidence_maturity: EvidenceMaturity
    decision: str
    decision_reasons: tuple[str, ...]


@dataclass(frozen=True)
class SnapshotSummary:
    snapshot_id: str
    snapshot_kind: str
    created_at: str
    task_key: str | None
    cycle_id: str | None


class EvaluationObservatory:
    """Read owned runtime facts and append immutable evaluation derivatives."""

    def __init__(self, store: Any, *, exchange: Any | None = None, schedule_path: Path | None = None) -> None:
        self.store = store
        self.exchange = exchange
        self.schedule_path = schedule_path
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self.store.connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS observatory_snapshot (
                  snapshot_id TEXT PRIMARY KEY,
                  snapshot_kind TEXT NOT NULL,
                  task_key TEXT,
                  cycle_id TEXT,
                  source_fingerprint TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_observatory_snapshot_query
                  ON observatory_snapshot(snapshot_kind,task_key,cycle_id,created_at);
                CREATE TABLE IF NOT EXISTS forecast_calibration_link (
                  forecast_snapshot_id TEXT PRIMARY KEY,
                  calibration_snapshot_id TEXT NOT NULL UNIQUE,
                  evaluation_snapshot_id TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS observatory_request (
                  request_id TEXT PRIMARY KEY,
                  snapshot_kind TEXT NOT NULL,
                  scope_key TEXT NOT NULL,
                  input_fingerprint TEXT NOT NULL,
                  snapshot_id TEXT NOT NULL UNIQUE,
                  created_at TEXT NOT NULL
                );
            """)

    def evaluate(self, request: EvaluationRequest) -> EvaluationSnapshot:
        cycle, attempts, artifacts, events, market_regime, evidence, checkpoints = self._evaluation_facts(request.cycle_id)
        if cycle["task_key"] != "daily.execution.0945":
            raise ValueError("the first evaluation slice only supports daily.execution.0945")
        observed = _parse(request.observed_at)
        planned = _parse(cycle["scheduled_for"])
        window_end = datetime.combine(planned.date(), time(10, 30), planned.tzinfo)

        start_events = [event["created_at"] for event in events if event["event_type"] == "m0.started"]
        completeness = ["cycle"]
        if request.legacy_incomplete:
            completeness.append("legacy_incomplete")
        if start_events:
            actual_start_at = min(start_events, key=_parse)
            completeness.append("actual_start_event")
        else:
            m0_attempts = [row["started_at"] for row in attempts if row["stage"] in {"m0_research", "m0_compose"}]
            actual_start_at = min(m0_attempts, key=_parse) if m0_attempts else None
            if actual_start_at:
                completeness.append("actual_start_inferred_from_attempt")

        attempt_by_id = {row["attempt_id"]: row for row in attempts}
        qualified_artifacts: list[dict[str, Any]] = []
        for artifact in artifacts:
            if artifact["kind"] != "m0":
                continue
            metadata = json.loads(artifact.get("metadata_json") or "{}")
            compose = attempt_by_id.get(metadata.get("compose_attempt_id"))
            verifier = json.loads((compose or {}).get("verifier_json") or "{}")
            if compose and compose.get("status") == "succeeded" and verifier.get("passed") is True:
                qualified_artifacts.append(artifact)
        qualified = min(qualified_artifacts, key=lambda row: _parse(row["sealed_at"])) if qualified_artifacts else None
        qualified_at = qualified["sealed_at"] if qualified else None
        if qualified:
            completeness.append("qualified_m0")

        delivery_state, failure_category, failure_reason = self._delivery_state(
            cycle, attempts, observed, window_end, qualified_at,
        )
        timeline = self._project_timeline(attempts, artifacts, events)
        research_quality = self._research_quality(attempts)
        shared_evidence = self._shared_evidence_attributions(evidence, actual_start_at)
        if shared_evidence:
            completeness.append("shared_evidence_attribution")
        judgment_outcomes = self._judgment_outcomes(checkpoints, market_regime)
        duration = None
        if delivery_state == "qualified" and actual_start_at and qualified_at:
            duration = max(0, int((_parse(qualified_at) - _parse(actual_start_at)).total_seconds()))

        facts = {"request": asdict(request), "cycle": cycle, "attempts": attempts, "artifacts": artifacts, "events": events, "market_regime": market_regime, "evidence": evidence, "checkpoints": checkpoints}
        fingerprint = hashlib.sha256(json.dumps(facts, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        existing = self._request_snapshot(request.request_id, "evaluation", request.cycle_id, fingerprint)
        if existing is not None:
            if not isinstance(existing, EvaluationSnapshot):
                raise ValueError("request id resolved to an incompatible snapshot")
            return existing
        snapshot = EvaluationSnapshot(
            snapshot_id=str(uuid.uuid4()), snapshot_kind="evaluation", created_at=_now(),
            source_fingerprint=fingerprint, cycle_id=cycle["cycle_id"], task_key=cycle["task_key"],
            stage="m0", observed_at=request.observed_at, planned_start_at=cycle["scheduled_for"],
            actual_start_at=actual_start_at, value_window_end=window_end.isoformat(),
            qualified_published_at=qualified_at, delivery_state=delivery_state,
            qualified_duration_seconds=duration, failure_category=failure_category,
            failure_reason=failure_reason, market_regime=market_regime,
            legacy_incomplete=request.legacy_incomplete,
            completeness=tuple(completeness), timeline=timeline, research_quality=research_quality,
            shared_evidence=shared_evidence,
            judgment_outcomes=judgment_outcomes,
        )
        stored = self._append_snapshot(
            snapshot, task_key=snapshot.task_key, cycle_id=snapshot.cycle_id,
            request_id=request.request_id, scope_key=request.cycle_id,
        )
        if not isinstance(stored, EvaluationSnapshot):
            raise ValueError("request id resolved to an incompatible snapshot")
        if stored.snapshot_id == snapshot.snapshot_id:
            self._calibrate_forecasts(snapshot)
        return stored

    def _evaluation_facts(self, cycle_id: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], str, list[dict[str, Any]], list[dict[str, Any]]]:
        with self.store.connection() as connection:
            cycle_row = connection.execute("SELECT * FROM companion_cycle WHERE cycle_id=?", (cycle_id,)).fetchone()
            if not cycle_row:
                raise ValueError("unknown cycle")
            attempts = [dict(row) for row in connection.execute(
                "SELECT * FROM llm_attempt WHERE cycle_id=? ORDER BY started_at,attempt_number", (cycle_id,),
            )]
            artifacts = [dict(row) for row in connection.execute(
                "SELECT * FROM narrative_artifact WHERE cycle_id=? ORDER BY sealed_at,revision", (cycle_id,),
            )]
            events = [dict(row) for row in connection.execute(
                "SELECT event_id,cycle_id,event_type,payload_json,created_at FROM companion_outbox WHERE cycle_id=? ORDER BY created_at,event_id",
                (cycle_id,),
            )]
            regime_row = connection.execute(
                "SELECT regime FROM market_regime_snapshot WHERE cycle_id=?", (cycle_id,),
            ).fetchone()
            evidence = [dict(row) for row in connection.execute(
                """SELECT e.*, COALESCE(u.stage,e.stage) AS use_stage,
                          COALESCE(u.used_at,e.known_at) AS used_at,
                          origin.task_key AS source_task_key
                     FROM evidence_ledger_entry e
                LEFT JOIN evidence_cycle_use u ON u.evidence_id=e.evidence_id
                LEFT JOIN companion_cycle origin ON origin.cycle_id=e.cycle_id
                    WHERE u.cycle_id=? OR (u.cycle_id IS NULL AND e.cycle_id=?)
                 ORDER BY COALESCE(u.used_at,e.known_at),e.evidence_id""",
                (cycle_id, cycle_id),
            )]
            checkpoints = [dict(row) for row in connection.execute(
                """SELECT s.snapshot_id,s.snapshot_json,s.as_of AS judgment_as_of,
                          o.checkpoint_id,o.horizon,o.due_at,o.status,o.as_of AS outcome_as_of,o.outcome_json,o.error
                     FROM judgment_snapshot s JOIN outcome_checkpoint o ON o.snapshot_id=s.snapshot_id
                    WHERE s.cycle_id=? ORDER BY s.created_at,o.due_at,o.horizon""",
                (cycle_id,),
            )]
        return dict(cycle_row), attempts, artifacts, events, str(regime_row["regime"] if regime_row else "unknown"), evidence, checkpoints

    @staticmethod
    def _research_quality(attempts: list[dict[str, Any]]) -> ResearchQuality:
        research = [attempt for attempt in attempts if attempt["stage"] == "m0_research"]
        if not research:
            return ResearchQuality(None, None, 0, 0, 0, None, (), ("gate_fact_missing",))
        latest = max(research, key=lambda row: (_parse(row.get("completed_at") or row["started_at"]), row["attempt_number"]))
        verifier = json.loads(latest.get("verifier_json") or "{}")
        gate = verifier.get("evidence_gate") if isinstance(verifier.get("evidence_gate"), dict) else verifier
        normalized = gate.get("normalized_evidence") if isinstance(gate.get("normalized_evidence"), dict) else {}
        coverage = [row for row in normalized.get("coverage") or [] if isinstance(row, dict)]
        covered = sum(1 for row in coverage if row.get("status") in {"covered", "checked_no_change"})
        coverage_ratio = covered / len(coverage) if coverage else None
        as_of = normalized.get("as_of")
        ages: list[int] = []
        if as_of:
            for source in normalized.get("sources") or []:
                if isinstance(source, dict) and source.get("fact_as_of"):
                    ages.append(max(0, int((_parse(str(as_of)) - _parse(str(source["fact_as_of"]))).total_seconds())))
        groups = {
            str(source.get("independence_group"))
            for source in normalized.get("sources") or []
            if isinstance(source, dict) and source.get("independence_group")
        }
        conflicts = [row for row in normalized.get("conflicts") or [] if isinstance(row, dict)]
        problems = tuple(dict.fromkeys(str(problem) for problem in gate.get("problems") or []))
        factual_prefixes = (
            "source_from_future", "source_published_at_in_future", "source_excerpt_",
            "evidence_as_of_", "frozen_as_of_",
        )
        factual_errors = sum(1 for problem in problems if problem.startswith(factual_prefixes))
        complete = ["gate_fact"]
        if coverage:
            complete.append("coverage")
        if normalized.get("sources"):
            complete.append("sources")
        return ResearchQuality(
            evidence_coverage=coverage_ratio,
            max_freshness_age_seconds=max(ages) if ages else None,
            independent_source_groups=len(groups), conflict_count=len(conflicts),
            factual_error_count=factual_errors,
            evidence_gate_passed=bool(gate.get("passed")) if "passed" in gate else None,
            rejection_reasons=problems if gate.get("passed") is False else (),
            completeness=tuple(complete),
        )

    @staticmethod
    def _shared_evidence_attributions(
        evidence: list[dict[str, Any]], actual_start_at: str | None,
    ) -> tuple[SharedEvidenceAttribution, ...]:
        start = _parse(actual_start_at) if actual_start_at else None
        result: list[SharedEvidenceAttribution] = []
        for row in evidence:
            source_cycle = row.get("cycle_id")
            if not source_cycle:
                continue
            # Evidence owned by the current cycle is local work. Reused material
            # remains attributed to its source cycle and is never folded into M0 wait.
            if row.get("used_at") == row.get("known_at") and row.get("use_stage") == row.get("stage"):
                continue
            known = str(row["known_at"])
            age = max(0, int((start - _parse(known)).total_seconds())) if start else None
            result.append(SharedEvidenceAttribution(
                evidence_id=str(row["evidence_id"]), source_cycle_id=str(source_cycle),
                source_task_key=str(row.get("source_task_key") or "unknown"),
                source_stage=str(row.get("stage")) if row.get("stage") else None,
                evidence_known_at=known, reused_at=str(row.get("used_at") or known),
                age_at_m0_start_seconds=age,
            ))
        return tuple(sorted(result, key=lambda item: (item.reused_at, item.evidence_id)))

    @staticmethod
    def _judgment_outcomes(checkpoints: list[dict[str, Any]], fallback_regime: str) -> tuple[JudgmentOutcome, ...]:
        outcomes: list[JudgmentOutcome] = []
        for row in checkpoints:
            original = json.loads(row.get("snapshot_json") or "{}")
            result = json.loads(row.get("outcome_json") or "{}")
            outcomes.append(JudgmentOutcome(
                snapshot_id=row["snapshot_id"], checkpoint_id=row["checkpoint_id"], horizon=row["horizon"],
                original_direction=str(original.get("direction") or "unknown"),
                original_triggers=tuple(str(item) for item in original.get("triggers") or ()),
                original_invalidations=tuple(str(item) for item in original.get("invalidations") or ()),
                original_market_regime=str(original.get("market_regime") or fallback_regime or "unknown"),
                original_judgment_text=str(original.get("original_judgment_text") or original.get("original_text")) if (original.get("original_judgment_text") or original.get("original_text")) else None,
                original_policy_version=str(original.get("strategy_policy_version")) if original.get("strategy_policy_version") else None,
                checkpoint_status=row["status"],
                verification_status=str(result.get("verification_status")) if result.get("verification_status") else None,
                outcome_summary=str(result.get("summary")) if result.get("summary") else None,
                data_gaps=tuple(str(item) for item in result.get("data_gaps") or ()),
                due_at=row.get("due_at"), outcome_as_of=row.get("outcome_as_of") or None,
            ))
        return tuple(outcomes)

    @staticmethod
    def _project_timeline(
        attempts: list[dict[str, Any]], artifacts: list[dict[str, Any]], events: list[dict[str, Any]],
    ) -> tuple[TimelineEvent, ...]:
        projected: dict[str, TimelineEvent] = {}

        def add(event: TimelineEvent) -> None:
            existing = projected.get(event.event_id)
            if existing is None:
                projected[event.event_id] = event
                return
            if existing != event:
                raise ValueError(f"runtime event id conflict: {event.event_id}")

        for event in events:
            add(TimelineEvent(
                event_id=event["event_id"], phase="runtime", event_type=event["event_type"],
                occurred_at=event["created_at"], status="observed", source_kind="outbox",
            ))
        for attempt in attempts:
            attempt_id = attempt["attempt_id"]
            add(TimelineEvent(
                event_id=f"{attempt_id}:started", phase="llm", event_type=attempt["stage"],
                occurred_at=attempt["started_at"], status="started", source_kind="llm_attempt",
            ))
            if attempt.get("completed_at"):
                duration = max(0, int((_parse(attempt["completed_at"]) - _parse(attempt["started_at"])).total_seconds()))
                add(TimelineEvent(
                    event_id=f"{attempt_id}:terminal", phase="llm", event_type=attempt["stage"],
                    occurred_at=attempt["completed_at"], status=attempt["status"],
                    source_kind="llm_attempt", duration_seconds=duration,
                ))
                verifier = json.loads(attempt.get("verifier_json") or "{}")
                gate = verifier.get("evidence_gate") if isinstance(verifier.get("evidence_gate"), dict) else None
                if gate is not None:
                    add(TimelineEvent(
                        event_id=f"{attempt_id}:evidence-gate", phase="evidence_gate", event_type="evidence_gate",
                        occurred_at=attempt["completed_at"], status="passed" if gate.get("passed") else "rejected",
                        source_kind="llm_attempt",
                    ))
            for index, trace in enumerate(json.loads(attempt.get("tool_trace_json") or "[]")):
                if not isinstance(trace, dict):
                    continue
                canonical = json.dumps(trace, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                event_id = str(trace.get("event_id") or trace.get("observation_id") or hashlib.sha256(canonical.encode("utf-8")).hexdigest())
                occurred_at = str(trace.get("started_at") or trace.get("occurred_at") or attempt["started_at"])
                completed_at = trace.get("completed_at")
                duration = None
                if completed_at:
                    duration = max(0, int((_parse(str(completed_at)) - _parse(occurred_at)).total_seconds()))
                tool = str(trace.get("tool") or trace.get("operation") or trace.get("backend") or f"tool-{index}")
                if "search" in tool:
                    phase = "search"
                elif "browser" in tool or "read" in tool:
                    phase = "browser"
                elif "cpa" in tool.lower():
                    phase = "cpa"
                else:
                    phase = "tool"
                add(TimelineEvent(
                    event_id=event_id, phase=phase, event_type=tool, occurred_at=occurred_at,
                    status=str(trace.get("status") or "observed"), source_kind="tool_trace",
                    duration_seconds=duration,
                ))
        for artifact in artifacts:
            if artifact["kind"] in {"m0", "m1", "m2"}:
                add(TimelineEvent(
                    event_id=f"{artifact['artifact_id']}:published", phase="publication",
                    event_type=f"{artifact['kind']}.published", occurred_at=artifact["sealed_at"],
                    status="published", source_kind="narrative_artifact",
                ))
        return tuple(sorted(projected.values(), key=lambda item: (_parse(item.occurred_at), item.event_id)))

    @staticmethod
    def _delivery_state(
        cycle: dict[str, Any], attempts: list[dict[str, Any]], observed: datetime,
        window_end: datetime, qualified_at: str | None,
    ) -> tuple[str, str | None, str | None]:
        if qualified_at:
            if _parse(qualified_at) <= window_end:
                return "qualified", None, None
            return "late", "value_window_missed", None
        statuses = {row["status"] for row in attempts}
        failed = [row for row in attempts if row["status"] in {"rejected", "timed_out", "failed"}]
        reason = failed[-1].get("error") if failed else None
        if "rejected" in statuses:
            return "rejected", "evidence_gate_or_verifier_rejection", reason
        if "timed_out" in statuses:
            return "timeout", "stage_timeout", reason
        if cycle["state"] == "missed":
            return "missed", "schedule_missed", reason
        if cycle["state"] == "failed" or "failed" in statuses:
            return "failed", "runtime_failure", reason
        if observed > window_end:
            return "incomplete", "no_qualified_delivery", None
        return "incomplete", None, None

    def get_snapshot(self, snapshot_id: str) -> ObservatorySnapshot:
        with self.store.connection() as connection:
            row = connection.execute("SELECT * FROM observatory_snapshot WHERE snapshot_id=?", (snapshot_id,)).fetchone()
        if not row:
            raise ValueError("unknown observatory snapshot")
        return self._decode_snapshot(row["snapshot_kind"], row["payload_json"])

    @staticmethod
    def _decode_snapshot(snapshot_kind: str, payload_json: str) -> ObservatorySnapshot:
        payload = json.loads(payload_json)
        if snapshot_kind == "evaluation":
            payload.setdefault("legacy_incomplete", False)
            payload["completeness"] = tuple(payload.get("completeness") or ())
            payload["timeline"] = tuple(TimelineEvent(**event) for event in payload.get("timeline") or ())
            quality = payload.get("research_quality") or {}
            quality["rejection_reasons"] = tuple(quality.get("rejection_reasons") or ())
            quality["completeness"] = tuple(quality.get("completeness") or ())
            payload["research_quality"] = ResearchQuality(**quality)
            payload["shared_evidence"] = tuple(
                SharedEvidenceAttribution(**item) for item in payload.get("shared_evidence") or ()
            )
            payload["judgment_outcomes"] = tuple(
                JudgmentOutcome(
                    **{
                        **outcome,
                        "original_triggers": tuple(outcome.get("original_triggers") or ()),
                        "original_invalidations": tuple(outcome.get("original_invalidations") or ()),
                        "data_gaps": tuple(outcome.get("data_gaps") or ()),
                    }
                )
                for outcome in payload.get("judgment_outcomes") or ()
            )
            return EvaluationSnapshot(**payload)
        if snapshot_kind == "forecast":
            payload["applicable_market_regimes"] = tuple(payload.get("applicable_market_regimes") or ())
            payload.setdefault("observed_at", None)
            payload.setdefault("trigger", "legacy")
            payload.setdefault("target_context_fingerprint", None)
            recommendation = payload.get("schedule_start_recommendation") or {
                "action": "insufficient_evidence", "rationale": ("legacy_snapshot",),
                "proposed_start_at": None, "schedule_sha256": None, "json_patch": (),
                "requires_explicit_apply": True,
            }
            recommendation["rationale"] = tuple(recommendation.get("rationale") or ())
            recommendation["json_patch"] = tuple(recommendation.get("json_patch") or ())
            payload["schedule_start_recommendation"] = ScheduleStartRecommendation(**recommendation)
            return ForecastSnapshot(**payload)
        if snapshot_kind == "forecast_calibration":
            return ForecastCalibrationSnapshot(**payload)
        if snapshot_kind == "experiment":
            for name in ("delivery_speed", "qualification", "research_quality", "judgment_outcome", "cost", "stability"):
                dimension = payload[name]
                dimension["deltas"] = tuple(dimension.get("deltas") or ())
                payload[name] = DimensionComparison(**dimension)
            payload["market_regimes"] = tuple(payload.get("market_regimes") or ())
            payload["evidence_maturity"] = EvidenceMaturity(**payload["evidence_maturity"])
            payload["decision_reasons"] = tuple(payload.get("decision_reasons") or ())
            return ExperimentAssessment(**payload)
        raise ValueError(f"unsupported snapshot kind: {snapshot_kind}")

    def query(self, query: SnapshotQuery) -> Sequence[SnapshotSummary]:
        clauses, values = [], []
        for column, value in (("snapshot_kind", query.snapshot_kind), ("task_key", query.task_key), ("cycle_id", query.cycle_id)):
            if value is not None:
                clauses.append(f"{column}=?")
                values.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(max(1, min(1000, query.limit)))
        with self.store.connection() as connection:
            rows = connection.execute(
                f"SELECT snapshot_id,snapshot_kind,created_at,task_key,cycle_id FROM observatory_snapshot {where} ORDER BY created_at DESC,snapshot_id DESC LIMIT ?",
                values,
            ).fetchall()
        return tuple(SnapshotSummary(**dict(row)) for row in rows)

    def _backfill_legacy(self) -> dict[str, int]:
        """Append low-completeness historical derivatives without fabricating facts."""
        with self.store.connection() as connection:
            cycles = [dict(row) for row in connection.execute(
                "SELECT cycle_id,updated_at FROM companion_cycle WHERE task_key='daily.execution.0945' ORDER BY scheduled_for,cycle_id",
            )]
            router_cells = [str(row["cell_key"]) for row in connection.execute("SELECT DISTINCT cell_key FROM router_evaluation")]
            strategy_cells = [str(row["cell_key"]) for row in connection.execute("SELECT DISTINCT cell_key FROM runtime_strategy_evaluation")]
        evaluations = 0
        experiments = 0
        for cycle in cycles:
            snapshot = self.evaluate(EvaluationRequest(
                cycle_id=cycle["cycle_id"], observed_at=str(cycle["updated_at"]), legacy_incomplete=True,
                request_id=f"legacy-evaluation:{cycle['cycle_id']}",
            ))
            if snapshot.snapshot_id:
                evaluations += 1
        for cell_key in dict.fromkeys([*router_cells, *strategy_cells]):
            snapshot = self.assess_experiment(ExperimentRequest(
                cell_key, source_kind="historical_replay", request_id=f"legacy-experiment:{cell_key}",
            ))
            if snapshot.snapshot_id:
                experiments += 1
        return {"evaluation_snapshots": evaluations, "experiment_assessments": experiments}

    def forecast(self, request: ForecastRequest) -> ForecastSnapshot:
        if request.stage != "m0":
            raise ValueError("the first forecast slice only supports m0")
        with self.store.connection() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM observatory_snapshot WHERE snapshot_kind='evaluation' ORDER BY created_at DESC,snapshot_id DESC",
            ).fetchall()
        latest_by_cycle: dict[str, dict[str, Any]] = {}
        for row in rows:
            payload = json.loads(row["payload_json"])
            latest_by_cycle.setdefault(payload["cycle_id"], payload)
        evaluations = [
            row for row in latest_by_cycle.values()
            if request.cycle_id is None or row["cycle_id"] != request.cycle_id
        ]
        exact = [row for row in evaluations if row["task_key"] == request.task_key and row["stage"] == request.stage and row.get("market_regime") == request.market_regime]
        same_task = [row for row in evaluations if row["task_key"] == request.task_key and row["stage"] == request.stage]
        stage_prior = [row for row in evaluations if row["stage"] == request.stage]
        if request.market_regime is not None and exact:
            sample, fallback = exact, "same_task_stage_market_regime"
        elif same_task:
            sample, fallback = same_task, "same_task_stage"
        else:
            sample, fallback = stage_prior, "stage_prior"

        n = len(sample)
        qualified = [row for row in sample if row["delivery_state"] == "qualified"]
        successes = len(qualified)
        probability = successes / n if n else None
        low, high = self._wilson_90(successes, n)
        durations = sorted(int(row["qualified_duration_seconds"]) for row in qualified if row.get("qualified_duration_seconds") is not None)
        completeness = sum(min(1.0, len(row.get("completeness") or []) / 3.0) for row in sample) / n if n else 0.0
        regimes = tuple(sorted({str(row.get("market_regime") or "unknown") for row in sample}))
        width = high - low if low is not None and high is not None else None
        if n == 0:
            maturity = "unavailable"
        elif width is not None and width <= .25 and completeness >= .9 and len(regimes) >= 2:
            maturity = "high"
        elif width is not None and width <= .50 and completeness >= .7:
            maturity = "medium"
        else:
            maturity = "low"
        target_context = self._forecast_target_context(request.cycle_id)
        target_context_fingerprint = hashlib.sha256(json.dumps(target_context, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest() if target_context else None
        fingerprint = hashlib.sha256(json.dumps({
            "scope": {"task_key": request.task_key, "stage": request.stage, "cycle_id": request.cycle_id, "market_regime": request.market_regime},
            "sample": [(row["snapshot_id"], row["source_fingerprint"]) for row in sample],
            "target_context": target_context,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        scope_key = "|".join((request.task_key, request.stage, request.market_regime or "*", request.cycle_id or "*"))
        existing = self._request_snapshot(request.request_id, "forecast", scope_key, fingerprint)
        if existing is not None:
            if not isinstance(existing, ForecastSnapshot):
                raise ValueError("request id resolved to an incompatible snapshot")
            return existing
        if request.request_id is None:
            existing = self._snapshot_by_fingerprint("forecast", fingerprint)
            if isinstance(existing, ForecastSnapshot):
                return existing
        snapshot = ForecastSnapshot(
            snapshot_id=str(uuid.uuid4()), snapshot_kind="forecast", created_at=_now(),
            source_fingerprint=fingerprint, task_key=request.task_key, stage=request.stage,
            target_cycle_id=request.cycle_id, observed_at=request.observed_at,
            trigger=request.trigger, target_context_fingerprint=target_context_fingerprint,
            market_regime=request.market_regime,
            fallback_level=fallback, sample_size=n,
            qualified_deliveries=successes, qualified_probability=probability,
            wilson_90_low=low, wilson_90_high=high,
            no_qualified_delivery_risk=(1.0 - probability) if probability is not None else None,
            min_seconds=durations[0] if durations else None,
            p50_seconds=self._empirical_quantile(durations, .50),
            p90_seconds=self._empirical_quantile(durations, .90),
            p95_seconds=self._empirical_quantile(durations, .95),
            max_seconds=durations[-1] if durations else None,
            maturity=maturity, interval_width=width, data_completeness=completeness,
            applicable_market_regimes=regimes,
            tail_precision="low" if width is None or width > .35 else "empirical",
            schedule_start_recommendation=self._schedule_start_recommendation(
                request.task_key, durations, probability, low,
            ),
        )
        stored = self._append_snapshot(
            snapshot, task_key=request.task_key, cycle_id=request.cycle_id,
            request_id=request.request_id, scope_key=scope_key,
        )
        if not isinstance(stored, ForecastSnapshot):
            raise ValueError("request id resolved to an incompatible snapshot")
        return stored

    def _schedule_start_recommendation(
        self, task_key: str, durations: list[int], probability: float | None, wilson_low: float | None,
    ) -> ScheduleStartRecommendation:
        unavailable = ScheduleStartRecommendation(
            action="insufficient_evidence", rationale=("qualified_delivery_distribution_unavailable",),
            proposed_start_at=None, schedule_sha256=None, json_patch=(), requires_explicit_apply=True,
        )
        if not durations or probability is None or wilson_low is None:
            return unavailable
        if self.schedule_path is None or not self.schedule_path.is_file():
            return ScheduleStartRecommendation(
                action="insufficient_evidence", rationale=("formal_tasks_json_not_available_to_observatory",),
                proposed_start_at=None, schedule_sha256=None, json_patch=(), requires_explicit_apply=True,
            )
        raw = self.schedule_path.read_bytes()
        schedule = json.loads(raw.decode("utf-8"))
        rows = schedule.get("daily") if isinstance(schedule.get("daily"), list) else []
        index = next((i for i, row in enumerate(rows) if isinstance(row, dict) and row.get("task_key") == task_key), None)
        if index is None or not isinstance(rows[index].get("at"), str):
            return ScheduleStartRecommendation(
                action="insufficient_evidence", rationale=("task_not_found_in_formal_schedule",),
                proposed_start_at=None, schedule_sha256=hashlib.sha256(raw).hexdigest(), json_patch=(),
                requires_explicit_apply=True,
            )
        if task_key != "daily.execution.0945":
            return ScheduleStartRecommendation(
                action="hold", rationale=("first_schedule_recommendation_slice_is_0945",),
                proposed_start_at=rows[index]["at"], schedule_sha256=hashlib.sha256(raw).hexdigest(), json_patch=(),
                requires_explicit_apply=True,
            )
        planned = datetime.strptime(rows[index]["at"], "%H:%M")
        deadline = planned.replace(hour=10, minute=30)
        desired = deadline - timedelta(seconds=self._empirical_quantile(durations, .90) or durations[-1]) - timedelta(minutes=5)
        delta_minutes = int((desired - planned).total_seconds() // 60)
        if wilson_low < .50:
            action, reasons, proposed = "hold", ("qualified_probability_lower_bound_too_low",), planned
        elif delta_minutes <= -5:
            action, reasons, proposed = "advance", ("p90_delivery_requires_earlier_start",), desired
        elif delta_minutes >= 10 and probability >= .90:
            action, reasons, proposed = "delay", ("p90_delivery_has_conservative_slack",), min(desired, planned + timedelta(minutes=15))
        else:
            action, reasons, proposed = "hold", ("current_start_matches_observed_delivery_distribution",), planned
        proposed_at = proposed.strftime("%H:%M")
        patch = () if action == "hold" else ({"op": "replace", "path": f"/daily/{index}/at", "value": proposed_at},)
        return ScheduleStartRecommendation(
            action=action, rationale=reasons, proposed_start_at=proposed_at,
            schedule_sha256=hashlib.sha256(raw).hexdigest(), json_patch=patch,
            requires_explicit_apply=True,
        )

    def _forecast_target_context(self, cycle_id: str | None) -> dict[str, Any] | None:
        if not cycle_id:
            return None
        with self.store.connection() as connection:
            cycle = connection.execute("SELECT cycle_id,state,updated_at FROM companion_cycle WHERE cycle_id=?", (cycle_id,)).fetchone()
            if not cycle:
                return None
            attempts = [dict(row) for row in connection.execute(
                "SELECT stage,status,started_at,completed_at,error FROM llm_attempt WHERE cycle_id=? ORDER BY started_at,attempt_number",
                (cycle_id,),
            )]
            events = [dict(row) for row in connection.execute(
                "SELECT event_type,created_at,payload_json FROM companion_outbox WHERE cycle_id=? ORDER BY created_at,event_id",
                (cycle_id,),
            )]
        return {"cycle": dict(cycle), "attempts": attempts, "events": events}

    def _snapshot_by_fingerprint(self, snapshot_kind: str, fingerprint: str) -> ObservatorySnapshot | None:
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT snapshot_kind,payload_json FROM observatory_snapshot WHERE snapshot_kind=? AND source_fingerprint=? ORDER BY created_at,snapshot_id LIMIT 1",
                (snapshot_kind, fingerprint),
            ).fetchone()
        return self._decode_snapshot(row["snapshot_kind"], row["payload_json"]) if row else None

    def _calibrate_forecasts(self, evaluation: EvaluationSnapshot) -> None:
        if evaluation.delivery_state == "incomplete" and _parse(evaluation.observed_at) <= _parse(evaluation.value_window_end):
            return
        with self.store.connection() as connection:
            rows = connection.execute(
                """SELECT s.payload_json FROM observatory_snapshot s
                     LEFT JOIN forecast_calibration_link l ON l.forecast_snapshot_id=s.snapshot_id
                    WHERE s.snapshot_kind='forecast' AND s.cycle_id=? AND l.forecast_snapshot_id IS NULL
                    ORDER BY s.created_at,s.snapshot_id""",
                (evaluation.cycle_id,),
            ).fetchall()
        observed = 1 if evaluation.delivery_state == "qualified" else 0
        for row in rows:
            forecast = json.loads(row["payload_json"])
            probability = forecast.get("qualified_probability")
            low, high = forecast.get("wilson_90_low"), forecast.get("wilson_90_high")
            fingerprint = hashlib.sha256(
                f"{forecast['snapshot_id']}|{evaluation.snapshot_id}|{observed}".encode("utf-8"),
            ).hexdigest()
            calibration = ForecastCalibrationSnapshot(
                snapshot_id=str(uuid.uuid4()), snapshot_kind="forecast_calibration", created_at=_now(),
                source_fingerprint=fingerprint, forecast_snapshot_id=forecast["snapshot_id"],
                evaluation_snapshot_id=evaluation.snapshot_id, cycle_id=evaluation.cycle_id,
                predicted_probability=float(probability) if probability is not None else None,
                observed_qualified=observed,
                probability_hit=((float(probability) >= .5) == bool(observed)) if probability is not None else None,
                interval_covered=(float(low) <= observed <= float(high)) if low is not None and high is not None else None,
                absolute_calibration_error=abs(observed - float(probability)) if probability is not None else None,
            )
            self._append_snapshot(calibration, task_key=evaluation.task_key, cycle_id=evaluation.cycle_id)
            with self.store.connection() as connection:
                connection.execute(
                    "INSERT OR IGNORE INTO forecast_calibration_link(forecast_snapshot_id,calibration_snapshot_id,evaluation_snapshot_id,created_at) VALUES(?,?,?,?)",
                    (forecast["snapshot_id"], calibration.snapshot_id, evaluation.snapshot_id, calibration.created_at),
                )

    @staticmethod
    def _wilson_90(successes: int, sample_size: int) -> tuple[float | None, float | None]:
        if sample_size <= 0:
            return None, None
        z = 1.6448536269514722
        p = successes / sample_size
        z2 = z * z
        denominator = 1.0 + z2 / sample_size
        center = (p + z2 / (2.0 * sample_size)) / denominator
        margin = z * math.sqrt((p * (1.0 - p) / sample_size) + z2 / (4.0 * sample_size * sample_size)) / denominator
        return max(0.0, center - margin), min(1.0, center + margin)

    @staticmethod
    def _empirical_quantile(values: list[int], probability: float) -> int | None:
        if not values:
            return None
        rank = max(1, math.ceil(probability * len(values)))
        return values[rank - 1]

    def assess_experiment(self, request: ExperimentRequest) -> ExperimentAssessment:
        if request.source_kind not in {"live_paired_shadow", "historical_replay", "paired_run", "shadow", "post_promotion_monitoring"}:
            raise ValueError("unsupported experiment evidence source")
        with self.store.connection() as connection:
            rows = [dict(row) for row in connection.execute(
                """SELECT evaluation_id,cell_key,cycle_id,horizon,regime,baseline_score_json,candidate_score_json,
                          state,created_at,'live_paired_shadow' AS recorded_source_kind
                     FROM router_evaluation WHERE cell_key=? AND state='resolved'
                   UNION ALL
                   SELECT evaluation_id,cell_key,cycle_id,horizon,regime,baseline_score_json,candidate_score_json,
                          state,created_at,source_kind AS recorded_source_kind
                     FROM runtime_strategy_evaluation WHERE cell_key=? AND state='resolved'
                   ORDER BY created_at,evaluation_id""",
                (request.experiment_key, request.experiment_key),
            )]
        decoded = [
            (row, json.loads(row["baseline_score_json"]), json.loads(row["candidate_score_json"]))
            for row in rows
        ]

        def compare(name: str, key: str, direction: str) -> DimensionComparison:
            pairs: list[tuple[float, float]] = []
            for _, baseline, candidate in decoded:
                left, right = baseline.get(key), candidate.get(key)
                if isinstance(left, bool):
                    left = float(left)
                if isinstance(right, bool):
                    right = float(right)
                if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                    pairs.append((float(left), float(right)))
            baseline_values = [left for left, _ in pairs]
            candidate_values = [right for _, right in pairs]
            deltas = tuple(right - left for left, right in pairs)
            return DimensionComparison(
                name=name, direction=direction, pair_count=len(pairs),
                baseline_mean=sum(baseline_values) / len(pairs) if pairs else None,
                candidate_mean=sum(candidate_values) / len(pairs) if pairs else None,
                mean_delta=sum(deltas) / len(deltas) if deltas else None,
                deltas=deltas,
            )

        dimensions = {
            "delivery_speed": compare("delivery_speed", "duration_seconds", "lower_is_better"),
            "qualification": compare("qualification", "qualified", "higher_is_better"),
            "research_quality": compare("research_quality", "research_quality", "higher_is_better"),
            "judgment_outcome": compare("judgment_outcome", "value", "higher_is_better"),
            "cost": compare("cost", "cost", "lower_is_better"),
            "stability": compare("stability", "stability", "higher_is_better"),
        }
        expected = len(rows) * len(dimensions)
        observed = sum(dimension.pair_count for dimension in dimensions.values())
        completeness = observed / expected if expected else 0.0
        regimes = tuple(sorted({str(row.get("regime") or "unknown") for row in rows}))
        required_regimes = {"trend_expansion", "divergence", "risk_contraction"}
        regime_coverage = len(required_regimes.intersection(regimes)) / len(required_regimes)
        protected = [dimensions[name] for name in ("qualification", "research_quality", "judgment_outcome", "stability")]
        effective_weight = sum(dimension.pair_count for dimension in dimensions.values()) / len(dimensions)
        # A conservative effective-weight width avoids claiming zero uncertainty
        # merely because a small paired sample happened to have zero variance.
        confidence_width = min(2.0, 1.45 / math.sqrt(effective_weight)) if effective_weight > 0 else None
        tolerances = {"qualification": -.02, "research_quality": -.03, "judgment_outcome": -.03, "stability": -.03}
        protection_stable = all(
            dimension.mean_delta is not None and dimension.mean_delta >= tolerances[dimension.name]
            for dimension in protected
        )
        maturity = EvidenceMaturity(
            mature=bool(
                rows and completeness >= .8 and regime_coverage == 1.0
                and confidence_width is not None and confidence_width <= .52
            ),
            effective_weight=effective_weight,
            confidence_interval_width=confidence_width, data_completeness=completeness,
            market_regime_coverage=regime_coverage, protection_dimensions_stable=protection_stable,
        )
        hard_fault = any(bool(candidate.get("hard_fault")) for _, _, candidate in decoded)
        speed, quality = dimensions["delivery_speed"], dimensions["research_quality"]
        if request.source_kind == "post_promotion_monitoring" and hard_fault:
            decision, reasons = "recommend_rollback", ("post_promotion_hard_fault",)
        elif hard_fault:
            decision, reasons = "reject", ("hard_protection_fault",)
        elif (
            speed.mean_delta is not None and speed.mean_delta < 0
            and quality.mean_delta is not None and quality.mean_delta < tolerances["research_quality"]
        ):
            decision, reasons = "ask_user", ("faster_but_research_quality_declined",)
        elif (
            speed.mean_delta is not None and speed.baseline_mean is not None
            and speed.mean_delta > max(1.0, speed.baseline_mean * .10)
            and quality.mean_delta is not None and quality.mean_delta >= .05
        ):
            decision, reasons = "ask_user", ("research_quality_improved_but_delivery_delayed",)
        elif request.source_kind == "historical_replay":
            decision, reasons = "insufficient_evidence", ("live_paired_shadow_required",)
        elif not maturity.mature:
            missing: list[str] = []
            if completeness < .8:
                missing.append("data_completeness_insufficient")
            if regime_coverage < 1.0:
                missing.append("market_regime_coverage_incomplete")
            if confidence_width is None or confidence_width > .52:
                missing.append("confidence_interval_too_wide")
            if not protection_stable:
                missing.append("protection_dimensions_not_stable")
            decision, reasons = "insufficient_evidence", tuple(missing or ["evidence_not_mature"])
        elif not protection_stable:
            decision, reasons = "reject", ("protection_dimension_inferior",)
        else:
            material = any((
                speed.mean_delta is not None and speed.baseline_mean is not None and speed.mean_delta <= -max(1.0, speed.baseline_mean * .10),
                dimensions["research_quality"].mean_delta is not None and dimensions["research_quality"].mean_delta >= .05,
                dimensions["judgment_outcome"].mean_delta is not None and dimensions["judgment_outcome"].mean_delta >= .05,
                dimensions["qualification"].mean_delta is not None and dimensions["qualification"].mean_delta >= .03,
                dimensions["cost"].mean_delta is not None and dimensions["cost"].baseline_mean is not None and dimensions["cost"].mean_delta <= -max(.01, dimensions["cost"].baseline_mean * .10),
                dimensions["stability"].mean_delta is not None and dimensions["stability"].mean_delta >= .02,
            ))
            if material:
                decision, reasons = "recommend_promotion", ("material_improvement", "all_protection_dimensions_noninferior")
            else:
                decision, reasons = "insufficient_evidence", ("no_material_improvement",)
        fingerprint = hashlib.sha256(json.dumps({
            "request": asdict(request),
            "rows": [(row["evaluation_id"], row.get("resolved_at")) for row in rows],
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        scope_key = f"{request.experiment_key}|{request.source_kind}"
        existing = self._request_snapshot(request.request_id, "experiment", scope_key, fingerprint)
        if existing is not None:
            if not isinstance(existing, ExperimentAssessment):
                raise ValueError("request id resolved to an incompatible snapshot")
            return existing
        assessment = ExperimentAssessment(
            snapshot_id=str(uuid.uuid4()), snapshot_kind="experiment", created_at=_now(),
            source_fingerprint=fingerprint, experiment_key=request.experiment_key,
            source_kind=request.source_kind, paired_runs=len(rows),
            delivery_speed=dimensions["delivery_speed"], qualification=dimensions["qualification"],
            research_quality=dimensions["research_quality"], judgment_outcome=dimensions["judgment_outcome"],
            cost=dimensions["cost"], stability=dimensions["stability"],
            data_completeness=completeness, market_regimes=regimes,
            evidence_maturity=maturity, decision=decision, decision_reasons=reasons,
        )
        task_key = request.experiment_key.split(":", 1)[1] if ":" in request.experiment_key else None
        stored = self._append_snapshot(
            assessment, task_key=task_key, cycle_id=None,
            request_id=request.request_id, scope_key=scope_key,
        )
        if not isinstance(stored, ExperimentAssessment):
            raise ValueError("request id resolved to an incompatible snapshot")
        return stored

    def _request_snapshot(
        self, request_id: str | None, snapshot_kind: str, scope_key: str, input_fingerprint: str,
    ) -> ObservatorySnapshot | None:
        if request_id is None:
            return None
        with self.store.connection() as connection:
            row = connection.execute("SELECT * FROM observatory_request WHERE request_id=?", (request_id,)).fetchone()
        if not row:
            return None
        if row["snapshot_kind"] != snapshot_kind or row["scope_key"] != scope_key or row["input_fingerprint"] != input_fingerprint:
            raise ValueError(f"request id conflict: {request_id}")
        return self.get_snapshot(row["snapshot_id"])

    def _append_snapshot(
        self, snapshot: ObservatorySnapshot, *, task_key: str | None, cycle_id: str | None,
        request_id: str | None = None, scope_key: str | None = None,
    ) -> ObservatorySnapshot:
        payload = json.dumps(asdict(snapshot), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        appended = True
        with self.store.connection() as connection:
            if request_id is not None:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT * FROM observatory_request WHERE request_id=?", (request_id,),
                ).fetchone()
                if existing:
                    expected_scope = scope_key or ""
                    if (
                        existing["snapshot_kind"] != snapshot.snapshot_kind
                        or existing["scope_key"] != expected_scope
                        or existing["input_fingerprint"] != snapshot.source_fingerprint
                    ):
                        raise ValueError(f"request id conflict: {request_id}")
                    row = connection.execute(
                        "SELECT snapshot_kind,payload_json FROM observatory_snapshot WHERE snapshot_id=?",
                        (existing["snapshot_id"],),
                    ).fetchone()
                    if row is None:
                        raise ValueError("idempotency record references a missing snapshot")
                    stored = self._decode_snapshot(row["snapshot_kind"], row["payload_json"])
                    appended = False
                else:
                    stored = snapshot
            else:
                stored = snapshot
            if appended:
                connection.execute(
                    "INSERT INTO observatory_snapshot(snapshot_id,snapshot_kind,task_key,cycle_id,source_fingerprint,payload_json,created_at) VALUES(?,?,?,?,?,?,?)",
                    (snapshot.snapshot_id, snapshot.snapshot_kind, task_key, cycle_id, snapshot.source_fingerprint, payload, snapshot.created_at),
                )
                if request_id is not None:
                    connection.execute(
                        "INSERT INTO observatory_request(request_id,snapshot_kind,scope_key,input_fingerprint,snapshot_id,created_at) VALUES(?,?,?,?,?,?)",
                        (request_id, snapshot.snapshot_kind, scope_key or "", snapshot.source_fingerprint, snapshot.snapshot_id, snapshot.created_at),
                    )
        if appended and self.exchange is not None:
            self.exchange.send("to-client", f"observatory-{snapshot.snapshot_id}", {
                "contract": "evaluation-observatory-snapshot/v1",
                "contract_version": 1,
                "message_id": f"observatory-{snapshot.snapshot_id}",
                "snapshot": asdict(snapshot),
                "created_at": snapshot.created_at,
            })
        return stored
