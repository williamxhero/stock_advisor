from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from ai_trading_companion.adapter_contract import AdapterDefinition
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.memory_port import InMemoryMemoryAdapter
from ai_trading_companion.multimodal_adapter import (
    CHART_GENERATION_VERSION,
    CONTRACT,
    RESULT_CONTRACT,
    build_chart_reference,
    build_input,
    build_output,
    execute_deterministic_chart_interpretation,
    frozen_replay,
    install_qualification,
    qualify_multimodal_adapter_output,
    sha256,
    validate_input,
    validate_vision_output,
    validate_multimodal_adapter_input,
    validate_multimodal_adapter_output,
)
from ai_trading_companion.packet_builder import RuntimePacketBuilder
from ai_trading_companion.store import CompanionStore
from ai_trading_companion.__main__ import (
    _persist_multimodal_execution,
    _run_m0_multimodal,
    _structured_market_data_from_evidence,
)


ROOT = Path(__file__).resolve().parents[2]
AS_OF = "2026-10-05T01:45:00Z"


def market_data(source: str = "markethub") -> dict[str, object]:
    return {
        "source": source,
        "source_ref": f"{source}:quotes:000001",
        "as_of": AS_OF,
        "instrument": "000001",
        "interval": "1d",
        "ohlcv": [
            {"timestamp": "2026-10-03T01:45:00Z", "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 1000, "source": source},
            {"timestamp": AS_OF, "open": 10.5, "high": 12, "low": 10, "close": 11.5, "volume": 1400, "source": source},
        ],
        "quotes": [{"name": "last_price", "value": 11.5, "source": source}],
        "indicators": [{"name": "sma_2", "value": 11.0, "source": "deterministic_computation"}],
    }


def interpretation() -> dict[str, object]:
    return {
        "output_kind": "interpretation",
        "interpretation": {
            "summary": "The rendered candles show a rising two-period structure.",
            "basis": "rendered_deterministic_chart",
        },
        "facts": [],
    }


def vision_adapter(data: dict[str, object]) -> dict[str, object]:
    return interpretation()


def build_vision_adapter() -> AdapterDefinition:
    return AdapterDefinition(
        "vision-fixture", "v1", CONTRACT, RESULT_CONTRACT, "probabilistic",
        vision_adapter, validate_multimodal_adapter_input,
        validate_multimodal_adapter_output, qualify_multimodal_adapter_output,
    )


def test_structured_input_chart_and_replay_are_deterministic() -> None:
    first = build_input(market_data(), cycle_id="cycle-1", request_id="request-1")
    second = build_input(copy.deepcopy(market_data()), cycle_id="cycle-1", request_id="request-1")
    assert first == second
    assert first["chart"]["generation_version"] == CHART_GENERATION_VERSION
    assert first["image_reference"] == first["chart"]["image_reference"]
    replay = frozen_replay(first, interpretation())
    assert replay == frozen_replay(copy.deepcopy(first), copy.deepcopy(interpretation()))
    assert replay["qualification"] == {
        "valid": True, "interpretation_only": True, "read_only": True,
        "chart_bound": True, "fact_sources_enforced": True,
    }


def test_schema_accepts_input_result_and_replay() -> None:
    value = build_input(market_data())
    result = build_output(value, interpretation())
    replay = frozen_replay(value, interpretation())
    schema = json.loads((ROOT / "resources/contracts/finagent-multimodal-adapter-spec-v1.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    assert list(validator.iter_errors(value)) == []
    assert list(validator.iter_errors(result)) == []
    assert list(validator.iter_errors(replay)) == []


@pytest.mark.parametrize("field", ["chart_sha256", "market_data_sha256", "render_parameters_sha256", "generation_version"])
def test_conflicting_image_provenance_is_rejected(field: str) -> None:
    value = build_input(market_data())
    value["image_reference"][field] = "0" * 64 if field.endswith("sha256") else "other-chart-v1"
    with pytest.raises(ValueError, match="image reference"):
        validate_input(value)


def test_image_reference_uri_and_media_type_are_chart_bound() -> None:
    value = build_input(market_data())
    value["chart"]["image_reference"]["uri"] = "https://unbound.example/chart.svg"
    value["image_reference"]["uri"] = value["chart"]["image_reference"]["uri"]
    with pytest.raises(ValueError, match="URI"):
        validate_input(value)
    value = build_input(market_data())
    value["chart"]["image_reference"]["media_type"] = "image/png"
    value["image_reference"]["media_type"] = value["chart"]["image_reference"]["media_type"]
    with pytest.raises(ValueError, match="deterministic chart"):
        validate_input(value)


def test_unbound_image_and_conflicting_chart_digests_are_rejected() -> None:
    value = build_input(market_data())
    image = copy.deepcopy(value["image_reference"])
    image["chart_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="image reference"):
        build_input(market_data(), image_reference=image)
    tampered = copy.deepcopy(value)
    tampered["chart"]["ohlcv_sha256"] = "0" * 64
    tampered["chart"]["chart_sha256"] = sha256({key: tampered["chart"][key] for key in (
        "contract", "version", "generation_version", "market_data_sha256", "ohlcv_sha256",
        "render_parameters", "render_parameters_sha256", "image_content_sha256",
    )})
    tampered["chart"]["image_reference"]["chart_sha256"] = tampered["chart"]["chart_sha256"]
    tampered["image_reference"]["chart_sha256"] = tampered["chart"]["chart_sha256"]
    tampered["sha256"] = sha256({key: item for key, item in tampered.items() if key != "sha256"})
    with pytest.raises(ValueError, match="OHLCV|chart"):
        validate_input(tampered)


def test_market_data_source_and_malformed_input_are_rejected() -> None:
    with pytest.raises(ValueError, match="MarketHub"):
        build_input(market_data("screenshot"))
    malformed = market_data()
    malformed["ohlcv"][0]["high"] = 8
    with pytest.raises(ValueError, match="high/low"):
        build_input(malformed)
    malformed = build_input(market_data())
    del malformed["chart"]
    with pytest.raises(ValueError, match="fields"):
        validate_input(malformed)


def test_interpretation_is_required_and_image_facts_are_rejected() -> None:
    with pytest.raises(ValueError, match="explicitly marked"):
        validate_vision_output({"interpretation": {"summary": "chart"}, "facts": []})
    for fact in (
        {"kind": "image_price", "value": 11, "source": "markethub"},
        {"kind": "vision_volume", "value": 100, "source": "markethub"},
        {"kind": "pixel_indicator", "value": 1, "source": "deterministic_computation"},
        {"kind": "price", "value": 11, "source": "image"},
    ):
        with pytest.raises(ValueError, match="(image-derived|permitted source)"):
            validate_vision_output({"output_kind": "interpretation", "interpretation": {"summary": "chart"}, "facts": [fact]})


def test_interpretation_bounds_and_temporal_binding_are_enforced() -> None:
    with pytest.raises(ValueError, match="interpretation"):
        validate_vision_output({
            "output_kind": "interpretation",
            "interpretation": {"summary": "x" * 2_001},
            "facts": [],
        })
    with pytest.raises(ValueError, match="cutoff"):
        build_input(market_data(), as_of="2026-10-05T02:00:00Z")


def test_protected_state_and_write_permissions_are_rejected() -> None:
    value = build_input(market_data())
    value["permissions"] = {"write_permissions": ["portfolio"]}
    value["sha256"] = sha256({key: item for key, item in value.items() if key != "sha256"})
    with pytest.raises(ValueError, match="read-only"):
        validate_input(value)
    with pytest.raises(ValueError, match="protected field"):
        validate_vision_output({
            "output_kind": "interpretation", "interpretation": {"summary": "chart"},
            "facts": [], "portfolio": {"position": "unchanged"},
        })


def test_deterministic_computation_indicator_and_positive_market_facts_are_allowed() -> None:
    data = market_data()
    data["source"] = "deterministic_computation"
    data["source_ref"] = "deterministic_computation:sma"
    for row in data["ohlcv"] + data["quotes"]:
        row["source"] = "deterministic_computation"
    value = build_input(data)
    result = build_output(value, {
        "output_kind": "interpretation", "interpretation": {"summary": "The chart is bound."},
        "facts": [{"kind": "observed_structure", "source": "deterministic_computation"}],
    })
    assert result["state"] == "qualified"


def test_engine_multimodal_seam_qualifies_rejects_and_replays(tmp_path: Path) -> None:
    engine = CompanionEngine(CompanionStore(tmp_path / "companion.sqlite3"))
    engine.register_adapter(build_vision_adapter())
    qualified = engine.execute_multimodal_adapter(
        "vision-fixture", market_data(), cycle_id="cycle-1", request_id="request-1",
    )
    assert qualified["state"] == "qualified"
    assert qualified["receipt"]["output_kind"] == "interpretation"
    assert qualified["input"]["permissions"] == {"write_permissions": []}

    def bad_adapter(data: dict[str, object]) -> dict[str, object]:
        return {"output_kind": "claim", "interpretation": {"summary": "bad"}, "facts": []}

    engine.register_adapter(AdapterDefinition(
        "bad-vision", "v1", CONTRACT, RESULT_CONTRACT, "probabilistic", bad_adapter,
        validate_multimodal_adapter_input, validate_multimodal_adapter_output,
        qualify_multimodal_adapter_output,
    ))
    rejected = engine.execute_multimodal_adapter("bad-vision", market_data(), cycle_id="cycle-1")
    assert rejected["state"] in {"rejected", "failed"}
    assert rejected["error_code"]


def test_installation_qualification_and_expected_output_digest() -> None:
    assert install_qualification() == install_qualification()
    value = build_input(market_data())
    with pytest.raises(ValueError, match="output digest"):
        frozen_replay(value, interpretation(), expected_output_sha256="0" * 64)


def _runtime_cycle(tmp_path: Path) -> tuple[CompanionStore, CompanionEngine, dict[str, object]]:
    store = CompanionStore(tmp_path / "companion.sqlite3")
    engine = CompanionEngine(store, memory=InMemoryMemoryAdapter())
    engine.register_adapter(AdapterDefinition(
        "deterministic-chart-vision-v1", "v1", CONTRACT, RESULT_CONTRACT, "deterministic",
        execute_deterministic_chart_interpretation, validate_multimodal_adapter_input,
        validate_multimodal_adapter_output, qualify_multimodal_adapter_output,
    ))
    cycle = engine.start_cycle(
        "daily.execution.0945", "2026-10-05T09:45:00+08:00", AS_OF,
    )
    return store, engine, cycle


def test_packet_builder_binds_multimodal_input_and_result_to_stage_and_as_of(tmp_path: Path) -> None:
    store, _engine, cycle = _runtime_cycle(tmp_path)
    builder = RuntimePacketBuilder(ROOT / "resources", store, memory=InMemoryMemoryAdapter())
    input_contract = build_input(
        market_data(), stage="m0_compose", as_of=AS_OF, cycle_id=cycle["cycle_id"],
    )
    result = build_output(input_contract, interpretation())
    packet = builder.build(
        cycle, "m0_compose", evidence={},
        multimodal_input=input_contract, multimodal_result=result,
    )
    assert packet["multimodal_adapter"]["sha256"] == input_contract["sha256"]
    assert packet["multimodal_adapter_result"]["input"]["sha256"] == input_contract["sha256"]
    assert packet["sha256"]

    wrong_stage = copy.deepcopy(input_contract)
    wrong_stage["stage"] = "m1_judgment"
    wrong_stage["sha256"] = sha256({key: item for key, item in wrong_stage.items() if key != "sha256"})
    with pytest.raises(ValueError, match="packet identity"):
        builder.build(cycle, "m0_compose", evidence={}, multimodal_input=wrong_stage)

    wrong_as_of = copy.deepcopy(input_contract)
    wrong_as_of["provenance"]["as_of"] = "2026-10-05T02:00:00Z"
    wrong_as_of["sha256"] = sha256({key: item for key, item in wrong_as_of.items() if key != "sha256"})
    with pytest.raises(ValueError, match="packet identity|cutoff"):
        builder.build(cycle, "m0_compose", evidence={}, multimodal_input=wrong_as_of)

    different_input = build_input(
        market_data(), stage="m0_compose", as_of=AS_OF,
        cycle_id=cycle["cycle_id"], request_id="not-present",
    )
    different_result = build_output(different_input, interpretation())
    with pytest.raises(ValueError, match="result does not match"):
        builder.build(
            cycle, "m0_compose", evidence={},
            multimodal_input=input_contract, multimodal_result=different_result,
        )


def test_structured_markethub_excerpt_is_required_for_automatic_chart_binding() -> None:
    data = market_data()
    source = {
        "evidence_ref": data["source_ref"],
        "excerpt": json.dumps({
            "contract": "MarketHubStructuredMarketData/v1", "market_data": data,
        }, ensure_ascii=False),
    }
    assert _structured_market_data_from_evidence({"sources": [source]}) == data
    assert _structured_market_data_from_evidence({
        "sources": [{"evidence_ref": data["source_ref"], "excerpt": "close=11.5"}],
    }) is None
    assert _structured_market_data_from_evidence({
        "sources": [{
            "evidence_ref": "different-ref",
            "excerpt": json.dumps({
                "contract": "MarketHubStructuredMarketData/v1", "market_data": data,
            }),
        }],
    }) is None


def test_optional_worker_execution_persists_and_reuses_immutable_receipt(tmp_path: Path) -> None:
    store, engine, cycle = _runtime_cycle(tmp_path)
    binding = _run_m0_multimodal(
        engine, store, cycle, {"sources": []}, market_data=market_data(),
    )
    assert binding is not None
    first_input, first_receipt = binding
    assert first_receipt["output_kind"] == "interpretation"
    artifacts = store.artifacts(cycle["cycle_id"])
    assert len([item for item in artifacts if item["kind"] == "multimodal"]) == 1

    repeated = _run_m0_multimodal(
        engine, store, cycle, {"sources": []}, market_data=market_data(),
    )
    assert repeated == (first_input, first_receipt)
    assert len([item for item in store.artifacts(cycle["cycle_id"]) if item["kind"] == "multimodal"]) == 1


def test_optional_worker_failure_falls_back_and_records_event(tmp_path: Path) -> None:
    store, engine, cycle = _runtime_cycle(tmp_path)
    binding = _run_m0_multimodal(
        engine, store, cycle, {"sources": []},
        adapter_id="missing-multimodal-adapter", market_data=market_data(),
    )
    assert binding is None
    events = store.client_events(0)
    assert any(event["event_type"] == "multimodal.adapter_failed" for event in events)
    artifact = store.latest_artifact(cycle["cycle_id"], "multimodal")
    assert artifact is not None
    assert json.loads(artifact["metadata_json"])["state"] == "failed"
