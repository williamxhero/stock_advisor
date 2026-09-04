from __future__ import annotations

import threading
from unittest.mock import patch

from ai_trading_companion.__main__ import run_background, run_chat_research
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.store import CompanionStore


def test_background_dispatcher_never_blocks_or_overlaps_foreground_ticks() -> None:
    from ai_trading_companion.__main__ import _BackgroundDispatcher

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    calls = 0

    def slow_background() -> None:
        nonlocal calls
        calls += 1
        started.set()
        release.wait(2)
        finished.set()

    dispatcher = _BackgroundDispatcher(slow_background)

    assert dispatcher.submit()
    assert started.wait(1)
    assert not dispatcher.submit()
    assert calls == 1
    release.set()
    assert finished.wait(1)
    assert dispatcher.wait_idle(1)
    assert dispatcher.submit()


def _queued_research(store: CompanionStore, *, day: str, text: str) -> tuple[dict, dict, str]:
    conversation = store.ensure_daily_conversation(day)
    message = store.stage_message(conversation["cycle_id"], text, "conversation")
    batch_id, _ = store.commit_staged_messages(conversation["cycle_id"], "conversation")
    source = store.append_artifact(
        conversation["cycle_id"], "chat_human", "human", text, conversation["as_of"],
        {"batch_id": batch_id, "message_ids": [message["message_id"]]},
    )
    job = store.queue_research_job(
        conversation["cycle_id"], source["artifact_id"],
        {"topics": ["A股盘后"], "questions": ["今天收盘后发生了什么？"]},
    )
    return conversation, job, batch_id


def test_stale_historical_cycle_does_not_starve_chat_research(tmp_path) -> None:
    store = CompanionStore(tmp_path / "runtime.sqlite3")
    engine = CompanionEngine(store)
    stale = store.create_cycle(
        "daily.review.1520", "2026-08-26T15:20:00+08:00", "2026-08-26T07:20:00Z",
    )
    store.transition(stale["cycle_id"], "researching_m0")
    _, job, _ = _queued_research(store, day="2026-09-03", text="做一次晚间盘后回顾")

    with patch("ai_trading_companion.__main__.BackupManager.ensure_daily", return_value=None), patch(
        "ai_trading_companion.__main__._seconds_until_next_schedule", return_value=3600,
    ), patch("ai_trading_companion.__main__.run_chat_research") as execute_research:
        result = run_background(engine, store, True)

    assert result == {"action": "chat_research", "job_id": job["job_id"]}
    execute_research.assert_called_once()


def test_live_worker_claim_still_defers_background_research(tmp_path) -> None:
    store = CompanionStore(tmp_path / "runtime.sqlite3")
    engine = CompanionEngine(store)
    active = store.create_cycle(
        "daily.review.1520", "2026-09-03T15:20:00+08:00", "2026-09-03T07:20:00Z",
    )
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO schedule_worker_claim(cycle_id,claimed_at) VALUES(?,?)",
            (active["cycle_id"], "2026-09-03T07:20:00Z"),
        )
    _queued_research(store, day="2026-09-03", text="做一次晚间盘后回顾")

    with patch("ai_trading_companion.__main__.BackupManager.ensure_daily", return_value=None):
        result = run_background(engine, store, True)

    assert result == {"action": "deferred", "reason": "foreground_cycle_has_priority"}


def test_research_for_an_unanswered_batch_precedes_orphaned_backlog(tmp_path) -> None:
    store = CompanionStore(tmp_path / "runtime.sqlite3")
    _, old_job, old_batch = _queued_research(store, day="2026-09-02", text="旧问题")
    store.mark_batches_responded([old_batch], "old-placeholder")
    _, current_job, _ = _queued_research(store, day="2026-09-03", text="做一次晚间盘后回顾")
    with store.connection() as connection:
        connection.execute(
            "UPDATE companion_research_job SET created_at='2026-09-01T00:00:00Z' WHERE job_id=?",
            (old_job["job_id"],),
        )
        connection.execute(
            "UPDATE companion_research_job SET created_at='2026-09-03T14:00:00Z' WHERE job_id=?",
            (current_job["job_id"],),
        )

    selected = store.pending_research_jobs(limit=1)

    assert selected[0]["job_id"] == current_job["job_id"]


