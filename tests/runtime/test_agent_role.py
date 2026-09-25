from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from ai_trading_companion.agent_contract import attach_input
from ai_trading_companion.agent_role import (
    CONTRACT,
    ROLE_IDS,
    CoordinatorStateStore,
    Spec95BaselineStore,
    SPEC95_DEPENDENCY_GRAPH,
    SPEC95_NODE_IDS,
    attach_role_inputs,
    build_input,
    build_output,
    build_runtime_coordinator_output,
    frozen_replay,
    install_qualification,
    role_definition,
    attach_runtime_qualification,
    validate_spec95_baseline,
    spec95_runtime_qualification,
    spec95_dependency_states,
    validate_output,
)
from ai_trading_companion.packet_builder import RuntimePacketBuilder
from ai_trading_companion.__main__ import _call_stage, finalize_stage_packet
from ai_trading_companion.runtime_strategy_policy import RuntimeStrategyControls
from ai_trading_companion.broker_client import BrokerError
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.evidence_gate import EvidenceInsufficient
from ai_trading_companion.store import CompanionStore
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
    coordinator_schema = json.loads((root / "resources/contracts/coordinator-spec-v1.schema.json").read_text(encoding="utf-8"))
    input_schema = json.loads((root / "resources/contracts/agent-role-input-v1.schema.json").read_text(encoding="utf-8"))
    packet = attach_role_inputs({**PACKET, "agent_contract": _agent_input()}, stage="m1_research")
    assert not list(Draft202012Validator(input_schema).iter_errors(packet["agent_role_inputs"][0]))
    output = build_runtime_coordinator_output(
        packet["agent_role_inputs"], status="succeeded", evidence_refs=["evidence-hash"],
        unknowns=[], attempt_id="attempt-1", bundle_sha256="bundle-1",
    )
    assert not list(Draft202012Validator(schema).iter_errors(output))
    coordinator_spec = output["provenance"]["coordinator_spec"]
    assert not list(Draft202012Validator(coordinator_schema).iter_errors(coordinator_spec))
    assert set(coordinator_spec["lifecycles"]) == set(coordinator_spec["dependency_graph"])
    invalid_output = copy.deepcopy(output)
    invalid_output["provenance"]["coordinator_spec"]["lifecycles"].pop("coordinator")
    with pytest.raises(ValueError, match="lifecycles must cover every dependency node"):
        validate_output(invalid_output)
    script = (root / "scripts/verify-install.ps1").read_text(encoding="utf-8")
    assert "resources\\contracts\\agent-role-spec-v1.schema.json" in script
    assert "resources\\contracts\\agent-role-input-v1.schema.json" in script
    assert "resources\\contracts\\coordinator-spec-v1.schema.json" in script
    assert "resources\\contracts\\companion-m1-result-v5.schema.json" in script
    assert "resources\\contracts\\narrative-review-m1-v1.schema.json" in script
    assert "runtime\\ai_trading_companion\\agent_role.py" in script
    assert "runtime\\ai_trading_companion\\judgment_publication.py" in script


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


def test_role_packet_does_not_infer_delivery_issue_qualification() -> None:
    packet = attach_role_inputs({**PACKET, "agent_contract": _agent_input()}, stage="m1_research")
    assert "spec_issue_states" not in packet
    assert "spec_evidence_gates" not in packet
    assert [row["role"] for row in packet["agent_role_inputs"]][-1] == "coordinator"


def test_missing_spec95_receipt_cannot_authorize_downstream_work() -> None:
    issue_states, evidence_gates = spec95_runtime_qualification()

    assert set(issue_states) == set(f"SPEC-95.{index}" for index in range(1, 7))
    assert set(evidence_gates) == set(issue_states)
    assert all(state == "blocked" for state in issue_states.values())
    assert all(gate is False for gate in evidence_gates.values())
    assert spec95_dependency_states(issue_states, evidence_gates)["coordinator"] == "blocked"


