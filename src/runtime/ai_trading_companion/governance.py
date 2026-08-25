from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from typing import Any


REGIMES = ("trend_expansion", "divergence", "risk_contraction")


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
            base_score = executable_value(baseline_snapshot, observations)
            candidate_score = executable_value(candidate, observations)
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
        verdict = self.promotion_verdict(cell_key)
        if verdict["action"] == "promote":
            self.store.set_router_policy_mode(cell_key, "promoted", verdict["fingerprint"])
        elif verdict["action"] == "reject":
            self.store.set_router_policy_mode(cell_key, "rolled_back", verdict["fingerprint"])
        return verdict

    def immediate_rollback(self, cell_key: str, reason: str) -> dict[str, Any]:
        # Security, deadline and data-isolation faults are never averaged away.
        if reason not in {"security", "deadline", "m1_blindness", "data_isolation"}:
            raise ValueError("only hard safety faults support immediate rollback")
        return self.store.set_router_policy_mode(cell_key, "rolled_back", f"hard_fault:{reason}")
