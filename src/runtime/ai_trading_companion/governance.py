from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .runtime_strategy_policy import RuntimeStrategyPolicy


REGIMES = ("trend_expansion", "divergence", "risk_contraction")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class GovernanceDecision:
    decision_id: str
    decision_version: int
    evidence_snapshot_id: str
    cell_key: str
    recommendation: str
    state: str
    approver: str
    target_policy_version: str
    evaluation_profile: str
    applicable_scope_json: str
    protected_dimensions_json: str
    created_at: str


@dataclass(frozen=True)
class StrategyApplicationReceipt:
    receipt_id: str
    decision_id: str
    evidence_snapshot_id: str
    target_policy_version: str
    cell_key: str
    previous_mode: str
    applied_mode: str
    state: str
    applied_at: str


@dataclass(frozen=True)
class ActiveResearchPolicyReceipt:
    receipt_id: str
    decision_id: str
    evidence_snapshot_id: str
    cell_key: str
    old_policy_version: str
    new_policy_version: str
    rollback_target_version: str
    applicable_scope_json: str
    result: str
    applied_at: str


def classify_regime(metrics: dict[str, Any]) -> str:
    """Frozen, explainable regime classifier; unknown is never used for promotion."""
    trend = metrics.get("index_trend")
    breadth = metrics.get("breadth")
    turnover = metrics.get("turnover_change")
    volatility = metrics.get("volatility")
    if not all(isinstance(value, (int, float)) for value in (trend, breadth, turnover, volatility)):
        return "unknown"
    if trend > 0 and breadth >= 0.55 and turnover >= 0:
        return "trend_expansion"
    if volatility >= 0.7 and breadth <= 0.40:
        return "risk_contraction"
    return "divergence"


def executable_value(snapshot: dict[str, Any], observations: list[dict[str, Any]]) -> dict[str, Any]:
    """Score only against frozen public outcome observations, never an LLM verdict."""
    values = [item.get("excess_return") for item in observations if isinstance(item, dict) and isinstance(item.get("excess_return"), (int, float))]
    if not values:
        return {"value": None, "reason": "public_outcome_missing"}
    move = sum(values) / len(values)
    direction = str(snapshot.get("direction") or "unknown")
    if direction == "bullish": value = 1.0 if move > 0 else 0.0
    elif direction == "bearish": value = 1.0 if move < 0 else 0.0
    elif direction in {"neutral", "avoid"}: value = 1.0 if abs(move) < 0.015 else 0.5
    else: value = 0.0
    execution = 1.0 if snapshot.get("triggers") and snapshot.get("invalidations") else 0.0
    return {"value": value, "direction_value": value, "execution_boundary": execution, "mean_excess_return": move}


