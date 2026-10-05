"""Runtime-owned AdapterContractSpec/v1 for isolated third-party capabilities."""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable

CONTRACT = "AdapterContractSpec/v1"
VERSION = 1
RESULT_CONTRACT = "AdapterContractResult/v1"
REPLAY_CONTRACT = "AdapterContractReplay/v1"
STATUSES = frozenset({"succeeded", "timed_out", "failed", "schema_mismatch", "untrusted_output", "unavailable"})
_FORBIDDEN = frozenset({"memoryhub", "memory", "portfolio", "positions", "orders", "schedule", "production_strategy", "final_judgment", "task_state"})


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _normalized_request(value: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(value)
    provenance = dict(normalized.get("provenance") or {})
    if "timeout_seconds" in provenance:
        provenance["timeout_seconds"] = float(provenance["timeout_seconds"])
    normalized["provenance"] = provenance
    return normalized


def _bounded(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 200:
        raise ValueError(f"{field} must be bounded")
    return value.strip()


def _walk_forbidden(value: Any, path: str = "adapter") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).casefold() in _FORBIDDEN:
                raise ValueError(f"AdapterContract forbids protected field at {path}.{key}")
            _walk_forbidden(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk_forbidden(child, f"{path}[{index}]")


def validate_input(value: dict[str, Any]) -> None:
    required = {"contract", "version", "adapter_id", "adapter_version", "input_contract", "output_contract", "mode", "inputs", "permissions", "provenance"}
    if not isinstance(value, dict) or value.get("contract") != CONTRACT or set(value) != required:
        raise ValueError("invalid AdapterContract input fields")
    if value["version"] != VERSION or value["mode"] not in {"deterministic", "probabilistic"}:
        raise ValueError("invalid AdapterContract identity")
    for field in ("adapter_id", "adapter_version", "input_contract", "output_contract"):
        _bounded(value[field], field)
    if not isinstance(value["inputs"], dict) or value["permissions"] != {"write_permissions": []}:
        raise ValueError("AdapterContract input must be bounded and read-only")
    if not isinstance(value["provenance"], dict) or not value["provenance"].get("as_of"):
        raise ValueError("AdapterContract provenance.as_of is required")
    _walk_forbidden(value)


def validate_output(value: dict[str, Any]) -> None:
    required = {"contract", "version", "adapter_id", "adapter_version", "status", "data", "provenance", "permissions", "error_code"}
    if not isinstance(value, dict) or value.get("contract") != RESULT_CONTRACT or set(value) != required:
        raise ValueError("invalid AdapterContract output fields")
    if value["version"] != VERSION or value["status"] not in STATUSES:
        raise ValueError("invalid AdapterContract output status")
    _bounded(value["adapter_id"], "adapter_id")
    _bounded(value["adapter_version"], "adapter_version")
    if not isinstance(value["data"], dict) or value["permissions"] != {"write_permissions": []}:
        raise ValueError("AdapterContract output must be bounded and read-only")
    if value["status"] == "succeeded" and value["error_code"] is not None:
        raise ValueError("successful AdapterContract output cannot have an error")
    if value["status"] != "succeeded" and not value["error_code"]:
        raise ValueError("failed AdapterContract output requires an error")
    if not isinstance(value["provenance"], dict) or not value["provenance"].get("input_sha256"):
        raise ValueError("AdapterContract output provenance is required")
    _walk_forbidden(value)


@dataclass(frozen=True)
class AdapterDefinition:
    adapter_id: str
    adapter_version: str
    input_contract: str
    output_contract: str
    mode: str
    execute: Callable[[dict[str, Any]], dict[str, Any]]
    validate: Callable[[dict[str, Any]], None] | None = None
    normalize: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    healthcheck: Callable[[], dict[str, Any]] | None = None


class AdapterRegistry:
    """Registry and bounded execution facade; adapters never receive write authority."""

    def __init__(self) -> None:
        self._adapters: dict[str, AdapterDefinition] = {}

    def register(self, adapter: AdapterDefinition) -> None:
        if adapter.mode not in {"deterministic", "probabilistic"}:
            raise ValueError("unsupported adapter mode")
        if adapter.adapter_id in self._adapters:
            raise ValueError("adapter already registered")
        self._adapters[adapter.adapter_id] = adapter

    def resolve(self, adapter_id: str) -> AdapterDefinition:
        try:
            return self._adapters[adapter_id]
        except KeyError as exc:
            raise ValueError("adapter is unavailable") from exc

    def execute(self, adapter_id: str, inputs: dict[str, Any], *, as_of: str, timeout_seconds: float = 10.0, cycle_id: str | None = None) -> dict[str, Any]:
        adapter = self.resolve(adapter_id)
        request = {
            "contract": CONTRACT, "version": VERSION, "adapter_id": adapter.adapter_id,
            "adapter_version": adapter.adapter_version, "input_contract": adapter.input_contract,
            "output_contract": adapter.output_contract, "mode": adapter.mode, "inputs": copy.deepcopy(inputs),
            "permissions": {"write_permissions": []}, "provenance": {"as_of": as_of, "cycle_id": cycle_id, "timeout_seconds": float(timeout_seconds)},
        }
        validate_input(request)
        if adapter.validate:
            adapter.validate(copy.deepcopy(inputs))
        try:
            data = adapter.execute(copy.deepcopy(inputs))
            if adapter.normalize:
                data = adapter.normalize(data)
            if not isinstance(data, dict):
                raise TypeError("adapter output must be an object")
            status, error = "succeeded", None
        except TimeoutError:
            data, status, error = {}, "timed_out", "adapter_timeout"
        except ValueError as exc:
            data, status, error = {}, "schema_mismatch", str(exc)[:200]
        except PermissionError:
            data, status, error = {}, "untrusted_output", "adapter_permission"
        except Exception as exc:
            data, status, error = {}, "failed", type(exc).__name__
        result = {
            "contract": RESULT_CONTRACT, "version": VERSION, "adapter_id": adapter.adapter_id,
            "adapter_version": adapter.adapter_version, "status": status, "data": data,
            "provenance": {"input_sha256": sha256(request), "as_of": as_of, "cycle_id": cycle_id, "timeout_seconds": timeout_seconds},
            "permissions": {"write_permissions": []}, "error_code": error,
        }
        validate_output(result)
        return result

    def healthcheck(self, adapter_id: str) -> dict[str, Any]:
        adapter = self.resolve(adapter_id)
        health = adapter.healthcheck() if adapter.healthcheck else {"state": "ready"}
        if not isinstance(health, dict) or health.get("state") not in {"ready", "degraded", "unavailable"}:
            raise ValueError("invalid adapter healthcheck")
        return {"adapter_id": adapter_id, "adapter_version": adapter.adapter_version, **health}

    def manifest(self) -> list[dict[str, Any]]:
        return [{"adapter_id": item.adapter_id, "adapter_version": item.adapter_version, "input_contract": item.input_contract, "output_contract": item.output_contract, "mode": item.mode} for item in self._adapters.values()]


def frozen_replay(request: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
    validate_input(request)
    validate_output(output)
    normalized = _normalized_request(request)
    input_hash = sha256(normalized)
    if output["provenance"].get("input_sha256") != input_hash:
        raise ValueError("AdapterContract replay provenance mismatch")
    return {"contract": REPLAY_CONTRACT, "version": VERSION, "source_input_sha256": input_hash, "source_output_sha256": sha256(output), "qualification": {"valid": True, "status": output["status"], "read_only": True}, "evaluation_vector": {"delivery_speed": {"state": "not_measured_in_frozen_replay"}, "qualification_probability": {"state": "not_estimated_in_frozen_replay"}, "research_quality": {"status": output["status"]}, "judgment_outcome": {"state": "adapter_not_a_judgment"}, "safety_reliability": {"write_permissions": [], "schema_bound": True}}}


def install_qualification() -> dict[str, Any]:
    registry = AdapterRegistry()
    registry.register(AdapterDefinition("fixture", "v1", "Input/v1", "Output/v1", "deterministic", lambda data: {"value": data["value"] + 1}))
    request = {"contract": CONTRACT, "version": VERSION, "adapter_id": "fixture", "adapter_version": "v1", "input_contract": "Input/v1", "output_contract": "Output/v1", "mode": "deterministic", "inputs": {"value": 1}, "permissions": {"write_permissions": []}, "provenance": {"as_of": "2026-01-01T00:00:00Z", "cycle_id": None, "timeout_seconds": 1}}
    output = registry.execute("fixture", {"value": 1}, as_of="2026-01-01T00:00:00Z", timeout_seconds=1)
    replay = frozen_replay(request, output)
    return {"contract": "AdapterContractInstallQualification/v1", "qualified": replay["qualification"]["valid"], "replay_sha256": sha256(replay), "evaluation_vector": replay["evaluation_vector"]}


if __name__ == "__main__":
    print(canonical_json(install_qualification()))
