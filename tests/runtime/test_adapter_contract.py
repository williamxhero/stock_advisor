from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from ai_trading_companion.adapter_contract import (
    AdapterDefinition, AdapterRegistry, frozen_replay, install_qualification, validate_output,
)
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.store import CompanionStore


def adapter(execute=lambda data: {"value": data["value"] + 1}) -> AdapterDefinition:
    return AdapterDefinition("fixture", "v1", "Input/v1", "Output/v1", "deterministic", execute)


def test_adapter_definition_execution_health_and_normalization() -> None:
    registry = AdapterRegistry()
    registry.register(AdapterDefinition("fixture", "v1", "Input/v1", "Output/v1", "deterministic", lambda data: {"value": str(data["value"])}, normalize=lambda data: {"value": int(data["value"])}, healthcheck=lambda: {"state": "ready"}))
    result = registry.execute("fixture", {"value": 1}, as_of="2026-09-20T01:00:00Z")
    assert result["data"]["value"] == 1
    assert result["status"] == "succeeded"
    assert registry.healthcheck("fixture")["state"] == "ready"
    assert registry.manifest()[0]["output_contract"] == "Output/v1"


@pytest.mark.parametrize("error,status", [
    (TimeoutError("timeout"), "timed_out"),
    (RuntimeError("crash"), "failed"),
    (ValueError("schema"), "schema_mismatch"),
    (PermissionError("denied"), "untrusted_output"),
])
def test_adapter_failure_is_explicit(error: Exception, status: str) -> None:
    registry = AdapterRegistry()
    registry.register(adapter(lambda _: (_ for _ in ()).throw(error)))
    result = registry.execute("fixture", {"value": 1}, as_of="2026-09-20T01:00:00Z")
    assert result["status"] == status
    assert result["error_code"]
    assert result["data"] == {}


def test_adapter_permissions_and_protected_business_state_are_rejected() -> None:
    registry = AdapterRegistry()
    registry.register(adapter())
    with pytest.raises(ValueError, match="protected field"):
        registry.execute("fixture", {"value": 1, "portfolio": {}}, as_of="2026-09-20T01:00:00Z")
    with pytest.raises(ValueError, match="protected field"):
        validate_output({"contract": "AdapterContractResult/v1", "version": 1, "adapter_id": "x", "adapter_version": "v1", "status": "succeeded", "data": {"final_judgment": "buy"}, "provenance": {"input_sha256": "x"}, "permissions": {"write_permissions": []}, "error_code": None})


def test_engine_adapter_seam_is_read_only(tmp_path: Path) -> None:
    engine = CompanionEngine(CompanionStore(tmp_path / "companion.sqlite3"))
    engine.register_adapter(adapter())
    result = engine.execute_adapter("fixture", {"value": 1}, as_of="2026-09-20T01:00:00Z")
    assert result["data"]["value"] == 2
    assert result["permissions"]["write_permissions"] == []


def test_adapter_replay_schema_and_install_qualification() -> None:
    registry = AdapterRegistry()
    registry.register(adapter())
    request = {"contract": "AdapterContractSpec/v1", "version": 1, "adapter_id": "fixture", "adapter_version": "v1", "input_contract": "Input/v1", "output_contract": "Output/v1", "mode": "deterministic", "inputs": {"value": 1}, "permissions": {"write_permissions": []}, "provenance": {"as_of": "2026-09-20T01:00:00Z", "cycle_id": None, "timeout_seconds": 10}}
    result = registry.execute("fixture", {"value": 1}, as_of="2026-09-20T01:00:00Z")
    assert frozen_replay(request, result) == frozen_replay(copy.deepcopy(request), copy.deepcopy(result))
    root = Path(__file__).parents[2]
    schema = json.loads((root / "resources/contracts/adapter-contract-spec-v1.schema.json").read_text(encoding="utf-8"))
    assert list(Draft202012Validator(schema).iter_errors(request)) == []
    assert install_qualification()["qualified"] is True
