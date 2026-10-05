from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from ai_trading_companion.broker_client import canonical_packet_hash
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.evidence_snapshot import build_snapshot, descriptor
from ai_trading_companion.m0_observation import (
    build_input,
    build_output,
    frozen_replay,
    install_qualification,
    sha256,
    validate_input,
    validate_output,
    validate_stage_output,
)
from ai_trading_companion.memory_port import InMemoryMemoryAdapter
from ai_trading_companion.packet_builder import RuntimePacketBuilder
from ai_trading_companion.router import CognitiveRouter
from ai_trading_companion.stage_expression import normalize_stage_output
from ai_trading_companion.store import CompanionStore


PROJECT_ROOT = Path(__file__).resolve().parents[2]
AS_OF = "2026-10-05T01:45:00Z"
CYCLE_ID = "cycle-m0"


# The six opaque references intentionally have different source kinds.  The
# stage contract is about auditability, while the router retains the stricter
# domain guards for facts, calculations, opinions, and observed propagation.
REFS = {
    "fact": "ev-fact",
    "calculation": "ev-calculation",
    "opinion": "ev-opinion",
    "propagation": "ev-propagation",
    "conflict": "ev-conflict",
    "unknown": "ev-unknown",
}


def evidence(*, marker: str = "base", refs: dict[str, str] | None = None) -> dict:
    refs = refs or REFS
    return {
        "schema_version": 3,
        "as_of": AS_OF,
        "spoken_summary": f"公开证据 {marker}",
        "sources": [
            {
                "evidence_ref": refs["fact"],
                "evidence_kind": "market_fact",
                "excerpt": "市场宽度与指数的公开事实",
            },
            {
                "evidence_ref": refs["calculation"],
                "evidence_kind": "derived_calculation",
                "excerpt": "由公开事实确定性计算出的差额",
            },
            {
                "evidence_ref": refs["opinion"],
                "evidence_kind": "source_opinion",
                "excerpt": "公开来源明确表达的观点",
            },
            {
                "evidence_ref": refs["propagation"],
                "evidence_kind": "social_propagation",
                "market_propagation": {"status": "observed"},
                "excerpt": "公开材料中观察到传播",
            },
            {
                "evidence_ref": refs["conflict"],
                "evidence_kind": "market_fact",
                "excerpt": "不同来源的统计口径",
            },
            {
                "evidence_ref": refs["unknown"],
                "evidence_kind": "market_fact",
                "excerpt": "尚不能由现有材料确认的范围",
            },
        ],
        "coverage": [],
        "critical_gaps": [],
        "conflicts": [],
        "high_impact_events": [],
    }


def snapshot_for(
    *, cycle_id: str = CYCLE_ID, baseline: dict | None = None, marker: str = "base",
) -> dict:
    baseline = baseline or evidence(marker=marker)
    return build_snapshot(
        cycle_id=cycle_id,
        as_of=AS_OF,
        evidence=baseline,
        source_watermarks={},
    )


def packet(
    *,
    snapshot: dict | None = None,
    baseline: dict | None = None,
    cycle_id: str = CYCLE_ID,
    stage: str = "m0_compose",
    mandate: dict | None = None,
    **extra,
) -> dict:
    baseline = baseline or evidence()
    snapshot = snapshot or snapshot_for(cycle_id=cycle_id, baseline=baseline)
    value = {
        "stage": stage,
        "cycle_id": cycle_id,
        "as_of": AS_OF,
        "task_key": "daily.execution.0945",
        "evidence": copy.deepcopy(baseline),
        "evidence_snapshot": descriptor(snapshot),
        "mandate": mandate or {
            "quantresearch_permission": {
                "access": "read_only",
                "write_permissions": [],
            },
        },
    }
    value.update(extra)
    value["sha256"] = canonical_packet_hash(value)
    return value


