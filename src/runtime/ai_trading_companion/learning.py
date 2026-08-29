from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .store import now


SHANGHAI = ZoneInfo("Asia/Shanghai")
HORIZONS = {"T+1": 1, "T+3": 3, "T+5": 5}


def _next_weekdays(start: datetime, count: int) -> datetime:
    value = start
    remaining = count
    while remaining:
        value += timedelta(days=1)
        if value.weekday() < 5:
            remaining -= 1
    return value


def _claims(text: str) -> list[str]:
    return [item.strip() for item in re.split(r"[\r\n]+|(?<=[。！？])", text) if item.strip()]


def heuristic_snapshot(text: str, *, reference_at: str, qualified: bool = True) -> dict[str, Any]:
    """Freeze only claims that can be read deterministically; unknown fields stay unknown."""
    lowered = text.lower()
    direction = "unknown"
    directional_terms = (
        ("bullish", ("看多", "转强", "上涨", "走强", "突破", "反弹")),
        ("bearish", ("看空", "转弱", "下跌", "回落", "冲高回落", "跌破")),
        ("avoid", ("不交易", "不买", "回避", "空仓")),
        ("neutral", ("观望", "中性")),
    )
    last_position = -1
    for candidate, terms in directional_terms:
        for term in terms:
            position = lowered.rfind(term)
            if position > last_position:
                last_position = position
                direction = candidate
    horizons = re.findall(r"(?:未来|接下来)?\s*(\d+)\s*(天|日|周|月)|短线|中线|长线|盘中|明天|次日", text)
    horizon = None
    if horizons:
        horizon = re.search(r"(?:未来|接下来)?\s*\d+\s*(?:天|日|周|月)|短线|中线|长线|盘中|明天|次日", text).group(0)
    subjects = list(dict.fromkeys(re.findall(r"(?<!\d)\d{6}(?!\d)", text)))
    claim_by_key: dict[tuple[tuple[str, ...], str | None], dict[str, Any]] = {}
    for claim_text in _claims(text):
        claim_direction = "unknown"
        claim_position = -1
        for candidate, terms in directional_terms:
            for term in terms:
                position = claim_text.lower().rfind(term)
                if position > claim_position:
                    claim_position = position
                    claim_direction = candidate
        if claim_direction == "unknown":
            continue
        claim_subjects = list(dict.fromkeys(re.findall(r"(?<!\d)\d{6}(?!\d)", claim_text)))
        key = (tuple(claim_subjects) or ("__market__",), horizon)
        claim_by_key[key] = {
            "subjects": claim_subjects,
            "direction": claim_direction,
            "horizon": horizon,
            "triggers": [],
            "invalidations": [],
            "confidence": None,
            "benchmark": None,
            "original_text": claim_text,
        }
    return {
        "subjects": subjects,
        "direction": direction,
        "horizon": horizon,
        "reference_at": reference_at,
        "triggers": [],
        "invalidations": [],
        "confidence": None,
        "benchmark": None,
        "qualified": bool(qualified),
        "original_claims": _claims(text),
        "claims": list(claim_by_key.values()),
    }


def normalize_snapshot(value: dict[str, Any] | None, text: str, *, reference_at: str, qualified: bool = True) -> dict[str, Any]:
    base = heuristic_snapshot(text, reference_at=reference_at, qualified=qualified)
    if not isinstance(value, dict):
        return base
    for key in ("subjects", "direction", "horizon", "reference_at", "triggers", "invalidations", "confidence", "benchmark", "qualified", "original_claims", "claims"):
        if key in value:
            base[key] = value[key]
    if base["direction"] not in {"bullish", "bearish", "neutral", "avoid", "unqualified", "unknown"}:
        base["direction"] = "unknown"
    base["subjects"] = [str(item) for item in base.get("subjects") or []]
    base["triggers"] = [str(item) for item in base.get("triggers") or []]
    base["invalidations"] = [str(item) for item in base.get("invalidations") or []]
    base["original_claims"] = [str(item) for item in base.get("original_claims") or _claims(text)]
    base["claims"] = [item for item in base.get("claims") or [] if isinstance(item, dict)]
    base["qualified"] = bool(qualified)
    return base


