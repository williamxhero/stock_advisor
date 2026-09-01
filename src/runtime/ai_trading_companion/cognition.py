from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any

from .learning import WorkflowEvolution
from .cognition_compat import adapt_legacy_cognition_result
from .portfolio import explicit_fixture_extraction, is_portfolio_statement
from .store import digest
from .task_profiles import AnalysisClarificationRequired
from .user_learning import explicit_expression_preference, user_method_claim


@dataclass(frozen=True)
class CognitionOutcome:
    job_id: str
    answer: dict[str, Any] | None
    receipts: tuple[dict[str, Any], ...]
    propositions_recorded: int
    needs_fresh_search: bool
    public_search_request: dict[str, Any] | None


class UnifiedCognition:
    """One semantic pass, followed by small deterministic capability executors."""

    def __init__(self, store: Any, portfolio: Any, engine: Any | None = None) -> None:
        self.store = store
        self.portfolio = portfolio
        self.engine = engine

    @staticmethod
    def prompt(cycle: dict[str, Any], messages: list[dict[str, Any]], mode: str, memories: list[dict[str, Any]]) -> str:
        transaction_only = mode == "h0"
        instructions = (
            "你是用户长期使用的同一个炒股伙伴。逐命题区分：用户自己的持仓、成交、资金、偏好和经历，"
            "以用户陈述为个人事实；用户对市场和外部世界的判断只是待核验观点，你必须独立判断。"
            "混合句按命题拆开，歧义只阻塞依赖它的动作。一次输出自然回复、可长期记住的命题和受控动作。"
            "动作只能是 portfolio.apply、portfolio.replace_complete_snapshot、workflow.propose 或 analysis.request。"
            "analysis.request 只表达明确的 subject、time_scope 和 goal；不得指定任务键、日程、证据策略或内部 ID。持仓表默认是局部更新；"
            "只有原文明确说明这是完整账户/全部持仓快照时，才能使用 replace_complete_snapshot；否则绝不能把缺失股票推成零。"
            "普通聊天绝不修订正式 M1/M2。"
            "不得宣称动作已经成功，系统会在本地执行后追加真实回执。每个命题和动作必须引用单条原消息的精确字符区间，"
            "start 包含、end 不包含，quote 必须与该切片逐字相同。"
        )
        if transaction_only:
            instructions += (
                "这是 H0 的事实处理分支：answer 必须为 null，不搜索，不评价策略；只提取个人事实、记忆与受控动作。"
                "同一原文会被独立交给策略分支，但本分支的结果不得进入当前 M1。"
            )
        else:
            instructions += (
                "自然回复像同一个熟悉用户的炒股搭档；需要核验当前公开事实时可提出公开搜索请求。"
                "自己的话只用自然短段，不用标题、列表、表格、字段名或内部任务名。"
                "只有可归属的短外部材料才可以放进 Markdown 引用块，并带可点击来源链接；"
                "长材料先给自然摘要和链接，不要倾倒原文。若确需转贴输入中的材料，只能输出"
                "[[material:材料ID]]；材料ID必须来自 current_personal_memory 中的 publication_material，"
                "不得自己编造材料、ID、标题或链接。"
            )
        packet = {
            "cycle_id": cycle["cycle_id"],
            "mode": mode,
            "as_of": cycle["as_of"],
            "messages": [
                {"message_id": item["message_id"], "text": item["body_text"], "known_at": item["known_at"]}
                for item in messages
            ],
            "current_personal_memory": memories,
        }
        return instructions + "\n\nInput:\n" + json.dumps(packet, ensure_ascii=False, indent=2)

    @staticmethod
    def fixture_result(messages: list[dict[str, Any]], mode: str) -> dict[str, Any]:
        actions: list[dict[str, Any]] = []
        for message in messages:
            text = message["body_text"]
            if not is_portfolio_statement(text):
                continue
            extraction = explicit_fixture_extraction(text)
            changes = []
            for change in extraction.get("changes") or []:
                evidence = change.get("evidence") or {}
                changes.append({
                    "action": change.get("action"), "code": change.get("code"), "name": change.get("name"),
                    "shares": change.get("shares"), "price": change.get("price"),
                    "average_cost": change.get("average_cost"), "total_assets": change.get("total_assets"),
                    "occurred_at": change.get("occurred_at"),
                    "evidence": {key: evidence.get(key) for key in ("instrument", "action", "shares", "price", "average_cost", "total_assets")},
                })
            actions.append({
                "action_type": "portfolio.apply", "statement_type": extraction.get("statement_type", "none"),
                "changes": changes,
                "source_span": {"message_id": message["message_id"], "start": 0, "end": len(text), "quote": text},
            })
        return {
            "answer": None if mode == "h0" else {"points": ["已收到这批消息。我会把你说的个人事实作为最新口径；涉及市场的判断仍由我独立核验。"], "material_ids": []},
            "needs_fresh_search": False, "public_search_request": None,
            "propositions": [], "actions": actions,
        }

    def apply(
        self, cycle: dict[str, Any], source_artifact: dict[str, Any], messages: list[dict[str, Any]], mode: str,
        result: dict[str, Any], *, memory_research: dict[str, Any] | None = None,
    ) -> CognitionOutcome:
        source_text = str(source_artifact.get("body_markdown") or "\n\n".join(item["body_text"] for item in messages))
        job = self.store.start_cognition_job(cycle["cycle_id"], source_artifact["artifact_id"], mode, source_text)
        if job["state"] == "completed" and job.get("result_json"):
            saved = adapt_legacy_cognition_result(json.loads(job["result_json"]))
            return CognitionOutcome(
                job["job_id"], saved.get("answer"), tuple(saved.get("receipts") or ()),
                int(saved.get("propositions_recorded") or 0), bool(saved.get("needs_fresh_search")), saved.get("public_search_request"),
            )

        result = adapt_legacy_cognition_result(result)
        by_id = {item["message_id"]: item for item in messages}
        propositions_recorded = 0
        for index, proposition in enumerate(result.get("propositions") or []):
            validated = self._validated_source_span(by_id, proposition.get("source_span") or {})
            if validated is None:
                continue
            message, source_span = validated
            parsed = dict(proposition)
            parsed["source_span"] = source_span
            try:
                parsed["object"] = json.loads(str(parsed.pop("object_json")))
            except json.JSONDecodeError:
                continue
            proposition_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{job['job_id']}:proposition:{index}:{digest(json.dumps(parsed, ensure_ascii=False, sort_keys=True))}"))
            try:
                self._record_proposition(proposition_id, parsed, message, cycle["cycle_id"])
                propositions_recorded += 1
            except ValueError:
                continue

        learned = [
            candidate for message in messages
            for candidate in (
                explicit_expression_preference(
                    message, self._current_expression_preference(),
                ),
                user_method_claim(message),
            )
            if candidate is not None
        ]
        for index, proposition in enumerate(learned, start=len(result.get("propositions") or [])):
            message = by_id[proposition["source_span"]["message_id"]]
            proposition_id = str(uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{job['job_id']}:deterministic-learning:{index}:{digest(json.dumps(proposition, ensure_ascii=False, sort_keys=True))}",
            ))
            try:
                self._record_proposition(proposition_id, proposition, message, cycle["cycle_id"])
                propositions_recorded += 1
            except ValueError:
                continue

        receipts: list[dict[str, Any]] = []
        for index, action in enumerate(result.get("actions") or []):
            action_type = str(action.get("action_type") or "")
            payload = {key: value for key, value in action.items() if key != "source_span"}
            action_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{job['job_id']}:action:{index}:{digest(json.dumps(payload, ensure_ascii=False, sort_keys=True))}"))
            existing = self.store.action_receipt(action_id)
            if existing:
                receipts.append(json.loads(existing["result_json"]))
                continue
            validated = self._validated_source_span(by_id, action.get("source_span") or {})
            if validated is None:
                receipt = {"action_id": action_id, "action_type": action_type, "state": "rejected", "reason": "原文证据区间无效"}
            else:
                message, source_span = validated
                executable_action = {**action, "source_span": source_span}
                try:
                    receipt = self._execute(action_id, action_type, executable_action, message, cycle, source_artifact)
                except Exception as exc:
                    receipt = {"action_id": action_id, "action_type": action_type, "state": "failed", "reason": str(exc)}
            self.store.save_action_receipt(action_id, job["job_id"], action_type, payload, receipt["state"], receipt)
            receipts.append(receipt)

        answer = None if mode == "h0" else self._final_answer(result.get("answer"), receipts)
        saved = {
            "answer": answer, "receipts": receipts, "propositions_recorded": propositions_recorded,
            "needs_fresh_search": bool(result.get("needs_fresh_search")),
            "public_search_request": result.get("public_search_request"),
            "memory_research": memory_research,
        }
        self.store.finish_cognition_job(job["job_id"], saved)
        return CognitionOutcome(
            job["job_id"], answer, tuple(receipts), propositions_recorded,
            saved["needs_fresh_search"], saved["public_search_request"],
        )

    def _current_expression_preference(self) -> dict[str, Any]:
        if self.engine is None or self.engine.memory is None:
            return {}
        snapshot = self.engine.memory.begin_snapshot({
            "memory_space_id": self.engine.memory_space_id,
            "as_of": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "stage": "expression_preference_write", "cycle_id": "preference-write",
        })
        hits = self.engine.memory.search(snapshot["snapshot_id"], "user.expression", limit=50)
        rows = [self.engine.memory.expand(snapshot["snapshot_id"], hit["episode_id"]) for hit in hits]
        current: dict[str, Any] = {}
        for row in reversed(rows):
            if row.get("episode_type") != "personal_fact":
                continue
            try:
                body = json.loads(str(row.get("body") or "{}"))
            except json.JSONDecodeError:
                continue
            predicate = str(body.get("predicate") or "")
            if body.get("subject") == "user.expression" and predicate.startswith("expression."):
                current[predicate] = {"proposition_id": (row.get("metadata") or {}).get("proposition_id"), **body}
        return current

    def _record_proposition(self, proposition_id: str, proposition: dict[str, Any], message: dict[str, Any], cycle_id: str) -> None:
        if self.engine is None or self.engine.memory is None:
            raise RuntimeError("MemoryHub is required to record long-term personal facts")
        known_at = str(message.get("known_at") or message.get("submitted_at") or message["staged_at"])
        body = json.dumps({
            "subject": proposition["subject"], "predicate": proposition["predicate"],
            "object": proposition.get("object"), "kind": proposition["kind"],
            "source_quote": proposition["source_span"]["quote"],
        }, ensure_ascii=False, sort_keys=True)
        self.engine.memory.append({
            "memory_space_id": self.engine.memory_space_id, "source_system": "stock-advisor",
            "source_event_id": proposition_id, "content_hash": "auto",
            "episode_type": "personal_fact" if proposition["kind"] in {"user_fact", "expression_preference"} else "proposition",
            "body": body, "occurred_at": str(message.get("occurred_at") or known_at),
            "known_at": known_at, "submitted_at": known_at,
            "authority": "user_private_fact", "protocol_version": "memoryhub/v1",
            "metadata": {"proposition_id": proposition_id, "cycle_id": cycle_id, "source_message_id": message["message_id"], "supersedes_id": proposition.get("supersedes_id")},
        })

    @staticmethod
    def _validated_source_span(
        by_id: dict[str, dict[str, Any]], span: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        message = by_id.get(str(span.get("message_id") or ""))
        if message is None:
            return None
        try:
            start, end = int(span["start"]), int(span["end"])
        except (KeyError, TypeError, ValueError):
            return None
        text = str(message["body_text"])
        quote = span.get("quote")
        if not isinstance(quote, str) or not quote:
            return None
        if start >= 0 and end > start and end <= len(text) and text[start:end] == quote:
            return message, {"message_id": message["message_id"], "start": start, "end": end, "quote": quote}

        # Structured models occasionally preserve the exact quote but miscount character
        # offsets (notably around multi-byte characters).  Rebind only an unambiguous,
        # verbatim quote in the declared immutable message; absent or repeated quotes
        # remain invalid rather than broadening the evidence boundary.
        rebound_start = text.find(quote)
        if rebound_start < 0 or text.find(quote, rebound_start + 1) >= 0:
            return None
        return message, {
            "message_id": message["message_id"],
            "start": rebound_start,
            "end": rebound_start + len(quote),
            "quote": quote,
        }

    def _execute(self, action_id: str, action_type: str, action: dict[str, Any], message: dict[str, Any], cycle: dict[str, Any], source_artifact: dict[str, Any]) -> dict[str, Any]:
        if action_type == "portfolio.apply":
            extraction = {"statement_type": action.get("statement_type"), "changes": action.get("changes") or []}
            applied = self.portfolio.apply_extraction(
                message["body_text"], extraction, cycle["cycle_id"], source_artifact["artifact_id"]
            )
            return {"action_id": action_id, "action_type": action_type, **applied}
        if action_type == "portfolio.replace_complete_snapshot":
            applied = self.portfolio.replace_complete_snapshot(
                message["body_text"], action.get("changes") or [], cycle["cycle_id"], source_artifact["artifact_id"]
            )
            return {"action_id": action_id, "action_type": action_type, **applied}
        if action_type == "analysis.request":
            if self.engine is None:
                return {"action_id": action_id, "action_type": action_type, "state": "failed", "reason": "formal analysis orchestrator is unavailable"}
            try:
                result = self.engine.request_formal_analysis({
                    "request_id": f"analysis:{action_id}",
                    "requested_at": message["known_at"],
                    "source": {
                        "conversation_cycle_id": cycle["cycle_id"],
                        "batch_id": message.get("batch_id"),
                        "message_id": message["message_id"],
                        "source_artifact_id": source_artifact["artifact_id"],
                        "source_span": action["source_span"],
                    },
                    "analysis": {
                        "subject": action.get("subject"),
                        "time_scope": action.get("time_scope"),
                        "goal": action.get("goal"),
                    },
                })
            except AnalysisClarificationRequired as exc:
                return {"action_id": action_id, "action_type": action_type, "state": "needs_clarification", "reason": str(exc)}
            receipt = result["receipt"]
            return {
                "action_id": action_id, "action_type": action_type,
                "state": receipt["state"], "request_id": receipt["request_id"],
                "cycle_id": receipt["cycle_id"],
            }
        if action_type == "workflow.propose":
            proposal = action.get("workflow_proposal")
            if not proposal:
                return {"action_id": action_id, "action_type": action_type, "state": "rejected", "reason": "缺少改进提案"}
            created = WorkflowEvolution(self.store).propose(cycle["cycle_id"], proposal, source_artifact_id=source_artifact["artifact_id"])
            return {"action_id": action_id, "action_type": action_type, "state": "proposed", "proposal_id": created["proposal_id"]}
        return {"action_id": action_id, "action_type": action_type, "state": "rejected", "reason": "动作不在允许范围内"}

    @staticmethod
    def _final_answer(answer: Any, receipts: list[dict[str, Any]]) -> dict[str, Any]:
        value = dict(answer) if isinstance(answer, dict) else {"points": ["收到。"], "material_ids": []}
        points = [str(point) for point in value.get("points") or []]
        if not receipts:
            return {"points": points, "material_ids": list(value.get("material_ids") or [])}
        summaries = []
        for receipt in receipts:
            state = receipt.get("state")
            if state == "applied":
                summaries.append("相关事实已通过本地校验并更新，保留了可撤回记录")
            elif state == "proposed":
                summaries.append("工作流改进已形成待确认提案，尚未自动生效")
            elif receipt.get("action_type") == "analysis.request" and state in {"created", "reused"}:
                summaries.append("正式研判任务已创建并进入任务列表" if state == "created" else "该正式研判任务已存在，已恢复其状态")
            elif receipt.get("action_type") == "analysis.request" and state == "needs_clarification":
                summaries.append("正式研判还需要澄清：" + str(receipt.get("reason") or "请说明要分析的对象、时间范围和目标"))
            elif state == "needs_input":
                summaries.append("有一项变更信息不足，未修改；还需要：" + "、".join(receipt.get("missing_fields") or []))
            elif state in {"failed", "rejected"}:
                summaries.append("有一项变更未执行：" + str(receipt.get("reason") or "未通过本地校验"))
        if summaries:
            points.append("；".join(summaries) + "。")
        return {"points": points, "material_ids": list(value.get("material_ids") or [])}