def _attempt_dimensions(attempt: dict[str, Any] | None) -> dict[str, Any]:
    if not attempt:
        return {}
    verifier = json.loads(attempt.get("verifier_json") or "{}")
    passed = attempt.get("status") == "succeeded" and bool(verifier.get("passed"))
    gate = verifier.get("evidence_gate") if isinstance(verifier.get("evidence_gate"), dict) else None
    problems = list((gate or verifier).get("problems") or [])
    quality = (1.0 if passed else 0.0) if gate is None else max(0.0, (1.0 if gate.get("passed") else .5) - .1 * len(problems))
    dimensions: dict[str, Any] = {
        "qualified": passed,
        "research_quality": quality,
        "stability": 1.0 if passed else 0.0,
    }
    if attempt.get("duration_ms") is not None:
        dimensions["duration_seconds"] = max(0.0, float(attempt["duration_ms"]) / 1000.0)
    if attempt.get("broker_cost_estimate") is not None:
        dimensions["cost"] = float(attempt["broker_cost_estimate"])
    quality_gate = gate or verifier
    normalized = quality_gate.get("normalized_evidence") if isinstance(quality_gate.get("normalized_evidence"), dict) else {}
    gap_states = quality_gate.get("gap_states") or normalized.get("research_gaps") or []
    blocking_gaps = [item for item in gap_states if isinstance(item, dict) and item.get("blocking", True)]
    if blocking_gaps:
        closed = sum(
            str(item.get("coverage_state") or "") in {"complete", "covered", "checked_no_change"}
            for item in blocking_gaps
        )
        dimensions["gap_closure_rate"] = closed / len(blocking_gaps)
    sources = [item for item in normalized.get("sources") or [] if isinstance(item, dict)]
    if sources:
        dimensions["citation_verifiability_rate"] = sum(
            bool(item.get("evidence_ref") and (item.get("canonical_url") or item.get("url")) and item.get("fact_as_of"))
            for item in sources
        ) / len(sources)
        groups = {
            str(item.get("independence_group") or item.get("original_source") or item.get("source_identity") or "")
            for item in sources
            if item.get("independence_group") or item.get("original_source") or item.get("source_identity")
        }
        dimensions["independent_source_coverage"] = min(1.0, len(groups) / 2.0)
    false_gap_markers = ("checked_no_change", "false_complete", "wrong_denial", "untraceable")
    dimensions["false_gap_declaration_rate"] = 1.0 if any(
        marker in str(problem) for problem in problems for marker in false_gap_markers
    ) else 0.0
    accuracy_markers = ("future", "date", "numeric", "unit", "currency", "fact_time")
    dimensions["numeric_date_accuracy_rate"] = 0.0 if any(
        marker in str(problem) for problem in problems for marker in accuracy_markers
    ) else 1.0
    try:
        trace = json.loads(attempt.get("tool_trace_json") or "[]")
    except json.JSONDecodeError:
        trace = []
    write_operations = {"write", "post", "submit", "trade", "upload", "send_message"}
    safety_faults = sum(
        bool(item.get("prompt_injection_succeeded"))
        or str(item.get("operation") or item.get("tool") or "").casefold() in write_operations
        for item in trace if isinstance(item, dict)
    )
    dimensions["safety_faults"] = float(safety_faults)
    dimensions["hard_fault"] = bool(safety_faults or dimensions["false_gap_declaration_rate"] > 0)
    dimensions["qualified_in_window"] = passed
    try:
        packet = json.loads(attempt.get("input_packet_json") or "{}")
        window_end = packet.get("value_window_end")
        if passed and window_end and attempt.get("completed_at"):
            dimensions["qualified_in_window"] = datetime.fromisoformat(
                str(attempt["completed_at"]).replace("Z", "+00:00"),
            ) <= datetime.fromisoformat(str(window_end).replace("Z", "+00:00"))
    except (TypeError, ValueError, json.JSONDecodeError):
        dimensions["qualified_in_window"] = False
    return dimensions