def test_coordinator_state_store_persists_every_spec95_lifecycle(tmp_path: Path) -> None:
    state_store = CoordinatorStateStore(tmp_path / "coordinator.json")
    states = {
        node: "blocked" for node in SPEC95_NODE_IDS
    }
    states["coordinator"] = "blocked"

    lifecycles = state_store.snapshot_graph(SPEC95_DEPENDENCY_GRAPH, states, now=10)

    assert set(lifecycles) == set(SPEC95_DEPENDENCY_GRAPH)
    persisted = json.loads((tmp_path / "coordinator.json").read_text(encoding="utf-8"))
    assert set(persisted) == set(SPEC95_NODE_IDS) | {"coordinator"}
    assert all(persisted[node]["status"] == "blocked" for node in SPEC95_NODE_IDS)


def _qualified_spec95_baseline() -> dict:
    nodes = [f"SPEC-95.{index}" for index in range(1, 7)]
    return {
        "issue": {"number": 95, "state": "closed", "declared_specs": nodes},
        "declared_specs": nodes,
        "synchronization_state": "synchronized",
        "spec_issue_states": {node: "closed" for node in nodes},
        "dependency_evidence": {node: {"passed": True} for node in nodes},
        "delivery_evidence": {node: {"verified": True} for node in nodes},
    }


def test_snapshot_graph_preserves_active_running_claim(tmp_path: Path) -> None:
    state_store = CoordinatorStateStore(tmp_path / "coordinator.json")
    claimed = state_store.claim("coordinator", "active-work", lease_seconds=60, now=10)

    state_store.snapshot_graph(
        {"coordinator": []}, {"coordinator": "pending"}, now=11,
    )

    current = state_store.get("coordinator")
    assert current["status"] == "running"
    assert current["execution_generation"] == claimed["execution_generation"]
    assert current["lease_until"] == claimed["lease_until"]


def test_terminal_coordinator_claim_survives_pending_snapshot_and_replays(tmp_path: Path) -> None:
    state_store = CoordinatorStateStore(tmp_path / "coordinator.json")
    first = state_store.claim("coordinator", "same-work", lease_seconds=60, now=10)
    state_store.finish(
        "coordinator", "succeeded", idempotency_key="same-work",
        execution_generation=first["execution_generation"], now=11,
    )

    state_store.snapshot_graph(
        {"coordinator": []}, {"coordinator": "pending"}, now=12,
    )
    replay = state_store.claim("coordinator", "same-work", lease_seconds=60, now=13)

    assert replay["duplicate"] is True
    assert replay["execution_count"] == 1
    assert state_store.get("coordinator")["status"] == "succeeded"


def test_coordinator_gated_packet_hash_tracks_current_baseline() -> None:
    controls = RuntimeStrategyControls(60, 0, (), ())
    packet = {
        "stage": "m0_compose", "cycle_id": "cycle", "as_of": "2026-09-21T00:00:00Z",
        "spec_issue_states": {},
    }
    with patch(
        "ai_trading_companion.__main__.load_authoritative_spec95_baseline",
        side_effect=[{"baseline": "old"}, None],
    ):
        first = finalize_stage_packet(packet, controls)
        second = finalize_stage_packet(packet, controls)

    assert first["spec95_baseline_sha256"]
    assert second["spec95_baseline_sha256"] is None
    assert first["sha256"] != second["sha256"]


def test_spec95_baseline_requires_real_issue_and_all_evidence_gates() -> None:
    baseline = _qualified_spec95_baseline()
    assert validate_spec95_baseline(baseline) is not None

    mutations = [
        lambda value: value.pop("issue"),
        lambda value: value["issue"].update(number=94),
        lambda value: value["issue"].update(state="open"),
        lambda value: value["issue"].pop("declared_specs"),
        lambda value: value.update(declared_specs=["SPEC-95.1"]),
        lambda value: value.update(synchronization_state="out_of_sync"),
        lambda value: value.pop("dependency_evidence"),
        lambda value: value["delivery_evidence"]["SPEC-95.6"].update(verified=False),
        lambda value: value["spec_issue_states"].update({"SPEC-95.5": "open"}),
    ]
    for mutate in mutations:
        invalid = copy.deepcopy(baseline)
        mutate(invalid)
        assert validate_spec95_baseline(invalid) is None
        states, gates = spec95_runtime_qualification(invalid)
        assert all(state == "blocked" for state in states.values())
        assert all(gate is False for gate in gates.values())


