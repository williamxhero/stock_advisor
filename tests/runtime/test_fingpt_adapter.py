from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from ai_trading_companion.adapter_contract import AdapterDefinition
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.fingpt_adapter import (
    CAPABILITIES,
    CONTRACT,
    PROVIDER_OUTPUT_CONTRACT,
    REPLAY_CONTRACT,
    RESULT_CONTRACT,
    build_adapter,
    build_input,
    build_output,
    controlled_degradation,
    execute_deterministic,
    frozen_replay,
    install_qualification,
    sha256,
    validate_input,
    validate_output,
    validate_replay,
)
from ai_trading_companion.memory_port import InMemoryMemoryAdapter
from ai_trading_companion.packet_builder import RuntimePacketBuilder
from ai_trading_companion.store import CompanionStore
from ai_trading_companion.__main__ import _run_m0_fingpt


ROOT = Path(__file__).resolve().parents[2]
AS_OF = "2026-10-06T01:45:00Z"


def document(text: str = "Company reported revenue growth.", *, source: str = "public_evidence", document_id: str = "doc-1", as_of: str = AS_OF) -> dict[str, str]:
    return {
        "document_id": document_id, "text": text, "source": source,
        "source_ref": f"evidence:test:{document_id}", "as_of": as_of,
        "known_at": as_of, "language": "en",
    }


def request(capability: str = "financial_sentiment", **kwargs: object) -> dict[str, object]:
    return build_input([document()], capability=capability, as_of=AS_OF, cycle_id="cycle-1", **kwargs)


def provider_output(input_contract: dict[str, object], *, provider: str = "fingpt") -> dict[str, object]:
    return execute_deterministic(input_contract) | {"provider": provider}


def test_contract_identity_and_capability_allowlist_are_versioned() -> None:
    assert CONTRACT == "FinGPTAdapterSpec/v1"
    assert RESULT_CONTRACT == "FinGPTAdapterResult/v1"
    assert REPLAY_CONTRACT == "FinGPTAdapterReplay/v1"
    assert CAPABILITIES == {
        "financial_sentiment", "headline_classification", "entity_recognition", "relation_extraction",
    }


@pytest.mark.parametrize("capability", sorted(CAPABILITIES))
def test_each_allowed_capability_has_a_bounded_output(capability: str) -> None:
    value = request(capability)
    output = execute_deterministic(value)
    assert output["contract"] == PROVIDER_OUTPUT_CONTRACT
    assert output["capability"] == capability
    assert all(row["evidence_ref"] == "evidence:test:doc-1" for row in output["annotations"])


@pytest.mark.parametrize("capability", ["buy_sell_advice", "investment_advice", "final_judgment", "direct_buy_sell_advice"])
def test_out_of_scope_capabilities_are_rejected(capability: str) -> None:
    with pytest.raises(ValueError, match="allowlist"):
        build_input([document()], capability=capability, as_of=AS_OF)


def test_input_requires_runtime_owned_public_evidence() -> None:
    with pytest.raises(ValueError, match="permitted evidence"):
        build_input([document(source="llm")], capability="financial_sentiment", as_of=AS_OF)
    with pytest.raises(ValueError, match="Runtime-owned"):
        value = request()
        value["provenance"]["source"] = "provider"
        value["sha256"] = sha256({key: item for key, item in value.items() if key != "sha256"})
        validate_input(value)


def test_future_and_conflicting_documents_are_rejected() -> None:
    with pytest.raises(ValueError, match="unavailable"):
        build_input([document(as_of="2026-10-07T01:45:00Z")], capability="financial_sentiment", as_of=AS_OF)
    with pytest.raises(ValueError, match="unique"):
        build_input([document(), document(document_id="doc-1")], capability="financial_sentiment", as_of=AS_OF)


def test_m0_input_cannot_carry_h0_m1_m2_or_private_state() -> None:
    value = request()
    value["documents"][0]["h0"] = "private opinion"
    value["sha256"] = sha256({key: item for key, item in value.items() if key != "sha256"})
    with pytest.raises(ValueError, match="protected field"):
        validate_input(value)
    value = request()
    value["documents"][0]["positions"] = {"000001": 100}
    value["sha256"] = sha256({key: item for key, item in value.items() if key != "sha256"})
    with pytest.raises(ValueError, match="protected field"):
        validate_input(value)


def test_input_permissions_and_quantresearch_are_read_only() -> None:
    value = request()
    value["permissions"] = {"write_permissions": ["memoryhub"]}
    value["sha256"] = sha256({key: item for key, item in value.items() if key != "sha256"})
    with pytest.raises(ValueError, match="read_only"):
        validate_input(value)
    value = request()
    value["quantresearch"] = {"access": "write", "write_permissions": ["strategy"]}
    value["sha256"] = sha256({key: item for key, item in value.items() if key != "sha256"})
    with pytest.raises(ValueError, match="read_only"):
        validate_input(value)


