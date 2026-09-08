"""Reversible runtime controls owned by governance, never by the Observatory."""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any


POLICY_KINDS = frozenset({"stage_budget", "search_breadth", "source_mix"})
ACTIVE_RESEARCH_SCOPE = (
    "daily.opportunity.0900",
    "daily.execution.0945",
    "daily.execution.1030",
    "daily.execution.1430",
    "daily.review.1520",
    "manual.non_trading_outlook",
    "portfolio.holdings",
)


@dataclass(frozen=True)
class RuntimeStrategyControls:
    timeout_seconds: int
    max_operations: int
    enabled_backends: tuple[str, ...]
    revisions: tuple[tuple[str, int], ...]
    market_understanding_enabled: bool = False


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
                  evaluation_profile TEXT NOT NULL DEFAULT 'generic/v1',
                  applicable_tasks_json TEXT NOT NULL DEFAULT '[]',
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
                  context_fingerprint TEXT,
                  frozen_as_of TEXT,
                  value_window_end TEXT,
                  error TEXT,
                  UNIQUE(cell_key,cycle_id,stage,baseline_attempt_id)
                );
            """)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(runtime_strategy_cell)")}
            if "automatic_authorized" not in columns:
                connection.execute("ALTER TABLE runtime_strategy_cell ADD COLUMN automatic_authorized INTEGER NOT NULL DEFAULT 0")
            if "evaluation_profile" not in columns:
                connection.execute("ALTER TABLE runtime_strategy_cell ADD COLUMN evaluation_profile TEXT NOT NULL DEFAULT 'generic/v1'")
            if "applicable_tasks_json" not in columns:
                connection.execute("ALTER TABLE runtime_strategy_cell ADD COLUMN applicable_tasks_json TEXT NOT NULL DEFAULT '[]'")
            shadow_columns = {row[1] for row in connection.execute("PRAGMA table_info(runtime_strategy_shadow_job)")}
            for name in ("context_fingerprint", "frozen_as_of", "value_window_end"):
                if name not in shadow_columns:
                    connection.execute(f"ALTER TABLE runtime_strategy_shadow_job ADD COLUMN {name} TEXT")

    @staticmethod
    def cell_key(policy_kind: str, stage: str) -> str:
        if policy_kind not in POLICY_KINDS:
            raise ValueError("unsupported runtime strategy policy kind")
        return f"{policy_kind}:{stage}"

    def register_shadow_candidate(
        self, policy_kind: str, stage: str, baseline: dict[str, Any], candidate: dict[str, Any], *,
        automatic_authorized: bool = False, evaluation_profile: str = "generic/v1",
        applicable_tasks: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """Provision a versioned shadow cell; callers cannot promote it here."""
        self._validate(policy_kind, baseline)
        self._validate(policy_kind, candidate)
        if evaluation_profile not in {"generic/v1", "active_evidence_research/v1"}:
            raise ValueError("unsupported evaluation profile")
        if evaluation_profile == "active_evidence_research/v1" and automatic_authorized:
            raise ValueError("active research candidates require the dedicated production promotion gate")
        scope = tuple(dict.fromkeys(applicable_tasks or (
            ACTIVE_RESEARCH_SCOPE if evaluation_profile == "active_evidence_research/v1" else ()
        )))
        if evaluation_profile == "active_evidence_research/v1" and set(scope) != set(ACTIVE_RESEARCH_SCOPE):
            raise ValueError("active research candidate scope must cover every formal market task, weekend, and holdings")
        key = self.cell_key(policy_kind, stage)
        from .store import now
        with self.store.connection() as connection:
            existing = connection.execute("SELECT * FROM runtime_strategy_cell WHERE cell_key=?", (key,)).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO runtime_strategy_cell(
                         cell_key,policy_kind,mode,baseline_json,candidate_json,automatic_authorized,
                         evaluation_profile,applicable_tasks_json,revision,updated_at)
                       VALUES(?,?,'shadow',?,?,?,?,?,1,?)""",
                    (key, policy_kind, json.dumps(baseline, sort_keys=True), json.dumps(candidate, sort_keys=True),
                     int(automatic_authorized), evaluation_profile, json.dumps(scope), now()),
                )
            elif existing["mode"] == "promoted":
                raise ValueError("register a new candidate only after rollback or an explicit replacement")
            else:
                connection.execute(
                    """UPDATE runtime_strategy_cell SET baseline_json=?,candidate_json=?,revision=revision+1,
                         previous_json=?,automatic_authorized=?,evaluation_profile=?,applicable_tasks_json=?,updated_at=? WHERE cell_key=?""",
                    (json.dumps(baseline, sort_keys=True), json.dumps(candidate, sort_keys=True),
                     json.dumps(dict(existing), sort_keys=True), int(automatic_authorized), evaluation_profile,
                     json.dumps(scope), now(), key),
                )
            row = connection.execute("SELECT * FROM runtime_strategy_cell WHERE cell_key=?", (key,)).fetchone()
        return dict(row)

    def provision_market_understanding_candidate(self) -> dict[str, Any] | None:
        """Create the single approved candidate definition without rewriting an operator's cell.

        Its only treatment is the candidate-only evidence contract.  The research
        backends and packet cutoff remain exactly the baseline's, so a paired run
        can attribute a quality delta to market understanding rather than a data
        source change.
        """
        baseline = {"enabled_backends": ["gateway", "market"], "market_understanding_enabled": False}
        candidate = {"enabled_backends": ["gateway", "market"], "market_understanding_enabled": True}
        key = self.cell_key("source_mix", "m0_research")
        with self.store.connection() as connection:
            existing = connection.execute("SELECT * FROM runtime_strategy_cell WHERE cell_key=?", (key,)).fetchone()
        if existing:
            if (
                existing["mode"] == "shadow"
                and json.loads(existing["baseline_json"]) == baseline
                and json.loads(existing["candidate_json"] or "null") == candidate
                and existing["evaluation_profile"] == "active_evidence_research/v1"
                and set(json.loads(existing["applicable_tasks_json"] or "[]")) == set(ACTIVE_RESEARCH_SCOPE)
            ):
                return dict(existing)
            return None
        return self.register_shadow_candidate(
            "source_mix", "m0_research", baseline, candidate,
            evaluation_profile="active_evidence_research/v1",
        )

    def controls(
        self, stage: str, *, timeout_seconds: int, search: bool, task_key: str | None = None,
    ) -> RuntimeStrategyControls:
        """Read current controls without creating rows or silently changing policy."""
        defaults = {
            "stage_budget": {"timeout_seconds": max(1, int(timeout_seconds))},
            "search_breadth": {"max_operations": 24 if search else 0},
            "source_mix": {
                "enabled_backends": ["gateway", "market"] if search else [],
                "market_understanding_enabled": False,
            },
        }
        values, revisions = dict(defaults), []
        with self.store.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM runtime_strategy_cell WHERE cell_key IN (?,?,?)",
                tuple(self.cell_key(kind, stage) for kind in ("stage_budget", "search_breadth", "source_mix")),
            ).fetchall()
        for row in rows:
            candidate_is_in_scope = True
            if row["evaluation_profile"] == "active_evidence_research/v1":
                scope = set(json.loads(row["applicable_tasks_json"] or "[]"))
                candidate_is_in_scope = bool(task_key and task_key in scope)
            decoded = json.loads(
                row["candidate_json"]
                if row["mode"] == "promoted" and row["candidate_json"] and candidate_is_in_scope
                else row["baseline_json"]
            )
            values[row["policy_kind"]] = decoded
            revisions.append((row["policy_kind"], int(row["revision"])))
        enabled = tuple(str(item) for item in values["source_mix"]["enabled_backends"])
        return RuntimeStrategyControls(
            timeout_seconds=min(max(1, int(timeout_seconds)), int(values["stage_budget"]["timeout_seconds"])),
            max_operations=max(0, min(24, int(values["search_breadth"]["max_operations"]))),
            enabled_backends=enabled if search else (), revisions=tuple(sorted(revisions)),
            market_understanding_enabled=bool(values["source_mix"].get("market_understanding_enabled")),
        )

    def shadow_controls(
        self, cell_key: str, stage: str, *, timeout_seconds: int, search: bool,
        task_key: str | None = None,
    ) -> RuntimeStrategyControls:
        """Return one shadow candidate's controls over the active baselines."""
        controls = self.controls(stage, timeout_seconds=timeout_seconds, search=search, task_key=task_key)
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
                market_understanding_enabled=controls.market_understanding_enabled,
            )
        if row["policy_kind"] == "search_breadth":
            return RuntimeStrategyControls(
                timeout_seconds=controls.timeout_seconds, max_operations=max(0, min(24, int(value["max_operations"]))),
                enabled_backends=controls.enabled_backends, revisions=revisions,
                market_understanding_enabled=controls.market_understanding_enabled,
            )
        return RuntimeStrategyControls(
            timeout_seconds=controls.timeout_seconds, max_operations=controls.max_operations,
            enabled_backends=tuple(str(item) for item in value["enabled_backends"]) if search else (), revisions=revisions,
            market_understanding_enabled=bool(value.get("market_understanding_enabled")),
        )

    def queue_shadows(
        self, cycle_id: str, stage: str, packet: dict[str, Any], schema_name: str, baseline_attempt_id: str,
    ) -> tuple[str, ...]:
        """Append eligible shadow jobs; official work never waits for them."""
        from .store import now
        serialized = json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        context_fingerprint = self._context_fingerprint(packet)
        frozen_as_of = str(packet.get("as_of") or "") or None
        value_window_end = str(packet.get("value_window_end") or "") or None
        with self.store.connection() as connection:
            cycle = connection.execute(
                "SELECT task_key,scheduled_for,m1_publish_deadline FROM companion_cycle WHERE cycle_id=?",
                (cycle_id,),
            ).fetchone()
            if value_window_end is None and cycle:
                if stage == "m0_research" and cycle["task_key"] == "daily.execution.0945":
                    scheduled = self._parse_timestamp(str(cycle["scheduled_for"]))
                    value_window_end = scheduled.replace(hour=10, minute=30, second=0, microsecond=0).isoformat()
                elif stage == "m1_judgment" and cycle["m1_publish_deadline"]:
                    value_window_end = str(cycle["m1_publish_deadline"])
            cells = connection.execute(
                """SELECT cell_key,evaluation_profile,applicable_tasks_json FROM runtime_strategy_cell
                     WHERE cell_key IN (?,?,?) AND mode='shadow' AND candidate_json IS NOT NULL
                     ORDER BY policy_kind""",
                tuple(self.cell_key(kind, stage) for kind in ("stage_budget", "search_breadth", "source_mix")),
            ).fetchall()
            job_ids: list[str] = []
            for cell in cells:
                if cycle and cell["evaluation_profile"] == "active_evidence_research/v1":
                    scope = set(json.loads(cell["applicable_tasks_json"] or "[]"))
                    if cycle["task_key"] not in scope:
                        continue
                job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"runtime-shadow|{cell['cell_key']}|{cycle_id}|{stage}|{baseline_attempt_id}"))
                connection.execute(
                    """INSERT OR IGNORE INTO runtime_strategy_shadow_job(
                         job_id,cell_key,cycle_id,stage,packet_json,schema_name,baseline_attempt_id,state,
                         context_fingerprint,frozen_as_of,value_window_end,created_at)
                       VALUES(?,?,?,?,?,?,?,'queued',?,?,?,?)""",
                    (job_id, cell["cell_key"], cycle_id, stage, serialized, schema_name, baseline_attempt_id,
                     context_fingerprint, frozen_as_of, value_window_end, now()),
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
        _validated_shadow_job_id: str | None = None,
    ) -> Any | None:
        if source_kind not in {"live_paired_shadow", "historical_replay", "post_promotion_monitoring"}:
            raise ValueError("unsupported runtime strategy evidence source")
        from .store import now
        with self.store.connection() as connection:
            cell = connection.execute(
                "SELECT automatic_authorized,mode,evaluation_profile FROM runtime_strategy_cell WHERE cell_key=?",
                (cell_key,),
            ).fetchone()
            if not cell:
                raise ValueError("unknown runtime strategy cell")
            if (
                source_kind == "live_paired_shadow"
                and cell["evaluation_profile"] == "active_evidence_research/v1"
                and not _validated_shadow_job_id
            ):
                raise ValueError("active research live evidence requires a validated shadow job")
            at = now()
            baseline_json = json.dumps(baseline_score, sort_keys=True)
            candidate_json = json.dumps(candidate_score, sort_keys=True)
            existing = connection.execute(
                """SELECT regime,baseline_score_json,candidate_score_json,source_kind,state
                     FROM runtime_strategy_evaluation WHERE cycle_id=? AND horizon=? AND cell_key=?""",
                (cycle_id, horizon, cell_key),
            ).fetchone()
            if existing and (
                str(existing["regime"] or "") != str(regime or "")
                or existing["baseline_score_json"] != baseline_json
                or existing["candidate_score_json"] != candidate_json
                or existing["source_kind"] != source_kind
                or existing["state"] != state
            ):
                raise ValueError("runtime strategy evaluation is immutable")
            connection.execute(
                """INSERT INTO runtime_strategy_evaluation(
                     evaluation_id,cell_key,cycle_id,horizon,regime,baseline_score_json,candidate_score_json,
                     source_kind,state,created_at,resolved_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(cycle_id,horizon,cell_key) DO NOTHING""",
                (str(uuid.uuid4()), cell_key, cycle_id, horizon, regime,
                 baseline_json, candidate_json,
                 source_kind, state, at, at if state == "resolved" else None),
            )
            authorized = bool(cell["automatic_authorized"])
            current_mode = str(cell["mode"])
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

    def record_live_shadow_evaluation(
        self, job_id: str, candidate_attempt_id: str, horizon: str, regime: str | None,
        baseline_score: dict[str, Any], candidate_score: dict[str, Any],
    ) -> Any | None:
        """Accept a live pair only when both attempts share the job's frozen context."""
        with self.store.connection() as connection:
            job = connection.execute(
                """SELECT j.*,c.evaluation_profile,c.updated_at AS candidate_registered_at,
                          cycle.scheduled_for
                     FROM runtime_strategy_shadow_job j
                     JOIN runtime_strategy_cell c ON c.cell_key=j.cell_key
                     JOIN companion_cycle cycle ON cycle.cycle_id=j.cycle_id
                    WHERE j.job_id=?""",
                (job_id,),
            ).fetchone()
            if not job or job["state"] not in {"running", "succeeded"}:
                raise ValueError("live pair requires a running or succeeded shadow job")
            if self._parse_timestamp(str(job["scheduled_for"])) < self._parse_timestamp(str(job["candidate_registered_at"])):
                raise ValueError("live pair must come from a future formal task")
            attempts = [dict(row) for row in connection.execute(
                "SELECT * FROM llm_attempt WHERE attempt_id IN (?,?)",
                (job["baseline_attempt_id"], candidate_attempt_id),
            )]
        by_id = {row["attempt_id"]: row for row in attempts}
        baseline = by_id.get(job["baseline_attempt_id"])
        candidate = by_id.get(candidate_attempt_id)
        if not baseline or not candidate:
            raise ValueError("live pair attempts are incomplete")
        if (
            baseline["cycle_id"] != job["cycle_id"] or candidate["cycle_id"] != job["cycle_id"]
            or bool(baseline["is_shadow"]) or not bool(candidate["is_shadow"])
        ):
            raise ValueError("live pair attempts violate baseline/shadow isolation")
        expected = str(job["context_fingerprint"] or "")
        contexts = []
        for attempt in (baseline, candidate):
            try:
                packet = json.loads(attempt["input_packet_json"] or "{}")
            except json.JSONDecodeError as exc:
                raise ValueError("live pair input packet is malformed") from exc
            contexts.append(self._context_fingerprint(packet))
        if not expected or any(value != expected for value in contexts):
            raise ValueError("live pair does not share the frozen market context")
        from .governance import _attempt_dimensions
        baseline_score = {**baseline_score, **_attempt_dimensions(baseline)}
        candidate_score = {**candidate_score, **_attempt_dimensions(candidate)}
        metadata = {
            "shared_context_fingerprint": expected,
            "frozen_as_of": job["frozen_as_of"],
            "value_window_end": job["value_window_end"],
            "shadow_isolated": True,
        }
        if job["value_window_end"]:
            window_end = self._parse_timestamp(str(job["value_window_end"]))
            for attempt, score in ((baseline, baseline_score), (candidate, candidate_score)):
                score["qualified_in_window"] = bool(
                    score.get("qualified") and attempt.get("completed_at")
                    and self._parse_timestamp(str(attempt["completed_at"])) <= window_end
                )
        return self.record_evaluation(
            job["cell_key"], job["cycle_id"], horizon, regime,
            {**baseline_score, **metadata}, {**candidate_score, **metadata},
            _validated_shadow_job_id=job_id,
        )

    def record_frozen_replay(
        self, cell_key: str, incident_id: str, cycle_id: str, frozen_as_of: str,
        regime: str | None, baseline_score: dict[str, Any], candidate_score: dict[str, Any], *,
        evidence_times: list[dict[str, str]],
    ) -> None:
        """Record one immutable incident replay after proving its information cutoff."""
        required_incidents = {
            "weekend_fund_flow", "industry_distribution", "holding_announcement",
            "expression_loss", "markethub_current_bar",
        }
        if incident_id not in required_incidents:
            raise ValueError("unsupported frozen replay incident")
        cutoff = self._parse_timestamp(frozen_as_of)
        if not evidence_times:
            raise ValueError("frozen replay requires auditable evidence times")
        for item in evidence_times:
            occurred = self._parse_timestamp(str(item.get("occurred_at") or ""))
            known = self._parse_timestamp(str(item.get("known_at") or ""))
            if occurred > known or known > cutoff:
                raise ValueError("frozen replay contains future evidence")
        candidate = {
            **candidate_score, "incident_id": incident_id, "replay_as_of": frozen_as_of,
            "future_data_leak": False,
        }
        baseline = {
            **baseline_score, "incident_id": incident_id, "replay_as_of": frozen_as_of,
            "future_data_leak": False,
        }
        self.record_evaluation(
            cell_key, cycle_id, f"incident:{incident_id}", regime, baseline, candidate,
            source_kind="historical_replay",
        )

    @staticmethod
    def _parse_timestamp(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("frozen replay timestamp must be ISO-8601") from exc
        if parsed.tzinfo is None:
            raise ValueError("frozen replay timestamp must be timezone-aware")
        return parsed

    @staticmethod
    def _context_fingerprint(packet: dict[str, Any]) -> str:
        frozen = {
            key: value for key, value in packet.items()
            # Evidence contract is the candidate treatment, rather than a market
            # input.  It intentionally differs while all acquired facts and the
            # frozen cutoff must remain identical.
            if key not in {
                "sha256", "runtime_strategy_controls", "allowed_research_backends",
                "evidence_contract", "evidence_requirements",
            }
        }
        return hashlib.sha256(
            json.dumps(frozen, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        ).hexdigest()

    @staticmethod
    def _validate(policy_kind: str, value: dict[str, Any]) -> None:
        if policy_kind not in POLICY_KINDS or not isinstance(value, dict):
            raise ValueError("invalid runtime strategy policy")
        if policy_kind == "stage_budget" and set(value) == {"timeout_seconds"} and isinstance(value["timeout_seconds"], int) and value["timeout_seconds"] > 0:
            return
        if policy_kind == "search_breadth" and set(value) == {"max_operations"} and isinstance(value["max_operations"], int) and 0 <= value["max_operations"] <= 24:
            return
        if (
            policy_kind == "source_mix"
            and set(value).issubset({"enabled_backends", "market_understanding_enabled"})
            and "enabled_backends" in value
            and isinstance(value["enabled_backends"], list)
            and set(value["enabled_backends"]).issubset({"gateway", "market"})
            and isinstance(value.get("market_understanding_enabled", False), bool)
        ):
            return
        raise ValueError("invalid runtime strategy value")
