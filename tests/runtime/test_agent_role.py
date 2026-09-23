from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from ai_trading_companion.agent_contract import attach_input
from ai_trading_companion.agent_role import (
    CONTRACT,
    ROLE_IDS,
    CoordinatorStateStore,
    SPEC95_DEPENDENCY_GRAPH,
    attach_role_inputs,
    build_input,
    build_output,
    build_runtime_coordinator_output,
    frozen_replay,
    install_qualification,
    role_definition,
    spec95_runtime_qualification,
    spec95_dependency_states,
    validate_output,
)
from jsonschema import Draft202012Validator

PACKET = {
    "cycle_id": "cycle-role-1",
    "stage": "m1_research",
    "as_of": "2026-09-21T01:00:00Z",
    "evidence_snapshot": {
        "contract": "evidence-snapshot-spec/v1", "snapshot_id": "snap-role-1",
        "as_of": "2026-09-21T01:00:00Z", "content_hash": "evidence-hash",
    },
    "memories": [{"artifact_id": "memory-1", "as_of": "2026-09-20T08:00:00Z"}],
    "sha256": "packet-hash",
}


def _agent_input() -> dict:
    return attach_input(PACKET, capability="research:m1_research")["agent_contract"]


def test_role_roster_is_versioned_internal_and_deterministic() -> None:
    agent = _agent_input()
    first = attach_role_inputs({**PACKET, "agent_contract": agent}, stage="m1_research")
    second = attach_role_inputs({**PACKET, "agent_contract": agent}, stage="m1_research")
    assert first == second
    assert [row["role"] for row in first["agent_role_inputs"]] == list(ROLE_IDS)
    assert all(row["contract"] == CONTRACT and row["visibility"] == "internal" for row in first["agent_role_inputs"])
    assert all("h0_frozen" not in row["input_refs"] for row in first["agent_role_inputs"])
    assert all(row["permissions"]["write_permissions"] == [] for row in first["agent_role_inputs"])
    for role in ROLE_IDS:
        definition = role_definition(role)
        assert definition["allowed_inputs"]
        assert definition["forbidden_actions"]
        assert definition["output_kinds"]
        assert definition["write_permissions"] == []

    m0_agent = attach_input({**PACKET, "stage": "m0_research"}, capability="research:m0_research")["agent_contract"]
    m0 = attach_role_inputs({**PACKET, "stage": "m0_research", "agent_contract": m0_agent}, stage="m0_research")
    assert "bull" not in {row["role"] for row in m0["agent_role_inputs"]}
    assert "bear" not in {row["role"] for row in m0["agent_role_inputs"]}


def test_role_input_rejects_disallowed_input_and_m1_h0_leak() -> None:
    with pytest.raises(ValueError, match="disallowed inputs|protected context"):
        build_input(_agent_input(), role="fundamental", stage="m1_research", input_refs={"h0_frozen": ["h0"]}, provenance={"as_of": PACKET["as_of"]})
    with pytest.raises(ValueError, match="forbidden context|protected context"):
        build_input(_agent_input(), role="coordinator", stage="m1_research", input_refs={"public_evidence": ["e"], "role_outputs": ["r"], "h0": ["h0"]}, provenance={"as_of": PACKET["as_of"]})


def test_role_output_is_structured_read_only_and_provenance_bound() -> None:
    agent = _agent_input()
    role_input = build_input(
        agent, role="risk", stage="m1_research",
        input_refs={"evidence_snapshot": ["snap-role-1"], "public_evidence": ["evidence-hash"], "risk_policy": ["runtime:risk-policy-v1"]},
        provenance={"cycle_id": "cycle-role-1", "as_of": PACKET["as_of"]},
    )
    output = build_output(
        role_input, status="succeeded", decision_effect="block",
        evidence_refs=["evidence-hash"], risks=[{"id": "risk-1", "description": "资格门未通过"}],
    )
    validate_output(output)
    assert output["provenance"]["input_sha256"]
    assert output["permissions"]["write_permissions"] == []
    with pytest.raises(ValueError, match="write_permissions|read-only"):
        invalid = copy.deepcopy(output)
        invalid["permissions"]["write_permissions"] = ["MemoryHub"]
        validate_output(invalid)
    with pytest.raises(ValueError, match="fact-system writes|unsupported fields"):
        invalid = copy.deepcopy(output)
        invalid["writes"] = {"portfolio": [{"symbol": "600000.SH"}]}
        validate_output(invalid)


def test_runtime_coordinator_artifact_preserves_role_boundary() -> None:
    packet = attach_role_inputs({**PACKET, "agent_contract": _agent_input()}, stage="m1_research")
    output = build_runtime_coordinator_output(
        packet["agent_role_inputs"], status="blocked", evidence_refs=["evidence-hash"],
        unknowns=["market breadth unavailable"], attempt_id="attempt-1", bundle_sha256="bundle-1",
    )
    assert output["role"] == "coordinator"
    assert output["decision_effect"] == "block"
    assert output["visibility"] == "internal"
    assert output["provenance"]["bundle_sha256"] == "bundle-1"


