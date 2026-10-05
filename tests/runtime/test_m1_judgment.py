from __future__ import annotations

import copy
import json
import time
from pathlib import Path

import pytest

from ai_trading_companion.broker_client import canonical_packet_hash
from ai_trading_companion.evidence_snapshot import build_snapshot, descriptor
from ai_trading_companion.judgment_publication import (
    JudgmentPublicationPipeline,
    JudgmentUnavailable,
    m1_coordination,
    render_core,
)
from ai_trading_companion.m1_judgment import (
    CONTRACT,
    RESULT_CONTRACT,
    assert_blind,
    bind_attempt,
    build_input,
    build_output,
    frozen_replay,
    validate_input,
    validate_output,
    validate_stage_output,
)
from ai_trading_companion.m0_observation import sha256
from ai_trading_companion.mandate_spec import build_mandate
from ai_trading_companion.packet_builder import RuntimePacketBuilder
from ai_trading_companion.memory_port import InMemoryMemoryAdapter
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.store import CompanionStore

from test_judgment_publication import Broker, SCHEMAS, core, packet as publication_packet, runtime


AS_OF = "2026-10-05T01:45:00Z"
CYCLE_ID = "cycle-m1"
TASK_KEY = "manual.non_trading_outlook"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def evidence(*, marker: str = "base", as_of: str = AS_OF) -> dict:
    return {
        "schema_version": 3,
        "as_of": as_of,
        "spoken_summary": f"公开证据 {marker}",
        "sources": [{
            "evidence_ref": "ev_market",
            "excerpt": "成交放大15.50%，下跌家数仍占优；力星弱于指数。",
            "fact_as_of": "2026-10-05T01:30:00Z",
            "known_at": "2026-10-05T02:00:00Z",
        }],
        "coverage": [], "critical_gaps": [], "conflicts": [], "high_impact_events": [],
    }


def m1_packet(*, baseline: dict | None = None, cycle_id: str = CYCLE_ID,
              as_of: str = AS_OF, **extra) -> dict:
    baseline = copy.deepcopy(baseline or evidence())
    snapshot = build_snapshot(
        cycle_id=cycle_id, as_of=as_of, evidence=baseline, source_watermarks={},
    )
    value = {
        "stage": "m1_judgment", "cycle_id": cycle_id, "task_key": TASK_KEY,
        "task_profile": {"evidence_family": "completed_trading_week"},
        "as_of": as_of, "scheduled_for": as_of,
        "business_context": {"private_context_before_h0": {"positions": [{"code": "300421", "shares": 200}]}},
        "mandate": build_mandate(TASK_KEY, "m1_judgment", as_of=as_of),
        "evidence": baseline,
        "evidence_snapshot": descriptor(snapshot),
        "frozen_m0": {
            "artifact_id": "m0-frozen-1", "sha256": sha256("m0 frozen observation"),
            "as_of": "2026-10-05T01:30:00Z", "known_at": "2026-10-05T02:00:00Z",
        },
    }
    value.update(extra)
    value["sha256"] = canonical_packet_hash(value)
    return value


def m1_output(packet: dict | None = None, decision: dict | None = None) -> dict:
    packet = packet or m1_packet()
    decision = copy.deepcopy(decision or core())
    coordination = m1_coordination(decision, packet)
    text = render_core(decision)
    core_hash = canonical_packet_hash(decision)
    draft_hash = canonical_packet_hash({"text": text})
    review = {
        "core_hash": core_hash, "draft_hash": draft_hash,
        "coordination_hash": canonical_packet_hash(coordination),
        "grounded": True, "faithful": True,
        "scores": {"specificity": 2, "causality": 2, "counterargument": 2,
                    "portfolio": 2, "naturalness": 2, "broadcast_risk": 0},
        "problems": [], "suggestions": [],
    }
    return {
        "result_version": 5, "decision_core": decision, "narrative": text,
        "publication": {
            "core_hash": core_hash, "core_attempt_id": "core-attempt",
            "core_review_attempt_id": "review-attempt", "core_review": review,
            "fallback": True, "coordination": coordination,
            "coordination_hash": canonical_packet_hash(coordination),
        },
    }