def test_unverified_or_contradictory_baseline_cannot_fill_packet_gates() -> None:
    forged = {
        **PACKET,
        "spec_issue_states": {f"SPEC-95.{index}": "closed" for index in range(1, 7)},
        "spec_evidence_gates": {f"SPEC-95.{index}": True for index in range(1, 7)},
    }
    qualified = attach_runtime_qualification(forged)
    assert qualified["spec95_baseline_verified"] is False
    assert set(qualified["spec_issue_states"].values()) == {"blocked"}
    assert set(qualified["spec_evidence_gates"].values()) == {False}

    conflicting = _qualified_spec95_baseline()
    conflicting["delivery_evidence"]["SPEC-95.3"] = False
    assert validate_spec95_baseline(conflicting) is None


def test_structurally_valid_caller_baseline_does_not_authorize_runtime_gates() -> None:
    packet = attach_runtime_qualification({
        **PACKET,
        "spec95_baseline": _qualified_spec95_baseline(),
    })

    assert packet["spec95_baseline_verified"] is False
    assert set(packet["spec_issue_states"].values()) == {"blocked"}
    assert set(packet["spec_evidence_gates"].values()) == {False}


def test_stale_coordinator_generation_cannot_finish_after_takeover() -> None:
    state_path = Path.cwd() / "_agent_role_generation_state.json"
    state_path.unlink(missing_ok=True)
    state_path.with_suffix(state_path.suffix + ".lock").unlink(missing_ok=True)
    state_store = CoordinatorStateStore(state_path)
    try:
        first = state_store.claim("coordinator", "same-work", lease_seconds=5, now=10)
        second = state_store.claim("coordinator", "same-work", lease_seconds=5, now=16)
        assert second["takeover"] is True
        assert second["execution_generation"] == first["execution_generation"] + 1

        with pytest.raises(ValueError, match="execution generation"):
            state_store.finish(
                "coordinator", "succeeded", idempotency_key="same-work",
                execution_generation=first["execution_generation"], now=17,
            )
        result = state_store.finish(
            "coordinator", "succeeded", idempotency_key="same-work",
            execution_generation=second["execution_generation"], now=17,
        )
        assert result["status"] == "succeeded"
    finally:
        state_path.unlink(missing_ok=True)
        state_path.with_suffix(state_path.suffix + ".lock").unlink(missing_ok=True)


def test_runtime_packet_builder_does_not_embed_delivery_issue_status() -> None:
    class PacketBuilder(RuntimePacketBuilder):
        def _calendar_context(self, _scheduled_for: str) -> dict:
            return {"authority": "test"}

        def _memory_cards(self, *_args, **_kwargs) -> list[dict]:
            return []

        def _public_scope(self, *_args, **_kwargs) -> dict:
            return {"mode": "test"}

        def _evidence_snapshot_descriptor(self, *_args, **_kwargs) -> dict:
            return {"snapshot_id": "test-snapshot"}

    cycle = {
        "cycle_id": "cycle-packet-qualification",
        "task_key": "daily.opportunity.0900",
        "scheduled_for": "2026-09-21T09:00:00+08:00",
        "as_of": "2026-09-21T01:00:00Z",
        "evidence_contract_json": json.dumps({"requirements": []}),
        "spec95_baseline": _qualified_spec95_baseline(),
    }
    builder = PacketBuilder(Path.cwd(), object())

    for stage in ("m0_research", "m1_research"):
        packet = builder.build(cycle, stage, evidence={"sources": []})
        assert "spec95_baseline" not in packet
        assert "spec_issue_states" not in packet
        assert "spec_evidence_gates" not in packet


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
        assert first["provenance"]["coordinator_recovery"]["execution_count"] == 1
        assert replay["provenance"]["coordinator_recovery"]["duplicate"] is True
        assert replay["provenance"]["coordinator_recovery"]["execution_count"] == 1
    finally:
        state_path.unlink(missing_ok=True)
        state_path.with_suffix(state_path.suffix + ".lock").unlink(missing_ok=True)


