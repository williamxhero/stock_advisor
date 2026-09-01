from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from ai_trading_companion.__main__ import run_chat_research, run_pending_workflow_feedback, run_reflection
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.store import CompanionStore


class _Builder:
    def __init__(self, *_args, **_kwargs):
        pass

    def build(self, *_args, **_kwargs):
        return {"sha256": "frozen"}


def _runtime(tmp_path: Path):
    store = CompanionStore(tmp_path / "runtime.sqlite3")
    engine = CompanionEngine(store)
    cycle = store.create_cycle(
        "daily.opportunity.0900", "2026-09-01T01:00:00Z", "2026-09-01T01:00:00Z",
    )
    return store, engine, cycle


def test_reflection_v2_semantics_reach_the_published_message_gate(tmp_path: Path) -> None:
    store, engine, cycle = _runtime(tmp_path)
    result = {"answer": {"points": ["这次判断错在忽略了成交量反证。"], "material_ids": []}, "memory_tags": ["counterevidence"], "workflow_proposal": None}
    with patch("ai_trading_companion.__main__.RuntimePacketBuilder", _Builder), patch(
        "ai_trading_companion.__main__._call_stage", return_value=(result, None),
    ) as call:
        artifact = run_reflection(engine, store, cycle["cycle_id"], "checkpoint-1", True)
    assert call.call_args.args[4] == "companion-reflection-result-v2.schema.json"
    assert artifact
    assert store.latest_artifact(cycle["cycle_id"], "reflection")["body_markdown"] == "这次判断错在忽略了成交量反证。"
    event = store.pending_events()[-1]
    payload = json.loads(event["payload_json"])
    assert payload["message"]["contract"] == "companion-published-message/v2"


def test_workflow_feedback_v2_semantics_reach_the_same_publication_gate(tmp_path: Path) -> None:
    store, engine, cycle = _runtime(tmp_path)
    with store.connection() as connection:
        connection.execute("UPDATE companion_cycle SET has_h0=1,state='complete' WHERE cycle_id=?", (cycle["cycle_id"],))
    store.append_artifact(cycle["cycle_id"], "h0", "human", "以后把搜索覆盖做得更完整。", cycle["as_of"])
    result = {"answer": {"points": ["我会先核对缺失来源，再决定是否扩大搜索。"], "material_ids": []}, "memory_tags": ["workflow_feedback"], "workflow_proposal": None}
    with patch("ai_trading_companion.__main__.RuntimePacketBuilder", _Builder), patch(
        "ai_trading_companion.__main__._call_stage", return_value=(result, None),
    ) as call:
        published = run_pending_workflow_feedback(engine, store, True)
    assert call.call_args.args[4] == "companion-reflection-result-v2.schema.json"
    assert published and store.latest_artifact(cycle["cycle_id"], "ai_chat")["body_markdown"] == "我会先核对缺失来源，再决定是否扩大搜索。"


def test_chat_revision_is_expressed_from_semantics_before_immutable_publication(tmp_path: Path) -> None:
    store, engine, cycle = _runtime(tmp_path)
    prior = store.append_artifact(cycle["cycle_id"], "m1", "model", "我原来偏多。", cycle["as_of"])
    source = store.append_artifact(cycle["cycle_id"], "ai_chat", "human", "请补查反证。", cycle["as_of"])
    job = store.queue_research_job(cycle["cycle_id"], source["artifact_id"], {"topics": ["反证"], "questions": ["是否转弱"]})
    evidence = {"as_of": cycle["as_of"], "spoken_summary": "出现新的反证。", "sources": [], "critical_gaps": []}
    followup = {
        "answer": {"points": ["补查后，我需要修正原判断。"], "material_ids": []},
        "needs_fresh_search": False, "public_search_request": None,
        "judgment_revision": {"revises_artifact_id": prior["artifact_id"], "answer": {"points": ["新的反证表明原判断不再成立。"], "material_ids": []}},
        "workflow_proposal": None, "proposal_decision": None,
    }
    with patch("ai_trading_companion.__main__.RuntimePacketBuilder", _Builder), patch(
        "ai_trading_companion.__main__._call_stage", side_effect=[(evidence, None), (followup, None)],
    ):
        run_chat_research(engine, store, job, True)
    revision = store.latest_artifact(cycle["cycle_id"], "judgment_revision")
    assert revision and "新的反证表明原判断不再成立" in revision["body_markdown"]
    metadata = json.loads(revision["metadata_json"])
    assert metadata["revises_artifact_id"] == prior["artifact_id"]
    revised_event = next(event for event in store.pending_events() if event["event_type"] == "judgment.revised")
    assert json.loads(revised_event["payload_json"])["message"]["contract"] == "companion-published-message/v2"
