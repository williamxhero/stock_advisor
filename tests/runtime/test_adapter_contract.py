from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from ai_trading_companion.adapter_contract import AdapterDefinition, AdapterRegistry, frozen_replay, install_qualification, validate_output
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.store import CompanionStore


def execute_increment(data: dict[str, object]) -> dict[str, object]:
    return {"value": int(data["value"]) + 1}


def execute_not_computable(_: dict[str, object]) -> dict[str, object]:
    return {"state": "NOT_COMPUTABLE", "value": None}


def qualify_not_computable(data: dict[str, object]) -> dict[str, object]:
    return {"passed": data == {"state": "NOT_COMPUTABLE", "value": None}}


def validate_not_computable(data: dict[str, object]) -> None:
    if data != {"state": "NOT_COMPUTABLE", "value": None}:
        raise ValueError("invalid incomputability result")


def execute_sleep(data: dict[str, object]) -> dict[str, object]:
    import time
    time.sleep(float(data.get("seconds", 1)))
    return {"value": 2}


def execute_fail_timeout(_: dict[str, object]) -> dict[str, object]:
    raise TimeoutError("timeout")


def execute_fail_crash(_: dict[str, object]) -> dict[str, object]:
    raise RuntimeError("crash")


def execute_fail_schema(_: dict[str, object]) -> dict[str, object]:
    raise ValueError("schema")


def execute_fail_permission(_: dict[str, object]) -> dict[str, object]:
    raise PermissionError("denied")


def validate_input_value(data: dict[str, object]) -> None:
    if not isinstance(data.get("value"), int):
        raise ValueError("value must be integer")


def validate_output_value(data: dict[str, object]) -> None:
    if not isinstance(data.get("value"), int):
        raise ValueError("value must be integer")


def qualify_value(data: dict[str, object]) -> dict[str, object]:
    return {"passed": isinstance(data.get("value"), int), "evidence_refs": ["adapter:fixture"]}


def adapter(execute=execute_increment) -> AdapterDefinition:
    return AdapterDefinition("fixture", "v1", "Input/v1", "Output/v1", "deterministic", execute, validate_input_value, validate_output_value, qualify_value)


def execute_raw(data: dict[str, object]) -> dict[str, object]:
    data["value"] = "2"
    return {"value": "2", "source": "fixture"}


def normalize_value(data: dict[str, object]) -> dict[str, object]:
    data["value"] = int(data["value"])
    return data


def execute_protected(_: dict[str, object]) -> dict[str, object]:
    return {"value": 2, "nested": [{" Write-Permissions ": ["portfolio"]}]}


def execute_non_object(_: dict[str, object]) -> list[int]:
    return [2]


def execute_large(_: dict[str, object]) -> dict[str, object]:
    return {"value": 2, "text": "x" * 100_000}


def test_normalization_preserves_original_output_and_inputs() -> None:
    registry = AdapterRegistry()
    registry.register(AdapterDefinition(
        "fixture", "v1", "Input/v1", "Output/v1", "deterministic", execute_raw,
        validate_input_value, validate_output_value, qualify_value, normalize_value,
    ))
    inputs = {"value": 1}
    result = registry.execute("fixture", inputs, as_of="2026-09-20T01:00:00Z")
    assert result["status"] == "succeeded"
    assert result["data"] == {"value": 2, "source": "fixture"}
    assert result["raw_output"] == {"value": "2", "source": "fixture"}
    assert inputs == {"value": 1}


@pytest.mark.parametrize("execute,status", [
    (execute_protected, "untrusted_output"),
    (execute_non_object, "schema_mismatch"),
    (execute_large, "succeeded"),
])
def test_output_boundary_returns_structured_results(execute, status: str) -> None:
    registry = AdapterRegistry()
    registry.register(adapter(execute))
    result = registry.execute("fixture", {"value": 1}, as_of="2026-09-20T01:00:00Z", timeout_seconds=3)
    assert result["status"] == status
    validate_output(result)
    if status != "succeeded":
        assert result["data"] == {}