def stage_output(*, refs: dict[str, str] | None = None, **overrides) -> dict:
    refs = refs or REFS
    semantic = {
        "summary": {
            "text": "截至固定时点，主要指数和市场宽度已记录。",
            "kind": "market_fact",
            "evidence_refs": [refs["fact"]],
        },
        "facts": [{
            "text": "上涨家数多于下跌家数。",
            "kind": "market_fact",
            "evidence_refs": [refs["fact"]],
        }],
        "derived_metrics": [{
            "text": "上涨家数与下跌家数的差额已计算。",
            "kind": "derived_calculation",
            "evidence_refs": [refs["calculation"]],
        }],
        "source_opinions": [{
            "text": "公开来源认为消息仍需继续核验。",
            "kind": "source_opinion",
            "evidence_refs": [refs["opinion"]],
        }],
        "propagation": [{
            "text": "该消息在公开材料中被观察到传播。",
            "kind": "market_propagation",
            "evidence_refs": [refs["propagation"]],
        }],
        "conflicts": [{
            "text": "两条来源的统计口径不同，暂未合并。",
            "kind": "conflict",
            "evidence_refs": [refs["conflict"]],
        }],
        "observations": [{
            "text": "盘面表现存在分化。",
            "kind": "unknown",
            "evidence_refs": [refs["fact"]],
        }],
        "connections": [{
            "text": "消息与相关板块的波动同时出现。",
            "kind": "unknown",
            "evidence_refs": [refs["propagation"]],
        }],
        "attention": [{
            "text": "后续仍需观察公开核验结果。",
            "kind": "unknown",
            "evidence_refs": [refs["unknown"]],
        }],
        "unknowns": [{
            "text": "传播范围是否继续扩大仍未知。",
            "kind": "unknown",
            "evidence_refs": [refs["unknown"]],
        }],
    }
    semantic.update(overrides)
    return {"result_version": 3, "semantic": semantic}


def _input_and_output(*, snapshot: dict | None = None, baseline: dict | None = None):
    input_packet = packet(snapshot=snapshot, baseline=baseline)
    return build_input(input_packet), stage_output()


def _engine_fixture(tmp_path: Path, *, persist_snapshot: bool = True):
    store = CompanionStore(tmp_path / "companion.sqlite3")
    memory = InMemoryMemoryAdapter()
    engine = CompanionEngine(store, memory=memory)
    cycle = engine.start_cycle(
        "daily.execution.0945",
        "2026-10-05T09:45:00+08:00",
        AS_OF,
    )
    engine.research_started(cycle["cycle_id"], as_of=AS_OF)
    baseline = evidence()
    snapshot = (
        store.create_evidence_snapshot(
            cycle["cycle_id"], baseline, as_of=AS_OF, source_watermarks={},
        )
        if persist_snapshot else None
    )
    return store, memory, engine, cycle, baseline, snapshot


def _persist_evidence_attempt(
    store: CompanionStore,
    cycle_id: str,
    baseline: dict,
    *,
    input_packet: dict | None = None,
    packet_hash: str = "evidence-packet",
) -> str:
    attempt = store.begin_attempt(
        cycle_id,
        "m0_research",
        AS_OF,
        packet_hash,
        input_packet=input_packet,
    )
    store.finish_attempt(
        attempt["attempt_id"],
        "succeeded",
        output=baseline,
        output_sha256=sha256(baseline),
        verifier={"passed": True, "problems": []},
    )
    return attempt["attempt_id"]


def _persist_compose_attempt(
    store: CompanionStore,
    cycle_id: str,
    compose_packet: dict,
    *,
    output: dict | None = None,
    receipt: dict | None = None,
    status: str = "succeeded",
    verifier_passed: bool = True,
) -> str:
    output = output or stage_output()
    attempt = store.begin_attempt(
        cycle_id,
        "m0_compose",
        AS_OF,
        compose_packet["sha256"],
        input_packet=compose_packet,
    )
    verifier = {"passed": verifier_passed, "problems": []}
    if receipt is not None:
        verifier["m0_observation"] = receipt
    store.finish_attempt(
        attempt["attempt_id"],
        status,
        output=output,
        output_sha256=sha256(output),
        verifier=verifier,
    )
    return attempt["attempt_id"]


