from __future__ import annotations

import copy
import threading
from pathlib import Path

import pytest

from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.learning import JudgmentLifecycle
from ai_trading_companion.memory_port import InMemoryMemoryAdapter, MemoryUnavailable
from ai_trading_companion.store import CompanionStore


AT = "2026-09-21T08:10:00Z"
SPACE = "lesson-test"


def test_runtime_outcome_is_owned_by_memoryhub_before_checkpoint_completion(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "runtime.sqlite3")
    engine = CompanionEngine(store)
    cycle = engine.start_cycle("daily.execution.0945", "2026-09-20T09:45:00+08:00", "2026-09-20T01:45:00Z")
    artifact = store.append_artifact(cycle["cycle_id"], "m1", "model", "条件尚未成立。", "2026-09-20T01:45:00Z")
    snapshot = JudgmentLifecycle(store).capture(artifact, "m1", "条件尚未成立。")
    checkpoint = store.schedule_outcome(snapshot["snapshot_id"], "T+1", AT)
    checkpoint.update({"cycle_id": cycle["cycle_id"], "snapshot_id": snapshot["snapshot_id"]})
    memory = InMemoryMemoryAdapter()
    result = {"as_of": AT, "verification_status": "incorrect", "summary": "结果偏离，不能据此认定推理错误。"}

    lifecycle = JudgmentLifecycle(store, memory=memory, memory_space_id=SPACE)
    recorded = lifecycle.record_outcome(checkpoint, result)
    episodes = memory.export_space(SPACE)["episodes"]
    assert len(episodes) == 1
    assert episodes[0]["metadata"]["memory_type"]["semantic_type"] == "outcome"
    assert episodes[0]["metadata"]["reflection"]["diagnosis"] == "random_outcome"
    assert episodes[0]["metadata"]["outcome_result"] == result
    assert episodes[0]["source_event_id"] == f"outcome:{checkpoint['checkpoint_id']}"
    assert recorded["artifact_id"] == lifecycle.record_outcome(checkpoint, copy.deepcopy(result))["artifact_id"]
    assert len(memory.export_space(SPACE)["episodes"]) == 1


@pytest.fixture
def ready_outcome_recovery(tmp_path):
    class GuardedMemory(InMemoryMemoryAdapter):
        exports = 0
        offline = False

        def export_space(self, memory_space_id):
            self.exports += 1
            if self.offline:
                raise MemoryUnavailable("MemoryHub is offline")
            return super().export_space(memory_space_id)

    store = CompanionStore(tmp_path / "runtime.sqlite3")
    memory = GuardedMemory()
    engine = CompanionEngine(store, memory=memory, memory_space_id=SPACE)
    cycle = engine.start_cycle("daily.execution.0945", "2026-09-20T09:45:00+08:00", "2026-09-20T01:45:00Z")
    artifact = store.append_artifact(cycle["cycle_id"], "m1", "model", "条件尚未成立。", "2026-09-20T01:45:00Z")
    snapshot = JudgmentLifecycle(store).capture(artifact, "m1", "条件尚未成立。")
    store.schedule_outcome(snapshot["snapshot_id"], "T+1", AT)
    checkpoint = next(row for row in store.due_outcomes(AT) if row["snapshot_id"] == snapshot["snapshot_id"])
    presented = engine.present_for_publication("结果偏离，不能据此认定推理错误。", AT, "outcome",
                                               message_id=f"outcome:{checkpoint['checkpoint_id']}", sealed_at=AT)
    result = {"as_of": AT, "checkpoint_ready": True, "verification_status": "incorrect",
              "summary": presented.markdown, "observations": [], "data_gaps": [],
              "presentation": presented.metadata()["presentation"], "published_message": presented.message()}
    JudgmentLifecycle(store, memory=memory, memory_space_id=SPACE).record_outcome(checkpoint, result)
    # Re-enter the public recovery path with a canonical ready outcome retained.
    store.fail_outcome(checkpoint["checkpoint_id"], "recovered pending checkpoint", retry_at=AT)
    store.append_artifact(cycle["cycle_id"], "reflection", "model", "复盘已经保留。", AT,
                          {"checkpoint_id": checkpoint["checkpoint_id"]})
    memory.exports = 0
    return engine, store, memory, checkpoint, result


