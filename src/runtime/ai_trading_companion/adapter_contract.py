"""Runtime-owned AdapterContractSpec/v1 for isolated third-party capabilities.

Adapters execute in a child process, return bounded structured results, and can
only produce evidence candidates. Runtime, MemoryHub, portfolio, schedules and
final judgment remain outside the adapter authority boundary.
"""
from __future__ import annotations

import copy
import hashlib
import json
import multiprocessing as mp
from dataclasses import dataclass
from queue import Empty
from typing import Any, Callable

CONTRACT = "AdapterContractSpec/v1"
VERSION = 1
RESULT_CONTRACT = "AdapterContractResult/v1"
REPLAY_CONTRACT = "AdapterContractReplay/v1"
STATUSES = frozenset({"succeeded", "timed_out", "failed", "schema_mismatch", "untrusted_output", "unavailable"})
_RETRYABLE = frozenset({"timed_out", "failed", "unavailable"})
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
    if "retry_limit" in provenance:
        provenance["retry_limit"] = int(provenance["retry_limit"])
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
    _walk_forbidden(value["inputs"], "adapter.inputs")


def validate_output(value: dict[str, Any]) -> None:
    required = {"contract", "version", "adapter_id", "adapter_version", "status", "data", "raw_output", "provenance", "permissions", "error_code", "qualification", "attempts"}
    if not isinstance(value, dict) or value.get("contract") != RESULT_CONTRACT or set(value) != required:
        raise ValueError("invalid AdapterContract output fields")
    if value["version"] != VERSION or value["status"] not in STATUSES:
        raise ValueError("invalid AdapterContract output status")
    _bounded(value["adapter_id"], "adapter_id")
    _bounded(value["adapter_version"], "adapter_version")
    if not isinstance(value["data"], dict) or not isinstance(value["raw_output"], dict) or value["permissions"] != {"write_permissions": []}:
        raise ValueError("AdapterContract output must be bounded and read-only")
    if not isinstance(value["attempts"], list) or any(not isinstance(item, str) for item in value["attempts"]):
        raise ValueError("AdapterContract attempts are invalid")
    if value["status"] == "succeeded":
        if value["error_code"] is not None or not isinstance(value["qualification"], dict) or value["qualification"].get("passed") is not True:
            raise ValueError("successful AdapterContract output requires qualification")
    elif not value["error_code"]:
        raise ValueError("failed AdapterContract output requires an error")
    if not isinstance(value["provenance"], dict) or not value["provenance"].get("input_sha256"):
        raise ValueError("AdapterContract output provenance is required")
    _walk_forbidden(value["data"], "adapter.data")
    if value["status"] == "succeeded":
        _walk_forbidden(value["raw_output"], "adapter.raw_output")


def _process_adapter(execute: Callable[[dict[str, Any]], dict[str, Any]], normalize: Callable[[dict[str, Any]], dict[str, Any]] | None, inputs: dict[str, Any], queue: Any) -> None:
    try:
        data = execute(copy.deepcopy(inputs))
        if normalize is not None:
            data = normalize(data)
        queue.put({"status": "succeeded", "data": data})
    except TimeoutError:
        queue.put({"status": "timed_out", "error_code": "adapter_timeout", "data": {}})
    except PermissionError:
        queue.put({"status": "untrusted_output", "error_code": "adapter_permission", "data": {}})
    except ValueError as exc:
        queue.put({"status": "schema_mismatch", "error_code": str(exc)[:200], "data": {}})
    except Exception as exc:
        queue.put({"status": "failed", "error_code": type(exc).__name__, "data": {}})