@pytest.fixture
def valid_input():
    return build_input(m1_packet())


# Contract, boundary, and temporal isolation ---------------------------------


def test_m1_input_has_exact_identity_boundary_and_read_only_permissions(valid_input):
    assert set(valid_input) == {
        "contract", "version", "stage", "source_packet", "evidence_snapshot", "evidence_refs",
        "mandate_reference", "boundary", "permissions", "quantresearch", "provenance",
    }
    assert (valid_input["contract"], valid_input["version"], valid_input["stage"]) == (CONTRACT, 1, "m1_judgment")
    assert valid_input["boundary"]["h0_visible"] is False
    assert valid_input["boundary"]["m2_visible"] is False
    assert valid_input["permissions"] == {"write_permissions": []}
    assert valid_input["quantresearch"] == {"access": "read_only", "write_permissions": []}
    assert valid_input["provenance"]["packet_sha256"] == valid_input["source_packet"]["sha256"]


@pytest.mark.parametrize("field,value", [("contract", "M1JudgmentSpec/v2"), ("version", True), ("stage", "m2")])
def test_m1_input_rejects_wrong_identity_and_boolean_version(valid_input, field, value):
    broken = copy.deepcopy(valid_input)
    broken[field] = value
    with pytest.raises(ValueError):
        validate_input(broken)


@pytest.mark.parametrize("nested", [
    {"h0": {"text": "human secret"}},
    {"nested": {"h0_source_text": "raw H0"}},
    {"nested": {"h0_propositions": ["claim"]}},
    {"nested": {"h0_actions": ["rebalance"]}},
    {"nested": {"h0_action_results": [{"ok": True}]}},
    {"nested": {"h0_derived_signals": ["signal"]}},
    {"nested": {"m2_output": {"text": "later"}}},
    {"nested": {"current_chat": "chat after cutoff"}},
    {"nested": {"premarket": "pre-market"}},
    {"nested": {"private_reasoning": "chain"}},
    {"artifacts": [{"kind": "h0", "text": "artifact"}]},
    {"artifacts": [{"kind": "m1", "text": "later stage"}]},
    {"artifacts": [{"kind": "premarket", "text": "pre-market"}]},
    {"encoded": '{"h0_action_results": ["secret"]}'},
])
def test_nested_later_chat_h0_premarket_and_json_artifacts_are_rejected(nested):
    with pytest.raises(ValueError):
        assert_blind(nested)


def test_human_text_escape_is_rejected_even_when_key_is_unremarkable():
    with pytest.raises(ValueError):
        assert_blind({"context": "unique H0 sentence"}, human_texts=["unique H0 sentence"])


def _refresh_snapshot(packet: dict) -> None:
    old = packet["evidence_snapshot"]
    packet["evidence_snapshot"] = descriptor(build_snapshot(
        cycle_id=packet["cycle_id"], as_of=old["as_of"], evidence=packet["evidence"],
        source_watermarks=old.get("source_watermarks") or {},
        parent_snapshot_id=old.get("parent_snapshot_id"), version=old.get("version", 1),
    ))


@pytest.mark.parametrize("mutator,match", [
    (lambda p: p.update(evidence_snapshot={**p["evidence_snapshot"], "content_hash": "bad"}), "snapshot"),
    (lambda p: p.update(evidence={**p["evidence"], "spoken_summary": "changed"}), "snapshot|baseline"),
    (lambda p: (p["evidence"].update(as_of="2026-10-05T02:00:00Z"), _refresh_snapshot(p)), "future evidence"),
    (lambda p: (p["evidence"]["sources"][0].update(fact_as_of="2026-10-05T02:00:00Z"), _refresh_snapshot(p)), "future evidence"),
    (lambda p: p.update(frozen_m0={**p["frozen_m0"], "as_of": "2026-10-05T02:00:00Z"}), "future M0"),
])
def test_snapshot_baseline_future_fact_and_future_m0_mismatch_are_rejected(mutator, match):
    broken = m1_packet()
    mutator(broken)
    broken["sha256"] = canonical_packet_hash({k: v for k, v in broken.items() if k != "sha256"})
    with pytest.raises(ValueError, match=match):
        build_input(broken)