@pytest.mark.parametrize(
    ("status", "effect"),
    [
        ("succeeded", "coordinate"),
        ("partial", "block"),
        ("blocked", "block"),
        ("failed", "block"),
        ("unknown", "unknown"),
    ],
)
def test_runtime_coordinator_terminal_status_is_explicit_and_idempotent(status: str, effect: str) -> None:
    packet = attach_role_inputs({**PACKET, "agent_contract": _agent_input()}, stage="m1_research")
    role_inputs = packet["agent_role_inputs"]
    original = copy.deepcopy(role_inputs)
    kwargs = {
        "status": status,
        "evidence_refs": ["evidence-hash"],
        "unknowns": ["source availability not verified"],
        "attempt_id": "attempt-stable",
        "bundle_sha256": "bundle-stable",
    }

    first = build_runtime_coordinator_output(role_inputs, **kwargs)
    recovered = build_runtime_coordinator_output(copy.deepcopy(role_inputs), **kwargs)

    assert first == recovered
    assert first["status"] == status
    assert first["decision_effect"] == effect
    assert first["provenance"]["attempt_id"] == "attempt-stable"
    assert role_inputs == original


def test_runtime_coordinator_rejects_ambiguous_identity_and_incomplete_attempt_context() -> None:
    packet = attach_role_inputs({**PACKET, "agent_contract": _agent_input()}, stage="m1_research")
    role_inputs = packet["agent_role_inputs"]
    coordinator = next(row for row in role_inputs if row["role"] == "coordinator")

    with pytest.raises(ValueError, match="ambiguous"):
        build_runtime_coordinator_output(
            [*role_inputs, copy.deepcopy(coordinator)], status="succeeded", evidence_refs=[],
            unknowns=[], attempt_id="attempt-1", bundle_sha256=None,
        )
    with pytest.raises(ValueError, match="attempt_id"):
        build_runtime_coordinator_output(
            role_inputs, status="succeeded", evidence_refs=[], unknowns=[],
            attempt_id="", bundle_sha256=None,
        )
    with pytest.raises(ValueError, match="bundle_sha256"):
        build_runtime_coordinator_output(
            role_inputs, status="succeeded", evidence_refs=[], unknowns=[],
            attempt_id="attempt-1", bundle_sha256="",
        )


def test_coordinator_output_validation_rejects_contradictory_or_untraceable_state() -> None:
    packet = attach_role_inputs({**PACKET, "agent_contract": _agent_input()}, stage="m1_research")
    coordinator = next(row for row in packet["agent_role_inputs"] if row["role"] == "coordinator")
    output = build_runtime_coordinator_output(
        [coordinator], status="blocked", evidence_refs=[], unknowns=["dependency evidence missing"],
        attempt_id="attempt-blocked", bundle_sha256=None,
    )

    contradictory = copy.deepcopy(output)
    contradictory["decision_effect"] = "coordinate"
    with pytest.raises(ValueError, match="status and decision_effect disagree"):
        validate_output(contradictory)

    untraceable = copy.deepcopy(output)
    untraceable["provenance"].pop("attempt_id")
    with pytest.raises(ValueError, match="provenance.attempt_id"):
        validate_output(untraceable)


def test_schema_and_install_contract_include_agent_role() -> None:
    root = Path(__file__).parents[2]
    schema = json.loads((root / "resources/contracts/agent-role-spec-v1.schema.json").read_text(encoding="utf-8"))
    input_schema = json.loads((root / "resources/contracts/agent-role-input-v1.schema.json").read_text(encoding="utf-8"))
    packet = attach_role_inputs({**PACKET, "agent_contract": _agent_input()}, stage="m1_research")
    assert not list(Draft202012Validator(input_schema).iter_errors(packet["agent_role_inputs"][0]))
    output = build_runtime_coordinator_output(
        packet["agent_role_inputs"], status="succeeded", evidence_refs=["evidence-hash"],
        unknowns=[], attempt_id="attempt-1", bundle_sha256="bundle-1",
    )
    assert not list(Draft202012Validator(schema).iter_errors(output))
    script = (root / "scripts/verify-install.ps1").read_text(encoding="utf-8")
    assert "resources\\contracts\\agent-role-spec-v1.schema.json" in script
    assert "resources\\contracts\\agent-role-input-v1.schema.json" in script
    assert "runtime\\ai_trading_companion\\agent_role.py" in script


