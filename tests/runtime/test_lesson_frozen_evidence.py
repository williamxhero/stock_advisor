from __future__ import annotations

import copy
from datetime import datetime, timedelta

import pytest

from ai_trading_companion.lesson_promotion import LessonPromotion
from ai_trading_companion.memory_port import InMemoryMemoryAdapter, MemoryUnavailable
from ai_trading_companion.memory_write import canonical_json, write_memory

SPACE = "frozen-lessons"
START = "2026-09-20T01:45:00Z"
END = "2026-09-21T08:10:00Z"


def evidence(memory, event, value, *, at=START):
    return write_memory(memory, "evidence", {
        "memory_space_id": SPACE, "source_system": "frozen-market-input",
        "source_event_id": event, "episode_type": "external_evidence",
        "authority": "immutable_source_reference", "body": canonical_json(value),
        "content_hash": "auto", "protocol_version": "memoryhub/v1",
        "occurred_at": at, "known_at": at, "submitted_at": at,
    }, semantic_type="evidence")["episode_id"]


def proposal(memory):
    supporting = evidence(memory, "mechanism", {"mechanism": "600519 momentum"})
    contrary = evidence(memory, "counterexample", {"counterexample": "600519 momentum can fail"})
    service = LessonPromotion(memory, SPACE)
    created = service.propose("momentum", "600519 risk: momentum only in validated regimes",
                              market_states=["trend_expansion", "divergence"],
                              evidence_episode_ids=[supporting], counterevidence_episode_ids=[contrary], as_of=START)
    return service, created


def frozen_pair(memory, cycle="cycle-1", *, state="trend_expansion", close=110, low=99, gaps=False, private=False, index=0, snapshot_benchmark="000300", candidate_text="600519上涨", lesson_candidate_id="momentum"):
    start = (datetime.fromisoformat(START.replace("Z", "+00:00")) + timedelta(days=3 * index)).isoformat().replace("+00:00", "Z")
    end = (datetime.fromisoformat(END.replace("Z", "+00:00")) + timedelta(days=3 * index)).isoformat().replace("+00:00", "Z")
    outcomes = []
    for name, text in (("candidate", candidate_text), ("baseline", "600519下跌")):
        judgment = write_memory(memory, "ai_message", {
            "memory_space_id": SPACE, "source_system": "stock-advisor", "source_event_id": f"judgment:{cycle}:{name}",
            "episode_type": "ai_message", "authority": "published_ai_message", "body": text,
            "content_hash": "auto", "protocol_version": "memoryhub/v1",
            "occurred_at": start, "known_at": start, "submitted_at": start,
            "metadata": {"stage": "m2_synthesis" if private and name == "candidate" else "m1_judgment"},
        }, semantic_type="judgment")["episode_id"]
        snapshot = {"subjects": ["600519"], "direction": "bullish" if name == "candidate" else "bearish",
                    "qualified": True, "reference_at": start, "window_end": end, "benchmark": snapshot_benchmark,
                    "original_judgment_text": text}
        if name == "candidate" and lesson_candidate_id is not None:
            snapshot["lesson_candidate_id"] = lesson_candidate_id
        # The model reports the opposite of the raw prices. Its scores and
        # verdicts must have no authority over the deterministic calculation.
        result = {"verification_status": "incorrect" if name == "candidate" else "correct",
                  "observations": [{"subject": "600519", "mae": 0, "excess_return": -999 if name == "candidate" else 999}]}
        metadata = {"cycle_id": cycle, "horizon": "T+1", "stage": "m1_judgment", "market_state": state,
                    "judgment_snapshot": snapshot, "parent_episode_ids": [judgment], "outcome_result": result, "reflection": {}}
        outcome = write_memory(memory, "learning", {
            "memory_space_id": SPACE, "source_system": "stock-advisor", "source_event_id": f"outcome:{cycle}:{name}",
            "episode_type": "outcome", "authority": "runtime_learning",
            "body": canonical_json({"result": result, "reflection": {}, "judgment_snapshot": snapshot, "parent_episode_ids": [judgment]}),
            "content_hash": "auto", "protocol_version": "memoryhub/v1",
            "occurred_at": end, "known_at": end, "submitted_at": end, "metadata": metadata,
        }, semantic_type="outcome")["episode_id"]
        outcomes.append(outcome)
    window = {
        "contract": "LessonFrozenWindow/v1", "version": 1, "cycle_id": cycle, "subject": "600519", "horizon": "T+1", "benchmark": "000300",
        "reference_at": start, "window_end": end, "reference_price": 100, "reference_benchmark_price": 100,
        "market_regime": {"index_trend": 0.1 if state == "trend_expansion" else -0.1,
                          "breadth": 0.7, "turnover_change": 0.1, "volatility": 0.2},
        "bars": [] if gaps else [{"start_at": start, "end_at": end, "close": close,
                                 "benchmark_close": 101, "low": low, "high": max(111, close)}],
    }
    window_id = evidence(memory, f"window:{cycle}", window, at=end)
    return {"contract": "LessonFrozenPair/v1", "version": 1, "subject": "600519",
            "outcome_episode_id": outcomes[0], "baseline_episode_id": outcomes[1], "window_episode_id": window_id}