def _qualified_runtime_attempts(
    store: CompanionStore,
    cycle: dict,
    baseline: dict,
    snapshot: dict,
    *,
    compose_packet_override: dict | None = None,
    receipt_override: dict | None = None,
    compose_output: dict | None = None,
):
    evidence_packet = packet(
        cycle_id=cycle["cycle_id"], stage="m0_research", snapshot=snapshot, baseline=baseline,
    )
    compose_packet = compose_packet_override or packet(
        cycle_id=cycle["cycle_id"], stage="m0_compose", snapshot=snapshot, baseline=baseline,
    )
    evidence_attempt_id = _persist_evidence_attempt(
        store, cycle["cycle_id"], baseline,
        input_packet=evidence_packet, packet_hash=evidence_packet["sha256"],
    )
    input_contract = build_input(compose_packet)
    compose_attempt = store.begin_attempt(
        cycle["cycle_id"],
        "m0_compose",
        AS_OF,
        compose_packet["sha256"],
        input_packet=compose_packet,
    )
    compose_output = compose_output or stage_output()
    receipt = receipt_override or build_output(
        input_contract,
        compose_output,
        attempt_id=compose_attempt["attempt_id"],
    )
    store.finish_attempt(
        compose_attempt["attempt_id"],
        "succeeded",
        output=compose_output,
        output_sha256=sha256(compose_output),
        verifier={"passed": True, "problems": [], "m0_observation": receipt},
    )
    return {
        "evidence_attempt_id": evidence_attempt_id,
        "compose_attempt_id": compose_attempt["attempt_id"],
        "evidence_packet": evidence_packet,
        "compose_packet": compose_packet,
        "output": compose_output,
        "receipt": receipt,
    }


# Contract-level tests -----------------------------------------------------


def test_positive_receipt_preserves_all_six_typed_categories_and_distinct_refs():
    input_contract, output = _input_and_output()
    receipt = build_output(input_contract, output, attempt_id="attempt-m0")

    assert receipt["contract"] == "M0ObservationResult/v1"
    assert receipt["source_result_version"] == 3
    assert receipt["provenance"]["attempt_id"] == "attempt-m0"
    assert receipt["permissions"] == {"write_permissions": []}
    assert receipt["quantresearch"] == {"access": "read_only", "write_permissions": []}
    for section, category in (
        ("facts", "fact"),
        ("derived_metrics", "calculation"),
        ("source_opinions", "opinion"),
        ("propagation", "propagation"),
        ("conflicts", "conflict"),
        ("unknowns", "unknown"),
    ):
        item = receipt["semantic"][section][0]
        assert item["kind"] in {
            "market_fact", "derived_calculation", "source_opinion",
            "market_propagation", "conflict", "unknown",
        }
        assert item["evidence_refs"] == [REFS[category]]
    assert validate_output(receipt) == receipt


def test_build_input_requires_a_real_snapshot_descriptor_and_matching_refs():
    baseline = evidence()
    real_snapshot = snapshot_for(baseline=baseline)
    valid = packet(snapshot=real_snapshot, baseline=baseline)
    assert build_input(valid)["evidence_snapshot"] == descriptor(real_snapshot)

    missing_descriptor = copy.deepcopy(valid)
    missing_descriptor.pop("evidence_snapshot")
    with pytest.raises(ValueError):
        build_input(missing_descriptor)

    fake_descriptor = copy.deepcopy(valid)
    fake_descriptor["evidence_snapshot"]["content_hash"] = "not-a-real-snapshot"
    with pytest.raises(ValueError):
        build_input(fake_descriptor)

    mismatched_baseline = copy.deepcopy(valid)
    mismatched_baseline["evidence"] = evidence(marker="changed")
    mismatched_baseline["sha256"] = canonical_packet_hash({k: v for k, v in mismatched_baseline.items() if k != "sha256"})
    with pytest.raises(ValueError, match="reference|snapshot|included"):
        build_input(mismatched_baseline)


def test_build_input_requires_quantresearch_read_only_fields_but_allows_full_mandate_fields():
    valid = packet(mandate={
        "contract": "MandateSpec/v1",
        "stage": "m0_compose",
        "quantresearch_permission": {
            "access": "read_only",
            "write_permissions": [],
            "reason": "full mandate may carry additional policy fields",
        },
    })
    assert build_input(valid)["quantresearch"] == {
        "access": "read_only", "write_permissions": [],
    }

    invalid = [
        {"access": "write", "write_permissions": []},
        {"access": "read_only", "write_permissions": ["portfolio"]},
        {"access": "read_only"},
        {"access": "read_only", "write_permissions": [], "unexpected": True},
    ]
    for permission in invalid:
        with pytest.raises(ValueError, match="read_only|permission|write"):
            build_input(packet(mandate={"quantresearch_permission": permission}))
    with pytest.raises(ValueError, match="permission|read_only"):
        build_input(packet(mandate={"contract": "MandateSpec/v1"}))