@pytest.mark.parametrize("offline", [False, True])
def test_outcome_preview_never_recovers_a_ready_canonical_outcome(ready_outcome_recovery, offline) -> None:
    from ai_trading_companion.__main__ import run_outcome
    engine, store, memory, checkpoint, canonical = ready_outcome_recovery
    artifacts = store.artifacts(checkpoint["cycle_id"])
    events = store.pending_events()
    memory.offline = offline

    preview = run_outcome(engine, store, checkpoint, execute=False)

    assert preview["checkpoint_ready"] is False
    assert preview["verification_status"] == "unverified"
    assert preview["data_gaps"] == ["fixture mode"]
    assert memory.exports == 0
    assert store.artifacts(checkpoint["cycle_id"]) == artifacts
    assert store.pending_events() == events
    pending = next(row for row in store.due_outcomes("2099-01-01T00:00:00Z", limit=100)
                   if row["checkpoint_id"] == checkpoint["checkpoint_id"])
    assert pending["status"] == "retry"
    memory.offline = False
    assert memory.export_space(SPACE)["episodes"][0]["metadata"]["outcome_result"] == canonical


def test_executed_outcome_recovers_canonical_result_idempotently(ready_outcome_recovery) -> None:
    from ai_trading_companion.__main__ import run_outcome
    engine, store, memory, checkpoint, canonical = ready_outcome_recovery
    artifacts = store.artifacts(checkpoint["cycle_id"])
    episodes = memory.export_space(SPACE)["episodes"]

    assert run_outcome(engine, store, checkpoint, execute=True) == canonical
    assert run_outcome(engine, store, checkpoint, execute=True) == canonical

    assert store.artifacts(checkpoint["cycle_id"]) == artifacts
    assert memory.export_space(SPACE)["episodes"] == episodes
    assert checkpoint["checkpoint_id"] not in {row["checkpoint_id"] for row in store.due_outcomes("2099-01-01T00:00:00Z", limit=100)}
    published = [row for row in store.pending_events() if row["event_type"] == "outcome.ready"]
    assert published
    assert store.judgment_snapshots(checkpoint["cycle_id"])[0]["verification_status"] == "incorrect"


def append_observation(memory, event, *, cycle="cycle-1", status="correct", quality="verified", mae=0.01, market_state="range", excess_return=None, parents=None):
    from ai_trading_companion.memory_write import canonical_json, write_memory
    result = {"verification_status": status, "data_gaps": [], "observations": [
        {"subject": "600519", "data_quality": quality, "mae": mae, "excess_return": excess_return if excess_return is not None else 0.05 if status == "correct" else -0.05},
    ]}
    return write_memory(memory, "learning", {
        "memory_space_id": SPACE, "source_system": "stock-advisor", "source_event_id": event,
        "episode_type": "outcome", "authority": "runtime_learning", "body": canonical_json(result),
        "content_hash": "auto", "protocol_version": "memoryhub/v1",
        "occurred_at": AT, "known_at": AT, "submitted_at": AT,
        "metadata": {"outcome_result": result, "cycle_id": cycle, "stage": "m1_judgment", "horizon": "T+1", "judgment_snapshot": {"direction": "bullish"}, "market_state": market_state, "parent_episode_ids": parents or []},
    }, semantic_type="outcome")["episode_id"]


def candidate(memory, request_id="candidate-1"):
    from ai_trading_companion.lesson_promotion import LessonPromotion
    from ai_trading_companion.memory_write import write_memory
    evidence = write_memory(memory, "evidence", {
        "memory_space_id": SPACE, "source_system": "stock-advisor", "source_event_id": "evidence",
        "episode_type": "external_evidence", "authority": "immutable_source_reference",
        "body": "600519: tested market mechanism and counterexample", "content_hash": "auto",
        "protocol_version": "memoryhub/v1", "occurred_at": AT, "known_at": AT, "submitted_at": AT,
    }, semantic_type="evidence")["episode_id"]
    service = LessonPromotion(memory, SPACE)
    created = service.propose(
        request_id, "600519 risk: only use the mechanism in tested states",
        market_states=["range", "trend"], evidence_episode_ids=[evidence],
        counterevidence_episode_ids=[evidence], as_of=AT,
    )
    return service, created