def test_known_at_is_a_receipt_clock_and_may_be_later_than_cutoff():
    p = m1_packet()
    p["evidence"]["sources"][0]["known_at"] = "2026-10-06T09:00:00Z"
    p["frozen_m0"]["known_at"] = "2026-10-06T09:00:00Z"
    _refresh_snapshot(p)
    p["sha256"] = canonical_packet_hash({k: v for k, v in p.items() if k != "sha256"})
    assert build_input(p)["source_packet"]["frozen_m0"]["known_at"] == "2026-10-06T09:00:00Z"


def test_output_citations_must_stay_inside_frozen_snapshot():
    input_contract = build_input(m1_packet())
    output = m1_output(input_contract["source_packet"])
    output["decision_core"]["reasons"][0]["evidence_refs"] = ["outside"]
    with pytest.raises(ValueError, match="evidence|reference|frozen"):
        validate_stage_output(output, input_contract)


# Structured result, hashes, replay, and immutability -------------------------


def test_structured_judgment_product_reconstructs_from_decision_core(valid_input):
    output = m1_output(valid_input["source_packet"])
    receipt = build_output(valid_input, output, attempt_id="attempt-1")
    assert receipt["contract"] == RESULT_CONTRACT
    assert receipt["product"]["conclusion_claims"][0]["claim"] == core()["thesis"]
    assert receipt["product"]["confidence_state"] == {"level": "medium", "qualified": True}
    assert receipt["product"]["invalidation_conditions"] == [core()["transition_conditions"][1]]
    assert receipt["permissions"] == {"write_permissions": []}
    assert receipt["quantresearch"]["access"] == "read_only"


def test_receipt_self_hash_provenance_hashes_and_attempt_binding(valid_input):
    output = m1_output(valid_input["source_packet"])
    receipt = build_output(valid_input, output)
    assert receipt["provenance"]["attempt_id"] is None
    assert receipt["provenance"]["input_sha256"] == sha256(valid_input)
    assert receipt["provenance"]["output_sha256"] == sha256(output)
    assert receipt["sha256"] == sha256({k: v for k, v in receipt.items() if k != "sha256"})
    bound = bind_attempt(receipt, "actual-attempt")
    assert bound["provenance"]["attempt_id"] == "actual-attempt"
    assert bound["sha256"] != receipt["sha256"]
    assert receipt["provenance"]["attempt_id"] is None


def test_frozen_replay_is_deterministic_deep_copied_and_non_mutating(valid_input):
    output = m1_output(valid_input["source_packet"])
    original_input, original_output = copy.deepcopy(valid_input), copy.deepcopy(output)
    first = frozen_replay(valid_input, output)
    second = frozen_replay(copy.deepcopy(valid_input), copy.deepcopy(output))
    assert first == second
    assert first["qualification"] == {"valid": True, "h0_blind": True, "m2_blind": True, "read_only": True}
    first["source_input"]["evidence_refs"].append("local")
    first["source_output"]["decision_core"]["thesis"] = "mutated"
    assert valid_input == original_input and output == original_output
    with pytest.raises(ValueError, match="digest mismatch"):
        frozen_replay(valid_input, output, expected_output_sha256="0" * 64)


def test_receipt_rejects_tampering_and_unknown_fields(valid_input):
    receipt = build_output(valid_input, m1_output(valid_input["source_packet"]))
    broken = copy.deepcopy(receipt)
    broken["sha256"] = "0" * 64
    with pytest.raises(ValueError):
        validate_output(broken)
    broken = copy.deepcopy(receipt)
    broken["unexpected"] = True
    with pytest.raises(ValueError):
        validate_output(broken)


# Publication pipeline behavior ------------------------------------------------


def test_publication_pipeline_success_and_expression_fallback_reuse_existing_helpers(tmp_path):
    for fail_expression in (False, True):
        pipeline, store, cycle = runtime(tmp_path / str(fail_expression), Broker(fail_expression=fail_expression))
        output = pipeline.produce("m1_judgment", cycle, publication_packet(), time.monotonic() + 30)
        assert output["decision_core"] == core()
        assert output["publication"]["fallback"] is fail_expression
        assert any(a["stage"] == "m1_review" and a["status"] == "succeeded"
                   for a in store.attempts(cycle["cycle_id"]))