class JudgmentLifecycle:
    """Deep module for immutable judgment snapshots and their future checkpoints."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def capture(
        self,
        artifact: dict[str, Any],
        kind: str,
        text: str,
        *,
        snapshot: dict[str, Any] | None = None,
        qualified: bool = True,
        reference_at: str | None = None,
        connection: Any | None = None,
    ) -> dict[str, Any]:
        as_of = reference_at or artifact.get("as_of") or artifact.get("sealed_at") or now()
        frozen = normalize_snapshot(snapshot, text, reference_at=as_of, qualified=qualified)
        cycle = self.store.get_cycle(artifact["cycle_id"], connection=connection)
        # The outcome evaluator must quote the conclusion that was actually
        # published, rather than reconstruct it from a later interpretation.
        frozen["original_judgment_text"] = text
        attempts = self.store.attempts(artifact["cycle_id"])
        judgment_attempt = next((item for item in reversed(attempts) if item.get("stage") == "m1_judgment"), None)
        if judgment_attempt and judgment_attempt.get("effort_policy_version"):
            frozen["strategy_policy_version"] = judgment_attempt["effort_policy_version"]
        default_horizon = {
                "daily.opportunity.0900": "开盘至09:45，并跟踪T+1/T+3/T+5",
                "daily.execution.0945": "09:45至10:30，并跟踪T+1/T+3/T+5",
                "daily.execution.1030": "10:30至14:30，并跟踪T+1/T+3/T+5",
                "daily.execution.1430": "收盘至下一交易日，并跟踪T+1/T+3/T+5",
                "daily.review.1520": "未来1至5个交易日",
            }.get(cycle["task_key"], "声明周期及T+1/T+3/T+5")
        if frozen.get("horizon") is None:
            frozen["horizon"] = default_horizon
        if frozen.get("benchmark") is None:
            frozen["benchmark"] = "同期A股主要宽基及相关行业或题材（系统默认）"
        for claim in frozen["claims"]:
            if claim.get("horizon") is None:
                claim["horizon"] = default_horizon
            if claim.get("benchmark") is None:
                claim["benchmark"] = frozen["benchmark"]
        row = self.store.save_judgment_snapshot(artifact["artifact_id"], artifact["cycle_id"], kind, frozen, as_of, connection=connection)
        reference = datetime.fromisoformat(as_of.replace("Z", "+00:00")).astimezone(SHANGHAI)
        for horizon, weekdays in HORIZONS.items():
            due_day = _next_weekdays(reference, weekdays)
            due = datetime.combine(due_day.date(), time(16, 10), SHANGHAI)
            self.store.schedule_outcome(row["snapshot_id"], horizon, due.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z"), connection=connection)
        return row

    def record_outcome(self, checkpoint: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        cycle_id = checkpoint["cycle_id"]
        as_of = str(result.get("as_of") or now())
        summary = str(result.get("summary") or "本次结果数据不足，暂不结案。")
        artifact = self.store.append_artifact(
            cycle_id,
            "outcome",
            "model",
            summary,
            as_of,
            {
                "checkpoint_id": checkpoint["checkpoint_id"],
                "snapshot_id": checkpoint["snapshot_id"],
                "horizon": checkpoint["horizon"],
                "verification_status": result.get("verification_status", "unverified"),
                "memory_tags": ["outcome", str(result.get("verification_status", "unverified"))],
            },
            known_at=as_of,
        )
        self.store.complete_outcome(checkpoint["checkpoint_id"], as_of, result, artifact["artifact_id"])
        return artifact

    def backfill(self) -> int:
        with self.store.connection() as connection:
            rows = [dict(row) for row in connection.execute(
                """SELECT a.* FROM narrative_artifact a
                   LEFT JOIN judgment_snapshot s ON s.artifact_id=a.artifact_id
                   WHERE a.kind IN ('h0','m1','m2','judgment_revision') AND s.artifact_id IS NULL
                   ORDER BY a.sealed_at"""
            )]
        for artifact in rows:
            self.capture(
                artifact, artifact["kind"], artifact["body_markdown"],
                qualified=artifact["kind"] != "m1" or "不应判断" not in artifact["body_markdown"],
                reference_at=artifact["as_of"],
            )
        return len(rows)


DEFAULT_RESEARCH_POLICY: dict[str, Any] = {
    "extra_categories": [],
    "extra_standing_questions": [],
    "extra_counterevidence_questions": [],
    "method_hypotheses": [],
}


class WorkflowEvolution:
    """Stores reviewable proposals and applies only an allow-listed research-policy patch."""

    ALLOWED_CATEGORIES = {"workflow_efficiency", "search_coverage", "investment_method"}
    PATCH_KEYS = {"add_categories", "add_standing_questions", "add_counterevidence_questions", "add_method_hypotheses"}

    def __init__(self, store: Any) -> None:
        self.store = store

    def active_policy(self) -> dict[str, Any]:
        stored = self.store.workflow_policy("research")
        return dict(stored["policy"]) if stored else dict(DEFAULT_RESEARCH_POLICY)

    def propose(self, cycle_id: str, proposal: dict[str, Any], *, source_artifact_id: str | None = None) -> dict[str, Any]:
        category = str(proposal.get("category") or "")
        if category not in self.ALLOWED_CATEGORIES:
            raise ValueError("unsupported workflow proposal category")
        patch = proposal.get("policy_patch")
        if not isinstance(patch, dict) or set(patch) - self.PATCH_KEYS:
            raise ValueError("workflow proposal contains unsupported policy fields")
        clean_patch = {key: [str(item).strip() for item in patch.get(key) or [] if str(item).strip()][:12] for key in self.PATCH_KEYS}
        proposal_id = str(uuid.uuid4())
        payload = {
            "title": str(proposal.get("title") or "工作流改进提案"),
            "problem": str(proposal.get("problem") or ""),
            "change": str(proposal.get("change") or ""),
            "policy_patch": clean_patch,
            "source_artifact_id": source_artifact_id,
        }
        evidence = [str(item) for item in proposal.get("evidence") or []]
        with self.store.connection() as c:
            existing_rows = [dict(row) for row in c.execute(
                """SELECT * FROM knowledge_change_proposal
                   WHERE category=? AND state NOT IN ('applied','rejected') ORDER BY created_at""",
                (category,),
            )]
        for existing in existing_rows:
            current_payload = json.loads(existing["changeset_json"])
            if current_payload.get("title") == payload["title"]:
                prior = json.loads(existing.get("evidence_json") or "[]")
                combined = list(dict.fromkeys([*prior, *evidence]))
                for key in self.PATCH_KEYS:
                    current_payload["policy_patch"][key] = list(dict.fromkeys([
                        *(current_payload["policy_patch"].get(key) or []), *(payload["policy_patch"].get(key) or []),
                    ]))[:12]
                state = "awaiting_approval" if category != "investment_method" or len(combined) >= 3 else "pending_validation"
                with self.store.connection() as c:
                    c.execute(
                        """UPDATE knowledge_change_proposal SET changeset_json=?,evidence_json=?,state=?,validation_json=?
                           WHERE proposal_id=?""",
                        (json.dumps(current_payload, ensure_ascii=False, sort_keys=True), json.dumps(combined, ensure_ascii=False), state,
                         json.dumps({"independent_evidence_count": len(combined), "minimum_independent_evidence": 3 if category == "investment_method" else 1}, ensure_ascii=False),
                         existing["proposal_id"]),
                    )
                return self.get(existing["proposal_id"])
        state = "pending_validation" if category == "investment_method" and len(set(evidence)) < 3 else "awaiting_approval"
        at = now()
        with self.store.connection() as c:
            c.execute(
                """INSERT INTO knowledge_change_proposal(
                     proposal_id,cycle_id,policy,changeset_json,state,created_at,applied_at,error,
                     category,evidence_json,validation_json,requires_approval,approved_at,decision_note)
                   VALUES(?,?,?,?,?,?,NULL,NULL,?,?,?,1,NULL,NULL)""",
                (proposal_id, cycle_id, "research", json.dumps(payload, ensure_ascii=False, sort_keys=True),
                 state, at, category, json.dumps(evidence, ensure_ascii=False),
                 json.dumps({"minimum_independent_evidence": 3 if category == "investment_method" else 1}, ensure_ascii=False)),
            )
        return self.get(proposal_id)

    def get(self, proposal_id: str) -> dict[str, Any]:
        with self.store.connection() as c:
            row = c.execute("SELECT * FROM knowledge_change_proposal WHERE proposal_id=?", (proposal_id,)).fetchone()
        if not row:
            raise ValueError("unknown workflow proposal")
        return dict(row)

    def pending(self, cycle_id: str | None = None) -> list[dict[str, Any]]:
        with self.store.connection() as c:
            if cycle_id:
                rows = c.execute(
                    "SELECT * FROM knowledge_change_proposal WHERE cycle_id=? AND state NOT IN ('applied','rejected') ORDER BY created_at",
                    (cycle_id,),
                )
            else:
                rows = c.execute("SELECT * FROM knowledge_change_proposal WHERE state NOT IN ('applied','rejected') ORDER BY created_at")
            return [dict(row) for row in rows]

    def decide(self, proposal_id: str, approved: bool, *, note: str = "") -> dict[str, Any]:
        proposal = self.get(proposal_id)
        if not approved:
            with self.store.connection() as c:
                c.execute("UPDATE knowledge_change_proposal SET state='rejected',decision_note=? WHERE proposal_id=?", (note, proposal_id))
            return self.get(proposal_id)
        if proposal["state"] == "pending_validation":
            raise ValueError("investment-method proposal lacks repeated historical evidence")
        payload = json.loads(proposal["changeset_json"])
        current = self.active_policy()
        if self.store.workflow_policy("research") is None:
            self.store.save_workflow_policy("research", current)
        patch = payload["policy_patch"]
        mapping = {
            "add_categories": "extra_categories",
            "add_standing_questions": "extra_standing_questions",
            "add_counterevidence_questions": "extra_counterevidence_questions",
            "add_method_hypotheses": "method_hypotheses",
        }
        for source, target in mapping.items():
            current[target] = list(dict.fromkeys([*(current.get(target) or []), *(patch.get(source) or [])]))[:40]
        saved = self.store.save_workflow_policy("research", current)
        at = now()
        with self.store.connection() as c:
            c.execute(
                """UPDATE knowledge_change_proposal SET state='applied',approved_at=?,applied_at=?,decision_note=?
                   WHERE proposal_id=?""",
                (at, at, note, proposal_id),
            )
        result = self.get(proposal_id)
        result["active_policy"] = saved
        return result

    def rollback(self) -> dict[str, Any] | None:
        return self.store.rollback_workflow_policy("research")
