from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.learning import JudgmentLifecycle
from ai_trading_companion.memory_port import HttpMemoryAdapter, InMemoryMemoryAdapter, MemoryUnavailable
from ai_trading_companion.memory_retrieval import MemoryIsolationError
from ai_trading_companion.memory_retrieval import build_profile
from ai_trading_companion.memory_write import canonical_json, write_memory
from ai_trading_companion.store import CompanionStore
from trading_memory_hub.server import make_server
from test_lesson_frozen_evidence import END, evidence, frozen_pair, proposal
from test_lesson_promotion import AT, SPACE, append_observation, candidate


def _lessons(bundle):
    return [row["episode_id"] for row in bundle["results"]
            if row["retrieval"]["semantic_type"] == "lesson"]


def _archived_promotion(memory):
    service, created = candidate(memory)
    good = append_observation(memory, "http-archived-good")
    baseline = append_observation(memory, "http-archived-baseline", status="incorrect")
    attempt = service.observe("http-archived-attempt", created["episode_id"], good, baseline,
                              subject="600519", market_state="range", as_of=AT)
    event = {**attempt["decision"], "state": "promoted"}
    receipt = write_memory(memory, "learning", {
        "memory_space_id": SPACE, "source_system": "stock-advisor", "source_event_id": "archived-http-promotion",
        "episode_type": "lesson", "authority": "runtime_learning", "body": canonical_json(event),
        "content_hash": "auto", "protocol_version": "memoryhub/v1",
        "occurred_at": AT, "known_at": AT, "submitted_at": AT,
        "metadata": {"lesson_promotion": event, "parent_episode_ids": [created["episode_id"]],
                     "memory_retrieval": build_profile(reliability="verified", outcome_support="supported",
                                                        lesson_state="verified", market_states=event["market_states"],
                                                        evidence_episode_ids=event["payload"]["evidence_episode_ids"],
                                                        outcome_episode_ids=event["payload"]["outcome_episode_ids"])},
    }, semantic_type="lesson")
    return receipt["episode_id"]