def test_duplicate_request_cache_preserves_cycle_provenance_and_is_immutable() -> None:
    registry = AdapterRegistry()
    registry.register(adapter())
    first = registry.execute("fixture", {"value": 1}, as_of="2026-09-20T01:00:00Z", cycle_id="cycle-1")
    first["data"]["value"] = 100
    second = registry.execute("fixture", {"value": 1}, as_of="2026-09-20T01:00:00Z", cycle_id="cycle-2")
    assert second["data"] == {"value": 2}
    assert second["provenance"]["cycle_id"] == "cycle-2"


def test_unavailable_health_prevents_execution() -> None:
    registry = AdapterRegistry()
    registry.register(AdapterDefinition(
        "fixture", "v1", "Input/v1", "Output/v1", "deterministic", execute_fail_crash,
        validate_input_value, validate_output_value, qualify_value,
        healthcheck=lambda: {"state": "unavailable"},
    ))
    result = registry.execute("fixture", {"value": 1}, as_of="2026-09-20T01:00:00Z")
    assert result["status"] == "unavailable"
    assert result["provenance"]["health"]["state"] == "unavailable"


def test_declared_provider_capabilities_and_permissions_are_read_only() -> None:
    definition = AdapterDefinition(
        "fixture", "v1", "Input/v1", "Output/v1", "deterministic", execute_increment,
        validate_input_value, validate_output_value, qualify_value,
        provider="fixture-provider", capabilities=("increment",),
        network_permissions=("example.test",), state_permissions=("read:fixture",),
    )
    registry = AdapterRegistry()
    registry.register(definition)
    declaration = registry.manifest()[0]
    assert declaration["provider"] == "fixture-provider"
    assert declaration["capabilities"] == ["increment"]
    assert declaration["permissions"] == {
        "write_permissions": [], "network_permissions": ["example.test"],
        "state_permissions": ["read:fixture"],
    }
    result = registry.execute("fixture", {"value": 1}, as_of="2026-09-20T01:00:00Z")
    assert result["provenance"]["declaration"] == declaration
    assert result["provenance"]["evidence_gate"]["state"] == "not_evaluated"
    with pytest.raises(ValueError, match="read-only"):
        AdapterDefinition(
            "fixture", "v1", "Input/v1", "Output/v1", "deterministic", execute_increment,
            validate_input_value, validate_output_value, qualify_value,
            state_permissions=("write:portfolio",),
        )


def test_health_failure_can_recover_on_retry() -> None:
    checks = []

    def healthcheck():
        checks.append(True)
        if len(checks) == 1:
            raise RuntimeError("health unavailable")
        return {"state": "ready"}

    registry = AdapterRegistry()
    registry.register(AdapterDefinition(
        "fixture", "v1", "Input/v1", "Output/v1", "deterministic", execute_increment,
        validate_input_value, validate_output_value, qualify_value, healthcheck=healthcheck,
    ))
    result = registry.execute("fixture", {"value": 1}, as_of="2026-09-20T01:00:00Z", retries=1)
    assert result["status"] == "succeeded"
    assert result["attempts"] == ["fixture:unavailable", "fixture:succeeded"]
    assert result["provenance"]["retry_limit"] == 1
    assert result["provenance"]["health"]["state"] == "ready"


@pytest.mark.parametrize("timeout,retries", [(float("nan"), 0), (float("inf"), 0), (1, 0.5), (1, True)])
def test_invalid_execution_policy_is_rejected(timeout, retries) -> None:
    registry = AdapterRegistry()
    registry.register(adapter())
    with pytest.raises(ValueError, match="timeout or retry"):
        registry.execute("fixture", {"value": 1}, as_of="2026-09-20T01:00:00Z", timeout_seconds=timeout, retries=retries)


