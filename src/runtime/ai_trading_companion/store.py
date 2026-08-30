from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from .secret_guard import assert_safe


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CompanionStore:
    def __init__(self, database: Path) -> None:
        self.database = Path(database)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connection() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS companion_cycle (
              cycle_id TEXT PRIMARY KEY, task_key TEXT NOT NULL, scheduled_for TEXT NOT NULL,
              as_of TEXT NOT NULL, state TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
              kind TEXT NOT NULL DEFAULT 'scheduled', work_start_at TEXT,
              trigger TEXT NOT NULL DEFAULT 'scheduled', request_id TEXT,
              requested_at TEXT, request_source_json TEXT,
              task_profile_id TEXT, task_profile_version INTEGER,
              task_profile_json TEXT, evidence_contract_version INTEGER,
              evidence_contract_hash TEXT, evidence_contract_json TEXT,
              human_deadline TEXT, voice_grace_deadline TEXT, m0_revealed_at TEXT,
              codex_session_id TEXT, packet_hash TEXT,
              m1_publish_deadline TEXT, h0_auto_submit_at TEXT, h0_locked_at TEXT,
              h0_artifact_id TEXT, has_h0 INTEGER NOT NULL DEFAULT 0,
              m1_started_at TEXT, m1_completed_at TEXT, m2_started_at TEXT, m2_completed_at TEXT,
              m1_reserve_seconds INTEGER, timing_policy_version INTEGER,
              private_context_json TEXT, private_context_sha256 TEXT, private_context_frozen_at TEXT,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              UNIQUE(task_key, scheduled_for, revision));
            CREATE TABLE IF NOT EXISTS narrative_artifact (
              artifact_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL REFERENCES companion_cycle(cycle_id),
              kind TEXT NOT NULL, revision INTEGER NOT NULL, actor TEXT NOT NULL, body_markdown TEXT NOT NULL,
              body_sha256 TEXT NOT NULL, as_of TEXT NOT NULL, sealed_at TEXT NOT NULL,
              metadata_json TEXT NOT NULL, occurred_at TEXT, known_at TEXT,
              UNIQUE(cycle_id, kind, revision));
            CREATE VIRTUAL TABLE IF NOT EXISTS narrative_fts USING fts5(artifact_id UNINDEXED, cycle_id UNINDEXED, kind UNINDEXED, body_markdown);
            CREATE TABLE IF NOT EXISTS companion_command_receipt (
              command_id TEXT PRIMARY KEY, cycle_id TEXT, command_type TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
              received_at TEXT NOT NULL, result_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS companion_outbox (
              event_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL, event_type TEXT NOT NULL, payload_json TEXT NOT NULL,
              created_at TEXT NOT NULL, delivered_at TEXT);
            CREATE TABLE IF NOT EXISTS client_event_log (
              sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
              contract TEXT NOT NULL, cycle_id TEXT, event_type TEXT NOT NULL,
              payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS ix_client_event_log_sequence ON client_event_log(sequence);
            CREATE TABLE IF NOT EXISTS portfolio_position (
              code TEXT PRIMARY KEY, name TEXT NOT NULL, shares INTEGER NOT NULL,
              average_cost REAL, last_price REAL, price_as_of TEXT, market_value REAL,
              unrealized_pnl REAL, weight REAL, updated_at TEXT NOT NULL,
              revision INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS portfolio_transaction (
              transaction_id TEXT PRIMARY KEY, source_artifact_id TEXT,
              source_cycle_id TEXT, source_text TEXT NOT NULL, action TEXT NOT NULL,
              code TEXT NOT NULL, name TEXT NOT NULL, shares INTEGER NOT NULL,
              price REAL NOT NULL, position_before INTEGER, position_after INTEGER,
              occurred_at TEXT NOT NULL, created_at TEXT NOT NULL,
              reversal_of TEXT, reverted_by TEXT, idempotency_key TEXT NOT NULL UNIQUE,
              action_group_id TEXT, before_json TEXT, after_json TEXT);
            CREATE TABLE IF NOT EXISTS portfolio_change_proposal (
              proposal_id TEXT PRIMARY KEY, source_artifact_id TEXT,
              source_cycle_id TEXT, source_text TEXT NOT NULL, proposal_json TEXT NOT NULL,
              state TEXT NOT NULL, missing_fields_json TEXT NOT NULL,
              created_at TEXT NOT NULL, resolved_at TEXT, error TEXT);
            CREATE TABLE IF NOT EXISTS portfolio_interpretation_job (
              job_id TEXT PRIMARY KEY, source_artifact_id TEXT NOT NULL UNIQUE,
              source_cycle_id TEXT, source_text TEXT NOT NULL, state TEXT NOT NULL,
              created_at TEXT NOT NULL, completed_at TEXT, error TEXT);
            CREATE TABLE IF NOT EXISTS portfolio_render_intent (
              intent_id TEXT PRIMARY KEY, transaction_id TEXT NOT NULL,
              expected_portfolio_sha256 TEXT, state TEXT NOT NULL,
              created_at TEXT NOT NULL, completed_at TEXT, error TEXT);
            CREATE TABLE IF NOT EXISTS portfolio_meta (
              key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS portfolio_outbox (
              event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL,
              payload_json TEXT NOT NULL, created_at TEXT NOT NULL, delivered_at TEXT);
            CREATE TABLE IF NOT EXISTS knowledge_change_proposal (
              proposal_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL, policy TEXT NOT NULL, changeset_json TEXT NOT NULL,
              state TEXT NOT NULL, created_at TEXT NOT NULL, applied_at TEXT, error TEXT,
              category TEXT, evidence_json TEXT, validation_json TEXT,
              requires_approval INTEGER NOT NULL DEFAULT 1, approved_at TEXT, decision_note TEXT);
            CREATE TABLE IF NOT EXISTS companion_schedule_claim (
              task_key TEXT NOT NULL, scheduled_for TEXT NOT NULL, cycle_id TEXT NOT NULL REFERENCES companion_cycle(cycle_id),
              claimed_at TEXT NOT NULL, PRIMARY KEY(task_key, scheduled_for), UNIQUE(cycle_id));
            CREATE TABLE IF NOT EXISTS companion_manual_analysis_claim (
              request_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL UNIQUE REFERENCES companion_cycle(cycle_id),
              claimed_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS companion_message (
              message_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL REFERENCES companion_cycle(cycle_id),
              actor TEXT NOT NULL, state TEXT NOT NULL, phase TEXT NOT NULL,
              batch_id TEXT, body_text TEXT NOT NULL, staged_at TEXT NOT NULL,
              submitted_at TEXT, withdrawn_at TEXT, source_artifact_id TEXT,
              occurred_at TEXT NOT NULL, known_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS ix_companion_message_cycle_state
              ON companion_message(cycle_id, state, staged_at);
            CREATE TABLE IF NOT EXISTS companion_message_batch (
              batch_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL REFERENCES companion_cycle(cycle_id),
              phase TEXT NOT NULL, state TEXT NOT NULL, submitted_at TEXT NOT NULL,
              completed_at TEXT, response_artifact_id TEXT);
            CREATE INDEX IF NOT EXISTS ix_companion_message_batch_pending
              ON companion_message_batch(cycle_id, phase, state, submitted_at);
            CREATE TABLE IF NOT EXISTS companion_stream_message (
              stream_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL REFERENCES companion_cycle(cycle_id),
              batch_ids_json TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL,
              created_at TEXT NOT NULL, completed_at TEXT, error TEXT);
            CREATE TABLE IF NOT EXISTS companion_stream_chunk (
              stream_id TEXT NOT NULL REFERENCES companion_stream_message(stream_id),
              sequence INTEGER NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL,
              PRIMARY KEY(stream_id, sequence));
            CREATE TABLE IF NOT EXISTS llm_attempt (
              attempt_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL REFERENCES companion_cycle(cycle_id),
              stage TEXT NOT NULL, attempt_number INTEGER NOT NULL, status TEXT NOT NULL,
              as_of TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT,
              input_sha256 TEXT, output_sha256 TEXT, error TEXT,
              UNIQUE(cycle_id, stage, attempt_number));
            CREATE TABLE IF NOT EXISTS stage_checkpoint (
              cycle_id TEXT NOT NULL REFERENCES companion_cycle(cycle_id),
              stage TEXT NOT NULL, packet_sha256 TEXT NOT NULL, attempt_id TEXT NOT NULL REFERENCES llm_attempt(attempt_id),
              output_json TEXT NOT NULL, output_sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
              PRIMARY KEY(cycle_id,stage,packet_sha256));
            CREATE TABLE IF NOT EXISTS preview_import (
              preview_id TEXT PRIMARY KEY, bundle_sha256 TEXT NOT NULL, cycle_id TEXT NOT NULL REFERENCES companion_cycle(cycle_id),
              imported_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS companion_research_job (
              job_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL, source_artifact_id TEXT NOT NULL,
              public_scope_json TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL,
              completed_at TEXT, error TEXT, attempt_count INTEGER NOT NULL DEFAULT 0,
              UNIQUE(source_artifact_id));
            CREATE TABLE IF NOT EXISTS companion_cognition_job (
              job_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL REFERENCES companion_cycle(cycle_id),
              source_artifact_id TEXT NOT NULL, mode TEXT NOT NULL, state TEXT NOT NULL,
              source_sha256 TEXT NOT NULL, result_json TEXT, created_at TEXT NOT NULL,
              claimed_at TEXT, attempt_count INTEGER NOT NULL DEFAULT 0, completed_at TEXT, error TEXT,
              UNIQUE(source_artifact_id, mode));
            CREATE TABLE IF NOT EXISTS companion_action_receipt (
              action_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES companion_cognition_job(job_id),
              action_type TEXT NOT NULL, state TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
              result_json TEXT NOT NULL, created_at TEXT NOT NULL, completed_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS memory_proposition (
              proposition_id TEXT PRIMARY KEY, subject TEXT NOT NULL, predicate TEXT NOT NULL,
              object_json TEXT NOT NULL, proposition_kind TEXT NOT NULL, status TEXT NOT NULL,
              confidence REAL, source_message_id TEXT NOT NULL REFERENCES companion_message(message_id),
              source_start INTEGER NOT NULL, source_end INTEGER NOT NULL, source_quote TEXT NOT NULL,
              known_at TEXT NOT NULL, supersedes_id TEXT, tombstoned_at TEXT, created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS ix_memory_proposition_current
              ON memory_proposition(subject,predicate,status,known_at);
            CREATE TABLE IF NOT EXISTS conversation_auto_submit_claim (
              task_key TEXT NOT NULL, scheduled_for TEXT NOT NULL, conversation_cycle_id TEXT NOT NULL,
              batch_id TEXT, claimed_at TEXT NOT NULL,
              PRIMARY KEY(task_key,scheduled_for));
            CREATE TABLE IF NOT EXISTS evidence_ledger_entry (
              evidence_id TEXT PRIMARY KEY, trading_date TEXT NOT NULL, cycle_id TEXT,
              source_url TEXT, source_title TEXT, body_text TEXT NOT NULL,
              occurred_at TEXT, known_at TEXT NOT NULL, metadata_json TEXT NOT NULL,
              stage TEXT, content_sha256 TEXT, coverage_state TEXT NOT NULL DEFAULT 'observed');
            CREATE TABLE IF NOT EXISTS evidence_cycle_use (
              cycle_id TEXT NOT NULL REFERENCES companion_cycle(cycle_id),
              evidence_id TEXT NOT NULL REFERENCES evidence_ledger_entry(evidence_id),
              stage TEXT NOT NULL,
              used_at TEXT NOT NULL,
              PRIMARY KEY(cycle_id,evidence_id,stage));
            CREATE INDEX IF NOT EXISTS ix_evidence_cycle_use_evidence
              ON evidence_cycle_use(evidence_id,cycle_id,used_at);
            CREATE TABLE IF NOT EXISTS judgment_snapshot (
              snapshot_id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL UNIQUE,
              cycle_id TEXT NOT NULL, kind TEXT NOT NULL, snapshot_json TEXT NOT NULL,
              as_of TEXT NOT NULL, created_at TEXT NOT NULL,
              verification_status TEXT NOT NULL DEFAULT 'unverified');
            CREATE TABLE IF NOT EXISTS outcome_checkpoint (
              checkpoint_id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL,
              horizon TEXT NOT NULL, as_of TEXT NOT NULL, outcome_json TEXT NOT NULL,
              created_at TEXT NOT NULL, due_at TEXT, status TEXT NOT NULL DEFAULT 'pending',
              result_artifact_id TEXT, error TEXT, attempt_count INTEGER NOT NULL DEFAULT 0,
              UNIQUE(snapshot_id,horizon));
            CREATE TABLE IF NOT EXISTS memory_index_entry (
              artifact_id TEXT PRIMARY KEY, known_at TEXT NOT NULL,
              tags_json TEXT NOT NULL, indexed_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS memory_retrieval_audit (
              retrieval_id TEXT PRIMARY KEY, task_key TEXT NOT NULL, query_text TEXT NOT NULL,
              known_at TEXT NOT NULL, selected_artifact_ids_json TEXT NOT NULL,
              created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS memory_index_intent (
              intent_id TEXT PRIMARY KEY, origin_type TEXT NOT NULL, origin_id TEXT NOT NULL,
              content_sha256 TEXT NOT NULL, known_at TEXT NOT NULL, state TEXT NOT NULL,
              created_at TEXT NOT NULL, completed_at TEXT, error TEXT,
              UNIQUE(origin_type, origin_id, content_sha256));
            CREATE TABLE IF NOT EXISTS memory_backup (
              backup_id TEXT PRIMARY KEY, path TEXT NOT NULL, sha256 TEXT NOT NULL,
              database_kind TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL,
              verified_at TEXT, error TEXT);
            CREATE TABLE IF NOT EXISTS memory_recovery_quarantine (
              quarantine_id TEXT PRIMARY KEY, source_path TEXT NOT NULL, record_type TEXT NOT NULL,
              record_id TEXT, reason TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS workflow_policy (
              policy_key TEXT PRIMARY KEY, policy_json TEXT NOT NULL, revision INTEGER NOT NULL,
              previous_policy_json TEXT, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS timing_policy (
              task_key TEXT PRIMARY KEY, reserve_seconds INTEGER NOT NULL,
              previous_reserve_seconds INTEGER, revision INTEGER NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ai_risk_doctrine (
              doctrine_key TEXT PRIMARY KEY, doctrine_json TEXT NOT NULL, revision INTEGER NOT NULL,
              previous_doctrine_json TEXT, state TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS market_regime_snapshot (
              snapshot_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL UNIQUE, as_of TEXT NOT NULL,
              regime TEXT NOT NULL, metrics_json TEXT NOT NULL, data_quality TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS router_policy_cell (
              cell_key TEXT PRIMARY KEY, mode TEXT NOT NULL, baseline_json TEXT NOT NULL,
              candidate_json TEXT, revision INTEGER NOT NULL, qualification_fingerprint TEXT,
              previous_json TEXT, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cognitive_route_decision (
              decision_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL, stage TEXT NOT NULL, cell_key TEXT NOT NULL,
              mode TEXT NOT NULL, profile_json TEXT NOT NULL, baseline_json TEXT NOT NULL,
              candidate_json TEXT, selected_json TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS router_shadow_job (
              job_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL UNIQUE, cycle_id TEXT NOT NULL, stage TEXT NOT NULL,
              packet_json TEXT NOT NULL, packet_sha256 TEXT NOT NULL, schema_name TEXT NOT NULL,
              candidate_json TEXT NOT NULL, state TEXT NOT NULL, priority INTEGER NOT NULL,
              created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT, output_json TEXT,
              output_sha256 TEXT, verifier_json TEXT, error TEXT);
            CREATE TABLE IF NOT EXISTS router_evaluation (
              evaluation_id TEXT PRIMARY KEY, cell_key TEXT NOT NULL, cycle_id TEXT NOT NULL,
              horizon TEXT NOT NULL, regime TEXT, baseline_artifact_id TEXT, shadow_job_id TEXT,
              baseline_score_json TEXT NOT NULL, candidate_score_json TEXT NOT NULL,
              state TEXT NOT NULL, created_at TEXT NOT NULL, resolved_at TEXT,
              UNIQUE(cycle_id,horizon,shadow_job_id));
            CREATE TABLE IF NOT EXISTS runtime_strategy_cell (
              cell_key TEXT PRIMARY KEY, policy_kind TEXT NOT NULL, mode TEXT NOT NULL,
              baseline_json TEXT NOT NULL, candidate_json TEXT, automatic_authorized INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL,
              previous_json TEXT, qualification_fingerprint TEXT, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runtime_strategy_evaluation (
              evaluation_id TEXT PRIMARY KEY, cell_key TEXT NOT NULL REFERENCES runtime_strategy_cell(cell_key),
              cycle_id TEXT NOT NULL, horizon TEXT NOT NULL, regime TEXT,
              baseline_score_json TEXT NOT NULL, candidate_score_json TEXT NOT NULL,
              source_kind TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL, resolved_at TEXT,
              UNIQUE(cycle_id,horizon,cell_key));
            CREATE TABLE IF NOT EXISTS runtime_strategy_shadow_job (
              job_id TEXT PRIMARY KEY, cell_key TEXT NOT NULL REFERENCES runtime_strategy_cell(cell_key),
              cycle_id TEXT NOT NULL, stage TEXT NOT NULL, packet_json TEXT NOT NULL, schema_name TEXT NOT NULL,
              baseline_attempt_id TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL, started_at TEXT,
              completed_at TEXT, candidate_attempt_id TEXT, error TEXT,
              UNIQUE(cell_key,cycle_id,stage,baseline_attempt_id));
            CREATE TABLE IF NOT EXISTS evolution_hypothesis (
              hypothesis_id TEXT PRIMARY KEY, family TEXT NOT NULL, title TEXT NOT NULL,
              spec_json TEXT NOT NULL, state TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
              evidence_json TEXT NOT NULL, regime_json TEXT NOT NULL, created_at TEXT NOT NULL,
              completed_at TEXT, replaced_by TEXT);
            CREATE TABLE IF NOT EXISTS schedule_template (
              schedule_id TEXT PRIMARY KEY, task_key TEXT NOT NULL UNIQUE,
              status TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
              current_revision INTEGER NOT NULL, created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL, status_reason TEXT);
            CREATE TABLE IF NOT EXISTS schedule_revision (
              schedule_id TEXT NOT NULL REFERENCES schedule_template(schedule_id),
              revision INTEGER NOT NULL, config_json TEXT NOT NULL,
              created_at TEXT NOT NULL, PRIMARY KEY(schedule_id, revision));
            CREATE TABLE IF NOT EXISTS schedule_outbox (
              event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, payload_json TEXT NOT NULL,
              created_at TEXT NOT NULL, delivered_at TEXT);
            CREATE TABLE IF NOT EXISTS schedule_worker_claim (
              cycle_id TEXT PRIMARY KEY REFERENCES companion_cycle(cycle_id), claimed_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS ix_schedule_template_status ON schedule_template(status, updated_at);
            CREATE TABLE IF NOT EXISTS legacy_import_record (
              source_name TEXT NOT NULL, source_id TEXT NOT NULL,
              imported_artifact_id TEXT, imported_at TEXT NOT NULL,
              detail_json TEXT NOT NULL,
              PRIMARY KEY(source_name, source_id));
            PRAGMA user_version = 18;
            """)
            cycle_columns = {row[1] for row in c.execute("PRAGMA table_info(companion_cycle)")}
            for name, declaration in {
                "m1_publish_deadline": "TEXT",
                "h0_auto_submit_at": "TEXT",
                "h0_locked_at": "TEXT",
                "h0_artifact_id": "TEXT",
                "has_h0": "INTEGER NOT NULL DEFAULT 0",
                "m1_started_at": "TEXT",
                "m1_completed_at": "TEXT",
                "m2_started_at": "TEXT",
                "m2_completed_at": "TEXT",
                "m1_reserve_seconds": "INTEGER",
                "timing_policy_version": "INTEGER",
                "kind": "TEXT NOT NULL DEFAULT 'scheduled'",
                "work_start_at": "TEXT",
                "private_context_json": "TEXT",
                "private_context_sha256": "TEXT",
                "private_context_frozen_at": "TEXT",
                "trigger": "TEXT NOT NULL DEFAULT 'scheduled'",
                "request_id": "TEXT",
                "requested_at": "TEXT",
                "request_source_json": "TEXT",
                "task_profile_id": "TEXT",
                "task_profile_version": "INTEGER",
                "task_profile_json": "TEXT",
                "evidence_contract_version": "INTEGER",
                "evidence_contract_hash": "TEXT",
                "evidence_contract_json": "TEXT",
            }.items():
                if name not in cycle_columns:
                    c.execute(f"ALTER TABLE companion_cycle ADD COLUMN {name} {declaration}")
            attempt_columns = {row[1] for row in c.execute("PRAGMA table_info(llm_attempt)")}
            for name, declaration in {
                "broker_provider": "TEXT",
                "broker_intellect": "TEXT",
                "broker_fulfilled_intellect": "TEXT",
                "broker_request_id": "TEXT",
                "broker_cost_estimate": "REAL",
                "broker_attempts_json": "TEXT",
                "tool_trace_json": "TEXT",
            }.items():
                if name not in attempt_columns:
                    c.execute(f"ALTER TABLE llm_attempt ADD COLUMN {name} {declaration}")
            cognition_columns = {row[1] for row in c.execute("PRAGMA table_info(companion_cognition_job)")}
            for name, declaration in {"claimed_at": "TEXT", "attempt_count": "INTEGER NOT NULL DEFAULT 0"}.items():
                if name not in cognition_columns:
                    c.execute(f"ALTER TABLE companion_cognition_job ADD COLUMN {name} {declaration}")
            artifact_columns = {row[1] for row in c.execute("PRAGMA table_info(narrative_artifact)")}
            if "occurred_at" not in artifact_columns:
                c.execute("ALTER TABLE narrative_artifact ADD COLUMN occurred_at TEXT")
            if "known_at" not in artifact_columns:
                c.execute("ALTER TABLE narrative_artifact ADD COLUMN known_at TEXT")
            c.execute("UPDATE narrative_artifact SET occurred_at=COALESCE(occurred_at,sealed_at), known_at=COALESCE(known_at,sealed_at)")
            evidence_columns = {row[1] for row in c.execute("PRAGMA table_info(evidence_ledger_entry)")}
            for name, declaration in {
                "stage": "TEXT",
                "content_sha256": "TEXT",
                "coverage_state": "TEXT NOT NULL DEFAULT 'observed'",
            }.items():
                if name not in evidence_columns:
                    c.execute(f"ALTER TABLE evidence_ledger_entry ADD COLUMN {name} {declaration}")
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_evidence_content ON evidence_ledger_entry(trading_date,source_url,content_sha256)")
            c.execute(
                """INSERT OR IGNORE INTO companion_message_batch(batch_id,cycle_id,phase,state,submitted_at,completed_at,response_artifact_id)
                   SELECT m.batch_id,m.cycle_id,m.phase,
                     CASE WHEN m.phase!='chat' OR EXISTS (
                       SELECT 1 FROM narrative_artifact a WHERE a.cycle_id=m.cycle_id AND a.kind IN ('ai_chat','premarket_chat')
                         AND a.metadata_json LIKE '%\"reply_to_batch_id\": \"' || m.batch_id || '\"%'
                     ) THEN 'completed' ELSE 'pending' END,
                     MIN(COALESCE(m.submitted_at,m.staged_at)),NULL,NULL
                   FROM companion_message m WHERE m.state='submitted' AND m.batch_id IS NOT NULL
                   GROUP BY m.batch_id,m.cycle_id,m.phase"""
            )

            snapshot_columns = {row[1] for row in c.execute("PRAGMA table_info(judgment_snapshot)")}
            if "verification_status" not in snapshot_columns:
                c.execute("ALTER TABLE judgment_snapshot ADD COLUMN verification_status TEXT NOT NULL DEFAULT 'unverified'")
            outcome_columns = {row[1] for row in c.execute("PRAGMA table_info(outcome_checkpoint)")}
            for name, declaration in {
                "due_at": "TEXT",
                "status": "TEXT NOT NULL DEFAULT 'pending'",
                "result_artifact_id": "TEXT",
                "error": "TEXT",
                "attempt_count": "INTEGER NOT NULL DEFAULT 0",
            }.items():
                if name not in outcome_columns:
                    c.execute(f"ALTER TABLE outcome_checkpoint ADD COLUMN {name} {declaration}")
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_outcome_snapshot_horizon ON outcome_checkpoint(snapshot_id,horizon)")
            attempt_columns = {row[1] for row in c.execute("PRAGMA table_info(llm_attempt)")}
            for name, declaration in {
                "model": "TEXT",
                "reasoning_effort": "TEXT",
                "search_enabled": "INTEGER",
                "timeout_seconds": "INTEGER",
                "routing_reason": "TEXT",
                "route_decision_id": "TEXT",
                "is_shadow": "INTEGER NOT NULL DEFAULT 0",
                "duration_ms": "INTEGER",
                "usage_json": "TEXT",
                "input_tokens": "INTEGER",
                "cached_input_tokens": "INTEGER",
                "output_tokens": "INTEGER",
                "reasoning_tokens": "INTEGER",
                "verifier_json": "TEXT",
                "runner_fingerprint": "TEXT",
                "input_packet_json": "TEXT",
                "output_json": "TEXT",
                "effort_policy_version": "TEXT",
                "effort_input_fingerprint": "TEXT",
            }.items():
                if name not in attempt_columns:
                    c.execute(f"ALTER TABLE llm_attempt ADD COLUMN {name} {declaration}")
            proposal_columns = {row[1] for row in c.execute("PRAGMA table_info(knowledge_change_proposal)")}
            for name, declaration in {
                "category": "TEXT",
                "evidence_json": "TEXT",
                "validation_json": "TEXT",
                "requires_approval": "INTEGER NOT NULL DEFAULT 1",
                "approved_at": "TEXT",
                "decision_note": "TEXT",
            }.items():
                if name not in proposal_columns:
                    c.execute(f"ALTER TABLE knowledge_change_proposal ADD COLUMN {name} {declaration}")
            transaction_columns = {row[1] for row in c.execute("PRAGMA table_info(portfolio_transaction)")}
            if "position_before" not in transaction_columns:
                c.execute("ALTER TABLE portfolio_transaction ADD COLUMN position_before INTEGER")
            if "position_after" not in transaction_columns:
                c.execute("ALTER TABLE portfolio_transaction ADD COLUMN position_after INTEGER")
            for name in ("action_group_id", "before_json", "after_json"):
                if name not in transaction_columns:
                    c.execute(f"ALTER TABLE portfolio_transaction ADD COLUMN {name} TEXT")
            c.execute("CREATE INDEX IF NOT EXISTS ix_portfolio_transaction_group ON portfolio_transaction(action_group_id,created_at)")
            cycle_columns = {row[1] for row in c.execute("PRAGMA table_info(companion_cycle)")}
            for name, declaration in {
                "schedule_id": "TEXT",
                "schedule_revision": "INTEGER",
                "schedule_snapshot_json": "TEXT",
            }.items():
                if name not in cycle_columns:
                    c.execute(f"ALTER TABLE companion_cycle ADD COLUMN {name} {declaration}")
            # v8 migration/backfill: existing immutable facts predate intents.
            # Deterministic IDs make this safe on every startup and avoid losing
            # the user's historical formal reasoning after the physical split.
            c.execute("""INSERT OR IGNORE INTO memory_index_intent(intent_id,origin_type,origin_id,content_sha256,known_at,state,created_at)
                         SELECT 'artifact:' || artifact_id || ':' || body_sha256,'artifact',artifact_id,body_sha256,COALESCE(known_at,sealed_at),'pending',?
                         FROM narrative_artifact
                         WHERE kind IN ('pre_m0','h0','m0','m1','m2','outcome','reflection','chat_human','ai_chat','premarket_chat')""", (now(),))
            c.execute("""INSERT OR IGNORE INTO memory_index_intent(intent_id,origin_type,origin_id,content_sha256,known_at,state,created_at)
                         SELECT 'evidence:' || evidence_id || ':' || COALESCE(content_sha256,''),'evidence',evidence_id,COALESCE(content_sha256,''),known_at,'pending',?
                         FROM evidence_ledger_entry""", (now(),))
            # v9 migration/backfill: historical cycles were created before
            # templates had identities.  Default template keys deliberately
            # retain the former task keys, which makes this deterministic and
            # preserves the user's existing run history in the new UI.
            c.execute(
                """UPDATE companion_cycle
                   SET schedule_id=(SELECT t.schedule_id FROM schedule_template t WHERE t.task_key=companion_cycle.task_key),
                       schedule_revision=(SELECT t.current_revision FROM schedule_template t WHERE t.task_key=companion_cycle.task_key),
                       schedule_snapshot_json=(SELECT r.config_json FROM schedule_template t JOIN schedule_revision r
                         ON r.schedule_id=t.schedule_id AND r.revision=t.current_revision WHERE t.task_key=companion_cycle.task_key)
                   WHERE schedule_id IS NULL AND EXISTS (SELECT 1 FROM schedule_template t WHERE t.task_key=companion_cycle.task_key)"""
            )

    def history_page(self, *, before: str | None = None, limit: int = 31, search: str = "") -> dict[str, Any]:
        """Authoritative compact history, deduplicated by task and scheduled time."""
        self.initialize(); limit = max(1, min(limit, 90))
        with self.connection() as c:
            clauses, values = ["1=1"], []
            if before: clauses.append("substr(c.scheduled_for,1,10)<?"); values.append(before)
            if search:
                clauses.append("(c.task_key LIKE ? OR EXISTS(SELECT 1 FROM narrative_artifact a WHERE a.cycle_id=c.cycle_id AND a.body_markdown LIKE ?))")
                values.extend([f"%{search}%", f"%{search}%"])
            rows = c.execute(f"""WITH ranked AS (
              SELECT c.*,COUNT(DISTINCT a.artifact_id) artifact_count,COUNT(DISTINCT m.message_id) message_count,
              ROW_NUMBER() OVER(PARTITION BY c.task_key,c.scheduled_for ORDER BY COUNT(DISTINCT a.artifact_id) DESC,COUNT(DISTINCT m.message_id) DESC,c.updated_at DESC,c.revision DESC) current_rank
              FROM companion_cycle c LEFT JOIN narrative_artifact a ON a.cycle_id=c.cycle_id LEFT JOIN companion_message m ON m.cycle_id=c.cycle_id AND m.state!='withdrawn'
              WHERE {' AND '.join(clauses)} GROUP BY c.cycle_id), dates AS (
              SELECT DISTINCT substr(scheduled_for,1,10) date FROM ranked WHERE current_rank=1 ORDER BY date DESC LIMIT ?)
              SELECT * FROM ranked WHERE current_rank=1 AND substr(scheduled_for,1,10) IN (SELECT date FROM dates) ORDER BY scheduled_for DESC,task_key""", (*values, limit)).fetchall()
        items = [dict(row) for row in rows]
        return {"contract":"companion-history-page/v1","items":items,"next_before":min((item["scheduled_for"][:10] for item in items), default=None)}

    def client_events(self, after: int, limit: int = 500) -> list[dict[str, Any]]:
        self.initialize()
        with self.connection() as c:
            return [dict(row) for row in c.execute("SELECT * FROM client_event_log WHERE sequence>? ORDER BY sequence LIMIT ?", (after, max(1, min(limit, 500))))]

    def _queue_client_event(self, event_id: str, contract: str, cycle_id: str | None, event_type: str, payload_json: str, created_at: str, connection: sqlite3.Connection) -> None:
        connection.execute("INSERT OR IGNORE INTO client_event_log(event_id,contract,cycle_id,event_type,payload_json,created_at) VALUES(?,?,?,?,?,?)", (event_id, contract, cycle_id, event_type, payload_json, created_at))

    def _schedule_snapshot(self) -> None:
        """Keep a small checksummed recovery aid after every schedule mutation."""
        with self.connection() as c:
            rows = [dict(row) for row in c.execute(
                """SELECT t.schedule_id,t.task_key,t.status,t.version,t.current_revision,r.config_json
                   FROM schedule_template t JOIN schedule_revision r
                     ON r.schedule_id=t.schedule_id AND r.revision=t.current_revision
                   ORDER BY t.schedule_id"""
            )]
        raw = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        target = self.database.parent.parent / "runtime" / "schedule-snapshots" / "current.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps({"sha256": digest(raw), "schedules": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)

    def repair_schedule_registry(self) -> bool:
        """Repair only current template projections from a verified local snapshot."""
        target = self.database.parent.parent / "runtime" / "schedule-snapshots" / "current.json"
        if not target.exists():
            return False
        payload = json.loads(target.read_text(encoding="utf-8")); rows = payload.get("schedules")
        raw = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if not isinstance(rows, list) or payload.get("sha256") != digest(raw):
            return False
        with self.connection() as c:
            for row in rows:
                c.execute(
                    """INSERT INTO schedule_template(schedule_id,task_key,status,version,current_revision,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?) ON CONFLICT(schedule_id) DO UPDATE SET
                         task_key=excluded.task_key,status=excluded.status,version=excluded.version,
                         current_revision=excluded.current_revision,updated_at=excluded.updated_at""",
                    (row["schedule_id"], row["task_key"], row["status"], row["version"], row["current_revision"], now(), now()),
                )
                c.execute(
                    "INSERT OR REPLACE INTO schedule_revision(schedule_id,revision,config_json,created_at) VALUES(?,?,?,?)",
                    (row["schedule_id"], row["current_revision"], row["config_json"], now()),
                )
        return True

    def seed_schedule(self, schedule_id: str, task_key: str, config: dict[str, Any]) -> None:
        """Install a default only once; upgrades never overwrite user changes."""
        self.initialize()
        at = now()
        with self.connection() as c:
            existing = c.execute("SELECT 1 FROM schedule_template WHERE schedule_id=?", (schedule_id,)).fetchone()
            if existing:
                return
            raw = json.dumps(config, ensure_ascii=False, sort_keys=True)
            c.execute("INSERT INTO schedule_template(schedule_id,task_key,status,version,current_revision,created_at,updated_at) VALUES(?,?, 'active',1,1,?,?)", (schedule_id, task_key, at, at))
            c.execute("INSERT INTO schedule_revision(schedule_id,revision,config_json,created_at) VALUES(?,?,?,?)", (schedule_id, 1, raw, at))
        self._schedule_snapshot()

    def list_schedules(self, *, include_inactive: bool = True) -> list[dict[str, Any]]:
        self.initialize()
        where = "" if include_inactive else "WHERE t.status='active'"
        with self.connection() as c:
            return [dict(row) for row in c.execute(
                f"""SELECT t.schedule_id,t.task_key,t.status,t.version,t.current_revision,t.created_at,t.updated_at,t.status_reason,
                            r.config_json,r.created_at AS revision_created_at
                     FROM schedule_template t JOIN schedule_revision r
                       ON r.schedule_id=t.schedule_id AND r.revision=t.current_revision
                     {where} ORDER BY t.created_at,t.schedule_id"""
            )]

    def get_schedule(self, schedule_id: str) -> dict[str, Any]:
        for row in self.list_schedules():
            if row["schedule_id"] == schedule_id:
                return row
        raise ValueError("任务不存在")

    def create_schedule(self, config: dict[str, Any]) -> dict[str, Any]:
        self.initialize()
        schedule_id = str(uuid.uuid4())
        task_key = f"schedule.{schedule_id}"
        at = now(); raw = json.dumps(config, ensure_ascii=False, sort_keys=True)
        with self.connection() as c:
            c.execute("INSERT INTO schedule_template(schedule_id,task_key,status,version,current_revision,created_at,updated_at) VALUES(?,?, 'active',1,1,?,?)", (schedule_id, task_key, at, at))
            c.execute("INSERT INTO schedule_revision(schedule_id,revision,config_json,created_at) VALUES(?,?,?,?)", (schedule_id, 1, raw, at))
        self._schedule_snapshot()
        return self.get_schedule(schedule_id)

    def update_schedule(self, schedule_id: str, expected_version: int, config: dict[str, Any]) -> dict[str, Any]:
        self.initialize(); at = now(); raw = json.dumps(config, ensure_ascii=False, sort_keys=True)
        with self.connection() as c:
            row = c.execute("SELECT version,current_revision FROM schedule_template WHERE schedule_id=?", (schedule_id,)).fetchone()
            if not row: raise ValueError("任务不存在")
            if row["version"] != expected_version: raise ValueError("任务已被其他修改更新，请刷新后再保存")
            revision = int(row["current_revision"]) + 1
            c.execute("INSERT INTO schedule_revision(schedule_id,revision,config_json,created_at) VALUES(?,?,?,?)", (schedule_id, revision, raw, at))
            c.execute("UPDATE schedule_template SET current_revision=?,version=version+1,updated_at=? WHERE schedule_id=?", (revision, at, schedule_id))
        self._schedule_snapshot()
        return self.get_schedule(schedule_id)

    def set_schedule_status(self, schedule_id: str, expected_version: int, status: str) -> dict[str, Any]:
        if status not in {"active", "paused", "archived"}: raise ValueError("不支持的任务状态")
        self.initialize()
        with self.connection() as c:
            row = c.execute("SELECT version FROM schedule_template WHERE schedule_id=?", (schedule_id,)).fetchone()
            if not row: raise ValueError("任务不存在")
            if row["version"] != expected_version: raise ValueError("任务已被其他修改更新，请刷新后再保存")
            c.execute("UPDATE schedule_template SET status=?,version=version+1,updated_at=?,status_reason=? WHERE schedule_id=?", (status, now(), "用户操作", schedule_id))
        self._schedule_snapshot()
        return self.get_schedule(schedule_id)

    def schedule_history(self, schedule_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        self.initialize()
        with self.connection() as c:
            return [dict(row) for row in c.execute(
                """WITH ranked_history AS (
                       SELECT cycle_id,scheduled_for,state,updated_at,
                              ROW_NUMBER() OVER (
                                PARTITION BY substr(scheduled_for, 1, 16)
                                ORDER BY updated_at DESC, cycle_id DESC
                              ) AS history_rank
                         FROM companion_cycle
                        WHERE schedule_id=?
                   )
                   SELECT cycle_id,scheduled_for,state,updated_at FROM ranked_history
                    WHERE history_rank=1
                    ORDER BY scheduled_for DESC LIMIT ?""", (schedule_id, limit)
            )]

    def claim_scheduled_workers(self, *, limit: int = 2, at: datetime | None = None) -> list[dict[str, Any]]:
        """Atomically reserve up to the available worker slots for queued cycles."""
        self.initialize()
        claimed_at = now()
        with self.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            active = c.execute("SELECT COUNT(*) FROM schedule_worker_claim").fetchone()[0]
            available = max(0, limit - int(active))
            due_at = (at or datetime.now(timezone.utc)).astimezone(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
            rows = c.execute(
                """SELECT c.* FROM companion_cycle c LEFT JOIN schedule_worker_claim w ON w.cycle_id=c.cycle_id
                   WHERE c.state='queued' AND w.cycle_id IS NULL
                     AND COALESCE(c.work_start_at,c.scheduled_for) <= ?
                   ORDER BY COALESCE(c.work_start_at,c.scheduled_for),c.created_at LIMIT ?""", (due_at, available)
            ).fetchall()
            for row in rows:
                c.execute("INSERT INTO schedule_worker_claim(cycle_id,claimed_at) VALUES(?,?)", (row["cycle_id"], claimed_at))
        return [dict(row) for row in rows]

    def finish_scheduled_worker(self, cycle_id: str) -> None:
        with self.connection() as c:
            c.execute("DELETE FROM schedule_worker_claim WHERE cycle_id=?", (cycle_id,))

    def create_cycle(self, task_key: str, scheduled_for: str, as_of: str, *, schedule_id: str | None = None, schedule_revision: int | None = None, schedule_snapshot: dict[str, Any] | None = None, kind: str = "scheduled", work_start_at: str | None = None) -> dict[str, Any]:
        self.initialize(); cycle_id = str(uuid.uuid4()); at = now()
        with self.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            claimed = c.execute("SELECT cycle_id FROM companion_schedule_claim WHERE task_key=? AND scheduled_for=?", (task_key, scheduled_for)).fetchone()
            if claimed:
                cycle_id = claimed["cycle_id"]
            else:
                existing = c.execute(
                    "SELECT * FROM companion_cycle WHERE task_key=? AND scheduled_for=? AND trigger='scheduled' ORDER BY created_at LIMIT 1",
                    (task_key, scheduled_for),
                ).fetchone()
                if existing:
                    cycle_id = existing["cycle_id"]
                else:
                    c.execute(
                        """INSERT INTO companion_cycle(
                             cycle_id,task_key,scheduled_for,as_of,state,revision,kind,work_start_at,
                             schedule_id,schedule_revision,schedule_snapshot_json,created_at,updated_at
                           ) VALUES(?,?,?,?, 'queued',1,?,?,?,?,?,?,?)""",
                        (cycle_id, task_key, scheduled_for, as_of, kind, work_start_at,
                         schedule_id, schedule_revision, json.dumps(schedule_snapshot, ensure_ascii=False, sort_keys=True) if schedule_snapshot else None, at, at),
                    )
                c.execute("INSERT INTO companion_schedule_claim(task_key,scheduled_for,cycle_id,claimed_at) VALUES(?,?,?,?)", (task_key, scheduled_for, cycle_id, at))
        return self.get_cycle(cycle_id)

    def create_manual_analysis_cycle(
        self,
        *,
        request_id: str,
        task_key: str,
        requested_at: str,
        source: dict[str, Any],
        task_profile_id: str,
        task_profile_version: int,
        task_profile: dict[str, Any] | None = None,
        evidence_contract: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Create one manual formal-analysis occurrence without claiming a schedule slot.

        The immutable request id, rather than wording or wall-clock time, is the
        idempotency key.  A separately submitted request must remain free to
        create another occurrence even when it is made at the same instant.
        """
        self.initialize()
        cycle_id = str(uuid.uuid4())
        claimed_at = now()
        with self.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            claim = c.execute(
                "SELECT cycle_id FROM companion_manual_analysis_claim WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if claim:
                return self.get_cycle(claim["cycle_id"], connection=c), False

            # `scheduled_for` remains the existing cycle ordering/projection key.
            # A manual occurrence has no schedule slot, so reserve a private
            # microsecond key and retain the exact user-visible request time in
            # `requested_at`.  This prevents it from consuming a future or
            # recovery-created scheduled occurrence at the same wall-clock time.
            scheduled_for = (
                datetime.fromisoformat(requested_at.replace("Z", "+00:00")) + timedelta(microseconds=1)
            ).isoformat(timespec="microseconds")
            offset = 1
            while c.execute(
                "SELECT 1 FROM companion_cycle WHERE task_key=? AND scheduled_for=? AND revision=1",
                (task_key, scheduled_for),
            ).fetchone():
                offset += 1
                value = datetime.fromisoformat(requested_at.replace("Z", "+00:00")) + timedelta(microseconds=offset)
                scheduled_for = value.isoformat(timespec="microseconds")

            c.execute(
                """INSERT INTO companion_cycle(
                     cycle_id,task_key,scheduled_for,as_of,state,revision,kind,work_start_at,
                     trigger,request_id,requested_at,request_source_json,task_profile_id,task_profile_version,
                     task_profile_json,evidence_contract_version,evidence_contract_hash,evidence_contract_json,
                     created_at,updated_at
                   ) VALUES(?,?,?,?, 'queued',1,'manual',?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    cycle_id, task_key, scheduled_for, requested_at, requested_at,
                    "manual_chat", request_id, requested_at,
                    json.dumps(source, ensure_ascii=False, sort_keys=True), task_profile_id, task_profile_version,
                    json.dumps(task_profile, ensure_ascii=False, sort_keys=True) if task_profile else None,
                    int(evidence_contract["version"]) if evidence_contract else None,
                    str(evidence_contract["contract_hash"]) if evidence_contract else None,
                    json.dumps(evidence_contract, ensure_ascii=False, sort_keys=True) if evidence_contract else None,
                    claimed_at, claimed_at,
                ),
            )
            c.execute(
                "INSERT INTO companion_manual_analysis_claim(request_id,cycle_id,claimed_at) VALUES(?,?,?)",
                (request_id, cycle_id, claimed_at),
            )
            return self.get_cycle(cycle_id, connection=c), True

    def find_cycle(self, task_key: str, scheduled_for: str) -> dict[str, Any] | None:
        self.initialize()
        with self.connection() as c:
            row = c.execute("SELECT * FROM companion_cycle WHERE task_key=? AND scheduled_for=? ORDER BY created_at LIMIT 1", (task_key, scheduled_for)).fetchone()
            return dict(row) if row else None

    def latest_cycles_for_date(self, scheduled_date: str) -> list[dict[str, Any]]:
        """Return the current local projection for each task on one calendar date.

        Import and recovery history can contain superseded empty copies of a
        cycle.  The desktop needs the active cycle first, then the richest
        persisted narrative rather than the most recently imported shell.
        """
        self.initialize()
        with self.connection() as c:
            rows = c.execute(
                """SELECT * FROM (
                       SELECT c.*,
                              COALESCE(a.artifact_count, 0) AS artifact_count,
                              COALESCE(m.message_count, 0) AS message_count,
                               ROW_NUMBER() OVER (
                                  PARTITION BY CASE WHEN c.trigger='manual_chat' THEN c.cycle_id ELSE c.task_key END
                                  ORDER BY
                                      CASE WHEN c.state IN (
                                          'queued','researching_m0','awaiting_h0','voice_grace',
                                          'h0_locked','researching_m1','judging_m1','m1_retry_wait',
                                          'synthesizing_m2','m2_deferred'
                                      ) THEN 1 ELSE 0 END DESC,
                                      COALESCE(a.artifact_count, 0) DESC,
                                      COALESCE(m.message_count, 0) DESC,
                                      c.updated_at DESC, c.created_at DESC, c.revision DESC
                              ) AS current_rank
                         FROM companion_cycle c
                    LEFT JOIN (
                        SELECT cycle_id, COUNT(*) AS artifact_count
                          FROM narrative_artifact
                         GROUP BY cycle_id
                    ) a ON a.cycle_id=c.cycle_id
                    LEFT JOIN (
                        SELECT cycle_id, COUNT(*) AS message_count
                          FROM companion_message
                         WHERE state != 'withdrawn'
                         GROUP BY cycle_id
                    ) m ON m.cycle_id=c.cycle_id
                        WHERE substr(scheduled_for, 1, 10)=?
                    )
                    WHERE current_rank=1
                    ORDER BY scheduled_for, task_key""",
                (scheduled_date,),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_cycle(self, cycle_id: str, *, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        if connection is not None:
            row = connection.execute("SELECT * FROM companion_cycle WHERE cycle_id=?", (cycle_id,)).fetchone()
            if not row:
                raise ValueError(f"unknown cycle: {cycle_id}")
            return dict(row)
        with self.connection() as c:
            return self.get_cycle(cycle_id, connection=c)

    def create_diagnostic_cycle(self, source_cycle_id: str, *, scheduled_for: str | None = None) -> dict[str, Any]:
        """Create an isolated rerun without claiming or rewriting a schedule slot."""
        source = self.get_cycle(source_cycle_id)
        at = now()
        rerun_for = scheduled_for or at
        snapshot = json.loads(source.get("schedule_snapshot_json") or "{}")
        snapshot.update({
            "diagnostic_rerun": True,
            "diagnostic_rerun_of": source_cycle_id,
            "diagnostic_rerun_created_at": at,
            "original_scheduled_for": source["scheduled_for"],
        })
        cycle_id = str(uuid.uuid4())
        with self.connection() as c:
            c.execute(
                """INSERT INTO companion_cycle(
                     cycle_id,task_key,scheduled_for,as_of,state,revision,
                     schedule_id,schedule_revision,schedule_snapshot_json,
                     h0_locked_at,has_h0,m1_started_at,created_at,updated_at
                   ) VALUES(?,?,?,?, 'researching_m1',1,?,?,?,?,?,?,?,?)""",
                (
                    cycle_id, source["task_key"], rerun_for, source["as_of"],
                    source.get("schedule_id"), source.get("schedule_revision"),
                    json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                    source.get("h0_locked_at"), int(source.get("has_h0") or 0), at, at, at,
                ),
            )

        copied_artifacts = {}
        for kind in ("evidence", "m0", "h0"):
            artifact = self.latest_artifact(source_cycle_id, kind)
            if not artifact:
                continue
            metadata = json.loads(artifact.get("metadata_json") or "{}")
            metadata.update({"diagnostic_rerun_of": source_cycle_id, "copied_from_artifact_id": artifact["artifact_id"]})
            copied = self.append_artifact(
                cycle_id, kind, artifact["actor"], artifact["body_markdown"], artifact["as_of"], metadata,
                occurred_at=artifact.get("occurred_at"), known_at=artifact.get("known_at"),
            )
            copied_artifacts[kind] = copied
        if copied_artifacts.get("h0"):
            with self.connection() as c:
                c.execute(
                    "UPDATE companion_cycle SET h0_artifact_id=?, updated_at=?, revision=revision+1 WHERE cycle_id=?",
                    (copied_artifacts["h0"]["artifact_id"], now(), cycle_id),
                )
        return self.get_cycle(cycle_id)

    def create_preview_cycle(self, source_cycle_id: str, known_at: str) -> dict[str, Any]:
        """Create a full rerun cycle in an already isolated database copy."""
        source = self.get_cycle(source_cycle_id)
        snapshot = json.loads(source.get("schedule_snapshot_json") or "{}")
        snapshot.update({
            "preview_rerun": True,
            "preview_rerun_of": source_cycle_id,
            "preview_known_at": known_at,
            "original_scheduled_for": source["scheduled_for"],
        })
        cycle_id = str(uuid.uuid4())
        at = now()
        with self.connection() as c:
            revision = c.execute(
                "SELECT COALESCE(MAX(revision),0)+1 FROM companion_cycle WHERE task_key=? AND scheduled_for=?",
                (source["task_key"], source["scheduled_for"]),
            ).fetchone()[0]
            c.execute(
                """INSERT INTO companion_cycle(
                     cycle_id,task_key,scheduled_for,as_of,state,revision,schedule_id,schedule_revision,
                     schedule_snapshot_json,created_at,updated_at)
                   VALUES(?,?,?,?, 'queued',?,?,?,?,?,?)""",
                (cycle_id, source["task_key"], source["scheduled_for"], source["as_of"], revision,
                 source.get("schedule_id"), source.get("schedule_revision"),
                 json.dumps(snapshot, ensure_ascii=False, sort_keys=True), at, at),
            )
        return self.get_cycle(cycle_id)

    def transition(self, cycle_id: str, state: str, *, connection: sqlite3.Connection | None = None, **fields: Any) -> dict[str, Any]:
        allowed={
            "as_of","human_deadline","voice_grace_deadline","m0_revealed_at","codex_session_id","packet_hash",
            "m1_publish_deadline","h0_auto_submit_at","h0_locked_at","h0_artifact_id","has_h0",
            "m1_started_at","m1_completed_at","m2_started_at","m2_completed_at",
            "m1_reserve_seconds","timing_policy_version",
            "kind","work_start_at","private_context_json","private_context_sha256","private_context_frozen_at",
        }
        bad=set(fields)-allowed
        if bad: raise ValueError(f"invalid cycle fields: {sorted(bad)}")
        values=[state, now()]; assignments=["state=?", "updated_at=?"]
        for key,value in fields.items(): assignments.append(f"{key}=?"); values.append(value)
        values.append(cycle_id)
        if connection is not None:
            connection.execute(f"UPDATE companion_cycle SET {', '.join(assignments)}, revision=revision+1 WHERE cycle_id=?", values)
            return self.get_cycle(cycle_id, connection=connection)
        with self.connection() as c:
            return self.transition(cycle_id, state, connection=c, **fields)

    def append_artifact(self, cycle_id: str, kind: str, actor: str, body: str, as_of: str, metadata: dict[str, Any] | None=None, *, occurred_at: str | None=None, known_at: str | None=None, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        if not body.strip(): raise ValueError("artifact body must not be empty")
        assert_safe(body, boundary="artifact fact storage")
        artifact_id=str(uuid.uuid4()); sealed=now(); metadata=metadata or {}
        def insert(c: sqlite3.Connection) -> None:
            revision=c.execute("SELECT COALESCE(MAX(revision),0)+1 FROM narrative_artifact WHERE cycle_id=? AND kind=?", (cycle_id,kind)).fetchone()[0]
            c.execute(
                """INSERT INTO narrative_artifact(
                     artifact_id,cycle_id,kind,revision,actor,body_markdown,body_sha256,as_of,sealed_at,
                     metadata_json,occurred_at,known_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (artifact_id,cycle_id,kind,revision,actor,body,digest(body),as_of,sealed,
                 json.dumps(metadata,ensure_ascii=False,sort_keys=True),occurred_at or sealed,known_at or sealed),
            )
            c.execute("INSERT INTO narrative_fts(artifact_id,cycle_id,kind,body_markdown) VALUES(?,?,?,?)", (artifact_id,cycle_id,kind,body))
            if kind in {"pre_m0", "h0", "m1", "m2", "outcome", "reflection", "m0", "chat_human", "ai_chat", "premarket_chat"}:
                c.execute(
                    "INSERT OR REPLACE INTO memory_index_entry(artifact_id,known_at,tags_json,indexed_at) VALUES(?,?,?,?)",
                    (artifact_id, known_at or sealed, json.dumps(metadata.get("memory_tags", []), ensure_ascii=False), sealed),
                )
                c.execute(
                    """INSERT OR IGNORE INTO memory_index_intent(
                         intent_id,origin_type,origin_id,content_sha256,known_at,state,created_at)
                       VALUES(?,?,?,?,?,'pending',?)""",
                    (str(uuid.uuid4()), "artifact", artifact_id, digest(body), known_at or sealed, sealed),
                )
            result["revision"] = revision
        result: dict[str, Any] = {}
        if connection is not None:
            insert(connection)
        else:
            with self.connection() as c:
                insert(c)
        return {
            "artifact_id": artifact_id, "cycle_id": cycle_id, "kind": kind,
            "revision": result["revision"], "sha256": digest(body), "sealed_at": sealed,
            "as_of": as_of, "known_at": known_at or sealed,
        }

    def artifacts(self, cycle_id: str) -> list[dict[str, Any]]:
        with self.connection() as c: return [dict(x) for x in c.execute("SELECT * FROM narrative_artifact WHERE cycle_id=? ORDER BY sealed_at, revision", (cycle_id,))]

    def latest_artifact(self, cycle_id: str, kind: str) -> dict[str, Any] | None:
        with self.connection() as c:
            row = c.execute(
                "SELECT * FROM narrative_artifact WHERE cycle_id=? AND kind=? ORDER BY revision DESC LIMIT 1",
                (cycle_id, kind),
            ).fetchone()
            return dict(row) if row else None

    def stage_message(self, cycle_id: str, text: str, phase: str, *, message_id: str | None = None) -> dict[str, Any]:
        if phase not in {"pre_m0", "h0", "chat", "conversation"}:
            raise ValueError(f"invalid message phase: {phase}")
        if not text.strip():
            raise ValueError("message text must not be empty")
        message_id = message_id or str(uuid.uuid4())
        at = now()
        with self.connection() as c:
            c.execute(
                """INSERT INTO companion_message(
                     message_id,cycle_id,actor,state,phase,batch_id,body_text,staged_at,
                     submitted_at,withdrawn_at,source_artifact_id,occurred_at,known_at
                   ) VALUES(?,?, 'human','staged',?,NULL,?,?,NULL,NULL,NULL,?,?)""",
                (message_id, cycle_id, phase, text.strip(), at, at, at),
            )
        return self.get_message(message_id)

    def ensure_daily_conversation(self, local_date: str, *, at: datetime | None = None) -> dict[str, Any]:
        """Open one logical conversation per Shanghai calendar day.

        Submitted messages stay with their original day.  Only unsent staged
        messages move forward, preserving their stable identities and timestamps.
        """
        local_midnight = datetime.fromisoformat(local_date).replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        scheduled_for = local_midnight.isoformat(timespec="seconds")
        cycle = self.find_cycle("conversation.daily", scheduled_for)
        created = cycle is None
        if created:
            cycle = self.create_cycle(
                "conversation.daily", scheduled_for,
                (at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                kind="daily_conversation", work_start_at=scheduled_for,
            )
            cycle = self.transition(cycle["cycle_id"], "open", kind="daily_conversation", work_start_at=scheduled_for)
        with self.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute(
                """UPDATE companion_message SET cycle_id=?,phase='conversation'
                   WHERE state='staged' AND cycle_id IN (
                     SELECT cycle_id FROM companion_cycle
                     WHERE kind='daily_conversation' AND cycle_id!=?
                   )""",
                (cycle["cycle_id"], cycle["cycle_id"]),
            )
            c.execute(
                """UPDATE companion_cycle SET state='closed',updated_at=?,revision=revision+1
                   WHERE kind='daily_conversation' AND cycle_id!=? AND state='open'""",
                (now(), cycle["cycle_id"]),
            )
        result = self.get_cycle(cycle["cycle_id"])
        result["_created"] = created
        return result

    def freeze_private_context(self, cycle_id: str) -> dict[str, Any]:
        """Freeze pre-H0 private facts so M1 cannot observe H0-derived updates."""
        at = now()
        with self.connection() as c:
            cycle = self.get_cycle(cycle_id, connection=c)
            if cycle.get("private_context_json"):
                return json.loads(cycle["private_context_json"])
            positions = [dict(row) for row in c.execute(
                "SELECT code,name,shares,average_cost,last_price,price_as_of,market_value,unrealized_pnl,weight,updated_at,revision FROM portfolio_position ORDER BY code"
            )]
            meta = {row["key"]: row["value"] for row in c.execute("SELECT key,value FROM portfolio_meta")}
            context = {"positions": positions, "total_assets": float(meta["total_assets"]) if meta.get("total_assets") else None, "frozen_at": at}
            raw = json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            c.execute(
                """UPDATE companion_cycle SET private_context_json=?,private_context_sha256=?,private_context_frozen_at=?,updated_at=?,revision=revision+1
                   WHERE cycle_id=? AND private_context_json IS NULL""",
                (raw, digest(raw), at, at, cycle_id),
            )
        return context

    def claim_conversation_auto_submit(self, task_key: str, scheduled_for: str, conversation_cycle_id: str) -> bool:
        with self.connection() as c:
            inserted = c.execute(
                """INSERT OR IGNORE INTO conversation_auto_submit_claim(
                     task_key,scheduled_for,conversation_cycle_id,claimed_at)
                   VALUES(?,?,?,?)""",
                (task_key, scheduled_for, conversation_cycle_id, now()),
            )
        return inserted.rowcount == 1

    def complete_conversation_auto_submit(self, task_key: str, scheduled_for: str, batch_id: str | None) -> None:
        with self.connection() as c:
            c.execute(
                "UPDATE conversation_auto_submit_claim SET batch_id=? WHERE task_key=? AND scheduled_for=?",
                (batch_id, task_key, scheduled_for),
            )

    def start_cognition_job(self, cycle_id: str, source_artifact_id: str, mode: str, source_text: str) -> dict[str, Any]:
        source_sha = digest(source_text)
        job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"cognition:{source_artifact_id}:{mode}:{source_sha}"))
        with self.connection() as c:
            c.execute(
                """INSERT OR IGNORE INTO companion_cognition_job(
                     job_id,cycle_id,source_artifact_id,mode,state,source_sha256,created_at)
                   VALUES(?,?,?,?, 'queued',?,?)""",
                (job_id, cycle_id, source_artifact_id, mode, source_sha, now()),
            )
            row = c.execute("SELECT * FROM companion_cognition_job WHERE source_artifact_id=? AND mode=?", (source_artifact_id, mode)).fetchone()
        return dict(row)

    def finish_cognition_job(self, job_id: str, result: dict[str, Any] | None = None, *, error: str | None = None) -> dict[str, Any]:
        state = "failed" if error else "completed"
        with self.connection() as c:
            c.execute(
                "UPDATE companion_cognition_job SET state=?,result_json=?,completed_at=?,error=? WHERE job_id=?",
                (state, json.dumps(result, ensure_ascii=False, sort_keys=True) if result is not None else None, now(), error[-2000:] if error else None, job_id),
            )
            row = c.execute("SELECT * FROM companion_cognition_job WHERE job_id=?", (job_id,)).fetchone()
        return dict(row)

    def claim_cognition_job(self, job_id: str) -> dict[str, Any]:
        """Atomically lease a queued/failed cognition result for one worker."""
        with self.connection() as c:
            claimed = c.execute(
                """UPDATE companion_cognition_job
                     SET state='running', claimed_at=?, attempt_count=attempt_count+1, error=NULL
                   WHERE job_id=? AND state IN ('queued','failed')""",
                (now(), job_id),
            )
            row = c.execute("SELECT * FROM companion_cognition_job WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError("unknown cognition job")
        return {**dict(row), "claimed": claimed.rowcount == 1}

    def action_receipt(self, action_id: str) -> dict[str, Any] | None:
        with self.connection() as c:
            row = c.execute("SELECT * FROM companion_action_receipt WHERE action_id=?", (action_id,)).fetchone()
        return dict(row) if row else None

    def save_action_receipt(self, action_id: str, job_id: str, action_type: str, payload: dict[str, Any], state: str, result: dict[str, Any]) -> dict[str, Any]:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        at = now()
        with self.connection() as c:
            c.execute(
                """INSERT OR IGNORE INTO companion_action_receipt(
                     action_id,job_id,action_type,state,payload_sha256,result_json,created_at,completed_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (action_id, job_id, action_type, state, digest(raw), json.dumps(result, ensure_ascii=False, sort_keys=True), at, at),
            )
            row = c.execute("SELECT * FROM companion_action_receipt WHERE action_id=?", (action_id,)).fetchone()
        return dict(row)

    def record_proposition(self, proposition_id: str, proposition: dict[str, Any], message: dict[str, Any]) -> dict[str, Any]:
        span = proposition["source_span"]
        supersedes_id = proposition.get("supersedes_id")
        at = now()
        with self.connection() as c:
            if supersedes_id:
                superseded = c.execute(
                    "SELECT subject,predicate,status FROM memory_proposition WHERE proposition_id=?",
                    (supersedes_id,),
                ).fetchone()
                if not superseded or superseded["status"] != "active" or superseded["subject"] != proposition["subject"] or superseded["predicate"] != proposition["predicate"]:
                    raise ValueError("superseded proposition must be the active version of the same subject and predicate")
                c.execute("UPDATE memory_proposition SET status='superseded' WHERE proposition_id=?", (supersedes_id,))
            c.execute(
                """INSERT OR IGNORE INTO memory_proposition(
                     proposition_id,subject,predicate,object_json,proposition_kind,status,confidence,
                     source_message_id,source_start,source_end,source_quote,known_at,supersedes_id,created_at)
                   VALUES(?,?,?,?,?,'active',?,?,?,?,?,?,?,?)""",
                (proposition_id, proposition["subject"], proposition["predicate"],
                 json.dumps(proposition.get("object"), ensure_ascii=False, sort_keys=True), proposition["kind"],
                 proposition.get("confidence"), message["message_id"], int(span["start"]), int(span["end"]),
                 span["quote"], message["known_at"], supersedes_id, at),
            )
            row = c.execute("SELECT * FROM memory_proposition WHERE proposition_id=?", (proposition_id,)).fetchone()
        return dict(row)

    def current_propositions(self, known_at: str, *, exclude_cycle_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        exclusion = "AND m.cycle_id!=?" if exclude_cycle_id else ""
        values: list[Any] = [known_at]
        if exclude_cycle_id:
            values.append(exclude_cycle_id)
        values.append(limit)
        with self.connection() as c:
            return [dict(row) for row in c.execute(
                f"""SELECT p.* FROM memory_proposition p
                   JOIN companion_message m ON m.message_id=p.source_message_id
                   WHERE p.status='active' AND p.tombstoned_at IS NULL AND p.known_at<=? {exclusion}
                   ORDER BY p.known_at DESC,p.created_at DESC LIMIT ?""",
                values,
            )]

    def get_message(self, message_id: str) -> dict[str, Any]:
        with self.connection() as c:
            row = c.execute("SELECT * FROM companion_message WHERE message_id=?", (message_id,)).fetchone()
            if not row:
                raise ValueError(f"unknown message: {message_id}")
            return dict(row)

    def update_staged_message(self, cycle_id: str, message_id: str, text: str) -> dict[str, Any]:
        if not text.strip():
            raise ValueError("message text must not be empty")
        assert_safe(text, boundary="staged message")
        with self.connection() as c:
            changed = c.execute(
                "UPDATE companion_message SET body_text=? WHERE cycle_id=? AND message_id=? AND state='staged'",
                (text.strip(), cycle_id, message_id),
            ).rowcount
            if changed != 1:
                raise ValueError("only pending messages can be edited")
        return self.get_message(message_id)

    def withdraw_message(self, cycle_id: str, message_id: str) -> dict[str, Any]:
        at = now()
        with self.connection() as c:
            changed = c.execute(
                "UPDATE companion_message SET state='withdrawn',withdrawn_at=? WHERE message_id=? AND cycle_id=? AND state='staged'",
                (at, message_id, cycle_id),
            ).rowcount
            if changed != 1:
                raise ValueError("only a staged message in the current cycle can be withdrawn")
        return self.get_message(message_id)

    def messages(self, cycle_id: str, *, state: str | None = None, phase: str | None = None) -> list[dict[str, Any]]:
        clauses = ["cycle_id=?"]
        values: list[Any] = [cycle_id]
        if state is not None:
            clauses.append("state=?")
            values.append(state)
        if phase is not None:
            clauses.append("phase=?")
            values.append(phase)
        with self.connection() as c:
            return [dict(row) for row in c.execute(
                f"SELECT * FROM companion_message WHERE {' AND '.join(clauses)} ORDER BY staged_at,message_id",
                values,
            )]

    def commit_staged_messages(self, cycle_id: str, phase: str) -> tuple[str, list[dict[str, Any]]]:
        batch_id = str(uuid.uuid4())
        submitted = now()
        with self.connection() as c:
            rows = [dict(row) for row in c.execute(
                "SELECT * FROM companion_message WHERE cycle_id=? AND phase=? AND state='staged' ORDER BY staged_at,message_id",
                (cycle_id, phase),
            )]
            if rows:
                c.execute(
                    """UPDATE companion_message SET state='submitted',batch_id=?,submitted_at=?,known_at=?
                       WHERE cycle_id=? AND phase=? AND state='staged'""",
                    (batch_id, submitted, submitted, cycle_id, phase),
                )
                c.execute(
                    "INSERT INTO companion_message_batch(batch_id,cycle_id,phase,state,submitted_at) VALUES(?,?,?,'pending',?)",
                    (batch_id, cycle_id, phase, submitted),
                )
        for row in rows:
            row.update({"state": "submitted", "batch_id": batch_id, "submitted_at": submitted, "known_at": submitted})
        return batch_id, rows

    def pending_message_batches(self, cycle_id: str, phase: str = "chat") -> list[dict[str, Any]]:
        with self.connection() as c:
            return [dict(row) for row in c.execute(
                "SELECT * FROM companion_message_batch WHERE cycle_id=? AND phase=? AND state='pending' ORDER BY submitted_at,batch_id",
                (cycle_id, phase),
            )]

    def messages_for_batches(self, batch_ids: list[str]) -> list[dict[str, Any]]:
        if not batch_ids:
            return []
        placeholders = ",".join("?" for _ in batch_ids)
        with self.connection() as c:
            return [dict(row) for row in c.execute(
                f"SELECT * FROM companion_message WHERE batch_id IN ({placeholders}) ORDER BY submitted_at,staged_at,message_id", batch_ids
            )]

    def mark_batches_responded(self, batch_ids: list[str], artifact_id: str) -> None:
        if not batch_ids:
            return
        placeholders = ",".join("?" for _ in batch_ids)
        with self.connection() as c:
            c.execute(
                f"UPDATE companion_message_batch SET state='completed',completed_at=?,response_artifact_id=? WHERE batch_id IN ({placeholders}) AND state='pending'",
                [now(), artifact_id, *batch_ids],
            )

    def start_stream_message(self, cycle_id: str, batch_ids: list[str], kind: str) -> dict[str, Any]:
        stream_id = str(uuid.uuid4())
        with self.connection() as c:
            c.execute(
                "INSERT INTO companion_stream_message(stream_id,cycle_id,batch_ids_json,kind,state,created_at) VALUES(?,?,?,?, 'streaming',?)",
                (stream_id, cycle_id, json.dumps(batch_ids, ensure_ascii=False), kind, now()),
            )
        return self.stream_message(stream_id)

    def append_stream_chunk(self, stream_id: str, text: str) -> dict[str, Any]:
        assert_safe(text, boundary="streamed model message")
        with self.connection() as c:
            row = c.execute("SELECT state FROM companion_stream_message WHERE stream_id=?", (stream_id,)).fetchone()
            if not row or row["state"] != "streaming":
                raise ValueError("stream is not active")
            sequence = c.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM companion_stream_chunk WHERE stream_id=?", (stream_id,)).fetchone()[0]
            c.execute("INSERT INTO companion_stream_chunk(stream_id,sequence,text,created_at) VALUES(?,?,?,?)", (stream_id, sequence, text, now()))
        return self.stream_message(stream_id)

    def finish_stream_message(self, stream_id: str, *, error: str | None = None) -> dict[str, Any]:
        with self.connection() as c:
            c.execute("UPDATE companion_stream_message SET state=?,completed_at=?,error=? WHERE stream_id=? AND state='streaming'", ("failed" if error else "completed", now(), error[-2000:] if error else None, stream_id))
        return self.stream_message(stream_id)

    def stream_message(self, stream_id: str) -> dict[str, Any]:
        with self.connection() as c:
            row = c.execute("SELECT * FROM companion_stream_message WHERE stream_id=?", (stream_id,)).fetchone()
            if not row:
                raise ValueError("stream not found")
            result = dict(row)
            chunks = c.execute("SELECT * FROM companion_stream_chunk WHERE stream_id=? ORDER BY sequence", (stream_id,)).fetchall()
        result["batch_ids"] = json.loads(result.pop("batch_ids_json"))
        result["text"] = "".join(chunk["text"] for chunk in chunks)
        result["chunks"] = [dict(chunk) for chunk in chunks]
        return result

    def stream_messages(self, cycle_id: str) -> list[dict[str, Any]]:
        with self.connection() as c:
            ids = [row["stream_id"] for row in c.execute("SELECT stream_id FROM companion_stream_message WHERE cycle_id=? ORDER BY created_at", (cycle_id,))]
        return [self.stream_message(stream_id) for stream_id in ids]

    def link_messages_to_artifact(self, message_ids: list[str], artifact_id: str) -> None:
        if not message_ids:
            return
        placeholders = ",".join("?" for _ in message_ids)
        with self.connection() as c:
            c.execute(
                f"UPDATE companion_message SET source_artifact_id=? WHERE message_id IN ({placeholders})",
                [artifact_id, *message_ids],
            )

    def begin_attempt(
        self, cycle_id: str, stage: str, as_of: str, input_sha256: str | None = None, *,
        model: str | None = None, reasoning_effort: str | None = None,
        search_enabled: bool | None = None, timeout_seconds: int | None = None,
        routing_reason: str | None = None, route_decision_id: str | None = None,
        is_shadow: bool = False, runner_fingerprint: str | None = None,
        effort_policy_version: str | None = None, effort_input_fingerprint: str | None = None,
        input_packet: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        attempt_id = str(uuid.uuid4())
        started = now()
        with self.connection() as c:
            number = c.execute(
                "SELECT COALESCE(MAX(attempt_number),0)+1 FROM llm_attempt WHERE cycle_id=? AND stage=?",
                (cycle_id, stage),
            ).fetchone()[0]
            c.execute(
                """INSERT INTO llm_attempt(
                     attempt_id,cycle_id,stage,attempt_number,status,as_of,started_at,completed_at,
                     input_sha256,output_sha256,error,model,reasoning_effort,search_enabled,
                     timeout_seconds,routing_reason,route_decision_id,is_shadow,runner_fingerprint,input_packet_json,
                     effort_policy_version,effort_input_fingerprint)
                   VALUES(?,?,?,?,?,?,?,NULL,?,NULL,NULL,?,?,?,?,?,?,?,?,?,?,?)""",
                (attempt_id, cycle_id, stage, number, "running", as_of, started, input_sha256,
                 model, reasoning_effort, None if search_enabled is None else int(search_enabled),
                 timeout_seconds, routing_reason, route_decision_id, int(is_shadow), runner_fingerprint,
                 json.dumps(input_packet, ensure_ascii=False, sort_keys=True) if input_packet is not None else None,
                 effort_policy_version, effort_input_fingerprint),
            )
        return {"attempt_id": attempt_id, "attempt_number": number, "started_at": started}

    def finish_attempt(self, attempt_id: str, status: str, *, output_sha256: str | None = None, output: dict[str, Any] | None = None, error: str | None = None, usage: dict[str, Any] | None = None, verifier: dict[str, Any] | None = None, broker_metadata: dict[str, Any] | None = None, tool_trace: list[dict[str, Any]] | None = None, actual_model: str | None = None) -> None:
        if status not in {"succeeded", "rejected", "failed", "timed_out"}:
            raise ValueError(f"invalid attempt status: {status}")
        with self.connection() as c:
            cursor = c.execute(
                """UPDATE llm_attempt SET status=?,completed_at=?,output_sha256=?,error=?,usage_json=?,
                   input_tokens=?,cached_input_tokens=?,output_tokens=?,reasoning_tokens=?,verifier_json=?,
                   broker_provider=?,broker_intellect=?,broker_fulfilled_intellect=?,broker_request_id=?,
                   broker_cost_estimate=?,broker_attempts_json=?,tool_trace_json=?,model=COALESCE(?,model),
                   output_json=?,duration_ms=CAST((julianday(?) - julianday(started_at))*86400000 AS INTEGER) WHERE attempt_id=? AND status='running'""",
                (status, now(), output_sha256, error[-2000:] if error else None,
                 json.dumps(usage or {}, ensure_ascii=False, sort_keys=True),
                 (usage or {}).get("input_tokens"), (usage or {}).get("cached_input_tokens"),
                 (usage or {}).get("output_tokens"), (usage or {}).get("reasoning_tokens"),
                  json.dumps(verifier or {}, ensure_ascii=False, sort_keys=True),
                  (broker_metadata or {}).get("provider"), (broker_metadata or {}).get("intellect"),
                  (broker_metadata or {}).get("fulfilled_intellect"), (broker_metadata or {}).get("request_id"),
                  (broker_metadata or {}).get("cost_estimate"), json.dumps((broker_metadata or {}).get("attempts") or [], ensure_ascii=False, sort_keys=True),
                  json.dumps(tool_trace or [], ensure_ascii=False, sort_keys=True), actual_model,
                  json.dumps(output, ensure_ascii=False, sort_keys=True) if output is not None else None,
                  now(), attempt_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("attempt is already terminal or does not exist")

    def attempts(self, cycle_id: str) -> list[dict[str, Any]]:
        with self.connection() as c:
            return [dict(row) for row in c.execute(
                "SELECT * FROM llm_attempt WHERE cycle_id=? ORDER BY started_at,attempt_number",
                (cycle_id,),
            )]

    def verified_attempt(self, attempt_id: str, cycle_id: str, stage: str, packet_sha256: str | None) -> dict[str, Any]:
        with self.connection() as c:
            row = c.execute(
                "SELECT * FROM llm_attempt WHERE attempt_id=? AND cycle_id=? AND stage=?",
                (attempt_id, cycle_id, stage),
            ).fetchone()
        if not row:
            raise ValueError("attempt does not belong to the requested cycle and stage")
        result = dict(row)
        verifier = json.loads(result.get("verifier_json") or "{}")
        if result["status"] != "succeeded" or not verifier.get("passed"):
            raise ValueError("attempt is not qualified for publication")
        if packet_sha256 is not None and result.get("input_sha256") != packet_sha256:
            raise ValueError("attempt packet hash does not match the frozen packet")
        result["verifier"] = verifier
        return result

    def save_stage_checkpoint(self, cycle_id: str, stage: str, packet_sha256: str, attempt_id: str, output: dict[str, Any]) -> dict[str, Any]:
        attempt = self.verified_attempt(attempt_id, cycle_id, stage, packet_sha256)
        raw = json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        attempt_output = json.loads(attempt.get("output_json") or "null")
        if attempt_output != output:
            raise ValueError("checkpoint output does not match the verified attempt output")
        with self.connection() as c:
            c.execute(
                """INSERT INTO stage_checkpoint(cycle_id,stage,packet_sha256,attempt_id,output_json,output_sha256,created_at)
                   VALUES(?,?,?,?,?,?,?) ON CONFLICT(cycle_id,stage,packet_sha256) DO NOTHING""",
                (cycle_id, stage, packet_sha256, attempt_id, raw, digest(raw), now()),
            )
            row = c.execute(
                "SELECT * FROM stage_checkpoint WHERE cycle_id=? AND stage=? AND packet_sha256=?",
                (cycle_id, stage, packet_sha256),
            ).fetchone()
        return dict(row)

    def stage_checkpoint(self, cycle_id: str, stage: str, packet_sha256: str) -> dict[str, Any] | None:
        with self.connection() as c:
            row = c.execute(
                "SELECT * FROM stage_checkpoint WHERE cycle_id=? AND stage=? AND packet_sha256=?",
                (cycle_id, stage, packet_sha256),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        self.verified_attempt(result["attempt_id"], cycle_id, stage, packet_sha256)
        result["output"] = json.loads(result["output_json"])
        return result

    def valid_daily_baseline(self, trading_date: str, known_at: str) -> dict[str, Any] | None:
        with self.connection() as c:
            row = c.execute(
                """SELECT c.*,m.body_markdown AS m0_text,m.known_at AS m0_known_at
                   FROM companion_cycle c
                   JOIN narrative_artifact m ON m.cycle_id=c.cycle_id AND m.kind='m0'
                   WHERE c.task_key='daily.opportunity.0900' AND substr(c.scheduled_for,1,10)=?
                     AND m.known_at<=? AND COALESCE(c.schedule_snapshot_json,'') NOT LIKE '%diagnostic_rerun%'
                     AND EXISTS (SELECT 1 FROM llm_attempt a WHERE a.cycle_id=c.cycle_id AND a.stage='m0_research'
                       AND a.status='succeeded' AND json_extract(a.verifier_json,'$.passed')=1
                       AND json_extract(a.verifier_json,'$.evidence_gate.validator_version')=3)
                     AND EXISTS (SELECT 1 FROM llm_attempt a WHERE a.cycle_id=c.cycle_id AND a.stage='m0_compose'
                       AND a.status='succeeded' AND json_extract(a.verifier_json,'$.passed')=1)
                   ORDER BY m.known_at DESC LIMIT 1""",
                (trading_date, known_at),
            ).fetchone()
        if not row:
            return None
        return {"cycle_id": row["cycle_id"], "known_at": row["m0_known_at"], "summary": row["m0_text"]}

    def frozen_judgments_before(self, trading_date: str, known_at: str, task_keys: tuple[str, ...]) -> list[dict[str, Any]]:
        if not task_keys:
            return []
        placeholders = ",".join("?" for _ in task_keys)
        with self.connection() as c:
            rows = c.execute(
                f"""SELECT c.task_key,c.scheduled_for,a.kind,a.body_markdown,a.as_of,a.known_at,s.snapshot_json
                    FROM companion_cycle c JOIN narrative_artifact a ON a.cycle_id=c.cycle_id
                    JOIN judgment_snapshot s ON s.artifact_id=a.artifact_id
                    WHERE substr(c.scheduled_for,1,10)=? AND c.task_key IN ({placeholders})
                      AND a.kind IN ('m1','m2') AND a.known_at<=?
                      AND COALESCE(c.schedule_snapshot_json,'') NOT LIKE '%diagnostic_rerun%'
                      AND EXISTS (SELECT 1 FROM llm_attempt attempt WHERE attempt.cycle_id=c.cycle_id
                        AND attempt.stage=CASE WHEN a.kind='m1' THEN 'm1_judgment' ELSE 'm2' END
                        AND attempt.status='succeeded' AND json_extract(attempt.verifier_json,'$.passed')=1)
                    ORDER BY c.scheduled_for,a.kind""",
                (trading_date, *task_keys, known_at),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["snapshot"] = json.loads(item.pop("snapshot_json"))
            result.append(item)
        return result

    def router_policy_cell(self, cell_key: str, baseline: dict[str, Any], candidate: dict[str, Any] | None) -> dict[str, Any]:
        self.initialize()
        baseline_json = json.dumps(baseline, ensure_ascii=False, sort_keys=True)
        candidate_json = json.dumps(candidate, ensure_ascii=False, sort_keys=True) if candidate else None
        with self.connection() as c:
            row = c.execute("SELECT * FROM router_policy_cell WHERE cell_key=?", (cell_key,)).fetchone()
            if not row:
                at = now()
                c.execute(
                    """INSERT INTO router_policy_cell(cell_key,mode,baseline_json,candidate_json,revision,updated_at)
                       VALUES(?, 'shadow', ?, ?, 1, ?)""",
                    (cell_key, baseline_json, candidate_json, at),
                )
                row = c.execute("SELECT * FROM router_policy_cell WHERE cell_key=?", (cell_key,)).fetchone()
            elif row["baseline_json"] != baseline_json or row["candidate_json"] != candidate_json:
                # A model/effort boundary changed. Old empirical evidence remains auditable but
                # cannot silently promote the new execution shape.
                c.execute(
                    """UPDATE router_policy_cell SET previous_json=?,mode='shadow',baseline_json=?,candidate_json=?,
                       revision=revision+1,qualification_fingerprint=NULL,updated_at=? WHERE cell_key=?""",
                    (json.dumps(dict(row), ensure_ascii=False, sort_keys=True), baseline_json, candidate_json, now(), cell_key),
                )
                row = c.execute("SELECT * FROM router_policy_cell WHERE cell_key=?", (cell_key,)).fetchone()
            return dict(row)

    def record_route_decision(self, cycle_id: str, stage: str, cell_key: str, mode: str, profile: dict[str, Any], baseline: dict[str, Any], candidate: dict[str, Any] | None, selected: dict[str, Any]) -> str:
        decision_id = str(uuid.uuid4())
        with self.connection() as c:
            c.execute(
                """INSERT INTO cognitive_route_decision(decision_id,cycle_id,stage,cell_key,mode,profile_json,baseline_json,candidate_json,selected_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (decision_id, cycle_id, stage, cell_key, mode, json.dumps(profile, ensure_ascii=False, sort_keys=True),
                 json.dumps(baseline, ensure_ascii=False, sort_keys=True), json.dumps(candidate, ensure_ascii=False, sort_keys=True) if candidate else None,
                 json.dumps(selected, ensure_ascii=False, sort_keys=True), now()),
            )
        return decision_id

    def queue_router_shadow(self, decision_id: str, cycle_id: str, stage: str, packet: dict[str, Any], schema_name: str, candidate: dict[str, Any], *, priority: int = 0) -> str | None:
        serialized = json.dumps(packet, ensure_ascii=False, sort_keys=True)
        with self.connection() as c:
            existing = c.execute("SELECT job_id FROM router_shadow_job WHERE decision_id=?", (decision_id,)).fetchone()
            if existing:
                return existing["job_id"]
            # Adaptive sentinel rather than a permanent percentage or daily
            # quota: only shadow a cell while it has not reached a sequential
            # promote/reject decision.  Drift/model/schema changes reset its
            # cell to shadow through router_policy_cell above.
            cell = c.execute("SELECT p.mode FROM cognitive_route_decision d JOIN router_policy_cell p ON p.cell_key=d.cell_key WHERE d.decision_id=?", (decision_id,)).fetchone()
            if not cell or cell["mode"] != "shadow":
                return None
            job_id = str(uuid.uuid4())
            c.execute(
                """INSERT INTO router_shadow_job(job_id,decision_id,cycle_id,stage,packet_json,packet_sha256,schema_name,candidate_json,state,priority,created_at)
                   VALUES(?,?,?,?,?,?,?,?, 'queued', ?, ?)""",
                (job_id, decision_id, cycle_id, stage, serialized, digest(serialized), schema_name,
                 json.dumps(candidate, ensure_ascii=False, sort_keys=True), priority, now()),
            )
            return job_id

    def next_router_shadow(self) -> dict[str, Any] | None:
        with self.connection() as c:
            row = c.execute("SELECT * FROM router_shadow_job WHERE state='queued' ORDER BY priority DESC,created_at LIMIT 1").fetchone()
            if not row:
                return None
            c.execute("UPDATE router_shadow_job SET state='running',started_at=? WHERE job_id=?", (now(), row["job_id"]))
            result = dict(row); result["state"] = "running"; return result

    def finish_router_shadow(self, job_id: str, *, output: dict[str, Any] | None = None, verifier: dict[str, Any] | None = None, error: str | None = None) -> None:
        serialized = json.dumps(output, ensure_ascii=False, sort_keys=True) if output is not None else None
        with self.connection() as c:
            c.execute(
                """UPDATE router_shadow_job SET state=?,completed_at=?,output_json=?,output_sha256=?,verifier_json=?,error=? WHERE job_id=?""",
                ("succeeded" if output is not None and not error else "failed", now(), serialized,
                 digest(serialized) if serialized else None, json.dumps(verifier or {}, ensure_ascii=False, sort_keys=True),
                 error[-2000:] if error else None, job_id),
            )

    def upsert_risk_doctrine(self, doctrine: dict[str, Any], *, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        """Only evidence-backed internal evolution may revise the doctrine; UI chat never calls this."""
        self.initialize()
        key = "independent_ai_v1"
        with self.connection() as c:
            old = c.execute("SELECT * FROM ai_risk_doctrine WHERE doctrine_key=?", (key,)).fetchone()
            payload = dict(doctrine); payload["evidence"] = evidence or payload.get("evidence") or []
            c.execute(
                """INSERT INTO ai_risk_doctrine(doctrine_key,doctrine_json,revision,previous_doctrine_json,state,updated_at)
                   VALUES(?,?,?,?, 'active', ?)
                   ON CONFLICT(doctrine_key) DO UPDATE SET doctrine_json=excluded.doctrine_json,
                     revision=ai_risk_doctrine.revision+1,previous_doctrine_json=ai_risk_doctrine.doctrine_json,updated_at=excluded.updated_at""",
                (key, json.dumps(payload, ensure_ascii=False, sort_keys=True), 1, None, now()),
            )
            return dict(c.execute("SELECT * FROM ai_risk_doctrine WHERE doctrine_key=?", (key,)).fetchone())

    def risk_doctrine(self) -> dict[str, Any]:
        self.initialize()
        with self.connection() as c:
            row = c.execute("SELECT * FROM ai_risk_doctrine WHERE doctrine_key='independent_ai_v1'").fetchone()
        if row:
            result = dict(row); result["doctrine"] = json.loads(result["doctrine_json"]); return result
        return self.upsert_risk_doctrine({
            "version": 1,
            "name": "AI 独立专业风险立场",
            "rules": {
                "leverage": "不认可杠杆建议",
                "single_stock_max_known_assets": 0.20,
                "same_theme_max_known_assets": 0.40,
                "planned_loss_max_known_assets": 0.01,
                "drawdown_review_threshold": 0.15,
                "exact_sizing_requires_portfolio_freshness_trading_days": 1,
            },
            "boundary": "用户实际交易具有事实权威；本立场只决定 AI 是否认可为合格执行建议。",
        }, evidence={"source": "APP_DEVELOPMENT_PRINCIPLES.md", "revision": 1})

    def save_market_regime(self, cycle_id: str, as_of: str, regime: str, metrics: dict[str, Any], data_quality: str) -> None:
        if regime not in {"trend_expansion", "divergence", "risk_contraction", "unknown"}:
            raise ValueError("unsupported market regime")
        with self.connection() as c:
            c.execute(
                """INSERT INTO market_regime_snapshot(snapshot_id,cycle_id,as_of,regime,metrics_json,data_quality,created_at)
                   VALUES(?,?,?,?,?,?,?) ON CONFLICT(cycle_id) DO UPDATE SET as_of=excluded.as_of,
                     regime=excluded.regime,metrics_json=excluded.metrics_json,data_quality=excluded.data_quality""",
                (str(uuid.uuid4()), cycle_id, as_of, regime, json.dumps(metrics, ensure_ascii=False, sort_keys=True), data_quality, now()),
            )

    def record_router_evaluation(self, cell_key: str, cycle_id: str, horizon: str, regime: str | None, baseline_artifact_id: str | None, shadow_job_id: str, baseline_score: dict[str, Any], candidate_score: dict[str, Any], state: str = "resolved") -> None:
        with self.connection() as c:
            c.execute(
                """INSERT INTO router_evaluation(evaluation_id,cell_key,cycle_id,horizon,regime,baseline_artifact_id,shadow_job_id,baseline_score_json,candidate_score_json,state,created_at,resolved_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(cycle_id,horizon,shadow_job_id) DO UPDATE SET
                     baseline_score_json=excluded.baseline_score_json,candidate_score_json=excluded.candidate_score_json,state=excluded.state,resolved_at=excluded.resolved_at""",
                (str(uuid.uuid4()), cell_key, cycle_id, horizon, regime, baseline_artifact_id, shadow_job_id,
                 json.dumps(baseline_score, ensure_ascii=False, sort_keys=True), json.dumps(candidate_score, ensure_ascii=False, sort_keys=True),
                 state, now(), now() if state == "resolved" else None),
            )

    def router_evaluations(self, cell_key: str) -> list[dict[str, Any]]:
        with self.connection() as c:
            return [dict(row) for row in c.execute("SELECT * FROM router_evaluation WHERE cell_key=? ORDER BY created_at", (cell_key,))]

    def set_router_policy_mode(self, cell_key: str, mode: str, qualification_fingerprint: str | None = None) -> dict[str, Any]:
        if mode not in {"shadow", "promoted", "rolled_back"}:
            raise ValueError("invalid router policy mode")
        with self.connection() as c:
            row = c.execute("SELECT * FROM router_policy_cell WHERE cell_key=?", (cell_key,)).fetchone()
            if not row:
                raise ValueError("unknown router policy cell")
            c.execute(
                """UPDATE router_policy_cell SET previous_json=?,mode=?,revision=revision+1,qualification_fingerprint=?,updated_at=? WHERE cell_key=?""",
                (json.dumps(dict(row), ensure_ascii=False, sort_keys=True), mode, qualification_fingerprint, now(), cell_key),
            )
            return dict(c.execute("SELECT * FROM router_policy_cell WHERE cell_key=?", (cell_key,)).fetchone())

    def get_router_policy_cell(self, cell_key: str) -> dict[str, Any]:
        with self.connection() as c:
            row = c.execute("SELECT * FROM router_policy_cell WHERE cell_key=?", (cell_key,)).fetchone()
        if not row:
            raise ValueError("unknown router policy cell")
        return dict(row)

    def record_evidence(self, cycle: dict[str, Any], stage: str, evidence: dict[str, Any]) -> list[str]:
        trading_date = cycle["scheduled_for"][:10]
        known_at = now()
        inserted: list[str] = []
        sources = evidence.get("sources") if isinstance(evidence.get("sources"), list) else []
        with self.connection() as c:
            for source in sources:
                if not isinstance(source, dict):
                    continue
                url = str(source.get("url") or "")
                title = str(source.get("title") or "")
                body = str(source.get("excerpt") or title).strip()
                if not body:
                    continue
                assert_safe(body, boundary="evidence fact storage")
                fingerprint = digest("\n".join((url, title, body)))
                evidence_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{trading_date}|{url}|{fingerprint}"))
                metadata = {
                    "task_key": cycle["task_key"],
                    "factual_reliability": source.get("factual_reliability"),
                    "market_propagation": source.get("market_propagation"),
                    "published_at": source.get("published_at"),
                    "source_family": source.get("source_family"),
                    "upstream_id": source.get("upstream_id"),
                    "tool_observation_id": source.get("tool_observation_id"),
                    "result_item_hash": source.get("result_item_hash"),
                }
                changed = c.execute(
                    """INSERT OR IGNORE INTO evidence_ledger_entry(
                         evidence_id,trading_date,cycle_id,source_url,source_title,body_text,
                         occurred_at,known_at,metadata_json,stage,content_sha256,coverage_state)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,'observed')""",
                    (evidence_id, trading_date, cycle["cycle_id"], url, title, body,
                     source.get("fact_as_of") or source.get("published_or_retrieved_at"), known_at,
                     json.dumps(metadata, ensure_ascii=False, sort_keys=True), stage, fingerprint),
                ).rowcount
                if changed:
                    inserted.append(evidence_id)
                    c.execute(
                        """INSERT OR IGNORE INTO memory_index_intent(
                             intent_id,origin_type,origin_id,content_sha256,known_at,state,created_at)
                           VALUES(?,?,?,?,?,'pending',?)""",
                        (str(uuid.uuid4()), "evidence", evidence_id, fingerprint, known_at, now()),
                    )
                c.execute(
                    """INSERT OR IGNORE INTO evidence_cycle_use(cycle_id,evidence_id,stage,used_at)
                       VALUES(?,?,?,?)""",
                    (cycle["cycle_id"], evidence_id, stage, known_at),
                )
            for gap in evidence.get("critical_gaps") or []:
                body = str(gap).strip()
                if not body:
                    continue
                assert_safe(body, boundary="evidence fact storage")
                fingerprint = digest(f"gap|{stage}|{body}")
                evidence_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{trading_date}|gap|{fingerprint}"))
                changed = c.execute(
                    """INSERT OR IGNORE INTO evidence_ledger_entry(
                         evidence_id,trading_date,cycle_id,source_url,source_title,body_text,
                         occurred_at,known_at,metadata_json,stage,content_sha256,coverage_state)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,'missing')""",
                    (evidence_id, trading_date, cycle["cycle_id"], "", "关键缺口", body,
                     None, known_at, json.dumps({"task_key": cycle["task_key"]}, ensure_ascii=False),
                     stage, fingerprint),
                ).rowcount
                if changed:
                    inserted.append(evidence_id)
                    c.execute(
                        """INSERT OR IGNORE INTO memory_index_intent(
                             intent_id,origin_type,origin_id,content_sha256,known_at,state,created_at)
                           VALUES(?,?,?,?,?,'pending',?)""",
                        (str(uuid.uuid4()), "evidence", evidence_id, fingerprint, known_at, now()),
                    )
                c.execute(
                    """INSERT OR IGNORE INTO evidence_cycle_use(cycle_id,evidence_id,stage,used_at)
                       VALUES(?,?,?,?)""",
                    (cycle["cycle_id"], evidence_id, stage, known_at),
                )
        return inserted

    def evidence_for_day(self, trading_date: str, known_at: str, *, limit: int = 120) -> list[dict[str, Any]]:
        with self.connection() as c:
            return [dict(row) for row in c.execute(
                """SELECT * FROM evidence_ledger_entry
                   WHERE trading_date=? AND known_at<=?
                   ORDER BY known_at,evidence_id LIMIT ?""",
                (trading_date, known_at, limit),
            )]

    def save_judgment_snapshot(
        self, artifact_id: str, cycle_id: str, kind: str, snapshot: dict[str, Any], as_of: str,
        *, connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        snapshot_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"judgment|{artifact_id}"))
        payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        def save(c: sqlite3.Connection) -> dict[str, Any]:
            c.execute(
                """INSERT OR IGNORE INTO judgment_snapshot(
                     snapshot_id,artifact_id,cycle_id,kind,snapshot_json,as_of,created_at,verification_status)
                   VALUES(?,?,?,?,?,?,?,'unverified')""",
                (snapshot_id, artifact_id, cycle_id, kind, payload, as_of, now()),
            )
            return dict(c.execute("SELECT * FROM judgment_snapshot WHERE artifact_id=?", (artifact_id,)).fetchone())
        if connection is not None:
            return save(connection)
        with self.connection() as c:
            return save(c)

    def judgment_snapshots(self, cycle_id: str | None = None) -> list[dict[str, Any]]:
        with self.connection() as c:
            if cycle_id:
                rows = c.execute("SELECT * FROM judgment_snapshot WHERE cycle_id=? ORDER BY created_at", (cycle_id,))
            else:
                rows = c.execute("SELECT * FROM judgment_snapshot ORDER BY created_at")
            return [dict(row) for row in rows]

    def schedule_outcome(self, snapshot_id: str, horizon: str, due_at: str, *, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        checkpoint_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"outcome|{snapshot_id}|{horizon}"))
        def save(c: sqlite3.Connection) -> dict[str, Any]:
            c.execute(
                """INSERT OR IGNORE INTO outcome_checkpoint(
                     checkpoint_id,snapshot_id,horizon,as_of,outcome_json,created_at,due_at,status,
                     result_artifact_id,error,attempt_count)
                   VALUES(?,?,?,'','{}',?,?, 'pending',NULL,NULL,0)""",
                (checkpoint_id, snapshot_id, horizon, now(), due_at),
            )
            return dict(c.execute("SELECT * FROM outcome_checkpoint WHERE checkpoint_id=?", (checkpoint_id,)).fetchone())
        if connection is not None:
            return save(connection)
        with self.connection() as c:
            return save(c)

    def due_outcomes(self, at: str, *, limit: int = 4) -> list[dict[str, Any]]:
        with self.connection() as c:
            return [dict(row) for row in c.execute(
                """SELECT o.*,s.cycle_id,s.kind,s.snapshot_json,s.as_of AS judgment_as_of,
                          a.body_markdown AS judgment_text
                   FROM outcome_checkpoint o
                   JOIN judgment_snapshot s ON s.snapshot_id=o.snapshot_id
                   JOIN narrative_artifact a ON a.artifact_id=s.artifact_id
                   WHERE o.status IN ('pending','retry') AND o.due_at<=?
                   ORDER BY o.due_at,o.checkpoint_id LIMIT ?""",
                (at, limit),
            )]

    def complete_outcome(self, checkpoint_id: str, as_of: str, outcome: dict[str, Any], artifact_id: str) -> None:
        status = str(outcome.get("verification_status") or "unverified")
        with self.connection() as c:
            row = c.execute("SELECT snapshot_id FROM outcome_checkpoint WHERE checkpoint_id=?", (checkpoint_id,)).fetchone()
            if not row:
                raise ValueError("unknown outcome checkpoint")
            c.execute(
                """UPDATE outcome_checkpoint SET status='complete',as_of=?,outcome_json=?,
                     result_artifact_id=?,error=NULL,attempt_count=attempt_count+1 WHERE checkpoint_id=?""",
                (as_of, json.dumps(outcome, ensure_ascii=False, sort_keys=True), artifact_id, checkpoint_id),
            )
            c.execute("UPDATE judgment_snapshot SET verification_status=? WHERE snapshot_id=?", (status, row["snapshot_id"]))

    def fail_outcome(self, checkpoint_id: str, error: str, *, retry: bool = True, retry_at: str | None = None) -> None:
        with self.connection() as c:
            c.execute(
                """UPDATE outcome_checkpoint SET status=?,error=?,due_at=COALESCE(?,due_at),attempt_count=attempt_count+1
                   WHERE checkpoint_id=?""",
                ("retry" if retry else "failed", error[-2000:], retry_at, checkpoint_id),
            )

    def defer_outcome(self, checkpoint_id: str, next_check_at: str, reason: str) -> None:
        with self.connection() as c:
            c.execute(
                """UPDATE outcome_checkpoint SET status='retry',due_at=?,error=?,attempt_count=attempt_count+1
                   WHERE checkpoint_id=?""",
                (next_check_at, reason[-2000:], checkpoint_id),
            )

    def workflow_policy(self, policy_key: str) -> dict[str, Any] | None:
        with self.connection() as c:
            row = c.execute("SELECT policy_json,revision,updated_at FROM workflow_policy WHERE policy_key=?", (policy_key,)).fetchone()
        if not row:
            return None
        return {"policy": json.loads(row["policy_json"]), "revision": row["revision"], "updated_at": row["updated_at"]}

    def save_workflow_policy(self, policy_key: str, policy: dict[str, Any]) -> dict[str, Any]:
        raw = json.dumps(policy, ensure_ascii=False, sort_keys=True)
        at = now()
        with self.connection() as c:
            existing = c.execute("SELECT policy_json,revision FROM workflow_policy WHERE policy_key=?", (policy_key,)).fetchone()
            if existing:
                revision = int(existing["revision"]) + 1
                c.execute(
                    """UPDATE workflow_policy SET previous_policy_json=policy_json,policy_json=?,revision=?,updated_at=?
                       WHERE policy_key=?""",
                    (raw, revision, at, policy_key),
                )
            else:
                revision = 1
                c.execute(
                    "INSERT INTO workflow_policy(policy_key,policy_json,revision,previous_policy_json,updated_at) VALUES(?,?,1,NULL,?)",
                    (policy_key, raw, at),
                )
        return {"policy": policy, "revision": revision, "updated_at": at}

    def rollback_workflow_policy(self, policy_key: str) -> dict[str, Any] | None:
        at = now()
        with self.connection() as c:
            row = c.execute(
                "SELECT policy_json,previous_policy_json,revision FROM workflow_policy WHERE policy_key=?",
                (policy_key,),
            ).fetchone()
            if not row or not row["previous_policy_json"]:
                return None
            c.execute(
                """UPDATE workflow_policy SET policy_json=?,previous_policy_json=policy_json,
                   revision=revision+1,updated_at=? WHERE policy_key=?""",
                (row["previous_policy_json"], at, policy_key),
            )
            policy = json.loads(row["previous_policy_json"])
        return {"policy": policy, "revision": int(row["revision"]) + 1, "updated_at": at}

    def effective_m1_reserve(self, task_key: str, default_seconds: int) -> tuple[int, int]:
        """Read the governed timing policy; statistics never mutate policy state."""
        with self.connection() as c:
            current = c.execute("SELECT * FROM timing_policy WHERE task_key=?", (task_key,)).fetchone()
            if current:
                return int(current["reserve_seconds"]), int(current["revision"])
        return int(default_seconds), 1

    def rollback_timing_policy(self, task_key: str) -> tuple[int, int] | None:
        with self.connection() as c:
            row = c.execute("SELECT * FROM timing_policy WHERE task_key=?", (task_key,)).fetchone()
            if not row or row["previous_reserve_seconds"] is None:
                return None
            reserve = int(row["previous_reserve_seconds"])
            revision = int(row["revision"]) + 1
            c.execute(
                """UPDATE timing_policy SET reserve_seconds=?,previous_reserve_seconds=reserve_seconds,
                   revision=?,updated_at=? WHERE task_key=?""",
                (reserve, revision, now(), task_key),
            )
        return reserve, revision

    def queue_research_job(self, cycle_id: str, source_artifact_id: str, public_scope: dict[str, Any]) -> dict[str, Any]:
        job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"chat-research|{source_artifact_id}"))
        with self.connection() as c:
            c.execute(
                """INSERT OR IGNORE INTO companion_research_job(
                     job_id,cycle_id,source_artifact_id,public_scope_json,state,created_at,completed_at,error,attempt_count)
                   VALUES(?,?,?,?,'pending',?,NULL,NULL,0)""",
                (job_id, cycle_id, source_artifact_id,
                 json.dumps(public_scope, ensure_ascii=False, sort_keys=True), now()),
            )
            row = c.execute("SELECT * FROM companion_research_job WHERE job_id=?", (job_id,)).fetchone()
        return dict(row)

    def pending_research_jobs(self, *, limit: int = 4) -> list[dict[str, Any]]:
        with self.connection() as c:
            return [dict(row) for row in c.execute(
                """SELECT * FROM companion_research_job WHERE state IN ('pending','retry')
                   ORDER BY created_at LIMIT ?""",
                (limit,),
            )]

    def finish_research_job(self, job_id: str, *, error: str | None = None, retry: bool = False) -> None:
        with self.connection() as c:
            c.execute(
                """UPDATE companion_research_job SET state=?,completed_at=?,error=?,attempt_count=attempt_count+1
                   WHERE job_id=?""",
                ("retry" if retry else "failed" if error else "complete", now(), error[-2000:] if error else None, job_id),
            )

    def receipt(self, command_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        raw=json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(",",":")); h=digest(raw)
        with self.connection() as c:
            row=c.execute("SELECT payload_sha256,result_json FROM companion_command_receipt WHERE command_id=?",(command_id,)).fetchone()
            if not row:return None
            if row["payload_sha256"]!=h: raise ValueError("command id conflict")
            return json.loads(row["result_json"])

    def save_receipt(self, command_id: str, cycle_id: str | None, command_type: str, payload: dict[str, Any], result: dict[str, Any]) -> None:
        raw=json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(",",":"));
        with self.connection() as c:c.execute("INSERT INTO companion_command_receipt VALUES(?,?,?,?,?,?)",(command_id,cycle_id,command_type,digest(raw),now(),json.dumps(result,ensure_ascii=False,sort_keys=True)))

    def queue_event(self, cycle_id: str, event_type: str, payload: dict[str, Any], *, connection: sqlite3.Connection | None = None) -> str:
        event_id=str(uuid.uuid4())
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        created_at = now()
        values = (event_id, cycle_id, event_type, payload_json, created_at)
        if connection is not None:
            connection.execute("INSERT INTO companion_outbox VALUES(?,?,?,?,?,NULL)", values)
            self._queue_client_event(event_id, "companion-client-event/v1", cycle_id, event_type, payload_json, created_at, connection)
        else:
            with self.connection() as c:
                c.execute("INSERT INTO companion_outbox VALUES(?,?,?,?,?,NULL)", values)
                self._queue_client_event(event_id, "companion-client-event/v1", cycle_id, event_type, payload_json, created_at, c)
        return event_id

    def pending_events(self) -> list[dict[str, Any]]:
        with self.connection() as c:return [dict(x) for x in c.execute("SELECT * FROM companion_outbox WHERE delivered_at IS NULL ORDER BY created_at")]

    def mark_event_delivered(self,event_id:str)->None:
        with self.connection() as c:c.execute("UPDATE companion_outbox SET delivered_at=? WHERE event_id=?",(now(),event_id))

    def queue_portfolio_event(self, event_type: str, payload: dict[str, Any]) -> str:
        event_id = str(uuid.uuid4())
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        created_at = now()
        with self.connection() as c:
            c.execute(
                "INSERT INTO portfolio_outbox VALUES(?,?,?,?,NULL)",
                (event_id, event_type, payload_json, created_at),
            )
            self._queue_client_event(event_id, "portfolio-client-event/v1", None, event_type, payload_json, created_at, c)
        return event_id

    def queue_schedule_event(self, event_type: str, payload: dict[str, Any]) -> str:
        event_id = str(uuid.uuid4())
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        created_at = now()
        with self.connection() as c:
            c.execute("INSERT INTO schedule_outbox VALUES(?,?,?,?,NULL)", (event_id, event_type, payload_json, created_at))
            self._queue_client_event(event_id, "schedule-client-event/v1", None, event_type, payload_json, created_at, c)
        return event_id

    def pending_schedule_events(self) -> list[dict[str, Any]]:
        with self.connection() as c:
            return [dict(row) for row in c.execute("SELECT * FROM schedule_outbox WHERE delivered_at IS NULL ORDER BY created_at")]

    def mark_schedule_event_delivered(self, event_id: str) -> None:
        with self.connection() as c:
            c.execute("UPDATE schedule_outbox SET delivered_at=? WHERE event_id=?", (now(), event_id))

    def pending_portfolio_events(self) -> list[dict[str, Any]]:
        with self.connection() as c:
            return [dict(row) for row in c.execute(
                "SELECT * FROM portfolio_outbox WHERE delivered_at IS NULL ORDER BY created_at"
            )]

    def mark_portfolio_event_delivered(self, event_id: str) -> None:
        with self.connection() as c:
            c.execute("UPDATE portfolio_outbox SET delivered_at=? WHERE event_id=?", (now(), event_id))