def test_publication_pipeline_repair_checkpoint_recovery_and_shadow_separation(tmp_path):
    broker = Broker(reject_draft=True)
    pipeline, store, cycle = runtime(tmp_path / "repair", broker)
    output = pipeline.produce("m1_judgment", cycle, publication_packet(), time.monotonic() + 30)
    assert output["publication"]["fallback"] is True
    assert sum(r.stage == "m1_reasoning" for r in broker.calls) == 1
    restarted = JudgmentPublicationPipeline(Broker(fail_expression=True), store, SCHEMAS, intellect="expert", effort="medium")
    recovered = restarted.produce("m1_judgment", cycle, publication_packet(), time.monotonic() + 30)
    assert recovered["decision_core"] == output["decision_core"]
    assert not any(r.stage == "m1_reasoning" for r in restarted.broker.calls)

    shadow_store = CompanionStore(tmp_path / "shadow" / "runtime.sqlite3")
    shadow_cycle = shadow_store.create_cycle(TASK_KEY, "2026-09-06T10:21:31Z", "2026-09-06T10:21:31Z")
    shadow_pipeline = JudgmentPublicationPipeline(
        Broker(), shadow_store, SCHEMAS, intellect="expert", effort="medium", is_shadow=True,
    )
    shadow_output = shadow_pipeline.produce(
        "m1_judgment", shadow_cycle, publication_packet(), time.monotonic() + 30,
    )
    assert shadow_output["decision_core"] == core()
    assert all(a["is_shadow"] for a in shadow_store.attempts(shadow_cycle["cycle_id"]))
    assert shadow_store.latest_artifact(shadow_cycle["cycle_id"], "m1") is None


def test_pipeline_rejects_bad_evidence_reference_and_never_publishes(tmp_path):
    pipeline, store, cycle = runtime(tmp_path, Broker(bad_ref=True))
    with pytest.raises(JudgmentUnavailable):
        pipeline.produce("m1_judgment", cycle, publication_packet(), time.monotonic() + 30)
    assert store.latest_artifact(cycle["cycle_id"], "m1") is None


# Runtime packet boundary ------------------------------------------------------


def test_router_qualifies_m1_and_leaves_receipt_unbound_until_runtime_attempt():
    packet = m1_packet()
    output = m1_output(packet)
    from ai_trading_companion.router import CognitiveRouter
    verdict = CognitiveRouter().verify("m1_judgment", packet, output)
    assert verdict["passed"], verdict["problems"]
    assert verdict["m1_judgment"]["provenance"]["attempt_id"] is None
    bound = bind_attempt(verdict["m1_judgment"], "runtime-attempt")
    assert bound["provenance"]["attempt_id"] == "runtime-attempt"