def test_typed_claims_require_refs_and_foreign_refs_are_rejected():
    missing = stage_output(facts=[{
        "text": "市场事实", "kind": "market_fact",
    }])
    with pytest.raises(ValueError, match="evidence_refs|refs"):
        validate_stage_output(missing, evidence_refs=REFS.values())

    empty = stage_output(derived_metrics=[{
        "text": "计算结果", "kind": "derived_calculation", "evidence_refs": [],
    }])
    with pytest.raises(ValueError, match="evidence_refs|refs"):
        validate_stage_output(empty, evidence_refs=REFS.values())

    foreign = stage_output(source_opinions=[{
        "text": "来源观点", "kind": "source_opinion", "evidence_refs": ["outside"],
    }])
    with pytest.raises(ValueError, match="outside|frozen"):
        validate_stage_output(foreign, evidence_refs=REFS.values())


def test_unknown_can_be_uncited_but_factual_promotion_cannot_be_uncited():
    uncited_unknown = stage_output(unknowns=[{
        "text": "传播范围仍未知", "kind": "unknown", "evidence_refs": [],
    }])
    assert validate_stage_output(uncited_unknown, evidence_refs=REFS.values()) == uncited_unknown

    uncited_fact = stage_output(facts=[{
        "text": "这是一条事实", "kind": "market_fact", "evidence_refs": [],
    }])
    with pytest.raises(ValueError, match="evidence_refs|refs"):
        validate_stage_output(uncited_fact, evidence_refs=REFS.values())

    uncited_unknown_kind_omitted = stage_output(observations=[{
        "text": "没有来源的待确认观察", "evidence_refs": [],
    }])
    assert validate_stage_output(uncited_unknown_kind_omitted, evidence_refs=REFS.values()) == uncited_unknown_kind_omitted


def test_empty_frozen_snapshot_refs_reject_every_citation():
    output = stage_output()
    with pytest.raises(ValueError, match="frozen|evidence_refs|refs"):
        validate_stage_output(output, evidence_refs=[])


@pytest.mark.parametrize(
    ("section", "is_summary"),
    [
        ("summary", True), ("facts", False), ("derived_metrics", False),
        ("source_opinions", False), ("propagation", False), ("conflicts", False),
        ("observations", False), ("connections", False), ("attention", False),
        ("unknowns", False),
    ],
)
@pytest.mark.parametrize(
    ("language", "forbidden_text"),
    [
        ("english", "The market is bullish; my recommendation is to buy."),
        ("chinese", "市场看涨，建议买入。"),
    ],
)
def test_directional_and_trading_language_is_rejected_in_every_section(
    section: str, is_summary: bool, language: str, forbidden_text: str,
):
    value = {"text": forbidden_text, "kind": "unknown", "evidence_refs": [REFS["fact"]]}
    if section == "summary":
        override = value
    else:
        # The section limits are all at least one; replacing the first item
        # isolates the language gate from every other semantic category.
        override = [value]
    invalid = stage_output(**{section: override})
    with pytest.raises(ValueError, match="directional|trading|action"):
        validate_stage_output(invalid, evidence_refs=REFS.values())


@pytest.mark.parametrize(
    "nested_forbidden",
    [
        {"context": {"h0": {"text": "private H0"}}},
        {"context": {"m1_output": {"text": "later M1"}}},
        {"context": {"m2_output": {"text": "later M2"}}},
        {"context": {"pre_m0": [{"text": "pre-M0"}]}},
        {"context": {"premarket": {"text": "premarket"}}},
        {"context": {"premarket_artifact": {"text": "premarket artifact"}}},
        {"context": {"artifacts": [{"kind": "h0", "text": "H0 artifact"}]}},
        {"context": {"artifacts": [{"kind": "m1", "text": "M1 artifact"}]}},
        {"context": {"artifacts": [{"kind": "m2", "text": "M2 artifact"}]}},
        {"context": {"artifacts": [{"kind": "pre_m0", "text": "pre-M0 artifact"}]}},
        {"context": {"artifacts": [{"kind": "premarket", "text": "premarket artifact"}]}},
    ],
)
def test_nested_h0_later_stage_and_premarket_channels_are_forbidden(nested_forbidden: dict):
    with pytest.raises(ValueError, match="forbidden|channel|boundary"):
        build_input(packet(**nested_forbidden))


