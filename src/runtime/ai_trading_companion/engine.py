from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .learning import JudgmentLifecycle
from .models import TASK_POLICIES
from .secret_guard import assert_safe


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class CompanionEngine:
    """Deep module for cycle commands, invariants, immutable artifacts and client events."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self.store.initialize()
        self.judgments = JudgmentLifecycle(store)

    def start_cycle(self, task_key: str, scheduled_for: str, as_of: str | None = None) -> dict[str, Any]:
        if task_key not in TASK_POLICIES:
            raise ValueError(f"unregistered task_key: {task_key}")
        cycle = self.store.create_cycle(task_key, scheduled_for, as_of or iso(utc_now()))
        self.emit(cycle, "cycle.created", cycle)
        return cycle

    def research_started(self, cycle_id: str) -> dict[str, Any]:
        cycle = self.store.get_cycle(cycle_id)
        if cycle["state"] != "queued":
            raise ValueError(f"cycle is not queued: {cycle['state']}")
        cycle = self.store.transition(cycle_id, "researching_m0")
        self.emit(cycle, "m0.started", cycle)
        return cycle

    def research_failed(self, cycle_id: str, reason: str) -> dict[str, Any]:
        cycle = self.store.transition(cycle_id, "failed")
        self.emit(cycle, "research.failed", {
            "cycle": cycle, "reason": self._user_fault_message(reason, "M0"),
            "diagnostic_code": self._diagnostic_code(reason),
        })
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
        self.emit(cycle, "cycle.missed", {"cycle": cycle, "reason": reason})
        return cycle

    def research_ready(
        self,
        cycle_id: str,
        m0: str,
        _legacy_hidden_m0: str | None = None,
        session_id: str | None = None,
        packet_hash: str | None = None,
        *,
        evidence_as_of: str | None = None,
    ) -> dict[str, Any]:
        cycle = self.store.get_cycle(cycle_id)
        if cycle["state"] not in {"researching_m0", "researching"}:
            raise ValueError(f"cycle is not researching M0: {cycle['state']}")
        artifact = self.store.append_artifact(
            cycle_id,
            "m0",
            "model",
            m0,
            evidence_as_of or cycle["as_of"],
            {"direction_free": True},
        )
        ready_at = utc_now()
        policy = TASK_POLICIES[cycle["task_key"]]
        reserve_seconds, timing_version = self.store.effective_m1_reserve(
            cycle["task_key"], int(policy.m1_reserve.total_seconds()),
        )
        auto_submit, publish = policy.deadlines(
            cycle["scheduled_for"], ready_at, reserve=timedelta(seconds=reserve_seconds),
        )
        cycle = self.store.transition(
            cycle_id,
            "awaiting_h0",
            human_deadline=iso(auto_submit),
            h0_auto_submit_at=iso(auto_submit),
            m1_publish_deadline=iso(publish),
            codex_session_id=session_id,
            packet_hash=packet_hash,
            m1_reserve_seconds=reserve_seconds,
            timing_policy_version=timing_version,
        )
        self.emit(
            cycle,
            "m0.ready",
            {
                "cycle": cycle,
                "m0": m0,
                "source_artifact_id": artifact["artifact_id"],
                "h0_auto_submit_at": cycle["h0_auto_submit_at"],
                "m1_publish_deadline": cycle["m1_publish_deadline"],
            },
        )
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
            scheduled_date = str(command.get("scheduled_date") or utc_now().date().isoformat())
            projections = []
            for current in self.store.latest_cycles_for_date(scheduled_date):
                projection = self._projection(current)
                self.emit(current, "projection.ready", projection)
                projections.append(projection)
            result = {"scheduled_date": scheduled_date, "projections": projections}
        elif typ == "start_cycle":
            result = self.start_cycle(command["task_key"], command["scheduled_for"], command.get("as_of"))
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
            elif typ in {"begin_voice_capture", "begin_h0_edit"}:
                result = self._begin_grace(cycle, typ)
            elif typ == "stage_message":
                result = self._stage_message(cycle, str(command.get("text", "")), command.get("message_id"))
            elif typ == "withdraw_staged_message":
                message = self.store.withdraw_message(cycle_id, str(command.get("message_id") or ""))
                self.emit(cycle, "message.withdrawn", {"cycle": cycle, "message_id": message["message_id"]})
                result = self._projection(cycle)
            elif typ in {"commit_h0", "skip_h0"}:
                result = self._lock_h0(cycle, "manual")
            elif typ in {"submit_h0", "submit_voice_h0"}:
                text = str(command.get("text", "")).strip()
                if text:
                    self._stage_message(cycle, text, command.get("message_id"), emit=False)
                result = self._lock_h0(self.store.get_cycle(cycle_id), "legacy_submit")
            elif typ == "commit_chat_batch":
                result = self._commit_chat(cycle)
            else:
                raise ValueError(f"unsupported command: {typ}")
        self.store.save_receipt(command["command_id"], cycle_id, typ, command, result)
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

    def _stage_message(self, cycle: dict[str, Any], text: str, message_id: str | None, *, emit: bool = True) -> dict[str, Any]:
        if cycle["state"] in {"queued", "researching_m0", "failed", "missed"}:
            raise ValueError("messages can only be staged after M0 is ready")
        # A blocked message is never persisted in a memory candidate or sent to
        # a later research packet.  The user can remove the secret and retry.
        assert_safe(text, boundary="user message storage")
        phase = "h0" if not cycle.get("h0_locked_at") else "chat"
        message = self.store.stage_message(cycle["cycle_id"], text, phase, message_id=message_id)
        if emit:
            self.emit(cycle, "message.staged", {"cycle": cycle, "message": message})
        return self._projection(cycle)

    def _lock_h0(self, cycle: dict[str, Any], reason: str) -> dict[str, Any]:
        if cycle["state"] not in {"awaiting_h0", "voice_grace"}:
            if cycle.get("h0_locked_at"):
                return self._projection(cycle)
            raise ValueError(f"H0 cannot be locked from state: {cycle['state']}")
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
        snapshot: dict[str, Any] | None = None, qualified: bool = True,
    ) -> dict[str, Any]:
        cycle = self.store.get_cycle(cycle_id)
        recovered = any(attempt["status"] in {"failed", "timed_out"} for attempt in self.store.attempts(cycle_id))
        if self.store.latest_artifact(cycle_id, "m1"):
            raise ValueError("formal M1 already exists")
        if cycle["state"] not in {"researching_m1", "judging_m1", "m1_retry_wait"}:
            raise ValueError(f"M1 cannot be published from: {cycle['state']}")
        artifact = self.store.append_artifact(cycle_id, "m1", "model", m1, as_of or iso(utc_now()), {"blind_to_h0": True})
        self.judgments.capture(artifact, "m1", m1, snapshot=snapshot, qualified=qualified)
        completed = iso(utc_now())
        next_state = "synthesizing_m2" if bool(cycle.get("has_h0")) else "complete"
        cycle = self.store.transition(
            cycle_id,
            next_state,
            m1_completed_at=completed,
            m2_started_at=completed if next_state == "synthesizing_m2" else None,
        )
        if recovered:
            self.emit(cycle, "m1.recovered", {
                "cycle": cycle,
                "message": "刚才 M1 因运行配置问题有所延迟，系统已修复并重新完成；最终判断使用的是修复后的完整流程。",
            })
        self.emit(cycle, "m1.ready", {"cycle": cycle, "m1": m1, "source_artifact_id": artifact["artifact_id"]})
        if next_state == "synthesizing_m2":
            self.emit(cycle, "m2.started", {"cycle": cycle})
        return cycle

    def m1_failed(self, cycle_id: str, reason: str, *, retryable: bool) -> dict[str, Any]:
        cycle = self.store.transition(cycle_id, "m1_retry_wait" if retryable else "waiting_for_repair")
        self.emit(cycle, "m1.failed", {
            "cycle": cycle, "reason": self._user_fault_message(reason, "M1"),
            "diagnostic_code": self._diagnostic_code(reason), "retryable": retryable,
        })
        return cycle

    def m2_ready(
        self, cycle_id: str, m2: str, *, as_of: str | None = None,
        snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        cycle = self.store.get_cycle(cycle_id)
        if not cycle.get("has_h0"):
            raise ValueError("M2 requires H0")
        if self.store.latest_artifact(cycle_id, "m2"):
            raise ValueError("formal M2 already exists")
        if cycle["state"] not in {"synthesizing_m2", "m2_deferred"}:
            raise ValueError(f"M2 cannot be published from: {cycle['state']}")
        artifact = self.store.append_artifact(cycle_id, "m2", "model", m2, as_of or iso(utc_now()))
        self.judgments.capture(artifact, "m2", m2, snapshot=snapshot)
        cycle = self.store.transition(cycle_id, "complete", m2_completed_at=iso(utc_now()))
        self.emit(cycle, "m2.ready", {"cycle": cycle, "m2": m2, "source_artifact_id": artifact["artifact_id"]})
        return cycle

    def m2_deferred(self, cycle_id: str, reason: str) -> dict[str, Any]:
        cycle = self.store.get_cycle(cycle_id)
        if cycle["state"] not in {"synthesizing_m2", "m2_deferred"}:
            return cycle
        cycle = self.store.transition(cycle_id, "m2_deferred")
        self.emit(cycle, "m2.deferred", {
            "cycle": cycle, "reason": self._user_fault_message(reason, "M2"),
            "diagnostic_code": self._diagnostic_code(reason),
        })
        return cycle

    def background_failed(self, cycle_id: str, stage: str, reason: str) -> dict[str, Any]:
        cycle = self.store.get_cycle(cycle_id)
        label = {"chat_research": "公开补查", "outcome": "结果验证", "workflow_feedback": "工作流反馈处理"}.get(stage, stage)
        self.emit(cycle, f"{stage}.failed", {
            "cycle": cycle, "reason": self._user_fault_message(reason, label),
            "diagnostic_code": self._diagnostic_code(reason),
        })
        return cycle

    def chat_ready(self, cycle_id: str, text: str, *, reply_to_batch_id: str | None = None) -> dict[str, Any]:
        cycle = self.store.get_cycle(cycle_id)
        artifact = self.store.append_artifact(
            cycle_id, "ai_chat", "model", text, iso(utc_now()), {"reply_to_batch_id": reply_to_batch_id}
        )
        self.emit(cycle, "chat.ready", {
            "cycle": cycle, "text": text, "reply_to_batch_id": reply_to_batch_id,
            "source_artifact_id": artifact["artifact_id"],
        })
        return cycle

    def judgment_revision_ready(self, cycle_id: str, text: str, revises_artifact_id: str) -> dict[str, Any]:
        cycle = self.store.get_cycle(cycle_id)
        if revises_artifact_id not in {artifact["artifact_id"] for artifact in self.store.artifacts(cycle_id)}:
            raise ValueError("judgment revision must reference an artifact in the same cycle")
        artifact = self.store.append_artifact(
            cycle_id, "judgment_revision", "model", text, iso(utc_now()),
            {"revises_artifact_id": revises_artifact_id},
        )
        self.judgments.capture(artifact, "judgment_revision", text)
        self.emit(cycle, "judgment.revised", {
            "cycle": cycle, "text": text, "revises_artifact_id": revises_artifact_id,
            "source_artifact_id": artifact["artifact_id"],
        })
        return artifact

    # Compatibility for the former single synthesis stage.
    def synthesis_ready(self, cycle_id: str, m1: str) -> dict[str, Any]:
        return self.m1_ready(cycle_id, m1)

    def _projection(self, cycle: dict[str, Any]) -> dict[str, Any]:
        artifacts = self.store.artifacts(cycle["cycle_id"])
        ai_kinds = {
            "m0", "m1", "m2", "ai_chat", "judgment_revision", "system_fault",
            "outcome", "reflection", "legacy_message",
        }
        ai_messages = [
            {
                "artifact_id": artifact["artifact_id"], "kind": artifact["kind"],
                "at": artifact["sealed_at"], "as_of": artifact["as_of"],
                "text": artifact["body_markdown"], "metadata": artifact["metadata_json"],
            }
            for artifact in artifacts if artifact["kind"] in ai_kinds
        ]
        user_messages = [
            {
                "message_id": message["message_id"], "state": message["state"], "phase": message["phase"],
                "batch_id": message["batch_id"], "text": message["body_text"], "at": message["staged_at"],
                "submitted_at": message["submitted_at"], "source_artifact_id": message["source_artifact_id"],
            }
            for message in self.store.messages(cycle["cycle_id"])
            if message["state"] != "withdrawn"
        ]
        latest = {kind: next((item for item in reversed(ai_messages) if item["kind"] == kind), None) for kind in ("m0", "m1", "m2")}
        judgments = [
            {
                "artifact_id": message["source_artifact_id"], "at": message["submitted_at"] or message["staged_at"],
                "text": message["body_text"], "counts_for_m1": message["phase"] == "h0" and message["state"] == "submitted",
            }
            for message in self.store.messages(cycle["cycle_id"], state="submitted")
        ]
        return {
            "cycle": cycle,
            "m0": latest["m0"]["text"] if latest["m0"] else None,
            "m1": latest["m1"]["text"] if latest["m1"] else None,
            "m2": latest["m2"]["text"] if latest["m2"] else None,
            "ai_messages": ai_messages,
            "user_messages": user_messages,
            "judgments": judgments,
            "has_h0": bool(cycle.get("has_h0")),
        }

    def emit(self, cycle: dict[str, Any], event_type: str, payload: dict[str, Any]) -> None:
        self.store.queue_event(cycle["cycle_id"], event_type, payload)

    @staticmethod
    def _diagnostic_code(reason: str) -> str:
        lowered = reason.lower()
        if "invalid_json_schema" in lowered: return "output_schema_invalid"
        if "timed out" in lowered or "timeout" in lowered: return "timeout"
        if "network" in lowered or "connection" in lowered or "dns" in lowered: return "network_unavailable"
        if "supports_parallel_tool_calls" in lowered: return "codex_model_cache_incompatible"
        return "llm_runtime_error"

    @classmethod
    def _user_fault_message(cls, reason: str, stage: str) -> str:
        code = cls._diagnostic_code(reason)
        return {
            "output_schema_invalid": f"{stage} 因输出格式配置错误中断。这不是市场信息缺失；系统会在修复配置后重新执行。",
            "timeout": f"{stage} 本次运行超时，当前信息可能不完整；系统会在时效窗口内重试。",
            "network_unavailable": f"{stage} 因网络连接异常没能取得当下公开信息，需要先恢复网络后再判断。",
            "codex_model_cache_incompatible": f"{stage} 因本机 Codex 模型配置不兼容而中断，需要修复本机运行环境。",
            "llm_runtime_error": f"{stage} 遇到技术故障，未能完成。详细诊断已保留在本地审计记录中。",
        }[code]