@dataclass(frozen=True)
class AdapterDefinition:
    adapter_id: str
    adapter_version: str
    input_contract: str
    output_contract: str
    mode: str
    execute: Callable[[dict[str, Any]], dict[str, Any]]
    validate: Callable[[dict[str, Any]], None] | None = None
    output_validate: Callable[[dict[str, Any]], None] | None = None
    qualify: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    normalize: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    healthcheck: Callable[[], dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if self.mode not in {"deterministic", "probabilistic"}:
            raise ValueError("unsupported adapter mode")
        for field in ("adapter_id", "adapter_version", "input_contract", "output_contract"):
            _bounded(getattr(self, field), field)
        if not callable(self.execute) or self.output_validate is None or self.qualify is None:
            raise ValueError("AdapterDefinition requires execute, output_validate and qualify")


class AdapterRegistry:
    """Process-isolated, retryable, fallback-capable adapter execution facade."""

    def __init__(self) -> None:
        self._adapters: dict[str, AdapterDefinition] = {}
        self._cache: dict[str, dict[str, Any]] = {}

    def register(self, adapter: AdapterDefinition) -> None:
        if adapter.adapter_id in self._adapters:
            raise ValueError("adapter already registered")
        self._adapters[adapter.adapter_id] = adapter

    def resolve(self, adapter_id: str) -> AdapterDefinition:
        try:
            return self._adapters[adapter_id]
        except KeyError as exc:
            raise ValueError("adapter is unavailable") from exc

    def execute(self, adapter_id: str, inputs: dict[str, Any], *, as_of: str, timeout_seconds: float = 10.0, cycle_id: str | None = None, retries: int = 0, fallbacks: tuple[str, ...] = (), request_id: str | None = None) -> dict[str, Any]:
        candidates = tuple(dict.fromkeys((adapter_id, *fallbacks)))
        if timeout_seconds <= 0 or retries < 0:
            raise ValueError("invalid adapter timeout or retry count")
        cache_key = sha256({"adapter": adapter_id, "inputs": inputs, "as_of": as_of, "request_id": request_id})
        if cache_key in self._cache:
            return copy.deepcopy(self._cache[cache_key])
        attempts: list[str] = []
        final: dict[str, Any] | None = None
        for candidate_id in candidates:
            adapter = self.resolve(candidate_id)
            for attempt in range(retries + 1):
                result = self._attempt(adapter, inputs, as_of=as_of, timeout_seconds=timeout_seconds, cycle_id=cycle_id, request_id=request_id, attempt=attempt, attempts=attempts)
                attempts.append(f"{candidate_id}:{result['status']}")
                final = result
                if result["status"] == "succeeded":
                    result["attempts"] = list(attempts)
                    self._cache[cache_key] = copy.deepcopy(result)
                    return result
                if result["status"] not in _RETRYABLE:
                    break
        assert final is not None
        final["attempts"] = list(attempts)
        return final

    def _attempt(self, adapter: AdapterDefinition, inputs: dict[str, Any], *, as_of: str, timeout_seconds: float, cycle_id: str | None, request_id: str | None, attempt: int, attempts: list[str]) -> dict[str, Any]:
        request = {
            "contract": CONTRACT, "version": VERSION, "adapter_id": adapter.adapter_id,
            "adapter_version": adapter.adapter_version, "input_contract": adapter.input_contract,
            "output_contract": adapter.output_contract, "mode": adapter.mode, "inputs": copy.deepcopy(inputs),
            "permissions": {"write_permissions": []},
            "provenance": {"as_of": as_of, "cycle_id": cycle_id, "timeout_seconds": float(timeout_seconds), "request_id": request_id, "attempt": attempt},
        }
        validate_input(request)
        try:
            if adapter.validate is not None:
                adapter.validate(copy.deepcopy(inputs))
        except Exception as exc:
            return self._result(adapter, "schema_mismatch", {}, str(exc)[:200], request, None, attempts)
        context = mp.get_context("spawn")
        queue = context.Queue(1)
        process = context.Process(target=_process_adapter, args=(adapter.execute, adapter.normalize, copy.deepcopy(inputs), queue))
        try:
            process.start()
            process.join(timeout_seconds)
            if process.is_alive():
                process.terminate(); process.join(2)
                return self._result(adapter, "timed_out", {}, "adapter_timeout", request, None, attempts)
            try:
                payload = queue.get(timeout=1)
            except Empty:
                return self._result(adapter, "failed", {}, "adapter_no_result", request, None, attempts)
        except Exception as exc:
            if process.is_alive():
                process.terminate(); process.join(2)
            return self._result(adapter, "failed", {}, type(exc).__name__, request, None, attempts)
        finally:
            queue.close(); queue.join_thread()
        status, data, error = payload.get("status"), payload.get("data") or {}, payload.get("error_code")
        if status != "succeeded":
            return self._result(adapter, status, data, error, request, None, attempts)
        try:
            if not isinstance(data, dict):
                raise ValueError("adapter output must be an object")
            adapter.output_validate(copy.deepcopy(data))
            qualification = adapter.qualify(copy.deepcopy(data))
            if not isinstance(qualification, dict) or qualification.get("passed") is not True:
                return self._result(adapter, "untrusted_output", data, "adapter_qualification_failed", request, qualification, attempts)
            return self._result(adapter, "succeeded", data, None, request, qualification, attempts)
        except ValueError as exc:
            return self._result(adapter, "schema_mismatch", data, str(exc)[:200], request, None, attempts)
        except Exception as exc:
            return self._result(adapter, "untrusted_output", data, type(exc).__name__, request, None, attempts)

    def _result(self, adapter: AdapterDefinition, status: str, data: dict[str, Any], error: str | None, request: dict[str, Any], qualification: dict[str, Any] | None, attempts: list[str]) -> dict[str, Any]:
        result = {
            "contract": RESULT_CONTRACT, "version": VERSION, "adapter_id": adapter.adapter_id,
            "adapter_version": adapter.adapter_version, "status": status, "data": copy.deepcopy(data),
            "raw_output": copy.deepcopy(data), "provenance": {"input_sha256": sha256(_normalized_request(request)), "as_of": request["provenance"]["as_of"], "cycle_id": request["provenance"].get("cycle_id"), "request_id": request["provenance"].get("request_id"), "attempt": request["provenance"]["attempt"]},
            "permissions": {"write_permissions": []}, "error_code": error, "qualification": qualification, "attempts": list(attempts),
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
    validate_input(request); validate_output(output)
    normalized = _normalized_request(request); input_hash = sha256(normalized)
    if output["provenance"].get("input_sha256") != input_hash:
        raise ValueError("AdapterContract replay provenance mismatch")
    return {"contract": REPLAY_CONTRACT, "version": VERSION, "source_input": normalized, "source_output": copy.deepcopy(output), "source_input_sha256": input_hash, "source_output_sha256": sha256(output), "qualification": {"valid": True, "status": output["status"], "record": copy.deepcopy(output.get("qualification"))}, "evaluation_vector": {"delivery_speed": {"state": "not_measured_in_frozen_replay"}, "qualification_probability": {"state": "not_estimated_in_frozen_replay"}, "research_quality": {"status": output["status"]}, "judgment_outcome": {"state": "adapter_not_a_judgment"}, "safety_reliability": {"write_permissions": [], "schema_bound": True, "isolated_execution": True, "deadline_enforced": True}}}


def _install_execute(data: dict[str, Any]) -> dict[str, Any]:
    return {"value": data["value"] + 1}


def _install_validate(data: dict[str, Any]) -> None:
    if not isinstance(data.get("value"), int): raise ValueError("value must be integer")


def _install_output_validate(data: dict[str, Any]) -> None:
    if not isinstance(data.get("value"), int): raise ValueError("value must be integer")


def _install_qualify(data: dict[str, Any]) -> dict[str, Any]:
    return {"passed": data.get("value") == 2, "evidence_refs": ["adapter:fixture"]}


def install_qualification() -> dict[str, Any]:
    registry = AdapterRegistry()
    registry.register(AdapterDefinition("fixture", "v1", "Input/v1", "Output/v1", "deterministic", _install_execute, _install_validate, _install_output_validate, _install_qualify))
    request = {"contract": CONTRACT, "version": VERSION, "adapter_id": "fixture", "adapter_version": "v1", "input_contract": "Input/v1", "output_contract": "Output/v1", "mode": "deterministic", "inputs": {"value": 1}, "permissions": {"write_permissions": []}, "provenance": {"as_of": "2026-01-01T00:00:00Z", "cycle_id": None, "timeout_seconds": 1, "request_id": None, "attempt": 0}}
    output = registry.execute("fixture", {"value": 1}, as_of="2026-01-01T00:00:00Z", timeout_seconds=1)
    replay = frozen_replay(request, output)
    return {"contract": "AdapterContractInstallQualification/v1", "qualified": replay["qualification"]["valid"], "replay_sha256": sha256(replay), "evaluation_vector": replay["evaluation_vector"]}


if __name__ == "__main__":
    print(canonical_json(install_qualification()))