def test_router_does_not_promote_source_opinions_or_ai_reasoning_to_facts():
    router = CognitiveRouter()
    valid = router.verify("m0_compose", packet(), stage_output())
    assert valid["passed"], valid["problems"]

    promoted_opinion = stage_output(facts=[{
        "text": "来源的观点被错误提升为事实",
        "kind": "source_opinion",
        "evidence_refs": [REFS["opinion"]],
    }])
    verdict = router.verify("m0_compose", packet(), promoted_opinion)
    assert not verdict["passed"]
    assert any("kind" in problem for problem in verdict["problems"])

    ai_reasoning = evidence()
    ai_reasoning["sources"][2]["evidence_kind"] = "ai_reasoning"
    ai_packet = packet(baseline=ai_reasoning, snapshot=snapshot_for(baseline=ai_reasoning))
    verdict = router.verify("m0_compose", ai_packet, stage_output())
    assert not verdict["passed"]
    assert any("source_kind" in problem for problem in verdict["problems"])


def test_conflicts_are_preserved_as_conflicts_with_their_own_refs():
    conflicts = [
        {"text": "来源甲与来源乙的统计口径不同。", "kind": "conflict", "evidence_refs": [REFS["conflict"]]},
        {"text": "两条公开材料的时间范围不同，暂不合并。", "kind": "conflict", "evidence_refs": [REFS["opinion"]]},
    ]
    output = stage_output(conflicts=conflicts)
    input_contract = build_input(packet())
    receipt = build_output(input_contract, output)
    assert receipt["semantic"]["conflicts"] == conflicts
    assert [item["kind"] for item in receipt["semantic"]["conflicts"]] == ["conflict", "conflict"]


def test_receipt_has_a_self_digest_and_semantic_output_digest():
    input_contract, output = _input_and_output()
    receipt = build_output(input_contract, output, attempt_id="attempt-digest")

    assert receipt["sha256"] == sha256({key: value for key, value in receipt.items() if key != "sha256"})
    assert receipt["provenance"]["input_sha256"] == sha256(input_contract)
    assert receipt["provenance"]["output_sha256"] == sha256({
        "result_version": 3, "semantic": output["semantic"],
    })

    tampered = copy.deepcopy(receipt)
    tampered["semantic"]["summary"]["text"] = "篡改后的观察"
    with pytest.raises(ValueError, match="digest|sha256"):
        validate_output(tampered)

    tampered = copy.deepcopy(receipt)
    tampered["evidence_refs"] = ["foreign"]
    with pytest.raises(ValueError, match="evidence|refs|snapshot"):
        validate_output(tampered)


@pytest.mark.parametrize(
    "malformed",
    [
        {},
        {"contract": "M0ObservationSpec/v1", "version": 1},
        {"contract": "M0ObservationSpec/v1", "version": 2},
    ],
)
def test_validate_input_rejects_strict_malformed_contracts(malformed: dict):
    with pytest.raises(ValueError):
        validate_input(malformed)


def test_validate_input_and_output_reject_unknown_fields_and_boundary_mutations():
    input_contract, output = _input_and_output()
    extra_input = copy.deepcopy(input_contract)
    extra_input["unexpected"] = True
    with pytest.raises(ValueError, match="exact|fields"):
        validate_input(extra_input)

    receipt = build_output(input_contract, output)
    for field, mutation in (
        ("permissions", {"write_permissions": ["portfolio"]}),
        ("quantresearch", {"access": "write", "write_permissions": []}),
        ("boundary", {**receipt["boundary"], "h0_visible": True}),
    ):
        broken = copy.deepcopy(receipt)
        broken[field] = mutation
        with pytest.raises(ValueError, match="read_only|boundary|visibility|digest"):
            validate_output(broken)

    for malformed_output in (
        {"result_version": 4, "semantic": output["semantic"]},
        {"result_version": 3, "semantic": {**output["semantic"], "extra": []}},
        {"result_version": 3, "semantic": {key: value for key, value in output["semantic"].items() if key != "unknowns"}},
    ):
        with pytest.raises(ValueError):
            validate_stage_output(malformed_output, evidence_refs=REFS.values())