def test_actual_store_backed_packet_builder_is_blind_to_human_h0(tmp_path):
    store = CompanionStore(tmp_path / "builder.sqlite3")
    engine = CompanionEngine(store, memory=InMemoryMemoryAdapter())
    cycle = engine.start_cycle("daily.execution.0945", "2026-10-05T09:45:00+08:00", AS_OF)
    engine.research_started(cycle["cycle_id"], as_of=AS_OF)
    baseline = evidence()
    store.create_evidence_snapshot(cycle["cycle_id"], baseline, as_of=AS_OF, source_watermarks={})
    evidence_attempt = store.begin_attempt(cycle["cycle_id"], "m0_research", AS_OF, "evidence-packet")
    store.finish_attempt(evidence_attempt["attempt_id"], "succeeded", output={},
                         output_sha256=sha256({}), verifier={"passed": True})
    m0_packet_hash = "m0-packet"
    m0_attempt = store.begin_attempt(cycle["cycle_id"], "m0_compose", AS_OF, m0_packet_hash)
    m0_body = {"m0_markdown": "公开市场观察"}
    store.finish_attempt(m0_attempt["attempt_id"], "succeeded", output=m0_body,
                         output_sha256=sha256(m0_body), verifier={"passed": True})
    engine.research_ready(
        cycle["cycle_id"], "公开市场观察", evidence_attempt_id=evidence_attempt["attempt_id"],
        compose_attempt_id=m0_attempt["attempt_id"], evidence_packet_hash="evidence-packet",
        packet_hash=m0_packet_hash,
    )
    secret = "H0_ONLY_SECRET_DO_NOT_LEAK"
    engine.command({"command_id": "h0-stage", "cycle_id": cycle["cycle_id"], "type": "stage_message", "text": secret})
    engine.command({"command_id": "h0-commit", "cycle_id": cycle["cycle_id"], "type": "commit_h0"})
    built = RuntimePacketBuilder(PROJECT_ROOT / "resources", store, memory=InMemoryMemoryAdapter()).build(
        store.get_cycle(cycle["cycle_id"]), "m1_judgment", evidence=baseline,
    )
    serialized = json.dumps(built, ensure_ascii=False)
    assert secret not in serialized
    assert all(str(a.get("kind")) != "h0" for a in built.get("artifacts") or [])
    assert built["frozen_m0"]["artifact_id"] == store.latest_artifact_before(cycle["cycle_id"], "m0", AS_OF)["artifact_id"]


# Installed qualification is deterministic -----------------------------------


def test_install_qualification_is_deterministic_and_read_only():
    from ai_trading_companion.m1_judgment import install_qualification
    first, second = install_qualification(), install_qualification()
    assert first == second
    assert first["qualified"] is True
    assert first["evaluation_vector"] == {
        "schema": True, "structured_judgment": True, "frozen_replay": True,
        "h0_m2_isolation": True, "quantresearch_read_only": True,
        "write_permissions_empty": True,
    }


def _runtime_builder_fixture(tmp_path):
    """Create the same frozen M0/H0 boundary that the production runner uses."""
    store = CompanionStore(tmp_path / "engine.sqlite3")
    engine = CompanionEngine(store, memory=InMemoryMemoryAdapter())
    cycle = engine.start_cycle("daily.execution.0945", "2026-10-05T09:45:00+08:00", AS_OF)
    engine.research_started(cycle["cycle_id"], as_of=AS_OF)
    baseline = evidence()
    store.create_evidence_snapshot(cycle["cycle_id"], baseline, as_of=AS_OF, source_watermarks={})
    evidence_attempt = store.begin_attempt(cycle["cycle_id"], "m0_research", AS_OF, "evidence-packet")
    store.finish_attempt(evidence_attempt["attempt_id"], "succeeded", output={},
                         output_sha256=sha256({}), verifier={"passed": True})
    compose_attempt = store.begin_attempt(cycle["cycle_id"], "m0_compose", AS_OF, "m0-packet")
    compose_output = {"m0_markdown": "公开市场观察"}
    store.finish_attempt(compose_attempt["attempt_id"], "succeeded", output=compose_output,
                         output_sha256=sha256(compose_output), verifier={"passed": True})
    engine.research_ready(
        cycle["cycle_id"], "公开市场观察", evidence_attempt_id=evidence_attempt["attempt_id"],
        compose_attempt_id=compose_attempt["attempt_id"], evidence_packet_hash="evidence-packet",
        packet_hash="m0-packet",
    )
    engine.command({"command_id": "stage-h0", "cycle_id": cycle["cycle_id"],
                    "type": "stage_message", "text": "H0 private opinion"})
    engine.command({"command_id": "commit-h0", "cycle_id": cycle["cycle_id"],
                    "type": "commit_h0"})
    raw_packet = RuntimePacketBuilder(
        PROJECT_ROOT / "resources", store, memory=InMemoryMemoryAdapter(),
    ).build(store.get_cycle(cycle["cycle_id"]), "m1_judgment", evidence=baseline)
    return store, engine, cycle, raw_packet