def test_formal_coordinator_gate_persists_blocked_attempt_without_provider(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "companion.sqlite3")
    cycle = CompanionEngine(store).start_cycle(
        "daily.review.1520", "2026-09-21T15:20:00+08:00", "2026-09-21T07:20:00Z",
    )
    packet = {"task_key": cycle["task_key"], "stage": "m0_compose", "as_of": cycle["as_of"]}
    broker = Mock()
    paths = SimpleNamespace(home=tmp_path, runtime=tmp_path, tools=tmp_path)
    settings = SimpleNamespace(research={}, broker={"url": "http://broker.test:8817"})
    with patch("ai_trading_companion.__main__.PATHS", paths), patch(
        "ai_trading_companion.__main__.load_settings", return_value=settings,
    ), patch("ai_trading_companion.__main__.ProviderBrokerClient", return_value=broker):
        with pytest.raises(EvidenceInsufficient, match="coordinator_qualification_missing_or_inconsistent"):
            _call_stage(
                store, cycle, "m0_compose", packet, "companion-m0-result-v3.schema.json",
                search=False, timeout=60, coordinator_gate=True,
            )
    broker.invoke.assert_not_called()
    attempts = store.attempts(cycle["cycle_id"])
    assert len(attempts) == 1 and attempts[0]["status"] == "failed"
    verifier = json.loads(attempts[0]["verifier_json"])
    assert verifier["coordinator_frontier_stopped"]
    assert set(verifier["coordinator_lifecycles"]) == set(SPEC95_DEPENDENCY_GRAPH)
    assert set((tmp_path / "coordinator-state").glob("*.json"))


def test_business_stage_does_not_require_delivery_issue_baseline(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "companion.sqlite3")
    cycle = CompanionEngine(store).start_cycle(
        "daily.review.1520", "2026-09-21T15:20:00+08:00", "2026-09-21T07:20:00Z",
    )
    packet = {"task_key": cycle["task_key"], "stage": "m0_compose", "as_of": cycle["as_of"]}
    broker = Mock()
    broker.invoke.side_effect = BrokerError("provider reached", category="broker_timeout")
    paths = SimpleNamespace(home=tmp_path, runtime=tmp_path, tools=tmp_path)
    settings = SimpleNamespace(research={}, broker={"url": "http://broker.test:8817"})
    with patch("ai_trading_companion.__main__.PATHS", paths), patch(
        "ai_trading_companion.__main__.load_settings", return_value=settings,
    ), patch("ai_trading_companion.__main__.ProviderBrokerClient", return_value=broker):
        with pytest.raises(BrokerError, match="provider reached"):
            _call_stage(
                store, cycle, "m0_compose", packet, "companion-m0-result-v3.schema.json",
                search=False, timeout=60,
            )
    broker.invoke.assert_called_once()


def test_authoritative_baseline_qualifies_downstream_packet_without_projection(tmp_path: Path) -> None:
    Spec95BaselineStore(tmp_path).persist(_qualified_spec95_baseline())
    store = CompanionStore(tmp_path / "companion.sqlite3")
    cycle = CompanionEngine(store).start_cycle(
        "daily.review.1520", "2026-09-21T15:20:00+08:00", "2026-09-21T07:20:00Z",
    )
    packet = {"task_key": cycle["task_key"], "stage": "m0_compose", "as_of": cycle["as_of"]}
    broker = Mock()
    broker.invoke.side_effect = BrokerError("provider reached", category="broker_timeout")
    paths = SimpleNamespace(home=tmp_path, runtime=tmp_path, tools=tmp_path)
    settings = SimpleNamespace(research={}, broker={"url": "http://broker.test:8817"})
    with patch("ai_trading_companion.__main__.PATHS", paths), patch(
        "ai_trading_companion.__main__.load_settings", return_value=settings,
    ), patch("ai_trading_companion.__main__.ProviderBrokerClient", return_value=broker):
        with pytest.raises(BrokerError, match="provider reached"):
            _call_stage(
                store, cycle, "m0_compose", packet, "companion-m0-result-v3.schema.json",
                search=False, timeout=60, coordinator_gate=True,
            )
    broker.invoke.assert_called_once()
    assert store.attempts(cycle["cycle_id"])[0]["status"] == "timed_out"
    state_files = [path for path in (tmp_path / "coordinator-state").glob("*.json") if path.name != "spec95-baseline.json"]
    assert len(state_files) == 1
    state = json.loads(state_files[0].read_text(encoding="utf-8"))
    assert state["coordinator"]["status"] == "blocked"
    assert state["coordinator"]["lease_until"] is None