def test_frozen_replay_is_deterministic_and_does_not_mutate_sources():
    input_contract, output = _input_and_output()
    original_input = copy.deepcopy(input_contract)
    original_output = copy.deepcopy(output)

    first = frozen_replay(input_contract, output)
    second = frozen_replay(copy.deepcopy(input_contract), copy.deepcopy(output))

    assert first == second
    assert input_contract == original_input
    assert output == original_output
    assert first["source_output_sha256"] == sha256(output)
    assert first["qualification"] == {
        "valid": True,
        "read_only": True,
        "m0_only": True,
        "h0_blind": True,
        "later_stages_blind": True,
    }
    first["source_input"]["evidence_refs"].append("local-mutation")
    first["source_output"]["semantic"]["summary"]["text"] = "local mutation"
    assert input_contract == original_input
    assert output == original_output
    with pytest.raises(ValueError, match="digest mismatch"):
        frozen_replay(input_contract, output, expected_output_sha256="0" * 64)


def test_install_qualification_is_deterministic_and_read_only():
    first = install_qualification()
    second = install_qualification()
    assert first == second
    assert first["qualified"] is True
    assert first["evaluation_vector"]["quantresearch_read_only"] is True
    assert first["evaluation_vector"]["frozen_replay"] is True


@pytest.mark.parametrize("result", [
    {"result_version": 4, "narrative": "截至固定时点，只有公开观察。", "candidate_research": []},
    {"m0_markdown": "截至固定时点，只有公开观察。"},
])
def test_legacy_and_v4_outputs_never_claim_a_versioned_receipt(result: dict):
    verdict = CognitiveRouter().verify("m0_compose", packet(), result)
    assert "m0_observation" not in verdict
    with pytest.raises(ValueError):
        validate_stage_output(result, evidence_refs=REFS.values())


# Runtime/store integration ------------------------------------------------


def test_engine_publishes_v3_with_receipt_metadata_event_and_frozen_snapshot(tmp_path: Path):
    store, _memory, engine, cycle, baseline, snapshot = _engine_fixture(tmp_path)
    attempts = _qualified_runtime_attempts(store, cycle, baseline, snapshot)

    ready = engine.research_ready(
        cycle["cycle_id"],
        normalize_stage_output("m0_compose", attempts["output"]).text,
        evidence_attempt_id=attempts["evidence_attempt_id"],
        compose_attempt_id=attempts["compose_attempt_id"],
        evidence_packet_hash=attempts["evidence_packet"]["sha256"],
        packet_hash=attempts["compose_packet"]["sha256"],
        evidence_as_of=AS_OF,
    )

    assert ready["state"] == "awaiting_h0"
    artifact = store.latest_artifact(cycle["cycle_id"], "m0")
    assert artifact is not None
    metadata = json.loads(artifact["metadata_json"])
    observation_metadata = metadata["m0_observation"]
    assert observation_metadata["contract"] == "M0ObservationResult/v1"
    assert observation_metadata["version"] == 1
    assert observation_metadata["snapshot_id"] == snapshot["snapshot_id"]
    assert observation_metadata["sha256"] == sha256(attempts["receipt"])

    event = next(item for item in store.pending_events() if item["event_type"] == "m0.ready")
    payload = json.loads(event["payload_json"])
    assert payload["m0_observation"] == observation_metadata
    assert payload["source_artifact_id"] == artifact["artifact_id"]
    assert store.evidence_snapshot(snapshot["snapshot_id"])["baseline"] == baseline