def test_engine_m1_ready_reconstructs_receipt_metadata_and_rejects_tampering(tmp_path):
    store, engine, cycle, raw_packet = _runtime_builder_fixture(tmp_path)
    decision = copy.deepcopy(core())
    decision["position_focus"] = []
    output = m1_output(raw_packet, decision)
    input_contract = build_input(raw_packet)
    research = store.begin_attempt(cycle["cycle_id"], "m1_research", AS_OF, "research-hash")
    store.finish_attempt(research["attempt_id"], "succeeded", output={},
                         output_sha256=sha256({}), verifier={"passed": True})
    judgment = store.begin_attempt(
        cycle["cycle_id"], "m1_judgment", AS_OF, raw_packet["sha256"], input_packet=raw_packet,
    )
    unbound = build_output(input_contract, output)
    bound = bind_attempt(unbound, judgment["attempt_id"])
    store.finish_attempt(
        judgment["attempt_id"], "succeeded", output=output, output_sha256=sha256(output),
        verifier={"passed": True, "m1_judgment": bound},
    )
    result = engine.m1_ready(
        cycle["cycle_id"], output["narrative"],
        research_attempt_id=research["attempt_id"], judgment_attempt_id=judgment["attempt_id"],
        research_packet_hash="research-hash", judgment_packet_hash=raw_packet["sha256"],
    )
    assert result["state"] == "synthesizing_m2"
    artifact = store.latest_artifact(cycle["cycle_id"], "m1")
    metadata = json.loads(artifact["metadata_json"])
    assert metadata["m1_judgment"]["contract"] == RESULT_CONTRACT
    assert metadata["m1_judgment"]["version"] == 1
    assert metadata["m1_judgment"]["sha256"] == bound["sha256"]
    assert metadata["m1_judgment"]["snapshot_id"] == raw_packet["evidence_snapshot"]["snapshot_id"]
    assert metadata["audit"]["contract"] == "AuditSpec/v1"
    audit_rows = store.audit_records(cycle["cycle_id"])
    m1_audit = next(item["record"] for item in audit_rows if item["record"]["stage"] == "m1_judgment")
    assert m1_audit["attempt_id"] == judgment["attempt_id"]
    assert m1_audit["propositions"]


def test_engine_m1_ready_rejects_foreign_stale_tampered_and_shadow_attempts(tmp_path):
    store, engine, cycle, raw_packet = _runtime_builder_fixture(tmp_path)
    decision = copy.deepcopy(core())
    decision["position_focus"] = []
    output = m1_output(raw_packet, decision)
    input_contract = build_input(raw_packet)
    research = store.begin_attempt(cycle["cycle_id"], "m1_research", AS_OF, "research-hash")
    store.finish_attempt(research["attempt_id"], "succeeded", output={},
                         output_sha256=sha256({}), verifier={"passed": True})
    judgment = store.begin_attempt(
        cycle["cycle_id"], "m1_judgment", AS_OF, raw_packet["sha256"], input_packet=raw_packet,
    )
    bound = bind_attempt(build_output(input_contract, output), judgment["attempt_id"])
    tampered = copy.deepcopy(bound)
    tampered["provenance"]["attempt_id"] = "foreign-attempt"
    store.finish_attempt(
        judgment["attempt_id"], "succeeded", output=output, output_sha256=sha256(output),
        verifier={"passed": True, "m1_judgment": tampered},
    )
    with pytest.raises(ValueError, match="receipt|qualified|attempt"):
        engine.m1_ready(
            cycle["cycle_id"], output["narrative"],
            research_attempt_id=research["attempt_id"], judgment_attempt_id=judgment["attempt_id"],
            research_packet_hash="research-hash", judgment_packet_hash=raw_packet["sha256"],
        )

    shadow = store.begin_attempt(
        cycle["cycle_id"], "m1_judgment", AS_OF, raw_packet["sha256"],
        input_packet=raw_packet, is_shadow=True,
    )
    store.finish_attempt(shadow["attempt_id"], "succeeded", output=output,
                         output_sha256=sha256(output), verifier={"passed": True, "m1_judgment": bound})
    with pytest.raises(ValueError, match="shadow"):
        engine.m1_ready(
            cycle["cycle_id"], output["narrative"],
            research_attempt_id=research["attempt_id"], judgment_attempt_id=shadow["attempt_id"],
            research_packet_hash="research-hash", judgment_packet_hash=raw_packet["sha256"],
        )