class RouterGovernance:
    """Evaluates candidate routes. It has no permission to change prompts, tools or budgets."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def evaluate_outcome(self, cycle_id: str, horizon: str, observations: list[dict[str, Any]], baseline_snapshot: dict[str, Any], baseline_artifact_id: str | None) -> int:
        with self.store.connection() as c:
            jobs = [dict(row) for row in c.execute(
                """SELECT j.*,d.cell_key FROM router_shadow_job j JOIN cognitive_route_decision d ON d.decision_id=j.decision_id
                   WHERE j.cycle_id=? AND j.stage='m1_judgment' AND j.state='succeeded'""", (cycle_id,)
            )]
            regime_row = c.execute("SELECT regime FROM market_regime_snapshot WHERE cycle_id=?", (cycle_id,)).fetchone()
        regime = regime_row["regime"] if regime_row else "unknown"
        written = 0
        for job in jobs:
            output = json.loads(job["output_json"])
            candidate = output.get("snapshot") if isinstance(output.get("snapshot"), dict) else {}
            with self.store.connection() as c:
                attempts = [dict(row) for row in c.execute(
                    """SELECT * FROM llm_attempt WHERE route_decision_id=? AND stage=?
                         ORDER BY started_at,attempt_number""",
                    (job["decision_id"], job["stage"]),
                )]
            baseline_attempt = next((attempt for attempt in reversed(attempts) if not attempt.get("is_shadow")), None)
            candidate_attempt = next((attempt for attempt in reversed(attempts) if attempt.get("is_shadow")), None)
            base_score = {**executable_value(baseline_snapshot, observations), **_attempt_dimensions(baseline_attempt)}
            candidate_score = {**executable_value(candidate, observations), **_attempt_dimensions(candidate_attempt)}
            if base_score["value"] is None or candidate_score["value"] is None:
                state = "deferred"
            else:
                state = "resolved"
            self.store.record_router_evaluation(job["cell_key"], cycle_id, horizon, regime, baseline_artifact_id, job["job_id"], base_score, candidate_score, state)
            written += 1
        return written

    def promotion_verdict(self, cell_key: str, *, material_uplift: float = 0.10, alpha: float = 0.10, fdr: float = 0.10) -> dict[str, Any]:
        rows = [row for row in self.store.router_evaluations(cell_key) if row["state"] == "resolved"]
        regime_counts = Counter(row["regime"] for row in rows)
        deltas = []
        for row in rows:
            base = json.loads(row["baseline_score_json"]).get("value")
            candidate = json.loads(row["candidate_score_json"]).get("value")
            if isinstance(base, (int, float)) and isinstance(candidate, (int, float)):
                deltas.append(float(candidate) - float(base))
        if not deltas:
            return {"action": "continue_shadow", "reason": "尚无可核验的配对结果", "pairs": 0}
        # An anytime-valid Hoeffding confidence sequence replaces a fixed N and
        # a permanent traffic split.  Scores are bounded in [-1,1].  Spending
        # alpha/(n(n+1)) remains valid under repeated peeking; fdr is exposed
        # for the experiment coordinator to allocate across active cells.
        n=len(deltas); mean=sum(deltas)/n; spent=max(1e-12, alpha*fdr/(n*(n+1)))
        radius=math.sqrt(math.log(1/spent)/(2*n))
        lower=mean-radius; upper=mean+radius
        coverage={regime:regime_counts[regime] for regime in REGIMES}
        fingerprint=hashlib.sha256(json.dumps({"rows":[(r["evaluation_id"],r["resolved_at"]) for r in rows],"lower":lower,"upper":upper,"uplift":material_uplift},sort_keys=True).encode()).hexdigest()
        if lower >= material_uplift:
            return {"action":"promote","reason":"候选已通过顺序配对证据门","pairs":n,"mean_delta":mean,"lower_bound":lower,"upper_bound":upper,"regime_coverage":coverage,"fingerprint":fingerprint}
        if upper < 0:
            return {"action":"reject","reason":"候选在顺序配对证据中已无正向空间","pairs":n,"mean_delta":mean,"lower_bound":lower,"upper_bound":upper,"regime_coverage":coverage,"fingerprint":fingerprint}
        return {"action":"continue_shadow","reason":"当前证据尚不能区分材料性提升与噪声","pairs":n,"mean_delta":mean,"lower_bound":lower,"upper_bound":upper,"regime_coverage":coverage}

    def promote_if_qualified(self, cell_key: str) -> dict[str, Any]:
        """Compatibility read: return evidence only; governance applies any change."""
        verdict = self.promotion_verdict(cell_key)
        return verdict

    def immediate_rollback(self, cell_key: str, reason: str) -> dict[str, Any]:
        # Compatibility read: hard faults become recommendations and still
        # require a versioned governance decision plus executor receipt.
        if reason not in {"security", "deadline", "m1_blindness", "data_isolation"}:
            raise ValueError("only hard safety faults support immediate rollback")
        return {"action": "recommend_rollback", "cell_key": cell_key, "reason": f"hard_fault:{reason}"}

    def record_effort_capability_fault(
        self, decision_id: str, cycle_id: str, fault_id: str,
    ) -> str:
        """Project a Broker capability rejection as a hard protection fault."""
        with self.store.connection() as connection:
            decision = connection.execute(
                "SELECT cell_key FROM cognitive_route_decision WHERE decision_id=?", (decision_id,),
            ).fetchone()
            if not decision:
                raise ValueError("unknown cognitive route decision")
            regime_row = connection.execute(
                "SELECT regime FROM market_regime_snapshot WHERE cycle_id=?", (cycle_id,),
            ).fetchone()
            baseline_attempt = connection.execute(
                """SELECT a.* FROM llm_attempt a
                     JOIN cognitive_route_decision d ON d.decision_id=a.route_decision_id
                    WHERE d.cell_key=? AND a.is_shadow=0 AND a.status='succeeded'
                    ORDER BY a.completed_at DESC,a.started_at DESC LIMIT 1""",
                (decision["cell_key"],),
            ).fetchone()
        baseline = _attempt_dimensions(dict(baseline_attempt) if baseline_attempt else None)
        candidate = {
            "qualified": False,
            "research_quality": 0.0,
            "stability": 0.0,
            "hard_fault": True,
            "fault": "broker_effort_unsupported",
        }
        self.store.record_router_evaluation(
            decision["cell_key"], cycle_id, "effort_capability", regime_row["regime"] if regime_row else "unknown",
            None, fault_id, baseline, candidate, "resolved", source_kind="post_promotion_monitoring",
        )
        return str(decision["cell_key"])


class EvolutionGovernance:
    """Own immutable approve/reject decisions; never applies strategy state."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self._ensure_schema()
        RuntimeStrategyPolicy(store)

    def _ensure_schema(self) -> None:
        with self.store.connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS evolution_governance_decision (
                  decision_id TEXT PRIMARY KEY,
                  decision_version INTEGER NOT NULL,
                  evidence_snapshot_id TEXT NOT NULL,
                  cell_key TEXT NOT NULL,
                  recommendation TEXT NOT NULL,
                  state TEXT NOT NULL,
                  approver TEXT NOT NULL,
                  target_policy_version TEXT NOT NULL,
                  evaluation_profile TEXT NOT NULL DEFAULT 'generic/v1',
                  applicable_scope_json TEXT NOT NULL DEFAULT '[]',
                  protected_dimensions_json TEXT NOT NULL DEFAULT '[]',
                  created_at TEXT NOT NULL,
                  UNIQUE(evidence_snapshot_id,state,approver)
                );
                CREATE TABLE IF NOT EXISTS strategy_application_receipt (
                  receipt_id TEXT PRIMARY KEY,
                  decision_id TEXT NOT NULL UNIQUE,
                  evidence_snapshot_id TEXT NOT NULL,
                  target_policy_version TEXT NOT NULL,
                  cell_key TEXT NOT NULL,
                  previous_mode TEXT NOT NULL,
                  applied_mode TEXT NOT NULL,
                  state TEXT NOT NULL,
                  applied_at TEXT NOT NULL
                );
            """)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(evolution_governance_decision)")}
            for name, declaration in {
                "evaluation_profile": "TEXT NOT NULL DEFAULT 'generic/v1'",
                "applicable_scope_json": "TEXT NOT NULL DEFAULT '[]'",
                "protected_dimensions_json": "TEXT NOT NULL DEFAULT '[]'",
            }.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE evolution_governance_decision ADD COLUMN {name} {declaration}")

    def decide(self, evidence_snapshot_id: str, action: str, *, approver: str) -> GovernanceDecision:
        if action not in {"approve", "reject"}:
            raise ValueError("governance action must be approve or reject")
        if not approver.strip():
            raise ValueError("governance decision requires an approver")
        with self.store.connection() as connection:
            snapshot = connection.execute(
                "SELECT snapshot_kind,payload_json FROM observatory_snapshot WHERE snapshot_id=?",
                (evidence_snapshot_id,),
            ).fetchone()
            if not snapshot or snapshot["snapshot_kind"] != "experiment":
                raise ValueError("governance evidence must be an experiment assessment")
            payload = json.loads(snapshot["payload_json"])
            recommendation = str(payload.get("decision") or "insufficient_evidence")
            if action == "approve" and recommendation not in {"recommend_promotion", "recommend_rollback", "ask_user"}:
                raise ValueError("assessment does not authorize an approval")
            if action == "approve" and recommendation == "ask_user" and approver == "automatic-governance":
                raise ValueError("tradeoff decisions require an explicit user approver")
            if action == "approve" and approver == "automatic-governance" and payload.get("source_kind") != "live_paired_shadow" and recommendation != "recommend_rollback":
                raise ValueError("automatic promotion requires live paired shadow evidence")
            runtime_cell = connection.execute(
                """SELECT policy_kind,mode,revision,evaluation_profile,applicable_tasks_json
                     FROM runtime_strategy_cell WHERE cell_key=?""",
                (payload["experiment_key"],),
            ).fetchone()
            if runtime_cell:
                target_policy_version = f"runtime-strategy/{runtime_cell['policy_kind']}/v{runtime_cell['revision']}"
                evaluation_profile = str(runtime_cell["evaluation_profile"])
                applicable_scope_json = str(runtime_cell["applicable_tasks_json"] or "[]")
            else:
                policy = connection.execute(
                    "SELECT policy_version FROM cognitive_effort_policy WHERE state='active' ORDER BY activated_at DESC LIMIT 1",
                ).fetchone()
                if not policy:
                    raise ValueError("no active cognitive effort policy")
                target_policy_version = policy["policy_version"]
                evaluation_profile = "generic/v1"
                applicable_scope_json = "[]"
            protected_dimensions = ["qualification", "research_quality", "stability"]
            if evaluation_profile == "active_evidence_research/v1":
                protected_dimensions.extend([
                    "false_gap_declaration", "citation_verifiability",
                    "numeric_date_accuracy", "safety_faults",
                ])
                if action == "approve" and recommendation == "recommend_promotion":
                    maturity = payload.get("evidence_maturity") or {}
                    replay_gate = payload.get("historical_replay_gate") or {}
                    safety = payload.get("safety_faults") or {}
                    if (
                        runtime_cell["mode"] != "shadow"
                        or payload.get("source_kind") != "live_paired_shadow"
                        or not maturity.get("mature")
                        or not maturity.get("protection_dimensions_stable")
                        or not replay_gate.get("passed")
                        or float(safety.get("candidate_mean") or 0) != 0
                    ):
                        raise ValueError("active research promotion evidence is not mature and protected")
                if action == "approve" and recommendation == "recommend_rollback" and (
                    runtime_cell["mode"] != "promoted"
                    or payload.get("source_kind") != "post_promotion_monitoring"
                ):
                    raise ValueError("active research rollback requires a promoted policy and post-promotion evidence")
            protected_dimensions_json = json.dumps(protected_dimensions, sort_keys=True)
            state = "approved" if action == "approve" else "rejected"
            decision_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"governance|{evidence_snapshot_id}|{state}|{approver}"))
            at = _now()
            connection.execute(
                """INSERT OR IGNORE INTO evolution_governance_decision(
                     decision_id,decision_version,evidence_snapshot_id,cell_key,recommendation,state,
                     approver,target_policy_version,evaluation_profile,applicable_scope_json,
                     protected_dimensions_json,created_at) VALUES(?,1,?,?,?,?,?,?,?,?,?,?)""",
                (decision_id, evidence_snapshot_id, payload["experiment_key"], recommendation,
                 state, approver, target_policy_version, evaluation_profile, applicable_scope_json,
                 protected_dimensions_json, at),
            )
            row = connection.execute(
                "SELECT * FROM evolution_governance_decision WHERE decision_id=?", (decision_id,),
            ).fetchone()
        return GovernanceDecision(**dict(row))


class StrategyPolicyExecutor:
    """Apply an approved reversible strategy decision and append its receipt."""

    def __init__(self, store: Any) -> None:
        self.store = store
        EvolutionGovernance(store)
        RuntimeStrategyPolicy(store)

    def apply(self, decision_id: str) -> StrategyApplicationReceipt:
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM strategy_application_receipt WHERE decision_id=?", (decision_id,),
            ).fetchone()
            if existing:
                return StrategyApplicationReceipt(**dict(existing))
            decision = connection.execute(
                "SELECT * FROM evolution_governance_decision WHERE decision_id=?", (decision_id,),
            ).fetchone()
            if not decision:
                raise ValueError("unknown governance decision")
            if decision["state"] != "approved":
                raise ValueError("only approved governance decisions can be applied")
            recommendation = decision["recommendation"]
            if recommendation == "recommend_promotion":
                target_mode, receipt_state = "promoted", "applied"
            elif recommendation == "recommend_rollback":
                target_mode, receipt_state = "rolled_back", "rollback_applied"
            elif recommendation == "ask_user":
                target_mode, receipt_state = "promoted", "user_tradeoff_applied"
            else:
                raise ValueError("governance recommendation is not executable")
            cell = connection.execute(
                "SELECT * FROM router_policy_cell WHERE cell_key=?", (decision["cell_key"],),
            ).fetchone()
            table = "router_policy_cell"
            if not cell:
                cell = connection.execute(
                    "SELECT * FROM runtime_strategy_cell WHERE cell_key=?", (decision["cell_key"],),
                ).fetchone()
                table = "runtime_strategy_cell"
            if not cell:
                raise ValueError("unknown reversible strategy cell")
            if table == "runtime_strategy_cell" and cell["evaluation_profile"] == "active_evidence_research/v1":
                raise ValueError("active research changes require the dedicated active research executor")
            previous_mode = str(cell["mode"])
            applied_at = _now()
            connection.execute(
                f"""UPDATE {table} SET previous_json=?,mode=?,revision=revision+1,
                     qualification_fingerprint=?,updated_at=? WHERE cell_key=?""",
                (json.dumps(dict(cell), ensure_ascii=False, sort_keys=True), target_mode,
                 f"assessment:{decision['evidence_snapshot_id']}", applied_at, decision["cell_key"]),
            )
            receipt = StrategyApplicationReceipt(
                receipt_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"strategy-receipt|{decision_id}")),
                decision_id=decision_id, evidence_snapshot_id=decision["evidence_snapshot_id"],
                target_policy_version=decision["target_policy_version"], cell_key=decision["cell_key"],
                previous_mode=previous_mode, applied_mode=target_mode, state=receipt_state, applied_at=applied_at,
            )
            connection.execute(
                """INSERT INTO strategy_application_receipt(
                     receipt_id,decision_id,evidence_snapshot_id,target_policy_version,cell_key,
                     previous_mode,applied_mode,state,applied_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                tuple(receipt.__dict__.values()),
            )
        return receipt