@pytest.fixture(params=["in_memory", "http"])
def memory_port(request, tmp_path):
    if request.param == "in_memory":
        yield InMemoryMemoryAdapter()
        return
    from ai_trading_companion.memory_port import HttpMemoryAdapter
    from trading_memory_hub.server import make_server
    server = make_server("127.0.0.1", 0, tmp_path / "memory.sqlite3", source_adapters={})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield HttpMemoryAdapter(f"http://127.0.0.1:{server.server_port}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_one_hit_and_repeated_requests_cannot_promote_a_lesson(memory_port) -> None:
    memory = memory_port
    service, created = candidate(memory)
    good = append_observation(memory, "good")
    baseline = append_observation(memory, "baseline", status="incorrect")
    trial = service.observe("trial-1", created["episode_id"], good, baseline, subject="600519", market_state="range", as_of=AT)
    assert trial["decision"]["state"] == "inconclusive"
    for _ in range(6):
        assert service.observe("trial-1", created["episode_id"], good, baseline, subject="600519", market_state="range", as_of=AT) == trial
    assert service.assess(created["episode_id"], as_of=AT)["maturity"]["independent_cycles"] == 1


def test_known_before_proposal_pair_is_retained_but_cannot_validate_a_candidate(memory_port) -> None:
    good = append_observation(memory_port, "already-known-good")
    baseline = append_observation(memory_port, "already-known-baseline", status="incorrect")
    service, created = candidate(memory_port)
    later = "2026-09-22T08:10:00Z"
    receipt = service.observe("retrospective", created["episode_id"], good, baseline,
                              subject="600519", market_state="range", as_of=later)
    trial = receipt["decision"]["payload"]["trial"]
    assert trial["state"] == "inconclusive"
    assert "outcome_known_before_proposal" in trial["reasons"]
    assert created["decision"]["payload"]["proposal_cutoff"]["as_of"] == AT
    assert good in receipt["decision"]["payload"]["outcome_episode_ids"]
    assert service.assess(created["episode_id"], as_of=later)["state"] == "inconclusive"


def test_unknown_market_state_keeps_an_inconclusive_attempt() -> None:
    memory = InMemoryMemoryAdapter()
    service, created = candidate(memory)
    good = append_observation(memory, "unknown-good", market_state="unknown")
    baseline = append_observation(memory, "unknown-baseline", status="incorrect", market_state="unknown")
    receipt = service.observe("unknown", created["episode_id"], good, baseline,
                              subject="600519", market_state="unknown", as_of=AT)
    assert receipt["decision"]["payload"]["trial"]["state"] == "inconclusive"
    assert "unqualified_market_state" in receipt["decision"]["payload"]["trial"]["reasons"]
    assert service.assess(created["episode_id"], as_of=AT)["maturity"]["independent_cycles"] == 1


def test_distinct_hypotheses_spend_separate_immutable_alpha_allocations() -> None:
    memory = InMemoryMemoryAdapter()
    service, first = candidate(memory)
    good = append_observation(memory, "good")
    baseline = append_observation(memory, "baseline", status="incorrect")
    service.observe("first", first["episode_id"], good, baseline,
                    subject="600519", market_state="range", as_of=AT)
    before = service.assess(first["episode_id"], as_of=AT)
    _, second = candidate(memory, "candidate-2")
    service.observe("second", second["episode_id"], good, baseline,
                    subject="600519", market_state="range", as_of=AT)
    after = service.assess(second["episode_id"], as_of=AT)
    assert before["maturity"]["hypothesis_alpha"] > after["maturity"]["hypothesis_alpha"]
    assert before["maturity"]["strata"]["range"]["support_interval"][1] < after["maturity"]["strata"]["range"]["support_interval"][1]
    assert service.assess(first["episode_id"], as_of=AT) == before


def test_candidate_retry_recovers_original_cutoff_after_later_writes(memory_port) -> None:
    service, created = candidate(memory_port)
    append_observation(memory_port, "intervening-outcome")
    _, recovered = candidate(memory_port)
    assert recovered == created
    with pytest.raises(MemoryUnavailable, match="immutable lesson request conflict"):
        service.propose("candidate-1", "changed hypothesis", market_states=["range", "trend"],
                        evidence_episode_ids=created["decision"]["provenance"]["evidence_episode_ids"],
                        counterevidence_episode_ids=created["decision"]["provenance"]["counterevidence_episode_ids"], as_of=AT)


@pytest.mark.parametrize("revision", ["candidate", "inconclusive", "superseded"])
def test_every_candidate_revision_can_be_rolled_back_without_rewriting(revision, memory_port) -> None:
    service, created = candidate(memory_port)
    target = created
    if revision != "candidate":
        outcome = append_observation(memory_port, "outcome", status="superseded" if revision == "superseded" else "unverified")
        baseline = append_observation(memory_port, "baseline", status="incorrect")
        target = service.observe("trial", created["episode_id"], outcome, baseline,
                                 subject="600519", market_state="range", as_of=AT)
    original = memory_port.export_space(SPACE)["episodes"]
    result = service.rollback("withdraw", target["episode_id"], reason="withdraw unsupported hypothesis", as_of=AT)
    append_observation(memory_port, "later-write")
    assert service.rollback("withdraw", target["episode_id"], reason="withdraw unsupported hypothesis", as_of=AT) == result
    assert memory_port.export_space(SPACE)["episodes"][:len(original)] == original
    assert result["decision"]["provenance"]["revision_episode_id"] == target["episode_id"]
    assert service.assess(created["episode_id"], as_of=AT)["state"] == "rolled_back"
    with pytest.raises(MemoryUnavailable, match="immutable lesson request conflict"):
        service.rollback("withdraw", target["episode_id"], reason="different reason", as_of=AT)


def lessons(bundle):
    return [row["episode_id"] for row in bundle["results"] if row["retrieval"]["semantic_type"] == "lesson"]


def test_repeated_model_hits_remain_blocked_in_all_states() -> None:
    memory = InMemoryMemoryAdapter()
    service, created = candidate(memory)
    before = memory.begin_snapshot({"memory_space_id": SPACE, "stage": "m1_judgment", "cycle_id": "next-cycle", "as_of": AT})
    assert lessons(memory.retrieve_bundle(before["snapshot_id"], "600519 risk")) == []
    for state in ("range", "trend"):
        for index in range(256):
            cycle = f"{state}-{index}"
            good = append_observation(memory, f"good:{cycle}", cycle=cycle, market_state=state)
            baseline = append_observation(memory, f"baseline:{cycle}", cycle=cycle, status="incorrect", market_state=state)
            promoted = service.observe(cycle, created["episode_id"], good, baseline, subject="600519", market_state=state, as_of=AT)
        if state == "range":
            assert promoted["decision"]["state"] == "inconclusive"
    assert promoted["decision"]["state"] == "inconclusive"
    assert service.assess(created["episode_id"], as_of=AT)["maturity"]["independent_cycles"] == 512
    assert promoted["decision"]["payload"]["blockers"] == [
        "authoritative_outcome_verification_unavailable", "prospective_pair_registration_unavailable",
    ]
    after = memory.begin_snapshot({"memory_space_id": SPACE, "stage": "m1_judgment", "cycle_id": "next-cycle", "as_of": AT})
    assert lessons(memory.retrieve_bundle(after["snapshot_id"], "600519 risk", context={"market_state": "range"})) == []


def test_archived_promotion_retrieval_is_frozen_blind_and_rollback_is_append_only() -> None:
    # This is an archived governance-contract fixture, not a claimed promotion
    # by today's incomplete factual evaluator. Test only retrieval and rollback.
    from ai_trading_companion.memory_retrieval import build_profile
    from ai_trading_companion.memory_write import canonical_json, write_memory
    memory = InMemoryMemoryAdapter()
    service, created = candidate(memory)
    before = memory.begin_snapshot({"memory_space_id": SPACE, "stage": "m1_judgment", "cycle_id": "next-cycle", "as_of": AT})
    archived_outcome = append_observation(memory, "archived-outcome")
    archived_baseline = append_observation(memory, "archived-baseline", status="incorrect")
    attempted = service.observe("archived-trial", created["episode_id"], archived_outcome, archived_baseline,
                                subject="600519", market_state="range", as_of=AT)
    event = {**attempted["decision"], "state": "promoted"}
    receipt = write_memory(memory, "learning", {
        "memory_space_id": SPACE, "source_system": "stock-advisor", "source_event_id": "archived-promotion",
        "episode_type": "lesson", "authority": "runtime_learning", "body": canonical_json(event),
        "content_hash": "auto", "protocol_version": "memoryhub/v1",
        "occurred_at": AT, "known_at": AT, "submitted_at": AT,
        "metadata": {"lesson_promotion": event, "parent_episode_ids": [created["episode_id"]],
                     "memory_retrieval": build_profile(reliability="verified", outcome_support="supported", lesson_state="verified",
                                                       market_states=event["market_states"], evidence_episode_ids=event["payload"]["evidence_episode_ids"],
                                                       outcome_episode_ids=event["payload"]["outcome_episode_ids"])},
    }, semantic_type="lesson")
    promoted = {**receipt, "decision": event}
    frozen = memory.begin_snapshot({"memory_space_id": SPACE, "stage": "m1_judgment", "cycle_id": "next-cycle", "as_of": AT})
    bundle = memory.retrieve_bundle(frozen["snapshot_id"], "600519 risk", context={"market_state": "range"})
    assert lessons(bundle) == [promoted["episode_id"]]
    assert lessons(memory.retrieve_bundle(before["snapshot_id"], "600519 risk")) == []
    assert lessons(memory.retrieve_bundle(frozen["snapshot_id"], "600519 risk", context={"market_state": "unknown"})) == []
    from ai_trading_companion.memory_write import write_memory
    h0 = write_memory(memory, "user_message", {
        "memory_space_id": SPACE, "source_system": "stock-advisor", "source_event_id": "h0",
        "content_hash": "auto", "episode_type": "user_message", "authority": "user_private_fact",
        "protocol_version": "memoryhub/v1", "body": "PRIVATE-DIRECTION",
        "occurred_at": AT, "known_at": AT, "submitted_at": AT, "metadata": {"stage": "h0"},
    }, semantic_type="message")
    poisoned = append_observation(memory, "poisoned", cycle="poisoned-cycle", parents=[h0["episode_id"]])
    baseline = append_observation(memory, "poisoned-baseline", cycle="poisoned-cycle", status="incorrect")
    service.observe("poisoned-trial", created["episode_id"], poisoned, baseline, subject="600519", market_state="range", as_of=AT)
    blind = memory.begin_snapshot({"memory_space_id": SPACE, "stage": "m1_judgment", "cycle_id": "next-cycle", "as_of": AT})
    blinded = memory.retrieve_bundle(blind["snapshot_id"], "600519 risk")
    assert lessons(blinded) == [promoted["episode_id"]]
    assert "PRIVATE-DIRECTION" not in str(blinded)
    rollback_at = "2026-09-22T08:10:00Z"
    rollback = service.rollback("rollback-1", promoted["episode_id"], reason="新反证否定适用条件", as_of=rollback_at)
    assert rollback["decision"]["state"] == "rolled_back"
    assert service.rollback("rollback-1", promoted["episode_id"], reason="新反证否定适用条件", as_of=rollback_at) == rollback
    later = memory.begin_snapshot({"memory_space_id": SPACE, "stage": "m1_judgment", "cycle_id": "next-cycle", "as_of": rollback_at})
    assert lessons(memory.retrieve_bundle(later["snapshot_id"], "600519 risk", context={"market_state": "range"})) == []
    assert lessons(memory.retrieve_bundle(frozen["snapshot_id"], "600519 risk", context={"market_state": "range"})) == [promoted["episode_id"]]
    with pytest.raises(ValueError, match="lesson_not_effective"):
        memory.expand(later["snapshot_id"], promoted["episode_id"])


def test_memoryhub_storage_does_not_authorize_model_metrics_and_repeated_cycle_is_one_unit() -> None:
    memory = InMemoryMemoryAdapter()
    service, created = candidate(memory)
    good = append_observation(memory, "good", status="incorrect", excess_return=0.05)
    baseline = append_observation(memory, "baseline", status="correct", excess_return=-0.05)
    receipt = service.observe("one", created["episode_id"], good, baseline, subject="600519", market_state="range", as_of=AT)
    trial = receipt["decision"]["payload"]["trial"]
    assert trial["state"] == "inconclusive"
    assert trial["support"] == trial["baseline_support"] == 0
    assert trial["quality_passed"] is False
    assert "authoritative_outcome_verification_unavailable" in trial["reasons"]
    service.observe("alias", created["episode_id"], good, baseline, subject="600519", market_state="range", as_of=AT)
    assert service.assess(created["episode_id"], as_of=AT)["maturity"]["independent_cycles"] == 1
    with pytest.raises(MemoryUnavailable, match="immutable lesson request conflict"):
        service.observe("one", created["episode_id"], baseline, good, subject="600519", market_state="range", as_of=AT)
    with pytest.raises(ValueError, match="versioned"):
        service.consume_trial({"contract": "LessonPromotionSpec/v1", "version": 1, "candidate_episode_id": created["episode_id"],
                               "baseline_episode_id": baseline, "subject": "600519", "support": 1},
                              good, request_id="forged", market_state="range", as_of=AT)


@pytest.mark.parametrize("status,quality,mae,expected", [
    ("incorrect", "verified", 0.01, "inconclusive"),
    ("unverified", "missing", None, "inconclusive"),
    ("superseded", "verified", 0.01, "superseded"),
])
def test_all_attempted_outcomes_are_retained(status, quality, mae, expected) -> None:
    memory = InMemoryMemoryAdapter()
    service, created = candidate(memory)
    outcome = append_observation(memory, "outcome", status=status, quality=quality, mae=mae)
    baseline = append_observation(memory, "baseline", status="incorrect")
    receipt = service.observe("attempt", created["episode_id"], outcome, baseline, subject="600519", market_state="range", as_of=AT)
    assert receipt["decision"]["payload"]["trial"]["state"] == expected
    assert receipt["decision"]["payload"]["trial"]["reported_verification_status"] == status
    assert [row["episode_id"] for row in memory.export_space(SPACE)["episodes"]][-1] == receipt["episode_id"]
    assert receipt["decision"]["state"] != "promoted"


def test_memoryhub_failure_keeps_outcome_pending_then_recovers_idempotently(tmp_path: Path) -> None:
    class OfflineMemory(InMemoryMemoryAdapter):
        offline = True

        def append(self, episode):
            if self.offline:
                raise MemoryUnavailable("offline")
            return super().append(episode)

    store = CompanionStore(tmp_path / "runtime.sqlite3")
    cycle = CompanionEngine(store).start_cycle("daily.execution.0945", "2026-09-20T09:45:00+08:00", "2026-09-20T01:45:00Z")
    artifact = store.append_artifact(cycle["cycle_id"], "m1", "model", "未达到触发条件。", "2026-09-20T01:45:00Z")
    snapshot = JudgmentLifecycle(store).capture(artifact, "m1", "未达到触发条件。")
    checkpoint = store.schedule_outcome(snapshot["snapshot_id"], "T+1", AT)
    checkpoint.update({"cycle_id": cycle["cycle_id"], "snapshot_id": snapshot["snapshot_id"]})
    memory = OfflineMemory()
    lifecycle = JudgmentLifecycle(store, memory=memory, memory_space_id=SPACE)
    result = {"as_of": AT, "verification_status": "incorrect", "summary": "结果尚不足以归因。"}
    with pytest.raises(MemoryUnavailable, match="offline"):
        lifecycle.record_outcome(checkpoint, result)
    assert checkpoint["checkpoint_id"] in [row["checkpoint_id"] for row in store.due_outcomes(AT)]
    assert not [row for row in store.artifacts(cycle["cycle_id"]) if row["kind"] == "outcome"]
    memory.offline = False
    lifecycle.record_outcome(checkpoint, result)
    lifecycle.record_outcome(checkpoint, result)
    assert len(memory.export_space(SPACE)["episodes"]) == 1
    assert checkpoint["checkpoint_id"] not in [row["checkpoint_id"] for row in store.due_outcomes(AT)]


def test_runtime_reflection_consumes_lesson_candidate_and_preserves_blind_provenance(tmp_path: Path) -> None:
    from ai_trading_companion.memory_write import write_memory
    memory = InMemoryMemoryAdapter()
    _, evidence_candidate = candidate(memory)
    evidence_id = evidence_candidate["decision"]["provenance"]["evidence_episode_ids"][0]
    judgment = write_memory(memory, "ai_message", {
        "memory_space_id": SPACE, "source_system": "stock-advisor", "source_event_id": "m1-source",
        "content_hash": "auto", "episode_type": "ai_message", "authority": "published_ai_message",
        "protocol_version": "memoryhub/v1", "body": "600519尚未突破。",
        "occurred_at": AT, "known_at": AT, "submitted_at": AT,
        "metadata": {"stage": "m1_judgment"},
    }, semantic_type="judgment")
    store = CompanionStore(tmp_path / "runtime.sqlite3")
    cycle = CompanionEngine(store).start_cycle("daily.execution.0945", "2026-09-20T09:45:00+08:00", "2026-09-20T01:45:00Z")
    artifact = store.append_artifact(cycle["cycle_id"], "m1", "model", "600519尚未突破。", "2026-09-20T01:45:00Z", {"published_message": {"message_id": "m1-source"}})
    snapshot = JudgmentLifecycle(store).capture(artifact, "m1", "600519尚未突破。")
    store.schedule_outcome(snapshot["snapshot_id"], "T+1", AT)
    checkpoint = next(row for row in store.due_outcomes(AT) if row["snapshot_id"] == snapshot["snapshot_id"])
    recorded = JudgmentLifecycle(store, memory=memory, memory_space_id=SPACE).record_outcome(checkpoint, {
        "as_of": AT, "verification_status": "incorrect", "summary": "触发条件在窗口结束后才成立。",
        "diagnosis": "timing_error", "diagnostic_evidence_refs": [evidence_id],
        "lesson_candidate": {"title": "时间窗口", "hypothesis": "600519信号须在原窗口内确认", "evidence_refs": [evidence_id]},
        "lesson_market_states": ["divergence"], "lesson_counterevidence_refs": [evidence_id],
    })
    episodes = memory.export_space(SPACE)["episodes"]
    outcome = next(row for row in episodes if row["source_event_id"] == f"outcome:{checkpoint['checkpoint_id']}")
    assert outcome["metadata"]["parent_episode_ids"] == [judgment["episode_id"]]
    proposed = episodes[-1]["metadata"]["lesson_promotion"]
    assert proposed["contract"] == "LessonPromotionSpec/v1"
    assert proposed["state"] == "candidate"
    assert proposed["payload"]["parent_episode_ids"] == [outcome["episode_id"]]
    assert recorded["artifact_id"]
    frozen = memory.begin_snapshot({"memory_space_id": SPACE, "stage": "m1_judgment", "cycle_id": "next-cycle", "as_of": AT})
    assert lessons(memory.retrieve_bundle(frozen["snapshot_id"], "600519")) == []


def test_outcome_v2_is_strict_without_extending_outcome_v1() -> None:
    import json
    from jsonschema import Draft202012Validator
    contracts = Path(__file__).resolve().parents[2] / "resources" / "contracts"
    v1 = json.loads((contracts / "companion-outcome-result-v1.schema.json").read_text(encoding="utf-8"))
    v2 = json.loads((contracts / "companion-outcome-result-v2.schema.json").read_text(encoding="utf-8"))
    result = {
        "as_of": AT, "checkpoint_ready": True, "target_session_date": "2026-09-21", "next_check_at": None,
        "verification_status": "unverified", "summary": "仍无法确定。", "observations": [], "data_gaps": [],
        "market_regime": {"index_trend": None, "breadth": None, "turnover_change": None, "volatility": None, "data_quality": "missing"},
    }
    Draft202012Validator(v1).validate(result)
    assert list(Draft202012Validator(v2).iter_errors(result))
    result.update({"diagnosis": None, "diagnostic_reason": None, "diagnostic_evidence_refs": [],
                   "lesson_candidate": None, "lesson_market_states": [], "lesson_counterevidence_refs": [], "lesson_trials": []})
    Draft202012Validator(v2).validate(result)
    assert list(Draft202012Validator(v1).iter_errors(result))
    assert list(Draft202012Validator(v2).iter_errors({**result, "support": 1}))