def test_committed_batch_without_a_cognition_job_is_recoverable_once_per_cycle(tmp_path) -> None:
    store = CompanionStore(tmp_path / "runtime.sqlite3")
    conversation = store.ensure_daily_conversation("2026-09-03")
    for text in ("第一条", "第二条"):
        message = store.stage_message(conversation["cycle_id"], text, "conversation")
        batch_id, _ = store.commit_staged_messages(conversation["cycle_id"], "conversation")
        artifact = store.append_artifact(
            conversation["cycle_id"], "chat_human", "human", text, conversation["as_of"],
            {"batch_id": batch_id, "message_ids": [message["message_id"]]},
        )
        with store.connection() as connection:
            connection.execute(
                "UPDATE companion_message SET source_artifact_id=? WHERE message_id=?",
                (artifact["artifact_id"], message["message_id"]),
            )

    recoverable = store.recoverable_conversation_jobs(before="9999-12-31T23:59:59Z")

    assert len(recoverable) == 1
    assert recoverable[0]["cycle_id"] == conversation["cycle_id"]
    assert recoverable[0]["source_kind"] == "chat_human"
    assert recoverable[0]["recovery_reason"] == "cognition_not_started"


def test_fresh_research_reply_is_the_artifact_that_completes_the_batch(tmp_path) -> None:
    store = CompanionStore(tmp_path / "runtime.sqlite3")
    engine = CompanionEngine(store)
    _, job, batch_id = _queued_research(store, day="2026-09-03", text="做一次晚间盘后回顾")
    evidence = {
        "as_of": "2026-09-03T14:30:00Z", "spoken_summary": "收盘数据已经核对。",
        "sources": [], "critical_gaps": [],
    }
    followup = {
        "answer": {"points": ["今天的盘后回顾已经完成。"], "material_ids": []},
        "needs_fresh_search": False, "public_search_request": None,
        "judgment_revision": None,
    }

    class Builder:
        def __init__(self, *_args, **_kwargs):
            pass

        def build(self, *_args, **_kwargs):
            return {"sha256": "frozen"}

    with patch("ai_trading_companion.__main__.RuntimePacketBuilder", Builder), patch(
        "ai_trading_companion.__main__._call_stage", side_effect=[(evidence, None), (followup, None)],
    ):
        run_chat_research(engine, store, job, True)

    with store.connection() as connection:
        batch = dict(connection.execute(
            "SELECT state,response_artifact_id FROM companion_message_batch WHERE batch_id=?",
            (batch_id,),
        ).fetchone())
    final = store.latest_artifact(job["cycle_id"], "ai_chat")
    assert batch == {"state": "completed", "response_artifact_id": final["artifact_id"]}
    assert final["body_markdown"] == "今天的盘后回顾已经完成。"


def test_fresh_research_reply_completes_every_batch_frozen_into_the_job(tmp_path) -> None:
    store = CompanionStore(tmp_path / "runtime.sqlite3")
    engine = CompanionEngine(store)
    _, old_job, old_batch_id = _queued_research(store, day="2026-09-03", text="第一次盘后回顾")
    conversation = store.get_cycle(old_job["cycle_id"])
    message = store.stage_message(conversation["cycle_id"], "请继续完成", "conversation")
    current_batch_id, _ = store.commit_staged_messages(conversation["cycle_id"], "conversation")
    source = store.append_artifact(
        conversation["cycle_id"], "chat_human", "human", "请继续完成", conversation["as_of"],
        {"batch_id": current_batch_id, "message_ids": [message["message_id"]]},
    )
    current_job = store.queue_research_job(
        conversation["cycle_id"], source["artifact_id"],
        {
            "topics": ["A股盘后"], "questions": ["今天收盘后发生了什么？"],
            "_reply_to_batch_ids": [old_batch_id, current_batch_id],
        },
    )
    evidence = {
        "as_of": "2026-09-03T14:30:00Z", "spoken_summary": "收盘数据已经核对。",
        "sources": [], "critical_gaps": [],
    }
    followup = {
        "answer": {"points": ["今天的盘后回顾已经完成。"], "material_ids": []},
        "needs_fresh_search": False, "public_search_request": None,
        "judgment_revision": None,
    }

    class Builder:
        seen_context = None

        def __init__(self, *_args, **_kwargs):
            pass

        def build(self, *_args, **kwargs):
            Builder.seen_context = kwargs.get("context")
            return {"sha256": "frozen"}

    with patch("ai_trading_companion.__main__.RuntimePacketBuilder", Builder), patch(
        "ai_trading_companion.__main__._call_stage", side_effect=[(evidence, None), (followup, None)],
    ):
        run_chat_research(engine, store, current_job, True)

    with store.connection() as connection:
        states = dict(connection.execute(
            "SELECT batch_id,state FROM companion_message_batch WHERE batch_id IN (?,?)",
            (old_batch_id, current_batch_id),
        ).fetchall())
    assert states == {old_batch_id: "completed", current_batch_id: "completed"}
    assert "_reply_to_batch_ids" not in Builder.seen_context