class ActiveResearchPolicyExecutor:
    """Apply only mature active-research decisions with immutable version and rollback receipts."""

    def __init__(self, store: Any) -> None:
        self.store = store
        EvolutionGovernance(store)
        with self.store.connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS active_research_policy_version (
                  version_id TEXT PRIMARY KEY,
                  cell_key TEXT NOT NULL,
                  revision INTEGER NOT NULL,
                  policy_json TEXT NOT NULL,
                  state TEXT NOT NULL,
                  previous_version_id TEXT,
                  rollback_target_version_id TEXT,
                  applicable_scope_json TEXT NOT NULL,
                  evidence_snapshot_id TEXT,
                  decision_id TEXT,
                  created_at TEXT NOT NULL,
                  UNIQUE(cell_key,revision)
                );
                CREATE TABLE IF NOT EXISTS active_research_policy_receipt (
                  receipt_id TEXT PRIMARY KEY,
                  decision_id TEXT NOT NULL UNIQUE,
                  evidence_snapshot_id TEXT NOT NULL,
                  cell_key TEXT NOT NULL,
                  old_policy_version TEXT NOT NULL,
                  new_policy_version TEXT NOT NULL,
                  rollback_target_version TEXT NOT NULL,
                  applicable_scope_json TEXT NOT NULL,
                  result TEXT NOT NULL,
                  applied_at TEXT NOT NULL
                );
            """)

    def apply(self, decision_id: str) -> ActiveResearchPolicyReceipt:
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM active_research_policy_receipt WHERE decision_id=?", (decision_id,),
            ).fetchone()
            if existing:
                return ActiveResearchPolicyReceipt(**dict(existing))
            decision = connection.execute(
                "SELECT * FROM evolution_governance_decision WHERE decision_id=?", (decision_id,),
            ).fetchone()
            if not decision or decision["state"] != "approved":
                raise ValueError("active research execution requires an approved governance decision")
            if decision["evaluation_profile"] != "active_evidence_research/v1":
                raise ValueError("decision does not target active evidence research")
            cell = connection.execute(
                "SELECT * FROM runtime_strategy_cell WHERE cell_key=?", (decision["cell_key"],),
            ).fetchone()
            if not cell or cell["evaluation_profile"] != "active_evidence_research/v1":
                raise ValueError("active research policy cell is unavailable")
            snapshot = connection.execute(
                "SELECT payload_json FROM observatory_snapshot WHERE snapshot_id=?",
                (decision["evidence_snapshot_id"],),
            ).fetchone()
            if not snapshot:
                raise ValueError("active research evidence snapshot is unavailable")
            payload = json.loads(snapshot["payload_json"])
            self._validate_snapshot(payload, str(decision["recommendation"]))
            if decision["recommendation"] == "recommend_promotion" and cell["mode"] != "shadow":
                raise ValueError("active research promotion requires a shadow policy cell")
            if decision["recommendation"] == "recommend_rollback" and cell["mode"] != "promoted":
                raise ValueError("active research rollback requires a promoted policy cell")
            scope_json = str(decision["applicable_scope_json"])
            latest = connection.execute(
                """SELECT * FROM active_research_policy_version WHERE cell_key=? AND state='active'
                   ORDER BY revision DESC LIMIT 1""",
                (decision["cell_key"],),
            ).fetchone()
            at = _now()
            if latest is None:
                baseline_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"active-research|{decision['cell_key']}|baseline"))
                connection.execute(
                    """INSERT OR IGNORE INTO active_research_policy_version(
                         version_id,cell_key,revision,policy_json,state,previous_version_id,
                         rollback_target_version_id,applicable_scope_json,evidence_snapshot_id,decision_id,created_at)
                       VALUES(?,?,1,?,'active',NULL,?,?,NULL,NULL,?)""",
                    (baseline_id, decision["cell_key"], cell["baseline_json"], baseline_id, scope_json, at),
                )
                latest = connection.execute(
                    "SELECT * FROM active_research_policy_version WHERE version_id=?", (baseline_id,),
                ).fetchone()
            old_version = str(latest["version_id"])
            recommendation = str(decision["recommendation"])
            if recommendation == "recommend_promotion":
                policy_json = str(cell["candidate_json"] or "")
                if not policy_json:
                    raise ValueError("active research candidate policy is missing")
                result = "applied"
                target_mode = "promoted"
                rollback_target = str(latest["rollback_target_version_id"] or old_version)
            elif recommendation == "recommend_rollback":
                rollback_target = str(latest["rollback_target_version_id"] or "")
                target = connection.execute(
                    "SELECT * FROM active_research_policy_version WHERE version_id=?", (rollback_target,),
                ).fetchone()
                if not target:
                    raise ValueError("active research rollback target is unavailable")
                policy_json = str(target["policy_json"])
                result = "rollback_applied"
                target_mode = "rolled_back"
            else:
                raise ValueError("active research recommendation is not executable")
            revision = int(latest["revision"]) + 1
            new_version = str(uuid.uuid5(
                uuid.NAMESPACE_URL, f"active-research|{decision['cell_key']}|v{revision}|{decision_id}",
            ))
            connection.execute(
                "UPDATE active_research_policy_version SET state='superseded' WHERE version_id=?",
                (old_version,),
            )
            connection.execute(
                """INSERT INTO active_research_policy_version(
                     version_id,cell_key,revision,policy_json,state,previous_version_id,
                     rollback_target_version_id,applicable_scope_json,evidence_snapshot_id,decision_id,created_at)
                   VALUES(?,?,?,?,'active',?,?,?,?,?,?)""",
                (new_version, decision["cell_key"], revision, policy_json, old_version,
                 rollback_target, scope_json, decision["evidence_snapshot_id"], decision_id, at),
            )
            connection.execute(
                """UPDATE runtime_strategy_cell SET previous_json=?,mode=?,revision=revision+1,
                     qualification_fingerprint=?,updated_at=? WHERE cell_key=?""",
                (json.dumps(dict(cell), ensure_ascii=False, sort_keys=True), target_mode,
                 f"assessment:{decision['evidence_snapshot_id']}", at, decision["cell_key"]),
            )
            receipt = ActiveResearchPolicyReceipt(
                receipt_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"active-research-receipt|{decision_id}")),
                decision_id=decision_id, evidence_snapshot_id=decision["evidence_snapshot_id"],
                cell_key=decision["cell_key"], old_policy_version=old_version,
                new_policy_version=new_version, rollback_target_version=rollback_target,
                applicable_scope_json=scope_json, result=result, applied_at=at,
            )
            connection.execute(
                """INSERT INTO active_research_policy_receipt(
                     receipt_id,decision_id,evidence_snapshot_id,cell_key,old_policy_version,
                     new_policy_version,rollback_target_version,applicable_scope_json,result,applied_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                tuple(receipt.__dict__.values()),
            )
        return receipt

    @staticmethod
    def _validate_snapshot(payload: dict[str, Any], recommendation: str) -> None:
        if payload.get("evaluation_profile") != "active_evidence_research/v1":
            raise ValueError("snapshot is not an active research assessment")
        if recommendation == "recommend_promotion":
            maturity = payload.get("evidence_maturity") or {}
            replay = payload.get("historical_replay_gate") or {}
            safety = payload.get("safety_faults") or {}
            if (
                payload.get("source_kind") != "live_paired_shadow"
                or payload.get("decision") != "recommend_promotion"
                or not maturity.get("mature")
                or not maturity.get("protection_dimensions_stable")
                or not replay.get("passed")
                or float(safety.get("candidate_mean") or 0) != 0
            ):
                raise ValueError("specified active research snapshot is not promotable")
        elif recommendation == "recommend_rollback":
            if (
                payload.get("source_kind") != "post_promotion_monitoring"
                or payload.get("decision") != "recommend_rollback"
            ):
                raise ValueError("specified active research snapshot does not authorize rollback")