def test_engine_rejects_missing_foreign_and_stale_receipts_without_publication(tmp_path: Path):
    cases = ("missing", "foreign", "stale")
    for case in cases:
        case_path = tmp_path / case
        case_path.mkdir()
        store, _memory, engine, cycle, baseline, snapshot = _engine_fixture(case_path)
        actual_packet = packet(cycle_id=cycle["cycle_id"], snapshot=snapshot, baseline=baseline)
        attempts = _qualified_runtime_attempts(
            store, cycle, baseline, snapshot, compose_packet_override=actual_packet,
        )
        compose_attempt = next(
            item for item in store.attempts(cycle["cycle_id"])
            if item["attempt_id"] == attempts["compose_attempt_id"]
        )
        output = attempts["output"]
        if case == "missing":
            verifier = {"passed": True, "problems": []}
        elif case == "foreign":
            verifier = {"passed": True, "problems": [], "m0_observation": build_output(
                build_input(actual_packet), output, attempt_id="a-foreign-attempt",
            )}
        else:
            old_baseline = evidence(marker="old")
            old_snapshot = store.create_evidence_snapshot(
                cycle["cycle_id"], old_baseline, as_of=AS_OF, source_watermarks={"old": "1"},
            )
            old_packet = packet(
                cycle_id=cycle["cycle_id"], snapshot=old_snapshot, baseline=old_baseline,
            )
            verifier = {"passed": True, "problems": [], "m0_observation": build_output(
                build_input(old_packet), output,
                attempt_id=attempts["compose_attempt_id"],
            )}
        # Replace only the verifier on the persisted succeeded attempt.  The
        # input packet and output remain the actual frozen compose attempt.
        with store.connection() as connection:
            connection.execute(
                "UPDATE llm_attempt SET verifier_json=? WHERE attempt_id=?",
                (json.dumps(verifier, ensure_ascii=False, sort_keys=True), compose_attempt["attempt_id"]),
            )
        with pytest.raises(ValueError, match="receipt|observation|attempt|snapshot|qualified"):
            engine.research_ready(
                cycle["cycle_id"], output["semantic"]["summary"]["text"],
                evidence_attempt_id=attempts["evidence_attempt_id"],
                compose_attempt_id=attempts["compose_attempt_id"],
                evidence_packet_hash=attempts["evidence_packet"]["sha256"],
                packet_hash=actual_packet["sha256"],
            )
        assert store.latest_artifact(cycle["cycle_id"], "m0") is None
        assert store.get_cycle(cycle["cycle_id"])["state"] == "researching_m0"


def test_engine_rejects_packet_without_a_persisted_snapshot_or_with_stale_baseline(tmp_path: Path):
    # No stored snapshot: the descriptor is cryptographically real, but it is
    # not a Runtime-owned baseline for this cycle.
    store, _memory, engine, cycle, baseline, _stored = _engine_fixture(
        tmp_path / "missing-snapshot", persist_snapshot=False,
    )
    ephemeral = snapshot_for(cycle_id=cycle["cycle_id"], baseline=baseline, marker="ephemeral")
    attempts = _qualified_runtime_attempts(
        store, cycle, baseline, ephemeral,
        compose_packet_override=packet(cycle_id=cycle["cycle_id"], snapshot=ephemeral, baseline=baseline),
    )
    with pytest.raises(ValueError, match="snapshot|receipt|qualified"):
        engine.research_ready(
            cycle["cycle_id"], normalize_stage_output("m0_compose", attempts["output"]).text,
            evidence_attempt_id=attempts["evidence_attempt_id"],
            compose_attempt_id=attempts["compose_attempt_id"],
            evidence_packet_hash=attempts["evidence_packet"]["sha256"],
            packet_hash=attempts["compose_packet"]["sha256"],
        )

    # The descriptor belongs to the stored snapshot, but the packet baseline
    # is altered after descriptor construction.  Runtime must compare the
    # complete immutable baseline, not only snapshot_id/content_hash fields.
    store, _memory, engine, cycle, baseline, snapshot = _engine_fixture(tmp_path / "stale-baseline")
    altered = copy.deepcopy(baseline)
    altered["spoken_summary"] = "altered after snapshot freeze"
    bad_packet = packet(cycle_id=cycle["cycle_id"], snapshot=snapshot, baseline=altered)
    with pytest.raises(ValueError, match="baseline|snapshot|identity|mismatch"):
        _qualified_runtime_attempts(
            store, cycle, baseline, snapshot, compose_packet_override=bad_packet,
        )


