from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .learning import JudgmentLifecycle
from .evidence_contract import EvidenceContractFactory
from .message_presentation import MessageQualificationError, PresentedMessage, present_message, repair_message_draft
from .publication_registry import published_event_types
from .stage_expression import normalize_stage_output
from .models import TASK_POLICIES
from .secret_guard import assert_safe
from .store import is_structured_test_provenance
from .task_profiles import ManualAnalysisProfileResolver


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class CompanionEngine:
    """Deep module for cycle commands, invariants, immutable artifacts and client events."""

    def __init__(
        self, store: Any, *, task_profiles: ManualAnalysisProfileResolver | None = None,
        evidence_contract_factory: EvidenceContractFactory | None = None,
        memory: Any | None = None, memory_space_id: str = "ai-trading-companion",
    ) -> None:
        self.store = store
        self.store.initialize()
        self.judgments = JudgmentLifecycle(store)
        self.task_profiles = task_profiles or ManualAnalysisProfileResolver()
        self.evidence_contract_factory = evidence_contract_factory or EvidenceContractFactory(self.task_profiles.calendar)
        self.memory = memory
        self.memory_space_id = memory_space_id

    def record_submitted_messages(self, cycle_id: str, messages: list[dict[str, Any]]) -> None:
        if self.memory is None:
            return
        for message in messages:
            submitted_at = str(message.get("submitted_at") or message["known_at"])
            provenance = json.loads(message.get("provenance_json") or "{}")
            is_test = is_structured_test_provenance(provenance)
            self.memory.append({
                "memory_space_id": self.memory_space_id,
                "source_system": "stock-advisor",
                "source_event_id": str(message["message_id"]),
                "content_hash": "auto",
                "episode_type": "test_utterance" if is_test else "user_message",
                "body": str(message["body_text"]),
                "occurred_at": str(message.get("occurred_at") or submitted_at),
                "known_at": str(message.get("known_at") or submitted_at),
                "submitted_at": submitted_at,
                "authority": "user_private_fact",
                "protocol_version": "memoryhub/v1",
                "metadata": {
                    "message_id": message["message_id"], "cycle_id": cycle_id,
                    "batch_id": message.get("batch_id"), "phase": message.get("phase"),
                    "state": "submitted", "actor": "human",
                    "provenance": provenance,
                },
            })

    def recover_interrupted_streams(self) -> int:
        streams = self.store.interrupted_stream_messages()
        for stream in streams:
            self.chat_stream_failed(stream["cycle_id"], stream["stream_id"], "runtime restarted")
        return len(streams)

    def start_cycle(
        self, task_key: str, scheduled_for: str, as_of: str | None = None, *,
        schedule_id: str | None = None, schedule_revision: int | None = None,
        schedule_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if task_key not in TASK_POLICIES:
            raise ValueError(f"unregistered task_key: {task_key}")
        work_start_at = scheduled_for
        if schedule_snapshot:
            lead = int((schedule_snapshot.get("trigger") or {}).get("lead_minutes", 0))
            work_start_at = (parse(scheduled_for) - timedelta(minutes=lead)).isoformat(timespec="seconds")
        cycle = self.store.create_cycle(
            task_key, scheduled_for, as_of or iso(utc_now()), schedule_id=schedule_id,
            schedule_revision=schedule_revision, schedule_snapshot=schedule_snapshot,
            work_start_at=work_start_at,
        )
        self.emit(cycle, "cycle.created", cycle)
        return cycle

    def request_formal_analysis(self, request: dict[str, Any]) -> dict[str, Any]:
        """Create or reuse one manual formal-analysis occurrence from a stable request."""
        missing = {"request_id", "requested_at", "source"} - request.keys()
        if missing:
            raise ValueError(f"formal analysis request missing: {sorted(missing)}")
        if not str(request["request_id"] or "").strip():
            raise ValueError("formal analysis request request_id is required")
        requested_at = str(request["requested_at"])
        requested = parse(requested_at)
        if requested.tzinfo is None:
            raise ValueError("requested_at must be timezone-aware")
        source = request["source"]
        if not isinstance(source, dict) or not source:
            raise ValueError("formal analysis request source must be a non-empty object")
        profile_snapshot: dict[str, Any] | None = None
        evidence_contract: dict[str, Any] | None = None
        if "analysis" in request:
            profile_snapshot = self.task_profiles.resolve(requested_at, request["analysis"])
            task_key = str(profile_snapshot["task_key"])
            profile = profile_snapshot
            # The manual cycle persists its contract before packet construction,
            # so it must freeze the same runtime-held portfolio universe that a
            # scheduled packet builder would supply later.
            with self.store.connection() as connection:
                positions = connection.execute(
                    "SELECT code FROM portfolio_position WHERE shares>0 ORDER BY code"
                ).fetchall()
            evidence_contract = self.evidence_contract_factory.build(
                task_key=task_key, stage="m0_research", as_of=requested_at,
                task_profile=profile_snapshot,
                internal_context={"portfolio_entities": [str(row["code"]) for row in positions],
                                  "prior_opportunity_plans": self.store.opportunity_plans_before(requested_at[:10], requested_at)},
            )
        else:
            missing = {"task_key", "task_profile"} - request.keys()
            if missing:
                raise ValueError(f"formal analysis request missing: {sorted(missing)}")
            task_key = str(request["task_key"])
            profile = dict(request["task_profile"] or {})
            # The explicit-profile API is used by trusted local clients that
            # already selected a profile. Persist the supplied snapshot rather
            # than dropping it; a malformed legacy profile is then terminally
            # reported by the worker instead of remaining queued forever.
            profile_snapshot = profile
        if task_key not in TASK_POLICIES:
            raise ValueError(f"unregistered task_key: {task_key}")
        if not isinstance(profile, dict) or not str(profile.get("profile_id") or ""):
            raise ValueError("formal analysis request task_profile.profile_id is required")
        try:
            profile_version = int(profile["version"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("formal analysis request task_profile.version is required") from exc
        if profile_version < 1:
            raise ValueError("formal analysis request task_profile.version must be positive")

        cycle, created = self.store.create_manual_analysis_cycle(
            request_id=str(request["request_id"]),
            task_key=task_key,
            requested_at=requested_at,
            source=source,
            task_profile_id=str(profile["profile_id"]),
            task_profile_version=profile_version,
            task_profile=profile_snapshot,
            evidence_contract=evidence_contract,
        )
        receipt = {
            "kind": "analysis.request",
            "state": "created" if created else "reused",
            "request_id": str(request["request_id"]),
            "cycle_id": cycle["cycle_id"],
        }
        self.emit(cycle, f"analysis.request.{receipt['state']}", {"cycle": cycle, "receipt": receipt})
        return {"receipt": receipt, "projection": self._projection(cycle)}

    def refresh_manual_analysis_contract(self, cycle_id: str, as_of: str) -> dict[str, Any]:
        """Bind a manual contract to the real acquisition start, not queue time."""
        cycle = self.store.get_cycle(cycle_id)
        if cycle.get("kind") != "manual":
            return cycle
        profile = json.loads(cycle.get("task_profile_json") or "{}")
        if not profile:
            raise ValueError("manual analysis task profile is missing")
        with self.store.connection() as connection:
            positions = connection.execute(
                "SELECT code FROM portfolio_position WHERE shares>0 ORDER BY code"
            ).fetchall()
        contract = self.evidence_contract_factory.build(
            task_key=str(cycle["task_key"]), stage="m0_research", as_of=as_of,
            task_profile=profile,
            internal_context={"portfolio_entities": [str(row["code"]) for row in positions],
                              "prior_opportunity_plans": self.store.opportunity_plans_before(cycle["scheduled_for"][:10], as_of)},
        )
        return self.store.refresh_manual_analysis_contract(cycle_id, as_of, contract)

    def start_diagnostic_rerun(self, source_cycle_id: str) -> dict[str, Any]:
        cycle = self.store.create_diagnostic_cycle(source_cycle_id)
        self.emit(cycle, "cycle.diagnostic_rerun.created", {
            "cycle": cycle,
            "source_cycle_id": source_cycle_id,
        })
        return cycle

    def research_started(self, cycle_id: str, *, as_of: str | None = None) -> dict[str, Any]:
        cycle = self.store.get_cycle(cycle_id)
        if cycle["state"] != "queued":
            raise ValueError(f"cycle is not queued: {cycle['state']}")
        batch_id, newly_submitted = self.store.commit_staged_messages(cycle_id, "pre_m0")
        messages = self.store.messages(cycle_id, state="submitted", phase="pre_m0")
        if messages and (newly_submitted or not self.store.latest_artifact(cycle_id, "pre_m0")):
            body = "\n\n".join(message["body_text"] for message in messages)
            artifact = self.store.append_artifact(
                cycle_id,
                "pre_m0",
                "human",
                body,
                as_of or iso(utc_now()),
                {
                    "batch_id": batch_id,
                    "message_ids": [message["message_id"] for message in messages],
                    "role": "unverified_companion_context",
                },
            )
            self.store.link_messages_to_artifact(
                [message["message_id"] for message in messages], artifact["artifact_id"]
            )
            self.emit(cycle, "pre_m0.locked", {
                "cycle": cycle,
                "batch_id": batch_id,
                "messages": messages,
                "source_artifact_id": artifact["artifact_id"],
            })
        cycle = self.store.transition(cycle_id, "researching_m0", as_of=as_of or iso(utc_now()))
        self.emit(cycle, "m0.started", cycle)
        return cycle

    def research_failed(self, cycle_id: str, reason: str, *, details: dict[str, Any] | None = None) -> dict[str, Any]:
        message = self._stage_failure_message("M0", reason, details)
        cycle = self.store.transition(cycle_id, "failed")
        self._emit_failure(
            cycle, "research.failed", message, reason,
            {"diagnostic_code": self._diagnostic_code(reason)},
        )
        return cycle

    def research_retrying(self, cycle_id: str, reason: str, attempt: int) -> dict[str, Any]:
        cycle = self.store.get_cycle(cycle_id)
        self.emit(cycle, "research.retrying", {
            "cycle": cycle, "reason": self._user_fault_message(reason, "M0"),
            "diagnostic_code": self._diagnostic_code(reason), "attempt": attempt,
        })
        return cycle

    def recover_research(self, cycle_id: str, reason: str) -> dict[str, Any]:
        cycle = self.store.get_cycle(cycle_id)
        if cycle["state"] not in {"researching", "researching_m0"}:
            raise ValueError(f"cycle is not researching: {cycle['state']}")
        cycle = self.store.transition(cycle_id, "queued")
        self.emit(cycle, "research.recovered", {"cycle": cycle, "reason": reason})
        return cycle

    def mark_missed(self, cycle_id: str, reason: str) -> dict[str, Any]:
        cycle = self.store.get_cycle(cycle_id)
        if cycle["state"] != "queued":
            raise ValueError(f"only queued cycle can be missed: {cycle['state']}")
        cycle = self.store.transition(cycle_id, "missed")
        self._emit_failure(cycle, "cycle.missed", self._user_fault_message(reason, "这次定时判断"), reason)
        return cycle

    def research_ready(
        self,
        cycle_id: str,
        m0: str,
        *,
        evidence_attempt_id: str,
        compose_attempt_id: str,
        evidence_packet_hash: str,
        packet_hash: str,
        evidence_as_of: str | None = None,
    ) -> dict[str, Any]:
        cycle = self.store.get_cycle(cycle_id)
        if cycle["state"] not in {"researching_m0", "researching"}:
            raise ValueError(f"cycle is not researching M0: {cycle['state']}")
        self.store.verified_attempt(evidence_attempt_id, cycle_id, "m0_research", evidence_packet_hash)
        compose_attempt = self.store.verified_attempt(compose_attempt_id, cycle_id, "m0_compose", packet_hash)
        compose_output = json.loads(compose_attempt.get("output_json") or "null")
        verified_m0 = normalize_stage_output("m0_compose", compose_output if isinstance(compose_output, dict) else {}).text
        if verified_m0 != m0:
            raise ValueError("M0 body does not match the verified compose attempt")
        ready_at = utc_now()
        profile_json = cycle.get("task_profile_json")
        if cycle.get("trigger") == "manual_chat" and profile_json:
            deadlines = self.task_profiles.delivery_deadlines(json.loads(profile_json), iso(ready_at))
            auto_submit, publish = parse(deadlines["h0_auto_submit_at"]), parse(deadlines["m1_publish_deadline"])
            reserve_seconds = int((publish - auto_submit).total_seconds())
            timing_version = int(cycle.get("task_profile_version") or 1)
        else:
            policy = TASK_POLICIES[cycle["task_key"]]
            reserve_seconds, timing_version = self.store.effective_m1_reserve(
                cycle["task_key"], int(policy.m1_reserve.total_seconds()),
            )
            auto_submit, publish = policy.deadlines(
                cycle["scheduled_for"], ready_at, reserve=timedelta(seconds=reserve_seconds),
            )
        presented = self.present_for_publication(m0, evidence_as_of or cycle["as_of"], "m0")
        self._append_published_memory(cycle, presented)
        with self.store.connection() as connection:
            artifact = self.store.append_artifact(
                cycle_id, "m0", "model", presented.markdown, evidence_as_of or cycle["as_of"],
                self._presentation_metadata({"direction_free": True, "evidence_attempt_id": evidence_attempt_id, "compose_attempt_id": compose_attempt_id}, presented),
                connection=connection,
            )
            cycle = self.store.transition(
                cycle_id, "awaiting_h0", connection=connection,
                human_deadline=iso(auto_submit), h0_auto_submit_at=iso(auto_submit),
                m1_publish_deadline=iso(publish), packet_hash=packet_hash,
                m1_reserve_seconds=reserve_seconds, timing_policy_version=timing_version,
            )
            resolved_fault_episode_ids = self.store.resolve_fault_episodes(
                cycle_id,
                stages=["m0"],
                resolution_artifact_id=artifact["artifact_id"],
                resolved_at=artifact["sealed_at"],
                connection=connection,
            )
            self._queue_event(cycle_id, "m0.ready", {
                "cycle": cycle,
                "m0": presented.markdown,
                "presentation": presented.metadata()["presentation"],
                "message": presented.message(),
                "source_artifact_id": artifact["artifact_id"],
                "resolved_fault_episode_ids": resolved_fault_episode_ids,
                "h0_auto_submit_at": cycle["h0_auto_submit_at"],
                "m1_publish_deadline": cycle["m1_publish_deadline"],
            }, connection=connection)
        return cycle

    def command(self, command: dict[str, Any]) -> dict[str, Any]:
        missing = {"command_id", "type"} - command.keys()
        if missing:
            raise ValueError(f"command missing: {sorted(missing)}")
        previous = self.store.receipt(command["command_id"], command)
        if previous is not None:
            return previous
        typ = command["type"]
        cycle_id = command.get("cycle_id")
        if typ == "request_today_projections":
            scheduled_date = str(command.get("scheduled_date") or utc_now().astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat())
            projections = []
            for current in self.store.latest_cycles_for_date(scheduled_date):
                projection = self._projection(current)
                self.emit(current, "projection.ready", projection)
                projections.append(projection)
            result = {"scheduled_date": scheduled_date, "projections": projections}
        elif typ == "start_cycle":
            result = self.start_cycle(command["task_key"], command["scheduled_for"], command.get("as_of"))
        elif typ == "request_formal_analysis":
            try:
                result = self.request_formal_analysis(command)
            except ValueError as exc:
                result = {
                    "receipt": {
                        "kind": "analysis.request",
                        "state": "rejected",
                        "request_id": str(command.get("request_id") or ""),
                        "reason": str(exc),
                    }
                }
            else:
                cycle_id = result["receipt"]["cycle_id"]
        elif typ == "dismiss_manual_analyses":
            task_profile_id = str(command.get("task_profile_id") or "").strip()
            if not task_profile_id:
                raise ValueError("task_profile_id required")
            reason = str(command.get("reason") or "user_requested_cleanup").strip()
            cycles = self.store.dismiss_manual_analyses(task_profile_id, reason)
            for dismissed_cycle in cycles:
                self.emit(dismissed_cycle, "analysis.dismissed", {
                    "cycle": dismissed_cycle,
                    "task_profile_id": task_profile_id,
                    "reason": reason,
                })
            result = {
                "task_profile_id": task_profile_id,
                "dismissed_count": len(cycles),
                "cycle_ids": [cycle["cycle_id"] for cycle in cycles],
            }
        elif typ == "dismiss_cycles":
            cycle_ids = command.get("cycle_ids")
            if not isinstance(cycle_ids, list):
                raise ValueError("cycle_ids required")
            reason = str(command.get("reason") or "user_requested_cleanup").strip()
            cycles = self.store.dismiss_cycles(cycle_ids, reason)
            for dismissed_cycle in cycles:
                self.emit(dismissed_cycle, "analysis.dismissed", {
                    "cycle": dismissed_cycle,
                    "reason": reason,
                })
            result = {
                "dismissed_count": len(cycles),
                "cycle_ids": [cycle["cycle_id"] for cycle in cycles],
            }
        else:
            if not cycle_id:
                raise ValueError("cycle_id required")
            cycle = self.store.get_cycle(cycle_id)
            expected = command.get("expected_revision")
            if expected is not None and int(expected) != cycle["revision"]:
                raise ValueError(f"revision conflict: expected {expected}, current {cycle['revision']}")
            if typ == "request_projection":
                result = self._projection(cycle)
                self.emit(cycle, "projection.ready", result)
            elif typ == "clear_operational_records":
                return self._clear_operational_records(cycle, command)
            elif typ == "invalidate_m0":
                if cycle["state"] not in {"awaiting_h0", "voice_grace"}:
                    raise ValueError(f"M0 cannot be invalidated from state: {cycle['state']}")
                if self.store.latest_artifact(cycle_id, "m0") is None:
                    raise ValueError("M0 cannot be invalidated before publication")
                problems = [str(item) for item in command.get("qualification_problems") or [] if str(item).strip()]
                if not problems:
                    raise ValueError("qualification_problems required")
                reason = str(command.get("reason") or "M0 failed deterministic qualification after publication").strip()
                result = self.store.transition(cycle_id, "failed")
                self._emit_failure(
                    result, "m0.invalidated", self._user_fault_message(reason, "刚才的客观信息"), reason,
                    {"qualification_problems": problems},
                )
            elif typ in {"begin_voice_capture", "begin_h0_edit"}:
                result = self._begin_grace(cycle, typ)
            elif typ == "stage_message":
                result = self._stage_message(
                    cycle,
                    str(command.get("text", "")),
                    command.get("message_id"),
                    provenance=command.get("provenance"),
                )
            elif typ == "edit_staged_message":
                message = self.store.update_staged_message(cycle_id, str(command.get("message_id") or ""), str(command.get("text", "")))
                self.emit(cycle, "message.edited", {"cycle": cycle, "message": message})
                result = self._projection(cycle)
            elif typ == "withdraw_staged_message":
                message = self.store.withdraw_message(cycle_id, str(command.get("message_id") or ""))
                self.emit(cycle, "message.withdrawn", {"cycle": cycle, "message_id": message["message_id"]})
                result = self._projection(cycle)
            elif typ == "commit_pre_m0":
                result = self._commit_pre_m0(cycle)
            elif typ in {"commit_h0", "skip_h0"}:
                result = self._lock_h0(cycle, "manual")
            elif typ in {"submit_h0", "submit_voice_h0"}:
                text = str(command.get("text", "")).strip()
                if text:
                    self._stage_message(
                        cycle,
                        text,
                        command.get("message_id"),
                        emit=False,
                        provenance=command.get("provenance"),
                    )
                result = self._lock_h0(self.store.get_cycle(cycle_id), "legacy_submit")
            elif typ == "commit_chat_batch":
                result = self._commit_chat(cycle)
            elif typ == "commit_conversation_batch":
                result = self._commit_conversation(cycle, reason="manual")
            elif typ == "terminate_chat_research":
                result = self.store.terminate_chat_research(cycle_id)
                self.emit(cycle, "chat.research.terminated", {"cycle": cycle, "state": result})
            elif typ == "continue_chat_research":
                result = self.store.continue_chat_research(cycle_id)
                if result.get("continued_now"):
                    self.emit(cycle, "chat.research.continued", {"cycle": cycle, "state": result})
            else:
                raise ValueError(f"unsupported command: {typ}")
        self.store.save_receipt(command["command_id"], cycle_id, typ, command, result)
        return result

    def _clear_operational_records(
        self, cycle: dict[str, Any], command: dict[str, Any],
    ) -> dict[str, Any]:
        if command.get("contract") != "companion-user-command/v1":
            raise ValueError("cleanup requires companion-user-command/v1")
        if command.get("confirmed") is not True:
            raise ValueError("cleanup requires confirmed=true")
        categories = command.get("categories")
        if not isinstance(categories, list):
            raise ValueError("cleanup categories must be a list")
        record_ids = command.get("record_ids")
        if record_ids is not None and not isinstance(record_ids, list):
            raise ValueError("cleanup record_ids must be a list")
        command_id = str(command["command_id"])
        with self.store.connection() as connection:
            previous = self.store.receipt(command_id, command, connection=connection)
            if previous is not None:
                return previous
            cleanup = self.store.clear_operational_records(
                cycle["cycle_id"],
                categories=[str(value) for value in categories],
                command_id=command_id,
                record_ids=[str(value) for value in record_ids] if record_ids is not None else None,
                connection=connection,
            )
            result = {
                "contract": "companion-operational-record-cleanup-result/v1",
                "command_id": command_id,
                "cycle_id": cycle["cycle_id"],
                "state": "completed",
                **cleanup,
            }
            self.store.queue_event(
                cycle["cycle_id"],
                "operational_records.cleared",
                {
                    "cycle": cycle,
                    "receipt": result,
                    "removed_record_ids": cleanup["removed_record_ids"],
                    "hidden_fault_episode_ids": cleanup["hidden_fault_episode_ids"],
                },
                connection=connection,
            )
            self.store.save_receipt(
                command_id,
                cycle["cycle_id"],
                str(command["type"]),
                command,
                result,
                connection=connection,
            )
        return result

    def _begin_grace(self, cycle: dict[str, Any], source: str) -> dict[str, Any]:
        deadline = parse(cycle["h0_auto_submit_at"]) if cycle.get("h0_auto_submit_at") else None
        publish = parse(cycle["m1_publish_deadline"]) if cycle.get("m1_publish_deadline") else None
        if cycle["state"] == "awaiting_h0" and deadline and utc_now() <= deadline:
            grace = deadline + timedelta(minutes=5)
            if publish:
                grace = min(grace, publish)
            result = self.store.transition(cycle["cycle_id"], "voice_grace", voice_grace_deadline=iso(grace))
            self.emit(result, "input.grace.accepted", {"cycle": result, "source": source, "deadline": iso(grace)})
            return result
        return {"accepted": False, "reason": "H0 window expired"}

    def _stage_message(
        self,
        cycle: dict[str, Any],
        text: str,
        message_id: str | None,
        *,
        emit: bool = True,
        provenance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if cycle.get("kind") == "daily_conversation":
            if cycle["state"] != "open":
                raise ValueError("conversation is not open")
            assert_safe(text, boundary="user message storage")
            message = self.store.stage_message(
                cycle["cycle_id"], text, "conversation",
                message_id=message_id, provenance=provenance,
            )
            if emit:
                self.emit(cycle, "message.staged", {"cycle": cycle, "message": message})
            return self._projection(cycle)
        if cycle["state"] == "queued" and cycle["task_key"] != "daily.opportunity.0900":
            raise ValueError("pre-M0 messages belong to the daily opportunity cycle")
        if cycle["state"] in {"researching_m0", "failed", "missed"}:
            raise ValueError("messages can only be staged after M0 is ready")
        # A blocked message is never persisted in a memory candidate or sent to
        # a later research packet.  The user can remove the secret and retry.
        assert_safe(text, boundary="user message storage")
        phase = "pre_m0" if cycle["state"] == "queued" else "h0" if not cycle.get("h0_locked_at") else "chat"
        message = self.store.stage_message(
            cycle["cycle_id"], text, phase,
            message_id=message_id, provenance=provenance,
        )
        if emit:
            self.emit(cycle, "message.staged", {"cycle": cycle, "message": message})
        return self._projection(cycle)

    def _commit_pre_m0(self, cycle: dict[str, Any]) -> dict[str, Any]:
        if cycle["state"] != "queued" or cycle["task_key"] != "daily.opportunity.0900":
            raise ValueError(f"pre-M0 messages cannot be submitted from state: {cycle['state']}")
        batch_id, messages = self.store.commit_staged_messages(cycle["cycle_id"], "pre_m0")
        if not messages:
            raise ValueError("no staged pre-M0 messages")
        body = "\n\n".join(message["body_text"] for message in messages)
        artifact = self.store.append_artifact(
            cycle["cycle_id"], "pre_m0_submission", "human", body, iso(utc_now()),
            {"batch_id": batch_id, "message_ids": [message["message_id"] for message in messages]},
        )
        self.store.link_messages_to_artifact(
            [message["message_id"] for message in messages], artifact["artifact_id"]
        )
        self.emit(cycle, "pre_m0.submitted", {
            "cycle": cycle,
            "batch_id": batch_id,
            "messages": messages,
            "source_artifact_id": artifact["artifact_id"],
        })
        projection = self._projection(cycle)
        projection["committed_batch_id"] = batch_id
        projection["source_artifact_id"] = artifact["artifact_id"]
        return projection

    def _lock_h0(self, cycle: dict[str, Any], reason: str) -> dict[str, Any]:
        if cycle["state"] not in {"awaiting_h0", "voice_grace"}:
            if cycle.get("h0_locked_at"):
                return self._projection(cycle)
            raise ValueError(f"H0 cannot be locked from state: {cycle['state']}")
        # This boundary is deliberately before H0 actions.  M1 packets read
        # only this snapshot, so a secretary-side portfolio update cannot leak
        # the user's current H0 into the independent strategy judgment.
        self.store.freeze_private_context(cycle["cycle_id"])
        cycle = self.store.get_cycle(cycle["cycle_id"])
        batch_id, messages = self.store.commit_staged_messages(cycle["cycle_id"], "h0")
        artifact = None
        if messages:
            body = "\n\n".join(message["body_text"] for message in messages)
            artifact = self.store.append_artifact(
                cycle["cycle_id"], "h0", "human", body, iso(utc_now()),
                {"batch_id": batch_id, "message_ids": [message["message_id"] for message in messages]},
            )
            self.judgments.capture(artifact, "h0", body)
            self.store.link_messages_to_artifact([message["message_id"] for message in messages], artifact["artifact_id"])
        locked_at = iso(utc_now())
        cycle = self.store.transition(
            cycle["cycle_id"],
            "researching_m1",
            h0_locked_at=locked_at,
            h0_artifact_id=artifact["artifact_id"] if artifact else None,
            has_h0=1 if messages else 0,
            m1_started_at=locked_at,
        )
        self.emit(
            cycle,
            "h0.locked",
            {
                "cycle": cycle,
                "reason": reason,
                "batch_id": batch_id,
                "has_h0": bool(messages),
                "messages": messages,
                "source_artifact_id": artifact["artifact_id"] if artifact else None,
            },
        )
        self.emit(cycle, "m1.started", {"cycle": cycle})
        return self._projection(cycle)

    def _commit_chat(self, cycle: dict[str, Any]) -> dict[str, Any]:
        if not cycle.get("h0_locked_at"):
            raise ValueError("chat cannot be committed before H0 is locked")
        batch_id, messages = self.store.commit_staged_messages(cycle["cycle_id"], "chat")
        if not messages:
            raise ValueError("no staged chat messages")
        body = "\n\n".join(message["body_text"] for message in messages)
        artifact = self.store.append_artifact(
            cycle["cycle_id"], "chat_human", "human", body, iso(utc_now()),
            {"batch_id": batch_id, "message_ids": [message["message_id"] for message in messages]},
        )
        self.store.link_messages_to_artifact([message["message_id"] for message in messages], artifact["artifact_id"])
        self.emit(cycle, "human.message_batch.accepted", {
            "cycle": cycle, "batch_id": batch_id, "messages": messages, "source_artifact_id": artifact["artifact_id"]
        })
        projection = self._projection(cycle)
        projection["committed_batch_id"] = batch_id
        projection["source_artifact_id"] = artifact["artifact_id"]
        return projection

    def _commit_conversation(self, cycle: dict[str, Any], *, reason: str) -> dict[str, Any]:
        if cycle.get("kind") != "daily_conversation" or cycle["state"] != "open":
            raise ValueError("messages can only be committed in the open daily conversation")
        batch_id, messages = self.store.commit_staged_messages(cycle["cycle_id"], "conversation")
        if not messages:
            raise ValueError("no staged conversation messages")
        body = "\n\n".join(message["body_text"] for message in messages)
        artifact = self.store.append_artifact(
            cycle["cycle_id"], "chat_human", "human", body, iso(utc_now()),
            {"batch_id": batch_id, "message_ids": [message["message_id"] for message in messages], "reason": reason},
        )
        self.store.link_messages_to_artifact([message["message_id"] for message in messages], artifact["artifact_id"])
        self.emit(cycle, "human.message_batch.accepted", {
            "cycle": cycle, "batch_id": batch_id, "messages": messages,
            "source_artifact_id": artifact["artifact_id"], "reason": reason,
        })
        projection = self._projection(cycle)
        projection["committed_batch_id"] = batch_id
        projection["source_artifact_id"] = artifact["artifact_id"]
        return projection

    def ensure_daily_conversation(self, at: datetime | None = None) -> dict[str, Any]:
        current = (at or utc_now()).astimezone(ZoneInfo("Asia/Shanghai"))
        cycle = self.store.ensure_daily_conversation(current.date().isoformat(), at=at)
        created = bool(cycle.pop("_created", False))
        if created:
            self.emit(cycle, "conversation.opened", {"cycle": cycle})
        return cycle

    def auto_submit_conversation(self, conversation_cycle_id: str, task_key: str, scheduled_for: str) -> dict[str, Any] | None:
        cycle = self.store.get_cycle(conversation_cycle_id)
        if not self.store.messages(conversation_cycle_id, state="staged", phase="conversation"):
            return None
        if not self.store.claim_conversation_auto_submit(task_key, scheduled_for, conversation_cycle_id):
            return None
        result = self._commit_conversation(cycle, reason=f"before:{task_key}")
        self.store.complete_conversation_auto_submit(task_key, scheduled_for, result["committed_batch_id"])
        return result

    def run_due(self, at: datetime | None = None) -> list[dict[str, Any]]:
        at = at or utc_now()
        changed: list[dict[str, Any]] = []
        with self.store.connection() as connection:
            rows = [dict(row) for row in connection.execute(
                "SELECT * FROM companion_cycle WHERE state IN ('awaiting_h0','voice_grace')"
            )]
        for cycle in rows:
            due = cycle.get("voice_grace_deadline") if cycle["state"] == "voice_grace" else cycle.get("h0_auto_submit_at")
            if due and at >= parse(due):
                changed.append(self._lock_h0(cycle, "auto_deadline"))
        return changed

    def m1_judgment_started(self, cycle_id: str) -> dict[str, Any]:
        cycle = self.store.get_cycle(cycle_id)
        if cycle["state"] not in {"researching_m1", "m1_retry_wait"}:
            raise ValueError(f"M1 judgment cannot start from: {cycle['state']}")
        cycle = self.store.transition(cycle_id, "judging_m1", m1_started_at=cycle.get("m1_started_at") or iso(utc_now()))
        self.emit(cycle, "m1.judging", {"cycle": cycle})
        return cycle

    def resume_m1_after_repair(self, cycle_id: str) -> dict[str, Any]:
        cycle = self.store.get_cycle(cycle_id)
        if cycle["state"] != "waiting_for_repair":
            return cycle
        cycle = self.store.transition(cycle_id, "m1_retry_wait")
        self.emit(cycle, "m1.repair_retrying", {"cycle": cycle})
        return cycle

    def m1_ready(
        self, cycle_id: str, m1: str, *, as_of: str | None = None,
        research_attempt_id: str, judgment_attempt_id: str,
        research_packet_hash: str, judgment_packet_hash: str,
        snapshot: dict[str, Any] | None = None, qualified: bool = True,
    ) -> dict[str, Any]:
        cycle = self.store.get_cycle(cycle_id)
        self.store.verified_attempt(research_attempt_id, cycle_id, "m1_research", research_packet_hash)
        judgment_attempt = self.store.verified_attempt(judgment_attempt_id, cycle_id, "m1_judgment", judgment_packet_hash)
        verified_output = json.loads(judgment_attempt.get("output_json") or "null")
        normalized_output = normalize_stage_output("m1_judgment", verified_output if isinstance(verified_output, dict) else {})
        verified_m1 = normalized_output.text
        if verified_m1 != m1:
            raise ValueError("M1 body does not match the verified judgment attempt")
        verified_snapshot = normalized_output.snapshot or snapshot
        verified_qualified = normalized_output.qualified if normalized_output.qualified is not None else qualified
        if isinstance(verified_snapshot, dict) and bool(verified_snapshot.get("qualified")) != verified_qualified:
            raise ValueError("M1 judgment qualification conflicts with the verified snapshot")
        if snapshot is not None and verified_output.get("snapshot") is not None and snapshot != verified_output.get("snapshot"):
            raise ValueError("M1 snapshot does not match the verified judgment attempt")
        verifier = json.loads(judgment_attempt.get("verifier_json") or "{}")
        prior_failed_attempt_ids = [
            attempt["attempt_id"] for attempt in self.store.attempts(cycle_id)
            if attempt["status"] in {"failed", "timed_out"}
        ]
        if self.store.latest_artifact(cycle_id, "m1"):
            raise ValueError("formal M1 already exists")
        if cycle["state"] not in {"researching_m1", "judging_m1", "m1_retry_wait"}:
            raise ValueError(f"M1 cannot be published from: {cycle['state']}")
        completed = iso(utc_now())
        next_state = "synthesizing_m2" if bool(cycle.get("has_h0")) and verified_qualified else "complete"
        presented = self.present_for_publication(m1, as_of or cycle["as_of"], "m1")
        self._append_published_memory(cycle, presented)
        with self.store.connection() as connection:
            artifact = self.store.append_artifact(
                cycle_id, "m1", "model", presented.markdown, as_of or iso(utc_now()),
                self._presentation_metadata({"blind_to_h0": True, "research_attempt_id": research_attempt_id, "judgment_attempt_id": judgment_attempt_id}, presented),
                connection=connection,
            )
            self.judgments.capture(
                artifact, "m1", presented.markdown, snapshot=verified_snapshot,
                qualified=verified_qualified, connection=connection,
            )
            cycle = self.store.transition(
                cycle_id, next_state, connection=connection, m1_completed_at=completed,
                m2_started_at=completed if next_state == "synthesizing_m2" else None,
            )
            resolved_fault_episode_ids = self.store.resolve_fault_episodes(
                cycle_id,
                stages=["m1"],
                resolution_artifact_id=artifact["artifact_id"],
                resolved_at=artifact["sealed_at"],
                connection=connection,
            )
            self._queue_event(
                cycle_id, "m1.ready", {
                    "cycle": cycle,
                    "m1": presented.markdown,
                    "presentation": presented.metadata()["presentation"],
                    "message": presented.message(),
                    "source_artifact_id": artifact["artifact_id"],
                    "resolved_fault_episode_ids": resolved_fault_episode_ids,
                },
                connection=connection,
            )
            if next_state == "synthesizing_m2":
                self.store.queue_event(cycle_id, "m2.started", {"cycle": cycle}, connection=connection)
            if prior_failed_attempt_ids:
                # Recovery is execution telemetry, not another AI utterance.
                # Keep real attempt references without guessing the fault cause.
                self.store.queue_event(cycle_id, "m1.retry_succeeded", {
                    "prior_failed_attempt_ids": prior_failed_attempt_ids,
                    "judgment_attempt_id": judgment_attempt_id,
                    "source_artifact_id": artifact["artifact_id"],
                    "fallback": bool(verifier.get("fallback")),
                }, connection=connection)
        try:
            self._publish_manual_analysis_completion(cycle, presented.markdown)
        except Exception:
            # The formal result is already committed.  Gateway recovery retries
            # the idempotent conversation delivery instead of corrupting M1.
            pass
        return cycle

    def recover_manual_analysis_completions(self) -> list[str]:
        """Finish conversation delivery after a crash between M1 and chat publication."""
        with self.store.connection() as connection:
            cycles = [dict(row) for row in connection.execute(
                """SELECT c.* FROM companion_cycle c
                     WHERE c.trigger='manual_chat' AND c.state='complete'
                       AND json_valid(COALESCE(c.request_source_json,'{}'))
                       AND EXISTS (
                         SELECT 1 FROM narrative_artifact a
                          WHERE a.cycle_id=c.cycle_id AND a.kind='m1'
                       )
                       AND EXISTS (
                         SELECT 1 FROM companion_message_batch b
                          WHERE b.batch_id=json_extract(c.request_source_json,'$.batch_id')
                            AND b.cycle_id=json_extract(c.request_source_json,'$.conversation_cycle_id')
                            AND b.state='pending'
                       )
                     ORDER BY c.updated_at LIMIT 8"""
            )]
        completed: list[str] = []
        for cycle in cycles:
            m1 = self.store.latest_artifact(cycle["cycle_id"], "m1")
            if not m1:
                continue
            self._publish_manual_analysis_completion(cycle, str(m1["body_markdown"]))
            completed.append(str(cycle["cycle_id"]))
        return completed

    def _publish_manual_analysis_completion(self, cycle: dict[str, Any], judgment: str) -> None:
        """Close the originating conversation with the completed formal result."""
        if cycle.get("trigger") != "manual_chat":
            return
        try:
            source = json.loads(cycle.get("request_source_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return
        conversation_cycle_id = str(source.get("conversation_cycle_id") or "")
        batch_id = str(source.get("batch_id") or "")
        if not conversation_cycle_id or not batch_id:
            return
        with self.store.connection() as connection:
            pending = connection.execute(
                "SELECT 1 FROM companion_message_batch WHERE batch_id=? AND cycle_id=? AND state='pending'",
                (batch_id, conversation_cycle_id),
            ).fetchone()
        if not pending:
            return
        batch_ids = [batch_id]
        source_messages = [
            str(item.get("body_text") or "") for item in self.store.messages_for_batches([batch_id])
        ]
        task_profile_id = str(cycle.get("task_profile_id") or "")
        if source_messages and task_profile_id:
            with self.store.connection() as connection:
                candidates = [str(row[0]) for row in connection.execute(
                    """SELECT DISTINCT b.batch_id
                         FROM companion_message_batch b
                         JOIN companion_cycle prior
                           ON prior.trigger='manual_chat'
                          AND prior.task_profile_id=?
                          AND json_extract(prior.request_source_json,'$.batch_id')=b.batch_id
                         JOIN companion_cycle_visibility visibility
                           ON visibility.cycle_id=prior.cycle_id
                        WHERE b.cycle_id=? AND b.state='pending' AND b.batch_id<>?
                        ORDER BY b.submitted_at,b.batch_id""",
                    (task_profile_id, conversation_cycle_id, batch_id),
                )]
            for candidate in candidates:
                candidate_messages = [
                    str(item.get("body_text") or "")
                    for item in self.store.messages_for_batches([candidate])
                ]
                if candidate_messages == source_messages:
                    batch_ids.append(candidate)
        text = self._manual_analysis_completion_text(cycle, judgment)
        self.chat_ready(
            conversation_cycle_id, text, reply_to_batch_id=batch_id,
            reply_to_batch_ids=batch_ids, kind="ai_chat",
        )

    def _manual_analysis_completion_text(self, cycle: dict[str, Any], judgment: str) -> str:
        evidence_artifact = self.store.latest_artifact(cycle["cycle_id"], "evidence")
        try:
            evidence = json.loads(evidence_artifact["body_markdown"]) if evidence_artifact else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            evidence = {}
        facts: list[dict[str, Any]] = []
        for item in evidence.get("sources") or []:
            try:
                value = json.loads(str(item.get("excerpt_text") or item.get("excerpt") or ""))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                facts.append(value)

        def number(value: Any, *, signed: bool = False) -> str:
            text = format(value, ".15g") if isinstance(value, float) else str(value)
            return "+" + text if signed and isinstance(value, (int, float)) and value > 0 else text

        indices = [row for fact in facts for row in fact.get("indices") or [] if isinstance(row, dict)]
        indices.sort(key=lambda row: {"000001": 0, "399001": 1, "399006": 2}.get(str(row.get("symbol")), 9))
        quotes = [row for fact in facts for row in fact.get("quotes") or [] if isinstance(row, dict)]
        try:
            evidence_contract = json.loads(cycle.get("evidence_contract_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            evidence_contract = {}
        required = next((
            item.get("required_entities") or [] for item in evidence_contract.get("requirements") or []
            if item.get("key") == "portfolio_market_state"
        ), [])
        order = {str(symbol): index for index, symbol in enumerate(required)}
        quotes.sort(key=lambda row: order.get(str(row.get("symbol")), 99))
        breadth = next((fact.get("breadth") for fact in facts if isinstance(fact.get("breadth"), dict)), {})
        trading_date = next((str(row.get("trading_date")) for row in [*indices, *quotes] if row.get("trading_date")), "")
        date_label = trading_date.replace("-", "年", 1).replace("-", "月", 1) + "日" if trading_date else "本次"
        sections = [f"{date_label}晚间盘后回顾已经完成。"]
        if indices:
            sections.append("三大指数：" + "；".join(
                f"{row.get('name') or row.get('symbol')} {number(row.get('price'))}（{number(row.get('change_percent'), signed=True)}%）"
                for row in indices if row.get("price") is not None and row.get("change_percent") is not None
            ) + "。")
        if all(breadth.get(key) is not None for key in ("up", "down", "flat")):
            breadth_text = (
                f"市场广度：上涨{number(breadth['up'])}家，下跌{number(breadth['down'])}家，"
                f"平盘{number(breadth['flat'])}家"
            )
            if breadth.get("limit_up") is not None and breadth.get("limit_down") is not None:
                breadth_text += (
                    f"；涨停候选{number(breadth['limit_up'])}家，"
                    f"跌停候选{number(breadth['limit_down'])}家"
                )
            sections.append(breadth_text + "。")
        if quotes:
            sections.append("当前持仓：" + "；".join(
                f"{row.get('name') or row.get('symbol')}（{row.get('symbol')}）收于{number(row.get('price'))}，"
                f"涨跌幅{number(row.get('change_percent'), signed=True)}%"
                for row in quotes if row.get("price") is not None and row.get("change_percent") is not None
            ) + "。")
        sections.append("我的判断：\n" + judgment.strip())
        return "\n\n".join(sections)

    def m1_failed(self, cycle_id: str, reason: str, *, retryable: bool, details: dict[str, Any] | None = None) -> dict[str, Any]:
        diagnostic_code = self._verifier_diagnostic_code(details) or self._diagnostic_code(str(reason))
        cycle = self.store.transition(cycle_id, "m1_retry_wait" if retryable else "waiting_for_repair")
        if retryable:
            message = self._stage_failure_message("M1", str(reason), details)
            self._queue_event(cycle_id, "m1.retrying", {
                "cycle": cycle, "reason": message, "diagnostic_code": diagnostic_code, "retryable": True,
            })
            return cycle
        message = self._stage_failure_message("M1", str(reason), details)
        self._emit_failure(cycle, "m1.failed", message, reason, {"diagnostic_code": diagnostic_code, "retryable": False})
        return cycle

    @classmethod
    def _stage_failure_message(cls, stage: str, reason: str, details: dict[str, Any] | None) -> str:
        verifier_code = cls._verifier_diagnostic_code(details)
        if verifier_code == "output_schema_invalid":
            return f"{stage} 返回内容未通过本地输出格式校验，本次结果未发布；具体缺失或冲突字段已保留在本地审计记录中。"
        if verifier_code == "output_quality_invalid":
            return f"{stage} 返回内容未通过本地判断质量校验，本次结果未发布；具体拒绝项已保留在本地审计记录中。"
        if not details:
            return cls._user_fault_message(reason, stage)
        public_failure = str(details.get("public_failure_message") or "").strip()
        if public_failure:
            return f"{stage} 未发布：{public_failure}"
        missing = "、".join(str(item) for item in details.get("missing_requirements") or []) or "关键事实覆盖"
        backends = "、".join(str(item) for item in details.get("attempted_backends") or []) or "未取得可用后端结果"
        boundary = str(details.get("safe_boundary") or "no_trading_action_qualified")
        return f"{stage} 未发布：缺少 {missing}；已尝试 {backends}。{boundary}；需要取得同一时点、可回溯到本轮工具结果的证据后再运行。"

    def m2_ready(
        self, cycle_id: str, m2: str, *, as_of: str | None = None,
        attempt_id: str, packet_hash: str, snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        cycle = self.store.get_cycle(cycle_id)
        verified_attempt = self.store.verified_attempt(attempt_id, cycle_id, "m2", packet_hash)
        verified_output = json.loads(verified_attempt.get("output_json") or "null")
        verified_m2 = normalize_stage_output("m2", verified_output if isinstance(verified_output, dict) else {}).text
        if verified_m2 != m2:
            raise ValueError("M2 body does not match the verified synthesis attempt")
        if not cycle.get("has_h0"):
            raise ValueError("M2 requires H0")
        m1_snapshots = [row for row in self.store.judgment_snapshots(cycle_id) if row.get("kind") == "m1"]
        if not m1_snapshots or not bool(json.loads(m1_snapshots[-1]["snapshot_json"]).get("qualified")):
            raise ValueError("M2 requires a qualified M1 judgment")
        if self.store.latest_artifact(cycle_id, "m2"):
            raise ValueError("formal M2 already exists")
        if cycle["state"] not in {"synthesizing_m2", "m2_deferred"}:
            raise ValueError(f"M2 cannot be published from: {cycle['state']}")
        presented = self.present_for_publication(m2, as_of or cycle["as_of"], "m2")
        self._append_published_memory(cycle, presented)
        with self.store.connection() as connection:
            artifact = self.store.append_artifact(
                cycle_id, "m2", "model", presented.markdown, as_of or iso(utc_now()),
                self._presentation_metadata({"attempt_id": attempt_id}, presented), connection=connection,
            )
            self.judgments.capture(
                artifact, "m2", presented.markdown, snapshot=snapshot,
                qualified=bool(snapshot.get("qualified")) if isinstance(snapshot, dict) else True,
                connection=connection,
            )
            cycle = self.store.transition(cycle_id, "complete", connection=connection, m2_completed_at=iso(utc_now()))
            resolved_fault_episode_ids = self.store.resolve_fault_episodes(
                cycle_id,
                stages=["m2"],
                resolution_artifact_id=artifact["artifact_id"],
                resolved_at=artifact["sealed_at"],
                connection=connection,
            )
            self._queue_event(
                cycle_id, "m2.ready", {
                    "cycle": cycle,
                    "m2": presented.markdown,
                    "presentation": presented.metadata()["presentation"],
                    "message": presented.message(),
                    "source_artifact_id": artifact["artifact_id"],
                    "resolved_fault_episode_ids": resolved_fault_episode_ids,
                },
                connection=connection,
            )
        return cycle

    def m2_deferred(self, cycle_id: str, reason: str) -> dict[str, Any]:
        cycle = self.store.get_cycle(cycle_id)
        if cycle["state"] not in {"synthesizing_m2", "m2_deferred"}:
            return cycle
        cycle = self.store.transition(cycle_id, "m2_deferred")
        self._emit_failure(cycle, "m2.deferred", self._user_fault_message(reason, "M2"), reason)
        return cycle

    def background_failed(self, cycle_id: str, stage: str, reason: str) -> dict[str, Any]:
        cycle = self.store.get_cycle(cycle_id)
        label = {"chat_research": "公开补查", "outcome": "结果验证", "workflow_feedback": "工作流反馈处理", "cognition": "消息理解与受控动作"}.get(stage, stage)
        self._emit_failure(cycle, f"{stage}.failed", self._user_fault_message(reason, label), reason)
        return cycle

    def chat_ready(
        self, cycle_id: str, text: str, *, reply_to_batch_id: str | None = None,
        reply_to_batch_ids: list[str] | None = None, stream_id: str | None = None, kind: str = "ai_chat",
        allow_structured_format: bool = False, presented: PresentedMessage | None = None,
        complete_batches: bool = True,
    ) -> dict[str, Any]:
        if kind not in {"ai_chat", "premarket_chat"}:
            raise ValueError(f"unsupported chat artifact kind: {kind}")
        if self.store.chat_research_terminated(cycle_id):
            raise RuntimeError("chat research was terminated; late result is not published")
        cycle = self.store.get_cycle(cycle_id)
        presented = presented or self.present_for_publication(text, iso(utc_now()), kind, allow_structured_format=allow_structured_format)
        published_at = iso(utc_now())
        memory_message_id = stream_id or f"{cycle_id}:{kind}:{reply_to_batch_id or hashlib.sha256(presented.markdown.encode('utf-8')).hexdigest()}"
        memory_receipt = None
        if self.memory is not None:
            memory_receipt = self.memory.append({
                "memory_space_id": self.memory_space_id,
                "source_system": "stock-advisor",
                "source_event_id": memory_message_id,
                "content_hash": "auto", "episode_type": "ai_message",
                "body": presented.markdown, "occurred_at": published_at,
                "known_at": published_at, "submitted_at": published_at,
                "authority": "published_ai_message", "protocol_version": "memoryhub/v1",
                "metadata": {
                    "message_id": memory_message_id, "cycle_id": cycle_id,
                    "reply_to_batch_id": reply_to_batch_id, "stream_id": stream_id,
                    "kind": kind, "state": "published", "actor": "ai",
                    "published_message": presented.message(),
                },
            })
        batch_ids = reply_to_batch_ids or ([reply_to_batch_id] if reply_to_batch_id else [])
        artifact = self.store.append_artifact(
            cycle_id, kind, "model", presented.markdown, published_at,
            self._presentation_metadata({
                "reply_to_batch_id": reply_to_batch_id, "stream_id": stream_id,
                "reply_to_batch_ids": batch_ids,
                "memory_message_id": memory_message_id,
                "memory_episode_id": memory_receipt["episode_id"] if memory_receipt else None,
            }, presented),
        )
        resolved_fault_episode_ids: list[str] = []
        if complete_batches:
            self.store.mark_batches_responded(batch_ids, artifact["artifact_id"])
            resolved_fault_episode_ids = self.store.resolve_fault_episodes(
                cycle_id,
                batch_ids=batch_ids,
                resolution_artifact_id=artifact["artifact_id"],
                resolved_at=artifact["sealed_at"],
            )
        event_type = "premarket.reply.ready" if kind == "premarket_chat" else "chat.ready"
        self.emit(cycle, event_type, {
            "cycle": cycle, "text": presented.markdown, "presentation": presented.metadata()["presentation"], "reply_to_batch_id": reply_to_batch_id, "stream_id": stream_id,
            "message": presented.message(),
            "source_artifact_id": artifact["artifact_id"], "response_complete": complete_batches,
            "resolved_fault_episode_ids": resolved_fault_episode_ids,
        })
        return cycle

    def publish_proactive_message(
        self,
        cycle_id: str,
        kind: str,
        text: str,
        *,
        meaningful: bool,
        required_confirmation: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Publish a scheduled message only when it earns an interruption."""
        if not meaningful and not required_confirmation:
            return None
        cycle = self.store.get_cycle(cycle_id)
        message = text if meaningful else "我已经核对过了，暂时没有需要你据此调整的新信息。"
        event_type = {"outcome": "outcome.ready", "reflection": "reflection.ready"}.get(kind, "chat.ready")
        presented = self.present_for_publication(message, iso(utc_now()), kind)
        self._append_published_memory(cycle, presented)
        artifact = self.store.append_artifact(
            cycle_id, kind, "model", presented.markdown, iso(utc_now()),
            self._presentation_metadata(metadata or {}, presented),
        )
        self.emit(cycle, event_type, {
            "cycle": cycle, "text": presented.markdown,
            "presentation": presented.metadata()["presentation"],
            "message": presented.message(),
            "source_artifact_id": artifact["artifact_id"],
        })
        return artifact

    def chat_stream_started(self, cycle_id: str, batch_ids: list[str], kind: str) -> dict[str, Any]:
        cycle = self.store.get_cycle(cycle_id)
        stream = self.store.start_stream_message(cycle_id, batch_ids, kind)
        self.emit(cycle, "chat.stream.started", {"cycle": cycle, "stream": stream})
        return stream

    def chat_stream_delta(self, cycle_id: str, stream_id: str, text: str) -> dict[str, Any]:
        cycle = self.store.get_cycle(cycle_id)
        current = self.store.stream_message(stream_id)
        presented = self.present_for_publication(
            text, cycle["as_of"], "ai_chat",
            message_id=stream_id, sealed_at=str(current["created_at"]),
        )
        stream = self.store.append_stream_chunk(stream_id, presented.markdown)
        self.emit(cycle, "chat.stream.delta", {
            "cycle": cycle, "stream_id": stream_id, "text": presented.markdown,
            "message": presented.message(), "state": stream["state"],
        })
        return stream

    def chat_stream_failed(self, cycle_id: str, stream_id: str, reason: str) -> dict[str, Any]:
        cycle = self.store.get_cycle(cycle_id)
        current = self.store.stream_message(stream_id)
        if self.memory is not None and current["text"]:
            occurred_at = str(current["created_at"])
            self.memory.append({
                "memory_space_id": self.memory_space_id,
                "source_system": "stock-advisor", "source_event_id": stream_id,
                "content_hash": "auto", "episode_type": "ai_message", "body": current["text"],
                "occurred_at": occurred_at, "known_at": occurred_at, "submitted_at": occurred_at,
                "authority": "published_ai_message", "protocol_version": "memoryhub/v1",
                "metadata": {
                    "message_id": stream_id, "cycle_id": cycle_id, "kind": "chat_incomplete",
                    "state": "incomplete", "actor": "ai", "batch_ids": current["batch_ids"],
                },
            })
        stream = self.store.finish_stream_message(stream_id, error=reason)
        self._emit_failure(
            cycle,
            "chat.stream.failed",
            self._user_fault_message(reason, "聊天回复"),
            reason,
            {
                "stream": stream,
                "stream_id": stream_id,
                "batch_ids": list(stream["batch_ids"]),
            },
        )
        return stream

    def judgment_revision_ready(self, cycle_id: str, text: str, revises_artifact_id: str) -> dict[str, Any]:
        cycle = self.store.get_cycle(cycle_id)
        prior = next((artifact for artifact in self.store.artifacts(cycle_id) if artifact["artifact_id"] == revises_artifact_id), None)
        if prior is None:
            raise ValueError("judgment revision must reference an artifact in the same cycle")
        text = self._with_revision_continuity(prior["body_markdown"], text)
        presented = self.present_for_publication(text, iso(utc_now()), "judgment_revision")
        self._append_published_memory(cycle, presented)
        artifact = self.store.append_artifact(
            cycle_id, "judgment_revision", "model", presented.markdown, iso(utc_now()),
            self._presentation_metadata({"revises_artifact_id": revises_artifact_id}, presented),
        )
        self.judgments.capture(artifact, "judgment_revision", presented.markdown)
        self.emit(cycle, "judgment.revised", {
            "cycle": cycle, "text": presented.markdown, "presentation": presented.metadata()["presentation"], "revises_artifact_id": revises_artifact_id,
            "message": presented.message(),
            "source_artifact_id": artifact["artifact_id"],
        })
        return artifact

    def _projection(self, cycle: dict[str, Any]) -> dict[str, Any]:
        artifacts = self.store.artifacts(cycle["cycle_id"])
        removed_record_ids = set(
            self.store.removed_operational_record_ids(cycle["cycle_id"])
        )
        ai_kinds = {
            "m0", "m1", "m2", "ai_chat", "premarket_chat", "judgment_revision", "system_fault",
            "outcome", "reflection", "recovery", "legacy_message",
        }
        ai_messages = []
        local_message_ids: set[str] = set()
        for artifact in artifacts:
            if artifact["kind"] not in ai_kinds:
                continue
            metadata = json.loads(artifact["metadata_json"] or "{}")
            published_message = metadata.get("published_message")
            if (
                (
                    artifact["kind"] not in {"ai_chat", "premarket_chat"}
                    or artifact["artifact_id"] in removed_record_ids
                )
                and isinstance(published_message, dict)
                and published_message.get("message_id")
            ):
                local_message_ids.add(str(published_message["message_id"]))
            if artifact["artifact_id"] in removed_record_ids:
                continue
            if artifact["kind"] == "system_fault":
                continue
            item = {
                "artifact_id": artifact["artifact_id"], "kind": artifact["kind"],
                "at": artifact["sealed_at"], "as_of": artifact["as_of"],
                "text": artifact["body_markdown"], "metadata": artifact["metadata_json"],
            }
            if isinstance(published_message, dict):
                item["message"] = published_message
            ai_messages.append(item)
        user_messages = [
            {
                "message_id": message["message_id"], "state": message["state"], "phase": message["phase"],
                "batch_id": message["batch_id"], "text": message["body_text"], "at": message["staged_at"],
                "submitted_at": message["submitted_at"], "source_artifact_id": message["source_artifact_id"],
            }
            for message in self.store.messages(cycle["cycle_id"])
            if message["state"] != "withdrawn"
            and message["message_id"] not in removed_record_ids
        ]
        if self.memory is not None:
            timeline = [
                item for item in self.memory.timeline(self.memory_space_id)
                if (item.get("metadata") or {}).get("cycle_id") == cycle["cycle_id"]
            ]
            memory_users = {
                str((item.get("metadata") or {}).get("message_id")): {
                    "message_id": (item.get("metadata") or {}).get("message_id"),
                    "state": "submitted", "phase": (item.get("metadata") or {}).get("phase"),
                    "batch_id": (item.get("metadata") or {}).get("batch_id"),
                    "text": item.get("body"), "at": item.get("occurred_at"),
                    "submitted_at": item.get("submitted_at"), "source_artifact_id": None,
                }
                for item in timeline if item.get("episode_type") == "user_message"
            }
            user_messages = [
                memory_users.get(str(message["message_id"]), message) for message in user_messages
            ]
            local_non_chat = [
                message for message in ai_messages
                if message["kind"] not in {"ai_chat", "premarket_chat"}
            ]
            memory_ai = [
                ({
                    "artifact_id": (item.get("metadata") or {}).get("message_id"),
                    "kind": (item.get("metadata") or {}).get("kind", "ai_chat"),
                    "at": item.get("submitted_at"), "as_of": item.get("known_at"),
                    "text": item.get("body"), "metadata": json.dumps(item.get("metadata") or {}, ensure_ascii=False),
                } | ({"message": (item.get("metadata") or {}).get("published_message")}
                     if isinstance((item.get("metadata") or {}).get("published_message"), dict) else {}))
                for item in timeline
                if item.get("episode_type") == "ai_message"
                and (item.get("metadata") or {}).get("kind") != "system_fault"
                and str((item.get("metadata") or {}).get("message_id") or "") not in removed_record_ids
                and str((item.get("metadata") or {}).get("message_id") or "") not in local_message_ids
            ]
            ai_messages = local_non_chat + memory_ai
        latest = {kind: next((item for item in reversed(ai_messages) if item["kind"] == kind), None) for kind in ("m0", "m1", "m2")}
        judgments = [
            {
                "artifact_id": message["source_artifact_id"], "at": message["submitted_at"] or message["staged_at"],
                "text": message["body_text"], "counts_for_m1": message["phase"] == "h0" and message["state"] == "submitted",
            }
            for message in self.store.messages(cycle["cycle_id"], state="submitted")
            if message["message_id"] not in removed_record_ids
        ]
        return {
            "cycle": cycle,
            "m0": latest["m0"]["text"] if latest["m0"] else None,
            "m1": latest["m1"]["text"] if latest["m1"] else None,
            "m2": latest["m2"]["text"] if latest["m2"] else None,
            "ai_messages": ai_messages,
            "fault_episodes": self._fault_episode_projection(cycle["cycle_id"]),
            "user_messages": user_messages,
            "stream_messages": self.store.stream_messages(cycle["cycle_id"]),
            "removed_operational_record_ids": sorted(removed_record_ids),
            "judgments": judgments,
            "has_h0": bool(cycle.get("has_h0")),
        }

    def emit(self, cycle: dict[str, Any], event_type: str, payload: dict[str, Any]) -> None:
        self._queue_event(cycle["cycle_id"], event_type, payload)

    def _queue_event(
        self, cycle_id: str, event_type: str, payload: dict[str, Any], *, connection: Any = None,
    ) -> None:
        if event_type in published_event_types():
            message = payload.get("message")
            if not isinstance(message, dict) or message.get("contract") != "companion-published-message/v2":
                raise ValueError(f"published event requires companion-published-message/v2: {event_type}")
        self.store.queue_event(cycle_id, event_type, payload, connection=connection)

    def _append_published_memory(self, cycle: dict[str, Any], presented: PresentedMessage) -> None:
        if self.memory is None:
            return
        self.memory.append({
            "memory_space_id": self.memory_space_id, "source_system": "stock-advisor",
            "source_event_id": presented.message_id, "content_hash": "auto",
            "episode_type": "ai_message", "body": presented.markdown,
            "occurred_at": presented.sealed_at, "known_at": presented.sealed_at,
            "submitted_at": presented.sealed_at, "authority": "published_ai_message",
            "protocol_version": "memoryhub/v1",
            "metadata": {
                "message_id": presented.message_id, "cycle_id": cycle["cycle_id"],
                "kind": presented.kind, "state": "published", "actor": "ai",
                "published_message": presented.message(),
            },
        })

    @staticmethod
    def present_for_publication(
        text: str, as_of: str, kind: str, *, allow_structured_format: bool = False,
        material_registry: dict[str, dict[str, str]] | None = None,
        expression_profile: dict[str, Any] | None = None,
        message_id: str | None = None, sealed_at: str | None = None,
    ) -> PresentedMessage:
        draft = text
        for repair_count in range(3):
            try:
                return present_message(
                    draft, as_of=as_of, kind=kind, allow_structured_format=allow_structured_format,
                    material_registry=material_registry,
                    expression_profile=expression_profile,
                    message_id=message_id, sealed_at=sealed_at,
                )
            except MessageQualificationError as error:
                if repair_count == 2:
                    raise
                draft = repair_message_draft(draft, error.problems)
        raise AssertionError("bounded message repair exhausted")

    @staticmethod
    def _presentation_metadata(metadata: dict[str, Any], presented: PresentedMessage) -> dict[str, Any]:
        return {**metadata, **presented.metadata()}

    def _emit_failure(
        self, cycle: dict[str, Any], event_type: str, message: str, reason: str, extra: dict[str, Any] | None = None,
    ) -> None:
        occurred_at = iso(utc_now())
        extra = dict(extra or {})
        presented = self.present_for_publication(message, occurred_at, "system_fault")
        reason_category = str(extra.get("diagnostic_code") or self._diagnostic_code(reason))
        targets = self._fault_targets(cycle, event_type, extra)
        episode_ids = [
            self.store.fault_episode_id(
                cycle["cycle_id"], target["scope_kind"], target["scope_key"], target["capability"],
            )
            for target in targets
        ]
        required_action = (
            "wait_for_retry"
            if extra.get("retryable") is True
            or reason_category in {
                "broker_unavailable", "broker_timeout", "broker_stream_incomplete",
                "network_unavailable", "timeout",
            }
            else "repair_required"
        )
        with self.store.connection() as connection:
            artifact = self.store.append_artifact(
                cycle["cycle_id"], "system_fault", "system", presented.markdown, occurred_at,
                self._presentation_metadata({
                    **extra,
                    "record_class": "fault_report",
                    "fault_contract": "companion-fault-episode/v1",
                    "fault_episode_ids": episode_ids,
                    "fault_targets": targets,
                    "reason_category": reason_category,
                    "user_impact": "reply_incomplete" if any(
                        target["capability"] == "conversation_reply" for target in targets
                    ) else "formal_stage_unavailable",
                    "required_action": required_action,
                }, presented),
                connection=connection,
            )
            for target in targets:
                self.store.record_fault_episode(
                    cycle["cycle_id"],
                    scope_kind=target["scope_kind"],
                    scope_key=target["scope_key"],
                    capability=target["capability"],
                    artifact_id=artifact["artifact_id"],
                    reason_category=reason_category,
                    user_impact=(
                        "reply_incomplete"
                        if target["capability"] == "conversation_reply"
                        else "formal_stage_unavailable"
                    ),
                    required_action=required_action,
                    occurred_at=occurred_at,
                    connection=connection,
                )
        fault_episodes = self._fault_episode_projection(
            cycle["cycle_id"], include_resolved=True, episode_ids=episode_ids,
        )
        self.emit(cycle, event_type, {
            "cycle": cycle, "reason": presented.markdown,
            "presentation": presented.metadata()["presentation"], "source_artifact_id": artifact["artifact_id"],
            "message": presented.message(),
            "diagnostic_code": reason_category,
            "fault_episodes": fault_episodes,
            **extra,
        })

    def _fault_targets(
        self, cycle: dict[str, Any], event_type: str, extra: dict[str, Any],
    ) -> list[dict[str, str]]:
        conversation_failures = {
            "chat.stream.failed", "chat_research.failed", "scheduled_conversation.failed",
            "cognition.failed", "workflow_feedback.failed",
        }
        batch_ids = sorted({
            str(value) for value in extra.get("batch_ids") or [] if str(value)
        })
        if event_type in conversation_failures and not batch_ids:
            batch_ids = sorted({
                str(batch["batch_id"])
                for phase in ("conversation", "chat")
                for batch in self.store.pending_message_batches(cycle["cycle_id"], phase)
            })
        if batch_ids:
            return [
                {
                    "scope_kind": "batch",
                    "scope_key": batch_id,
                    "capability": "conversation_reply",
                }
                for batch_id in batch_ids
            ]
        stage = {
            "research.failed": "m0",
            "m0.invalidated": "m0",
            "m1.failed": "m1",
            "m2.deferred": "m2",
            "outcome.failed": "outcome",
            "cycle.missed": "cycle",
        }.get(event_type, event_type.removesuffix(".failed"))
        return [{"scope_kind": "stage", "scope_key": stage, "capability": stage}]

    def _fault_episode_projection(
        self,
        cycle_id: str,
        *,
        include_resolved: bool = False,
        episode_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        artifacts = {
            str(artifact["artifact_id"]): artifact
            for artifact in self.store.artifacts(cycle_id)
        }
        projected: list[dict[str, Any]] = []
        for episode in self.store.fault_episodes(
            cycle_id, include_resolved=include_resolved, episode_ids=episode_ids,
        ):
            artifact = artifacts.get(str(episode["current_artifact_id"]))
            if artifact is None:
                continue
            metadata = json.loads(artifact["metadata_json"] or "{}")
            item = {
                "contract": "companion-fault-episode/v1",
                "episode_id": episode["episode_id"],
                "state": episode["state"],
                "scope_kind": episode["scope_kind"],
                "scope_key": episode["scope_key"],
                "capability": episode["capability"],
                "attempt_count": episode["attempt_count"],
                "first_failed_at": episode["first_failed_at"],
                "last_failed_at": episode["last_failed_at"],
                "occurred_at": episode["last_failed_at"],
                "current_artifact_id": episode["current_artifact_id"],
                "reason_category": episode["current_reason_category"],
                "user_impact": episode["current_user_impact"],
                "required_action": episode["current_required_action"],
                "resolved_at": episode["resolved_at"],
                "resolution_artifact_id": episode["resolution_artifact_id"],
                "text": artifact["body_markdown"],
            }
            published_message = metadata.get("published_message")
            if isinstance(published_message, dict):
                item["message"] = published_message
            projected.append(item)
        return projected

    @staticmethod
    def _with_revision_continuity(previous: str, revision: str) -> str:
        direction_flip = ("看多" in previous and "看空" in revision) or ("看空" in previous and "看多" in revision)
        if not direction_flip or any(marker in revision for marker in ("前面", "原先", "之前")):
            return revision
        prior_view = "偏多" if "看多" in previous else "偏空"
        return f"前面我的判断是{prior_view}。现在出现了足以改变它的新依据，所以我调整为：{revision}"

    @staticmethod
    def _verifier_diagnostic_code(details: dict[str, Any] | None) -> str | None:
        if not isinstance(details, dict):
            return None
        schema = details.get("schema") if isinstance(details.get("schema"), dict) else None
        business = details.get("business") if isinstance(details.get("business"), dict) else None
        if schema is not None and schema.get("passed") is False:
            return "output_schema_invalid"
        if business is not None and business.get("passed") is False:
            return "output_quality_invalid"
        return None

    @staticmethod
    def _diagnostic_code(reason: str) -> str:
        lowered = reason.lower()
        if "invalid_json_schema" in lowered: return "output_schema_invalid"
        if "broker_unavailable" in lowered or "broker http 503" in lowered: return "broker_unavailable"
        if "broker_timeout" in lowered: return "broker_timeout"
        if "broker_stream_incomplete" in lowered: return "broker_stream_incomplete"
        if "timed out" in lowered or "timeout" in lowered: return "timeout"
        if "current_information_unavailable" in lowered: return "current_information_unavailable"
        if "evidence_insufficient" in lowered: return "evidence_insufficient"
        if "network" in lowered or "connection" in lowered or "dns" in lowered: return "network_unavailable"
        return "llm_runtime_error"

    @classmethod
    def _user_fault_message(cls, reason: str, stage: str) -> str:
        code = cls._diagnostic_code(reason)
        return {
            "output_schema_invalid": f"{stage} 因输出格式配置错误中断。这不是市场信息缺失；系统会在修复配置后重新执行。",
            "timeout": f"{stage} 本次运行超时，当前信息可能不完整；系统会在时效窗口内重试。",
            "current_information_unavailable": f"{stage} 的当前信息后端都没有取得可用资料，系统不会据此生成市场结论。",
            "evidence_insufficient": f"{stage} 的关键事实仍未达到可核验标准，系统只保留缺口记录，不生成正式研判。",
            "broker_unavailable": f"{stage} 的 LLM 服务当前没有可用上游，系统没有生成结论；稍后会按阶段策略重试。",
            "broker_timeout": f"{stage} 的 LLM 服务在时限内没有返回，系统没有生成结论；稍后会按阶段策略重试。",
            "broker_stream_incomplete": f"{stage} 的回复未完整结束；已显示的文本会保留，系统将记录独立失败。",
            "network_unavailable": f"{stage} 因网络连接异常没能取得当下公开信息，需要先恢复网络后再判断。",
            "llm_runtime_error": f"{stage} 遇到技术故障，未能完成。详细诊断已保留在本地审计记录中。",
        }[code]