def test_frozen_prices_not_model_scores_determine_an_offline_attempt():
    memory = InMemoryMemoryAdapter()
    service, created = proposal(memory)
    pair = frozen_pair(memory)
    receipt = service.observe_frozen_pair("one", created["episode_id"], pair, as_of=END)
    trial = receipt["decision"]["payload"]["trial"]
    assert trial["state"] == "supported"
    assert trial["support"] == 1
    assert trial["baseline_support"] == 0
    assert trial["quality_passed"] is True
    assert trial["safety_passed"] is True
    assert trial["metrics"]["excess_return"] == pytest.approx(0.09)
    assert receipt["decision"]["state"] == "inconclusive"
    assert receipt["decision"]["payload"]["frozen_pair"] == pair
    assert service.observe_frozen_pair("one", created["episode_id"], copy.deepcopy(pair), as_of=END) == receipt
    with pytest.raises(MemoryUnavailable, match="immutable lesson request conflict"):
        service.observe_frozen_pair("one", created["episode_id"], {**pair, "subject": "000001"}, as_of=END)


@pytest.mark.parametrize("options,missing,expected,reason", [
    ({"gaps": True}, False, "inconclusive", "incomplete_frozen_window"),
    ({"private": True}, False, "inconclusive", "h0_or_conversation_lineage"),
    ({"close": 100}, False, "inconclusive", "directional_outcome_inconclusive"),
    ({"low": 70}, False, "failed", None),
    ({}, True, "inconclusive", "missing_frozen_input"),
    ({"snapshot_benchmark": "000001"}, False, "inconclusive", "frozen_benchmark_conflict"),
    ({"candidate_text": "600519下跌；000001上涨"}, False, "inconclusive", "unsupported_frozen_direction"),
    ({"lesson_candidate_id": "unrelated-hypothesis"}, False, "inconclusive", "frozen_candidate_identity_conflict"),
    ({"lesson_candidate_id": None}, False, "inconclusive", "frozen_candidate_identity_conflict"),
])
def test_failed_missing_and_private_frozen_inputs_are_retained_not_promoted(options, missing, expected, reason):
    memory = InMemoryMemoryAdapter()
    service, created = proposal(memory)
    pair = frozen_pair(memory, **options)
    if missing:
        pair["window_episode_id"] = "missing-window"
    receipt = service.observe_frozen_pair("attempt", created["episode_id"], pair, as_of=END)
    trial = receipt["decision"]["payload"]["trial"]
    assert trial["state"] == expected
    assert receipt["decision"]["state"] != "promoted"
    if reason:
        assert reason in trial["reasons"]
    trace = service.trace(created["episode_id"], as_of=END)
    assert trace["revisions"][-1]["episode_id"] == receipt["episode_id"]
    withdrawal = service.rollback("withdraw", receipt["episode_id"], reason="review failed attempt", as_of=END)
    assert withdrawal["decision"]["state"] == "rolled_back"


def test_frozen_windows_cannot_inflate_independence_with_new_cycle_labels():
    memory = InMemoryMemoryAdapter()
    service, created = proposal(memory)
    for cycle in ("one", "alias"):
        pair = frozen_pair(memory, cycle)
        service.observe_frozen_pair(cycle, created["episode_id"], pair, as_of=END)
    assert service.assess(created["episode_id"], as_of=END)["maturity"]["independent_cycles"] == 1


def test_frozen_evidence_body_must_match_its_immutable_source_hash():
    class DamagedExport(InMemoryMemoryAdapter):
        def export_space(self, memory_space_id):
            result = super().export_space(memory_space_id)
            for row in result["episodes"]:
                if row["source_event_id"].startswith("window:"):
                    row["body"] = row["body"].replace('"close":110', '"close":109')
            return result
    memory = DamagedExport()
    service, created = proposal(memory)
    pair = frozen_pair(memory)
    trial = service.observe_frozen_pair("damaged", created["episode_id"], pair, as_of=END)["decision"]["payload"]["trial"]
    assert trial["support"] == 0
    assert trial["reasons"] == ["frozen_window_integrity_conflict"]