def test_adapter_definition_execution_health_and_normalization() -> None:
    registry = AdapterRegistry()
    registry.register(AdapterDefinition("fixture", "v1", "Input/v1", "Output/v1", "deterministic", execute_increment, validate_input_value, validate_output_value, qualify_value, healthcheck=lambda: {"state": "ready"}))
    result = registry.execute("fixture", {"value": 1}, as_of="2026-09-20T01:00:00Z")
    assert result["data"]["value"] == 2
    assert result["status"] == "succeeded"
    assert result["qualification"]["passed"] is True
    assert result["provenance"]["input_sha256"]
    assert registry.healthcheck("fixture")["state"] == "ready"


@pytest.mark.parametrize("execute,status", [
    (execute_fail_timeout, "timed_out"), (execute_fail_crash, "failed"),
    (execute_fail_schema, "schema_mismatch"), (execute_fail_permission, "untrusted_output"),
])
def test_adapter_failure_is_explicit(execute, status: str) -> None:
    registry = AdapterRegistry()
    registry.register(adapter(execute))
    result = registry.execute("fixture", {"value": 1}, as_of="2026-09-20T01:00:00Z")
    assert result["status"] == status
    assert result["error_code"]
    assert result["data"] == {}


def test_timeout_is_enforced_by_process_boundary() -> None:
    registry = AdapterRegistry()
    registry.register(adapter(execute_sleep))
    result = registry.execute("fixture", {"value": 1, "seconds": 2}, as_of="2026-09-20T01:00:00Z", timeout_seconds=0.05)
    assert result["status"] == "timed_out"
    assert result["error_code"] == "adapter_timeout"


def test_adapter_permissions_and_protected_business_state_are_rejected() -> None:
    registry = AdapterRegistry()
    registry.register(adapter())
    with pytest.raises(ValueError, match="protected field"):
        registry.execute("fixture", {"value": 1, "portfolio": {}}, as_of="2026-09-20T01:00:00Z")
    with pytest.raises(ValueError, match="protected field"):
        validate_output({"contract": "AdapterContractResult/v1", "version": 1, "adapter_id": "x", "adapter_version": "v1", "status": "succeeded", "data": {"final_judgment": "buy"}, "raw_output": {}, "provenance": {"input_sha256": "x"}, "permissions": {"write_permissions": []}, "error_code": None, "qualification": {"passed": True}, "attempts": []})


def test_engine_adapter_seam_is_read_only(tmp_path: Path) -> None:
    engine = CompanionEngine(CompanionStore(tmp_path / "companion.sqlite3"))
    engine.register_adapter(adapter())
    result = engine.execute_adapter("fixture", {"value": 1}, as_of="2026-09-20T01:00:00Z")
    assert result["data"]["value"] == 2
    assert result["permissions"]["write_permissions"] == []


def test_engine_exposes_terminal_computation_without_replacing_failed_transport(tmp_path: Path) -> None:
    engine = CompanionEngine(CompanionStore(tmp_path / "companion.sqlite3"))
    engine.register_adapter(adapter(execute_fail_crash))
    result = engine.execute_adapter("fixture", {"value": 1}, as_of="2026-09-20T01:00:00Z", retries=1)
    assert result["status"] == "failed" and result["data"] == {}
    assert result["attempts"] == ["fixture:failed", "fixture:failed"]
    assert result["provenance"]["computation"] == {"state": "NOT_COMPUTABLE", "value": None}
    validate_output(result)
    altered = copy.deepcopy(result)
    altered["status"] = "succeeded"
    altered["error_code"] = None
    altered["qualification"] = {"passed": True}
    altered["data"] = {"state": "NOT_COMPUTABLE", "value": None}
    from ai_trading_companion.adapter_contract import sha256
    altered["provenance"]["data_sha256"] = sha256(altered["data"])
    with pytest.raises(ValueError, match="computation"):
        validate_output(altered)


def test_retry_fallback_and_idempotency() -> None:
    registry = AdapterRegistry()
    registry.register(adapter(execute_fail_crash))
    registry.register(AdapterDefinition("fallback", "v1", "Input/v1", "Output/v1", "deterministic", execute_increment, validate_input_value, validate_output_value, qualify_value))
    result = registry.execute("fixture", {"value": 1}, as_of="2026-09-20T01:00:00Z", retries=1, fallbacks=("fallback",), request_id="request-1")
    assert result["adapter_id"] == "fallback"
    assert result["attempts"] == ["fixture:failed", "fixture:failed", "fallback:succeeded"]
    replay = registry.execute("fixture", {"value": 1}, as_of="2026-09-20T01:00:00Z", retries=1, fallbacks=("fallback",), request_id="request-1")
    assert replay == result