def test_model_data_versions_and_request_provenance_are_auditable() -> None:
    value = request(model_version="FinGPT-2026.1", data_version="news-2026-10-06")
    assert value["model_version"] == "FinGPT-2026.1"
    assert value["data_version"] == "news-2026-10-06"
    assert value["provenance"] == {"source": "runtime", "as_of": AS_OF, "cycle_id": "cycle-1", "request_id": None}


def test_provider_output_must_bind_capability_and_documents() -> None:
    value = request()
    output = execute_deterministic(value)
    output["capability"] = "entity_recognition"
    with pytest.raises(ValueError, match="does not match"):
        build_output(value, output)
    output = execute_deterministic(value)
    output["annotations"][0]["document_id"] = "not-in-input"
    with pytest.raises(ValueError, match="not in the input"):
        build_output(value, output)


def test_provider_output_rejects_final_investment_advice() -> None:
    value = request()
    output = execute_deterministic(value)
    output["annotations"][0]["value"] = {"recommendation": "buy"}
    with pytest.raises(ValueError, match="final investment advice"):
        build_output(value, output)
    output = execute_deterministic(value)
    output["annotations"][0]["value"] = {"summary": "建议卖出"}
    with pytest.raises(ValueError, match="final investment advice"):
        build_output(value, output)


def test_provider_output_rejects_memoryhub_and_position_writes() -> None:
    value = request()
    output = execute_deterministic(value)
    output["annotations"][0]["value"] = {"memoryhub_write": {"body": "record"}}
    with pytest.raises(ValueError, match="protected field"):
        build_output(value, output)
    output = execute_deterministic(value)
    output["annotations"][0]["value"] = {"positions_write": {"ticker": "000001"}}
    with pytest.raises(ValueError, match="protected field"):
        build_output(value, output)


def test_qualified_result_contains_confidence_and_read_only_envelope() -> None:
    value = request("headline_classification", model_version="fingpt/v1", data_version="headlines/v3")
    result = build_output(value, execute_deterministic(value))
    assert result["state"] == "qualified"
    assert result["confidence"]["state"] == "reported"
    assert 0 <= result["confidence"]["score"] <= 1
    assert result["permissions"] == {"write_permissions": []}
    assert result["quantresearch"] == {"access": "read_only", "write_permissions": []}
    assert result["provenance"]["input_sha256"] == value["sha256"]
    assert result["provenance"]["confidence_recomputed"] is False


def test_result_digest_tampering_is_rejected() -> None:
    value = request()
    result = build_output(value, execute_deterministic(value))
    result["annotations"][0]["confidence"]["score"] = 0.01
    with pytest.raises(ValueError, match="digest"):
        validate_output(result)


def test_degraded_path_recomputes_provenance_and_confidence() -> None:
    value = request()
    failed = {"status": "failed", "error_code": "provider_timeout", "provider": "fingpt"}
    fallback = execute_deterministic(value)
    fallback["provider"] = "controlled_llm"
    fallback["confidence"]["score"] = 0.99
    result = controlled_degradation(value, fallback, failed_output=failed)
    assert result["state"] == "degraded"
    assert result["degradation"] == {
        "used": True, "path": "controlled_llm_cognition", "reason": "primary_provider_failed",
    }
    assert result["confidence"]["state"] == "recomputed"
    assert result["confidence"]["method"] == "controlled-degradation-v1"
    assert result["confidence"]["score"] != 0.99
    assert result["provenance"]["source"] == "controlled_llm"
    assert result["provenance"]["confidence_recomputed"] is True
    assert result["provenance"]["source_output_sha256"] == sha256(failed)


def test_degraded_result_still_rejects_advice_and_writes() -> None:
    value = request()
    fallback = execute_deterministic(value)
    fallback["provider"] = "controlled_llm"
    fallback["annotations"][0]["value"] = {"recommendation": "hold"}
    with pytest.raises(ValueError, match="final investment advice"):
        controlled_degradation(value, fallback)