def test_retry_checkpoint_recovery_is_idempotent_and_duplicate_publication_adds_nothing(tmp_path: Path):
    store, _memory, engine, cycle, baseline, snapshot = _engine_fixture(tmp_path)
    attempts = _qualified_runtime_attempts(store, cycle, baseline, snapshot)

    failed = store.begin_attempt(
        cycle["cycle_id"], "m0_compose", AS_OF,
        attempts["compose_packet"]["sha256"], input_packet=attempts["compose_packet"],
    )
    store.finish_attempt(
        failed["attempt_id"], "rejected", output=attempts["output"],
        output_sha256=sha256(attempts["output"]),
        verifier={"passed": False, "problems": ["provider candidate rejected"]},
    )
    checkpoint = store.save_stage_checkpoint(
        cycle["cycle_id"], "m0_compose", attempts["compose_packet"]["sha256"],
        attempts["compose_attempt_id"], attempts["output"],
    )
    replay_checkpoint = store.stage_checkpoint(
        cycle["cycle_id"], "m0_compose", attempts["compose_packet"]["sha256"],
    )
    assert checkpoint["attempt_id"] == attempts["compose_attempt_id"]
    assert replay_checkpoint["attempt_id"] == attempts["compose_attempt_id"]
    assert replay_checkpoint["output"] == attempts["output"]
    assert {item["status"] for item in store.attempts(cycle["cycle_id"]) if item["stage"] == "m0_compose"} == {"rejected", "succeeded"}

    reopened = CompanionEngine(CompanionStore(store.database), memory=InMemoryMemoryAdapter())
    kwargs = {
        "evidence_attempt_id": attempts["evidence_attempt_id"],
        "compose_attempt_id": attempts["compose_attempt_id"],
        "evidence_packet_hash": attempts["evidence_packet"]["sha256"],
        "packet_hash": attempts["compose_packet"]["sha256"],
    }
    reopened.research_ready(
        cycle["cycle_id"], normalize_stage_output("m0_compose", attempts["output"]).text, **kwargs,
    )
    artifacts_before = store.artifacts(cycle["cycle_id"])
    events_before = store.pending_events()
    with pytest.raises(ValueError, match="researching|already|M0"):
        reopened.research_ready(
            cycle["cycle_id"], normalize_stage_output("m0_compose", attempts["output"]).text, **kwargs,
        )
    assert store.artifacts(cycle["cycle_id"]) == artifacts_before
    assert store.pending_events() == events_before


def test_router_binds_receipt_without_attempt_then_runtime_binds_attempt_before_persistence():
    verdict = CognitiveRouter().verify("m0_compose", packet(), stage_output())
    assert verdict["passed"], verdict["problems"]
    assert verdict["m0_observation"]["provenance"]["attempt_id"] is None

    input_contract = build_input(packet())
    bound = build_output(input_contract, stage_output(), attempt_id="runtime-attempt")
    assert bound["provenance"]["attempt_id"] == "runtime-attempt"


def test_m1_packet_is_blind_to_h0_in_actual_store_backed_packet(tmp_path: Path):
    store, _memory, engine, cycle, baseline, snapshot = _engine_fixture(tmp_path)
    attempts = _qualified_runtime_attempts(store, cycle, baseline, snapshot)
    engine.research_ready(
        cycle["cycle_id"], normalize_stage_output("m0_compose", attempts["output"]).text,
        evidence_attempt_id=attempts["evidence_attempt_id"],
        compose_attempt_id=attempts["compose_attempt_id"],
        evidence_packet_hash=attempts["evidence_packet"]["sha256"],
        packet_hash=attempts["compose_packet"]["sha256"],
    )
    secret = "这是 H0 私人判断，不能进入 M1 的实际输入包"
    engine.command({
        "command_id": "stage-h0", "cycle_id": cycle["cycle_id"],
        "type": "stage_message", "text": secret,
    })
    engine.command({
        "command_id": "commit-h0", "cycle_id": cycle["cycle_id"],
        "type": "commit_h0",
    })
    builder = RuntimePacketBuilder(
        PROJECT_ROOT / "resources", store, memory=InMemoryMemoryAdapter(),
    )
    m1_packet = builder.build(
        store.get_cycle(cycle["cycle_id"]), "m1_judgment", evidence=baseline,
    )
    serialized = json.dumps(m1_packet, ensure_ascii=False)
    assert secret not in serialized
    assert "h0" not in {
        str(artifact.get("kind")) for artifact in m1_packet.get("artifacts") or []
    }