def test_frozen_replay_is_deterministic_and_keeps_evaluation_axes_separate() -> None:
    packet = attach_role_inputs({**PACKET, "agent_contract": _agent_input()}, stage="m1_research")
    output = build_runtime_coordinator_output(
        packet["agent_role_inputs"], status="succeeded", evidence_refs=["evidence-hash"],
        unknowns=[], attempt_id="attempt-replay", bundle_sha256="bundle-replay",
    )
    coordinator = next(row for row in packet["agent_role_inputs"] if row["role"] == "coordinator")
    source_input = copy.deepcopy(coordinator)
    source_output = copy.deepcopy(output)
    first = frozen_replay(coordinator, output)
    second = frozen_replay(copy.deepcopy(coordinator), copy.deepcopy(output))
    assert first == second
    assert coordinator == source_input and output == source_output
    assert set(first["evaluation_vector"]) == {
        "delivery_speed", "qualification_probability", "research_quality", "judgment_outcome", "safety_reliability",
    }
    assert install_qualification()["qualified"] is True


def test_coordinator_m2_can_reference_frozen_h0_but_other_roles_cannot() -> None:
    agent = attach_input({**PACKET, "stage": "m2"}, capability="research:m2")["agent_contract"]
    coordinator = build_input(
        agent, role="coordinator", stage="m2",
        input_refs={"public_evidence": ["e"], "role_outputs": ["roles"], "h0_frozen": ["h0:snapshot"]},
        provenance={"as_of": PACKET["as_of"]},
    )
    assert "h0_frozen" in coordinator["input_refs"]
    with pytest.raises(ValueError, match="disallowed inputs"):
        build_input(agent, role="risk", stage="m2", input_refs={"h0_frozen": ["h0:snapshot"]}, provenance={"as_of": PACKET["as_of"]})


def test_runtime_installs_spec95_graph_and_stops_after_a_failed_evidence_gate() -> None:
    packet = attach_role_inputs({**PACKET, "agent_contract": _agent_input()}, stage="m1_research")
    states = spec95_dependency_states(
        {"SPEC-95.1": "closed", "SPEC-95.2": "closed", "SPEC-95.3": "closed"},
        {"SPEC-95.3": False},
    )
    output = build_runtime_coordinator_output(
        packet["agent_role_inputs"], status="succeeded", evidence_refs=[], unknowns=[],
        attempt_id="attempt-graph", bundle_sha256=None,
        dependency_graph=SPEC95_DEPENDENCY_GRAPH, dependency_states=states,
    )
    coordinator_spec = output["provenance"]["coordinator_spec"]
    assert set(f"SPEC-95.{index}" for index in range(1, 7)) <= set(coordinator_spec["dependency_graph"])
    assert output["status"] == "blocked"
    assert output["decision_effect"] == "block"
    assert coordinator_spec["states"]["SPEC-95.6"] == "blocked"


def test_missing_spec95_prerequisite_metadata_blocks_every_node() -> None:
    states = spec95_dependency_states()

    assert all(states[node] == "blocked" for node in SPEC95_DEPENDENCY_GRAPH if node != "coordinator")
    assert states["coordinator"] == "blocked"


def test_role_packet_emits_runtime_qualification_metadata() -> None:
    packet = attach_role_inputs({**PACKET, "agent_contract": _agent_input()}, stage="m1_research")
    issue_states, evidence_gates = spec95_runtime_qualification()

    assert packet["spec_issue_states"] == issue_states
    assert packet["spec_evidence_gates"] == evidence_gates
    assert all(
        state == "succeeded"
        for state in spec95_dependency_states(packet["spec_issue_states"], packet["spec_evidence_gates"]).values()
        if state != "pending"
    )


def test_partial_spec95_metadata_is_not_filled_in() -> None:
    packet = attach_role_inputs(
        {**PACKET, "agent_contract": _agent_input(), "spec_issue_states": {}},
        stage="m1_research",
    )

    assert "spec_evidence_gates" not in packet
    assert spec95_dependency_states(packet["spec_issue_states"], packet.get("spec_evidence_gates"))["SPEC-95.1"] == "blocked"


def test_durable_coordinator_claim_uses_scheduling_idempotency_key() -> None:
    packet = attach_role_inputs({**PACKET, "agent_contract": _agent_input()}, stage="m1_research")
    state_path = Path.cwd() / "_agent_role_coordinator_state.json"
    state_path.unlink(missing_ok=True)
    state_path.with_suffix(state_path.suffix + ".lock").unlink(missing_ok=True)
    store = CoordinatorStateStore(state_path)
    kwargs = {
        "status": "succeeded", "evidence_refs": [], "unknowns": [],
        "bundle_sha256": None, "state_store": store,
        "idempotency_key": "cycle-role-1:m1_research:packet-hash",
    }
    try:
        first = build_runtime_coordinator_output(packet["agent_role_inputs"], attempt_id="attempt-1", **kwargs)
        replay = build_runtime_coordinator_output(packet["agent_role_inputs"], attempt_id="attempt-2", **kwargs)
        assert first["provenance"]["coordinator_spec"]["recovery"]["execution_count"] == 1
        assert replay["provenance"]["coordinator_spec"]["recovery"]["duplicate"] is True
        assert replay["provenance"]["coordinator_spec"]["recovery"]["execution_count"] == 1
    finally:
        state_path.unlink(missing_ok=True)
        state_path.with_suffix(state_path.suffix + ".lock").unlink(missing_ok=True)
