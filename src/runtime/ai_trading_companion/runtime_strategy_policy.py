"""Reversible runtime controls owned by governance, never by the Observatory."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any


POLICY_KINDS = frozenset({"stage_budget", "search_breadth", "source_mix"})


@dataclass(frozen=True)
class RuntimeStrategyControls:
    timeout_seconds: int
    max_operations: int
    enabled_backends: tuple[str, ...]
    revisions: tuple[tuple[str, int], ...]


class RuntimeStrategyPolicy:
    """Deep module for the three governance-authorized reversible controls."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self.store.connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS runtime_strategy_cell (
                  cell_key TEXT PRIMARY KEY,
                  policy_kind TEXT NOT NULL,
                  mode TEXT NOT NULL,
                  baseline_json TEXT NOT NULL,
                  candidate_json TEXT,
                  automatic_authorized INTEGER NOT NULL DEFAULT 0,
                  revision INTEGER NOT NULL,
                  previous_json TEXT,
                  qualification_fingerprint TEXT,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime_strategy_evaluation (
                  evaluation_id TEXT PRIMARY KEY,
                  cell_key TEXT NOT NULL REFERENCES runtime_strategy_cell(cell_key),
                  cycle_id TEXT NOT NULL,
                  horizon TEXT NOT NULL,
                  regime TEXT,
                  baseline_score_json TEXT NOT NULL,
                  candidate_score_json TEXT NOT NULL,
                  source_kind TEXT NOT NULL,
                  state TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  resolved_at TEXT,
                  UNIQUE(cycle_id,horizon,cell_key)
                );
                CREATE TABLE IF NOT EXISTS runtime_strategy_shadow_job (
                  job_id TEXT PRIMARY KEY,
                  cell_key TEXT NOT NULL REFERENCES runtime_strategy_cell(cell_key),
                  cycle_id TEXT NOT NULL,
                  stage TEXT NOT NULL,
                  packet_json TEXT NOT NULL,
                  schema_name TEXT NOT NULL,
                  baseline_attempt_id TEXT NOT NULL,
                  state TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  started_at TEXT,
                  completed_at TEXT,
                  candidate_attempt_id TEXT,
                  error TEXT,
                  UNIQUE(cell_key,cycle_id,stage,baseline_attempt_id)
                );
            """)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(runtime_strategy_cell)")}
            if "automatic_authorized" not in columns:
                connection.execute("ALTER TABLE runtime_strategy_cell ADD COLUMN automatic_authorized INTEGER NOT NULL DEFAULT 0")

    @staticmethod
    def cell_key(policy_kind: str, stage: str) -> str:
        if policy_kind not in POLICY_KINDS:
            raise ValueError("unsupported runtime strategy policy kind")
        return f"{policy_kind}:{stage}"

    def register_shadow_candidate(
        self, policy_kind: str, stage: str, baseline: dict[str, Any], candidate: dict[str, Any], *,
        automatic_authorized: bool = False,
    ) -> dict[str, Any]:
        """Provision a versioned shadow cell; callers cannot promote it here."""
        self._validate(policy_kind, baseline)
        self._validate(policy_kind, candidate)
        key = self.cell_key(policy_kind, stage)
        from .store import now
        with self.store.connection() as connection:
            existing = connection.execute("SELECT * FROM runtime_strategy_cell WHERE cell_key=?", (key,)).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO runtime_strategy_cell(
                         cell_key,policy_kind,mode,baseline_json,candidate_json,automatic_authorized,revision,updated_at)
                       VALUES(?,?,'shadow',?,?,?,1,?)""",
                    (key, policy_kind, json.dumps(baseline, sort_keys=True), json.dumps(candidate, sort_keys=True), int(automatic_authorized), now()),
                )
            elif existing["mode"] == "promoted":
                raise ValueError("register a new candidate only after rollback or an explicit replacement")
            else:
                connection.execute(
                    """UPDATE runtime_strategy_cell SET baseline_json=?,candidate_json=?,revision=revision+1,
                         previous_json=?,automatic_authorized=?,updated_at=? WHERE cell_key=?""",
                    (json.dumps(baseline, sort_keys=True), json.dumps(candidate, sort_keys=True),
                     json.dumps(dict(existing), sort_keys=True), int(automatic_authorized), now(), key),
                )
            row = connection.execute("SELECT * FROM runtime_strategy_cell WHERE cell_key=?", (key,)).fetchone()
        return dict(row)

    def controls(self, stage: str, *, timeout_seconds: int, search: bool) -> RuntimeStrategyControls:
        """Read current controls without creating rows or silently changing policy."""
        defaults = {
            "stage_budget": {"timeout_seconds": max(1, int(timeout_seconds))},
            "search_breadth": {"max_operations": 24 if search else 0},
            "source_mix": {"enabled_backends": ["gateway", "market"] if search else []},
        }
        values, revisions = dict(defaults), []
        with self.store.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM runtime_strategy_cell WHERE cell_key IN (?,?,?)",
                tuple(self.cell_key(kind, stage) for kind in ("stage_budget", "search_breadth", "source_mix")),
            ).fetchall()
        for row in rows:
            decoded = json.loads(row["candidate_json"] if row["mode"] == "promoted" and row["candidate_json"] else row["baseline_json"])
            values[row["policy_kind"]] = decoded
            revisions.append((row["policy_kind"], int(row["revision"])))
        enabled = tuple(str(item) for item in values["source_mix"]["enabled_backends"])
        return RuntimeStrategyControls(
            timeout_seconds=min(max(1, int(timeout_seconds)), int(values["stage_budget"]["timeout_seconds"])),
            max_operations=max(0, min(24, int(values["search_breadth"]["max_operations"]))),
            enabled_backends=enabled if search else (), revisions=tuple(sorted(revisions)),
        )

    def shadow_controls(self, cell_key: str, stage: str, *, timeout_seconds: int, search: bool) -> RuntimeStrategyControls:
        """Return one shadow candidate's controls over the active baselines."""
        controls = self.controls(stage, timeout_seconds=timeout_seconds, search=search)
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_strategy_cell WHERE cell_key=? AND mode='shadow'", (cell_key,),
            ).fetchone()
        if not row or not row["candidate_json"]:
            raise ValueError("runtime strategy shadow candidate is unavailable")
        value = json.loads(row["candidate_json"])
        revisions = tuple(sorted([*controls.revisions, (str(row["policy_kind"]), int(row["revision"]))]))
        if row["policy_kind"] == "stage_budget":
            return RuntimeStrategyControls(
                timeout_seconds=min(max(1, int(timeout_seconds)), int(value["timeout_seconds"])),
                max_operations=controls.max_operations, enabled_backends=controls.enabled_backends, revisions=revisions,
            )
        if row["policy_kind"] == "search_breadth":
            return RuntimeStrategyControls(
                timeout_seconds=controls.timeout_seconds, max_operations=max(0, min(24, int(value["max_operations"]))),
                enabled_backends=controls.enabled_backends, revisions=revisions,
            )
        return RuntimeStrategyControls(
            timeout_seconds=controls.timeout_seconds, max_operations=controls.max_operations,
            enabled_backends=tuple(str(item) for item in value["enabled_backends"]) if search else (), revisions=revisions,
        )

    def queue_shadows(
        self, cycle_id: str, stage: str, packet: dict[str, Any], schema_name: str, baseline_attempt_id: str,
    ) -> tuple[str, ...]:
        """Append eligible shadow jobs; official work never waits for them."""
        from .store import now
        serialized = json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.store.connection() as connection:
            cells = connection.execute(
                """SELECT cell_key FROM runtime_strategy_cell
                     WHERE cell_key IN (?,?,?) AND mode='shadow' AND candidate_json IS NOT NULL
                     ORDER BY policy_kind""",
                tuple(self.cell_key(kind, stage) for kind in ("stage_budget", "search_breadth", "source_mix")),
            ).fetchall()
            job_ids: list[str] = []
            for cell in cells:
                job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"runtime-shadow|{cell['cell_key']}|{cycle_id}|{stage}|{baseline_attempt_id}"))
                connection.execute(
                    """INSERT OR IGNORE INTO runtime_strategy_shadow_job(
                         job_id,cell_key,cycle_id,stage,packet_json,schema_name,baseline_attempt_id,state,created_at)
                       VALUES(?,?,?,?,?,?,?,'queued',?)""",
                    (job_id, cell["cell_key"], cycle_id, stage, serialized, schema_name, baseline_attempt_id, now()),
                )
                job_ids.append(job_id)
        return tuple(job_ids)

    def next_shadow(self) -> dict[str, Any] | None:
        from .store import now
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_strategy_shadow_job WHERE state='queued' ORDER BY created_at,job_id LIMIT 1",
            ).fetchone()
            if not row:
                return None
            connection.execute(
                "UPDATE runtime_strategy_shadow_job SET state='running',started_at=? WHERE job_id=?",
                (now(), row["job_id"]),
            )
            result = dict(row)
            result["state"] = "running"
            return result

    def finish_shadow(self, job_id: str, *, candidate_attempt_id: str | None = None, error: str | None = None) -> None:
        from .store import now
        with self.store.connection() as connection:
            connection.execute(
                """UPDATE runtime_strategy_shadow_job SET state=?,completed_at=?,candidate_attempt_id=?,error=?
                   WHERE job_id=? AND state='running'""",
                ("succeeded" if error is None else "failed", now(), candidate_attempt_id,
                 error[-2000:] if error else None, job_id),
            )

    def record_evaluation(
        self, cell_key: str, cycle_id: str, horizon: str, regime: str | None,
        baseline_score: dict[str, Any], candidate_score: dict[str, Any], *,
        source_kind: str = "live_paired_shadow", state: str = "resolved",
    ) -> Any | None:
        if source_kind not in {"live_paired_shadow", "historical_replay", "post_promotion_monitoring"}:
            raise ValueError("unsupported runtime strategy evidence source")
        from .store import now
        with self.store.connection() as connection:
            if not connection.execute("SELECT 1 FROM runtime_strategy_cell WHERE cell_key=?", (cell_key,)).fetchone():
                raise ValueError("unknown runtime strategy cell")
            at = now()
            connection.execute(
                """INSERT INTO runtime_strategy_evaluation(
                     evaluation_id,cell_key,cycle_id,horizon,regime,baseline_score_json,candidate_score_json,
                     source_kind,state,created_at,resolved_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(cycle_id,horizon,cell_key) DO UPDATE SET
                     baseline_score_json=excluded.baseline_score_json,candidate_score_json=excluded.candidate_score_json,
                     source_kind=excluded.source_kind,state=excluded.state,resolved_at=excluded.resolved_at""",
                (str(uuid.uuid4()), cell_key, cycle_id, horizon, regime,
                 json.dumps(baseline_score, sort_keys=True), json.dumps(candidate_score, sort_keys=True),
                 source_kind, state, at, at if state == "resolved" else None),
            )
            policy_state = connection.execute(
                "SELECT automatic_authorized,mode FROM runtime_strategy_cell WHERE cell_key=?", (cell_key,),
            ).fetchone()
            authorized = bool(policy_state["automatic_authorized"])
            current_mode = str(policy_state["mode"])
        if not authorized:
            return None
        # The authorization is stored with the versioned cell. Assessment still
        # decides whether evidence is mature; this hook merely executes an
        # already-authorized recommendation through the normal receipt chain.
        from .governance import EvolutionGovernance, StrategyPolicyExecutor
        from .observatory import EvaluationObservatory, ExperimentRequest
        assessment = EvaluationObservatory(self.store).assess_experiment(ExperimentRequest(
            cell_key, source_kind=source_kind,
        ))
        if assessment.decision not in {"recommend_promotion", "recommend_rollback"}:
            return None
        if assessment.decision == "recommend_promotion" and current_mode == "promoted":
            return None
        if assessment.decision == "recommend_rollback" and current_mode == "rolled_back":
            return None
        decision = EvolutionGovernance(self.store).decide(
            assessment.snapshot_id, "approve", approver="automatic-governance",
        )
        return StrategyPolicyExecutor(self.store).apply(decision.decision_id)

    @staticmethod
    def _validate(policy_kind: str, value: dict[str, Any]) -> None:
        if policy_kind not in POLICY_KINDS or not isinstance(value, dict):
            raise ValueError("invalid runtime strategy policy")
        if policy_kind == "stage_budget" and set(value) == {"timeout_seconds"} and isinstance(value["timeout_seconds"], int) and value["timeout_seconds"] > 0:
            return
        if policy_kind == "search_breadth" and set(value) == {"max_operations"} and isinstance(value["max_operations"], int) and 0 <= value["max_operations"] <= 24:
            return
        if policy_kind == "source_mix" and set(value) == {"enabled_backends"} and isinstance(value["enabled_backends"], list) and set(value["enabled_backends"]).issubset({"gateway", "market"}):
            return
        raise ValueError("invalid runtime strategy value")