def test_computational_failure_remains_blocked_even_when_transport_succeeds() -> None:
    registry = AdapterRegistry()
    registry.register(AdapterDefinition(
        "calculation", "v1", "Input/v1", "Output/v1", "deterministic", execute_not_computable,
        output_validate=validate_not_computable, qualify=qualify_not_computable,
    ))
    result = registry.execute("calculation", {}, as_of="2026-09-20T01:00:00Z")
    assert result["status"] == "succeeded"  # A valid unavailable result, not a computed number.
    assert result["data"] == {"state": "NOT_COMPUTABLE", "value": None}
    assert result["provenance"]["fallback"]["state"] == "NOT_COMPUTABLE"
    assert result["provenance"]["fallback"]["continuation"] == "blocked"


def test_deterministic_failure_cannot_fall_back_to_a_model_estimate() -> None:
    registry = AdapterRegistry()
    registry.register(adapter(execute_fail_crash))
    registry.register(AdapterDefinition(
        "estimate", "v1", "Input/v1", "Output/v1", "probabilistic", execute_increment,
        validate_input_value, validate_output_value, qualify_value,
    ))
    result = registry.execute(
        "fixture", {"value": 1}, as_of="2026-09-20T01:00:00Z", fallbacks=("estimate",),
    )
    assert result["status"] == "failed"
    assert result["data"] == {}  # Legacy transport payload remains compatible.
    assert result["provenance"]["computation"] == {"state": "NOT_COMPUTABLE", "value": None}
    validate_output(result)
    tampered = copy.deepcopy(result)
    tampered["provenance"]["computation"]["value"] = 2
    with pytest.raises(ValueError, match="computation"):
        validate_output(tampered)
    receipt = result["provenance"]["fallback"]
    assert receipt["contract"] == "FallbackSpec/v1"
    assert receipt["state"] == "NOT_COMPUTABLE"
    assert receipt["substitute_value"] is None
    assert receipt["input"]["attempts"] == ["fixture:failed", "estimate:fallback_rejected"]
    schema_path = Path(__file__).parents[2] / "resources/contracts/fallback-spec-v1.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert not list(Draft202012Validator(schema).iter_errors(receipt))


def test_adapter_replay_preserves_inputs_output_qualification_and_schema() -> None:
    registry = AdapterRegistry()
    registry.register(adapter())
    request = {"contract": "AdapterContractSpec/v1", "version": 1, "adapter_id": "fixture", "adapter_version": "v1", "input_contract": "Input/v1", "output_contract": "Output/v1", "mode": "deterministic", "inputs": {"value": 1}, "permissions": {"write_permissions": []}, "provenance": {"as_of": "2026-09-20T01:00:00Z", "cycle_id": None, "timeout_seconds": 10, "request_id": None, "attempt": 0}}
    result = registry.execute("fixture", {"value": 1}, as_of="2026-09-20T01:00:00Z")
    replay = frozen_replay(request, result)
    assert replay["source_input"]["inputs"] == {"value": 1}
    assert replay["source_output"]["qualification"]["passed"] is True
    assert replay == frozen_replay(copy.deepcopy(request), copy.deepcopy(result))
    for field in ("data", "raw_output"):
        altered = copy.deepcopy(result)
        altered[field]["value"] = 999
        with pytest.raises(ValueError, match="digest mismatch"):
            frozen_replay(request, altered)
    assert request["permissions"] == {"write_permissions": []}
    root = Path(__file__).parents[2]
    schema = json.loads((root / "resources/contracts/adapter-contract-spec-v1.schema.json").read_text(encoding="utf-8"))
    assert list(Draft202012Validator(schema).iter_errors(request)) == []
    assert install_qualification()["qualified"] is True