def test_judgment_lifecycle_consumes_versioned_frozen_evidence_refs(tmp_path):
    from ai_trading_companion.engine import CompanionEngine
    from ai_trading_companion.learning import JudgmentLifecycle
    from ai_trading_companion.store import CompanionStore
    memory = InMemoryMemoryAdapter()
    service, created = proposal(memory)
    store = CompanionStore(tmp_path / "runtime.sqlite3")
    cycle = CompanionEngine(store).start_cycle("daily.execution.0945", START, START)
    pair = frozen_pair(memory, cycle["cycle_id"])
    artifact = store.append_artifact(cycle["cycle_id"], "m1", "model", "600519上涨", START,
                                     {"published_message": {"message_id": f"judgment:{cycle['cycle_id']}:candidate"}})
    lifecycle = JudgmentLifecycle(store, memory=memory, memory_space_id=SPACE)
    captured = lifecycle.capture(artifact, "m1", "600519上涨", snapshot={"window_end": END, "benchmark": "000300", "lesson_candidate_id": created["decision"]["candidate_id"]})
    checkpoint = store.schedule_outcome(captured["snapshot_id"], "T+1", END)
    checkpoint.update({"cycle_id": cycle["cycle_id"], "snapshot_id": captured["snapshot_id"]})
    result = {"as_of": END, "verification_status": "incorrect", "summary": "原始窗口价格已经冻结。",
              "market_regime": {"index_trend": 0.1, "breadth": 0.7, "turnover_change": 0.1, "volatility": 0.2},
              "lesson_trials": [{"contract": "LessonPromotionSpec/v1", "version": 1,
                                 "candidate_episode_id": created["episode_id"], "baseline_episode_id": pair["baseline_episode_id"],
                                 "subject": "600519", "window_episode_id": pair["window_episode_id"]}]}
    recorded = lifecycle.record_outcome(checkpoint, result)
    trace = service.trace(created["episode_id"], as_of=END)
    assert trace["revisions"][-1]["decision"]["payload"]["trial"]["support"] == 1
    assert lifecycle.record_outcome(checkpoint, copy.deepcopy(result))["artifact_id"] == recorded["artifact_id"]
    assert service.trace(created["episode_id"], as_of=END) == trace


@pytest.mark.slow
# Full-ledger public-seam stress: confidence spending, both strata, immutable
# provenance and rollback. Repeated export/requalification is performance debt.
def test_frozen_maturity_requires_independent_windows_and_every_market_state():
    memory = InMemoryMemoryAdapter()
    service, created = proposal(memory)
    as_of = "2031-01-01T00:00:00Z"
    adverse_pair = frozen_pair(memory, "ordinary-prelude", low=88)
    missing = service.observe_frozen_pair("missing-prelude", created["episode_id"],
                                         {**adverse_pair, "window_episode_id": "unavailable-window"}, as_of=as_of)
    assert missing["decision"]["payload"]["trial"]["state"] == "inconclusive"
    adverse = service.observe_frozen_pair("ordinary-prelude", created["episode_id"], adverse_pair, as_of=as_of)
    assert adverse["decision"]["payload"]["trial"]["safety_passed"] is False
    assert adverse["decision"]["state"] == "inconclusive"
    for index in range(256):
        pair = frozen_pair(memory, f"trend-{index}", index=index)
        receipt = service.observe_frozen_pair(f"trend-{index}", created["episode_id"], pair, as_of=as_of)
    assert receipt["decision"]["state"] == "inconclusive"
    assert receipt["decision"]["payload"]["maturity"]["strata"]["trend_expansion"]["qualified"] is True
    for index in range(256):
        pair = frozen_pair(memory, f"divergence-{index}", state="divergence", index=256 + index)
        receipt = service.observe_frozen_pair(f"divergence-{index}", created["episode_id"], pair, as_of=as_of)
    assert receipt["decision"]["state"] == "promoted"
    assert receipt["decision"]["payload"]["blockers"] == []
    assert receipt["decision"]["payload"]["maturity"]["adverse_cycles"] == 1
    revisions = service.trace(created["episode_id"], as_of=as_of)["revisions"]
    assert missing["episode_id"] in [row["episode_id"] for row in revisions]
    assert adverse["episode_id"] in [row["episode_id"] for row in revisions]
    assert service.assess(created["episode_id"], as_of=as_of)["state"] == "promoted"
    snapshot = memory.begin_snapshot({"memory_space_id": SPACE, "stage": "m1_judgment", "cycle_id": "next", "as_of": as_of})
    bundle = memory.retrieve_bundle(snapshot["snapshot_id"], "600519 risk", context={"market_state": "trend_expansion"})
    assert [row["episode_id"] for row in bundle["results"] if row["retrieval"]["semantic_type"] == "lesson"] == [receipt["episode_id"]]
    withdrawn = service.rollback("withdraw", receipt["episode_id"], reason="new counterevidence", as_of=as_of)
    pair = frozen_pair(memory, "after-withdraw", index=512)
    attempted = service.observe_frozen_pair("after-withdraw", created["episode_id"], pair, as_of=as_of)
    assert attempted["decision"]["state"] == "rolled_back"
    assert service.trace(created["episode_id"], as_of=as_of)["effective_state"] == "rolled_back"
    assert withdrawn["episode_id"] in [row["episode_id"] for row in service.trace(created["episode_id"], as_of=as_of)["revisions"]]