def test_replay_is_frozen_and_validates_source_digests() -> None:
    value = request("relation_extraction")
    result = build_output(value, execute_deterministic(value))
    first = frozen_replay(value, result)
    second = frozen_replay(copy.deepcopy(value), copy.deepcopy(result))
    assert first == second
    assert validate_replay(first) == first
    assert first["source_input_sha256"] == value["sha256"]
    assert first["source_output_sha256"] == result["sha256"]
    tampered = copy.deepcopy(first)
    tampered["source_output_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="provenance"):
        validate_replay(tampered)


def test_json_schema_accepts_input_provider_result_and_replay() -> None:
    value = request("entity_recognition")
    provider = execute_deterministic(value)
    result = build_output(value, provider)
    replay = frozen_replay(value, result)
    schema = json.loads((ROOT / "resources/contracts/fingpt-adapter-spec-v1.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    for item in (value, provider, result, replay):
        assert list(validator.iter_errors(item)) == []


def test_installation_qualification_is_deterministic() -> None:
    assert install_qualification() == install_qualification()
    assert install_qualification()["qualified"] is True


def test_adapter_definition_declares_capabilities_and_read_only_state() -> None:
    definition = build_adapter()
    declaration = definition.declaration()
    assert declaration["input_contract"] == CONTRACT
    assert declaration["output_contract"] == PROVIDER_OUTPUT_CONTRACT
    assert set(declaration["capabilities"]) == CAPABILITIES
    assert declaration["permissions"]["write_permissions"] == []
    assert declaration["permissions"]["state_permissions"] == ["read:evidence"]


def test_engine_process_isolated_adapter_returns_bounded_provider_output(tmp_path: Path) -> None:
    engine = CompanionEngine(CompanionStore(tmp_path / "companion.sqlite3"))
    engine.register_adapter(build_adapter())
    value = request()
    execution = engine.execute_adapter(
        "deterministic-fingpt-nlp-v1", value, as_of=AS_OF,
        cycle_id="cycle-1", request_id="request-1",
    )
    assert execution["status"] == "succeeded"
    assert execution["data"]["capability"] == "financial_sentiment"
    assert execution["permissions"] == {"write_permissions": []}
    assert execution["provenance"]["cycle_id"] == "cycle-1"
    assert execution["provenance"]["request_id"] == "request-1"


def test_engine_fingpt_adapter_cannot_be_registered_with_write_state() -> None:
    with pytest.raises(ValueError, match="read-only"):
        AdapterDefinition(
            "unsafe-fingpt", "v1", CONTRACT, PROVIDER_OUTPUT_CONTRACT, "probabilistic",
            execute_deterministic, lambda value: None, lambda value: None,
            lambda value: {"passed": True}, state_permissions=("write:memoryhub",),
        )


def _runtime_cycle(tmp_path: Path) -> tuple[CompanionStore, CompanionEngine, dict[str, object]]:
    store = CompanionStore(tmp_path / "runtime.sqlite3")
    engine = CompanionEngine(store, memory=InMemoryMemoryAdapter())
    cycle = engine.start_cycle("daily.execution.0945", AS_OF, AS_OF)
    engine.register_adapter(build_adapter())
    engine.register_adapter(build_adapter(adapter_id="controlled-llm-fingpt-nlp-v1", fallback=True))
    return store, engine, cycle


def test_m0_runtime_path_persists_only_source_bound_nlp_receipt(tmp_path: Path) -> None:
    store, engine, cycle = _runtime_cycle(tmp_path)
    bindings = _run_m0_fingpt(
        engine, store, cycle,
        [{"capability": "financial_sentiment", "documents": [document()], "request_id": "r-1"}],
    )
    assert len(bindings) == 1
    receipt = bindings[0]["result"]
    assert receipt["state"] == "qualified"
    artifact = store.latest_artifact(cycle["cycle_id"], "fingpt_nlp")
    assert artifact is not None
    metadata = json.loads(artifact["metadata_json"])
    assert metadata["write_permissions"] == []
    assert metadata["input_sha256"] == bindings[0]["input"]["sha256"]


def test_m0_runtime_path_uses_controlled_degradation_with_recomputed_confidence(tmp_path: Path) -> None:
    store, engine, cycle = _runtime_cycle(tmp_path)
    bindings = _run_m0_fingpt(
        engine, store, cycle,
        [{"capability": "entity_recognition", "documents": [document()], "request_id": "r-2"}],
        adapter_id="missing-fingpt-provider",
    )
    assert len(bindings) == 1
    result = bindings[0]["result"]
    assert result["state"] == "degraded"
    assert result["provenance"]["source"] == "controlled_llm"
    assert result["confidence"]["state"] == "recomputed"
    assert result["degradation"]["path"] == "controlled_llm_cognition"


def test_packet_builder_keeps_fingpt_visible_only_to_m0(tmp_path: Path) -> None:
    store, engine, cycle = _runtime_cycle(tmp_path)
    bindings = _run_m0_fingpt(
        engine, store, cycle,
        [{"capability": "relation_extraction", "documents": [document()], "request_id": "r-3"}],
    )
    builder = RuntimePacketBuilder(ROOT / "resources", store, memory=InMemoryMemoryAdapter())
    packet = builder.build(cycle, "m0_compose", fingpt_bindings=bindings)
    assert packet["fingpt_adapters"][0]["result"]["input"]["sha256"] == bindings[0]["input"]["sha256"]
    with pytest.raises(ValueError, match="only to M0"):
        builder.build(cycle, "m1_judgment", fingpt_bindings=bindings)


def test_packet_builder_rejects_result_bound_to_different_input(tmp_path: Path) -> None:
    store, engine, cycle = _runtime_cycle(tmp_path)
    first = request()
    second = request("headline_classification")
    result = build_output(second, execute_deterministic(second))
    builder = RuntimePacketBuilder(ROOT / "resources", store, memory=InMemoryMemoryAdapter())
    with pytest.raises(ValueError, match="does not match"):
        builder.build(cycle, "m0_compose", fingpt_input=first, fingpt_result=result)
