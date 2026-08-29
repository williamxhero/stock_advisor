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
            None, fault_id, baseline, candidate, "resolved",
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
                "SELECT policy_kind,revision FROM runtime_strategy_cell WHERE cell_key=?",
                (payload["experiment_key"],),
            ).fetchone()
            if runtime_cell:
                target_policy_version = f"runtime-strategy/{runtime_cell['policy_kind']}/v{runtime_cell['revision']}"
            else:
                policy = connection.execute(
                    "SELECT policy_version FROM cognitive_effort_policy WHERE state='active' ORDER BY activated_at DESC LIMIT 1",
                ).fetchone()
                if not policy:
                    raise ValueError("no active cognitive effort policy")
                target_policy_version = policy["policy_version"]
            state = "approved" if action == "approve" else "rejected"
            decision_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"governance|{evidence_snapshot_id}|{state}|{approver}"))
            at = _now()
            connection.execute(
                """INSERT OR IGNORE INTO evolution_governance_decision(
                     decision_id,decision_version,evidence_snapshot_id,cell_key,recommendation,state,
                     approver,target_policy_version,created_at) VALUES(?,1,?,?,?,?,?,?,?)""",
                (decision_id, evidence_snapshot_id, payload["experiment_key"], recommendation,
                 state, approver, target_policy_version, at),
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