@pytest.fixture(params=["in_memory", "http"])
def retrieval_memory(request, tmp_path):
    if request.param == "in_memory":
        yield InMemoryMemoryAdapter()
        return
    server = make_server("127.0.0.1", 0, tmp_path / "memory.sqlite3", source_adapters={})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield HttpMemoryAdapter(f"http://127.0.0.1:{server.server_port}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_archived_promoted_lesson_retrieves_through_enclosing_space(retrieval_memory):
    memory = retrieval_memory
    promoted_id = _archived_promotion(memory)
    snapshot = memory.begin_snapshot({"memory_space_id": SPACE, "stage": "m1_judgment", "cycle_id": "next", "as_of": AT})
    result = memory.retrieve_bundle(snapshot["snapshot_id"], "600519 risk", context={"market_state": "range"})
    assert _lessons(result) == [promoted_id]
    original_export = memory.export_space(SPACE)
    memory.expand(snapshot["snapshot_id"], promoted_id)
    assert memory.export_space(SPACE) == original_export
    from ai_trading_companion.lesson_promotion import LessonPromotion
    LessonPromotion(memory, SPACE).rollback("archived-withdraw", promoted_id, reason="review", as_of=AT)
    # The old frozen view survives; new snapshots must not resurrect the head.
    assert _lessons(memory.retrieve_bundle(snapshot["snapshot_id"], "600519 risk")) == [promoted_id]
    latest = memory.begin_snapshot({"memory_space_id": SPACE, "stage": "m1_judgment", "cycle_id": "latest", "as_of": AT})
    assert _lessons(memory.retrieve_bundle(latest["snapshot_id"], "600519 risk")) == []
    with pytest.raises(MemoryIsolationError):
        memory.expand(latest["snapshot_id"], promoted_id)
    assert memory.export_space(SPACE)["episodes"][:len(original_export["episodes"])] == original_export["episodes"]


@pytest.mark.parametrize("corruption", ["export_space", "explicit_row_space"])
def test_lesson_export_space_boundary_is_fail_closed(corruption):
    class CrossSpaceExport(InMemoryMemoryAdapter):
        corrupt = False

        def export_space(self, space):
            exported = super().export_space(space)
            if self.corrupt:
                if corruption == "export_space":
                    exported["memory_space_id"] = "another-space"
                else:
                    for row in exported["episodes"]:
                        if row.get("metadata", {}).get("lesson_promotion"):
                            row["memory_space_id"] = "another-space"
            return exported

    memory = CrossSpaceExport()
    _archived_promotion(memory)
    snapshot = memory.begin_snapshot({"memory_space_id": SPACE, "stage": "m1_judgment", "cycle_id": "next", "as_of": AT})
    memory.corrupt = True
    if corruption == "export_space":
        with pytest.raises(MemoryIsolationError, match="cross_space_export"):
            memory.retrieve_bundle(snapshot["snapshot_id"], "600519 risk")
    else:
        assert _lessons(memory.retrieve_bundle(snapshot["snapshot_id"], "600519 risk")) == []


def test_incomplete_attempt_is_traceable_but_does_not_poison_corrected_evidence():
    memory = InMemoryMemoryAdapter()
    service, created = proposal(memory)
    complete_pair = frozen_pair(memory)
    missing_pair = {**complete_pair, "window_episode_id": "window-not-yet-available"}
    first = service.observe_frozen_pair("missing", created["episode_id"], missing_pair, as_of=END)
    corrected = service.observe_frozen_pair("corrected", created["episode_id"], complete_pair, as_of=END)
    assessment = service.assess(created["episode_id"], as_of=END)
    trace = service.trace(created["episode_id"], as_of=END)

    assert first["decision"]["payload"]["trial"]["reasons"] == ["missing_frozen_input"]
    assert corrected["decision"]["payload"]["trial"]["state"] == "supported"
    assert len(trace["revisions"]) == 3
    assert assessment["maturity"]["qualified_cycles"] == 1
    assert assessment["maturity"]["quality_passed"] is True
    assert assessment["maturity"]["safety_passed"] is True


def test_incomplete_same_cycle_window_does_not_veto_qualified_replacement():
    memory = InMemoryMemoryAdapter()
    service, created = proposal(memory)
    complete_pair = frozen_pair(memory)
    source = next(row for row in memory.export_space("frozen-lessons")["episodes"]
                  if row["episode_id"] == complete_pair["window_episode_id"])
    incomplete_id = evidence(memory, "incomplete-window", {**json.loads(source["body"]), "bars": []}, at=END)
    service.observe_frozen_pair("incomplete", created["episode_id"],
                                {**complete_pair, "window_episode_id": incomplete_id}, as_of=END)
    service.observe_frozen_pair("complete", created["episode_id"], complete_pair, as_of=END)
    assessment = service.assess(created["episode_id"], as_of=END)
    assert assessment["maturity"]["independent_cycles"] == 1
    assert assessment["maturity"]["qualified_cycles"] == 1
    assert assessment["maturity"]["quality_passed"] is True
    assert assessment["maturity"]["adverse_cycles"] == 0
    assert len(service.trace(created["episode_id"], as_of=END)["revisions"]) == 3


def test_ordinary_adverse_facts_enter_harm_interval_without_permanent_veto():
    memory = InMemoryMemoryAdapter()
    service, created = proposal(memory)
    adverse_pair = frozen_pair(memory, low=88)
    first = service.observe_frozen_pair("ordinary-harm", created["episode_id"], adverse_pair, as_of=END)
    assert first["decision"]["payload"]["trial"]["safety_passed"] is False
    assert first["decision"]["payload"]["trial"]["quality_passed"] is True
    assert first["decision"]["state"] == "inconclusive"
    assessment = service.assess(created["episode_id"], as_of=END)
    assert assessment["maturity"]["qualified_cycles"] == 1
    assert assessment["maturity"]["adverse_cycles"] == 1
    assert assessment["maturity"]["strata"]["trend_expansion"]["harm_interval"][0] > 0
    pair = frozen_pair(memory, "later", index=1)
    service.observe_frozen_pair("later", created["episode_id"], pair, as_of="2026-10-01T00:00:00Z")
    later = service.assess(created["episode_id"], as_of="2026-10-01T00:00:00Z")
    assert later["state"] == "inconclusive"
    assert later["maturity"]["qualified_cycles"] == 2
    assert later["maturity"]["adverse_cycles"] == 1
    assert later["maturity"]["strata"]["trend_expansion"]["harm_interval"][0] > 0


@pytest.mark.parametrize("close,benchmark_close,expected_state,expected_support,quality", [
    (100, 101, "inconclusive", 0, False),
    (101, 102, "supported", 1, True),
])
def test_absolute_original_claim_is_not_reinterpreted_as_benchmark_outperformance(
    close, benchmark_close, expected_state, expected_support, quality,
):
    memory = InMemoryMemoryAdapter()
    service, created = proposal(memory)
    pair = frozen_pair(memory, close=close)
    source = next(row for row in memory.export_space("frozen-lessons")["episodes"]
                  if row["episode_id"] == pair["window_episode_id"])
    window = json.loads(source["body"])
    window["bars"][-1]["benchmark_close"] = benchmark_close
    window_id = evidence(memory, "explicit-benchmark-window", window, at=END)
    receipt = service.observe_frozen_pair("absolute-direction", created["episode_id"],
                                         {**pair, "window_episode_id": window_id}, as_of=END)
    trial = receipt["decision"]["payload"]["trial"]
    assert trial["state"] == expected_state
    assert trial["support"] == expected_support
    assert trial["baseline_support"] == 0
    assert trial["quality_passed"] is quality
    assert trial["metrics"]["stock_return"] == pytest.approx(0 if close == 100 else 0.01)
    assert trial["metrics"]["excess_return"] == pytest.approx(-0.01)
    assert receipt["decision"]["state"] != "promoted"
    if close == 100:
        assert trial["reasons"] == ["directional_outcome_inconclusive"]
    else:
        assert trial["reasons"] == []


@pytest.mark.parametrize("text", ["600519不会上涨", "如果600519上涨，则再考虑买入", "600519可能上涨", "600519上涨但也可能下跌"])
def test_negated_and_conditional_judgments_do_not_create_directional_support(text):
    memory = InMemoryMemoryAdapter()
    service, created = proposal(memory)
    pair = frozen_pair(memory, candidate_text=text)
    receipt = service.observe_frozen_pair("unsupported-text", created["episode_id"], pair, as_of=END)
    trial = receipt["decision"]["payload"]["trial"]
    assert trial["state"] == "inconclusive"
    assert trial["support"] == 0
    assert trial["quality_passed"] is False
    assert "unsupported_frozen_direction" in trial["reasons"]


def test_invalid_optional_lesson_evidence_does_not_block_canonical_outcome(tmp_path: Path):
    store = CompanionStore(tmp_path / "runtime.sqlite3")
    memory = InMemoryMemoryAdapter()
    cycle = CompanionEngine(store).start_cycle("daily.execution.0945", "2026-09-20T09:45:00+08:00", "2026-09-20T01:45:00Z")
    artifact = store.append_artifact(cycle["cycle_id"], "m1", "model", "600519上涨", "2026-09-20T01:45:00Z")
    snapshot = JudgmentLifecycle(store).capture(artifact, "m1", "600519上涨")
    checkpoint = store.schedule_outcome(snapshot["snapshot_id"], "T+1", AT)
    checkpoint.update({"cycle_id": cycle["cycle_id"], "snapshot_id": snapshot["snapshot_id"]})
    lifecycle = JudgmentLifecycle(store, memory=memory, memory_space_id=SPACE)
    result = {"as_of": AT, "checkpoint_ready": True, "verification_status": "incorrect",
              "summary": "Outcome remains canonical despite rejected optional lesson.",
              "diagnosis": "timing_error", "diagnostic_evidence_refs": ["missing-evidence"],
              "lesson_candidate": {"title": "timing", "hypothesis": "timing matters", "evidence_refs": ["missing-evidence"]},
              "lesson_market_states": ["divergence"], "lesson_counterevidence_refs": []}

    recorded = lifecycle.record_outcome(checkpoint, result)
    repeated = lifecycle.record_outcome(checkpoint, result)
    episodes = memory.export_space(SPACE)["episodes"]
    canonical = [row for row in episodes if row["source_event_id"] == f"outcome:{checkpoint['checkpoint_id']}"]
    assert recorded["artifact_id"] == repeated["artifact_id"]
    assert len(canonical) == 1
    rejection = json.loads(repeated["metadata_json"])["lesson_candidate_receipt"]
    assert rejection["state"] == "rejected"
    assert rejection["reason"] == "invalid_evidence_reference"
    assert rejection["outcome_episode_id"] == canonical[0]["episode_id"]
    assert rejection["request"]["evidence_episode_ids"] == ["missing-evidence"]
    assert canonical[0]["metadata"]["outcome_result"] == result
    assert checkpoint["checkpoint_id"] not in {row["checkpoint_id"] for row in store.due_outcomes(AT)}


def _optional_outcome(tmp_path, memory):
    store = CompanionStore(tmp_path / "runtime.sqlite3")
    cycle = CompanionEngine(store).start_cycle("daily.execution.0945", "2026-09-20T09:45:00+08:00", "2026-09-20T01:45:00Z")
    artifact = store.append_artifact(cycle["cycle_id"], "m1", "model", "600519上涨", "2026-09-20T01:45:00Z")
    snapshot = JudgmentLifecycle(store).capture(artifact, "m1", "600519上涨")
    checkpoint = store.schedule_outcome(snapshot["snapshot_id"], "T+1", AT)
    checkpoint.update({"cycle_id": cycle["cycle_id"], "snapshot_id": snapshot["snapshot_id"]})
    lifecycle = JudgmentLifecycle(store, memory=memory, memory_space_id=SPACE)
    result = {"as_of": AT, "checkpoint_ready": True, "verification_status": "incorrect", "summary": "Canonical outcome."}
    return store, checkpoint, lifecycle, result


@pytest.mark.parametrize("trial", [
    {"contract": "LessonPromotionSpec/v1", "version": 1, "candidate_episode_id": "missing-candidate",
     "baseline_episode_id": "missing-baseline", "subject": "600519"},
    {"contract": "LessonPromotionSpec/v1", "version": 99, "candidate_episode_id": "missing-candidate",
     "baseline_episode_id": "missing-baseline", "subject": "600519"},
])
def test_invalid_optional_trial_is_rejected_without_blocking_canonical_outcome(tmp_path, trial):
    memory = InMemoryMemoryAdapter()
    store, checkpoint, lifecycle, result = _optional_outcome(tmp_path, memory)
    result["lesson_trials"] = [trial]
    first = lifecycle.record_outcome(checkpoint, result)
    recovered = lifecycle.record_outcome(checkpoint, result)
    assert first["artifact_id"] == recovered["artifact_id"]
    rejected = json.loads(recovered["metadata_json"])["lesson_trial_receipts"][0]
    assert rejected["state"] == "rejected"
    assert rejected["reason"] == "invalid_trial_reference"
    assert rejected["request"]["trial"] == trial
    assert len(rejected["request_sha256"]) == 64
    assert int(rejected["request_sha256"], 16) > 0
    assert len(memory.export_space(SPACE)["episodes"]) == 1
    assert checkpoint["checkpoint_id"] not in {row["checkpoint_id"] for row in store.due_outcomes(AT)}


@pytest.mark.parametrize("failure", ["outage", "immutable conflict"])
def test_optional_lesson_memory_failure_stays_retriable_not_rejected(tmp_path, failure):
    class UnavailableLessonMemory(InMemoryMemoryAdapter):
        unavailable = True

        def export_space(self, space):
            if self.unavailable:
                raise MemoryUnavailable(failure)
            return super().export_space(space)

    memory = UnavailableLessonMemory()
    store, checkpoint, lifecycle, result = _optional_outcome(tmp_path, memory)
    result["lesson_trials"] = [{"contract": "LessonPromotionSpec/v1", "version": 1,
                               "candidate_episode_id": "missing-candidate", "baseline_episode_id": "missing-baseline", "subject": "600519"}]
    for _ in range(2):
        with pytest.raises(MemoryUnavailable, match=failure):
            lifecycle.record_outcome(checkpoint, result)
    assert not [row for row in store.artifacts(checkpoint["cycle_id"]) if row["kind"] == "outcome"]
    assert checkpoint["checkpoint_id"] in {row["checkpoint_id"] for row in store.due_outcomes(AT)}
    memory.unavailable = False
    assert len(memory.export_space(SPACE)["episodes"]) == 1
    lifecycle.record_outcome(checkpoint, result)
    assert checkpoint["checkpoint_id"] not in {row["checkpoint_id"] for row in store.due_outcomes(AT)}
