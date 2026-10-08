"""Runtime-owned AdapterContractSpec/v1 for isolated third-party capabilities.

Adapters execute in a child process, return bounded structured results, and can
only produce evidence candidates. Runtime, MemoryHub, portfolio, schedules and
final judgment remain outside the adapter authority boundary.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import multiprocessing as mp
import time
from dataclasses import dataclass
from queue import Empty
from typing import Any, Callable

from .fallback_spec import build_receipt, replacement_allowed, validate_receipt

CONTRACT = "AdapterContractSpec/v1"
VERSION = 1
RESULT_CONTRACT = "AdapterContractResult/v1"
REPLAY_CONTRACT = "AdapterContractReplay/v1"
STATUSES = frozenset({"succeeded", "timed_out", "failed", "schema_mismatch", "untrusted_output", "unavailable"})
_RETRYABLE = frozenset({"timed_out", "failed", "unavailable"})
_FORBIDDEN = frozenset({"runtime", "memoryhub", "memory", "portfolio", "positions", "orders", "schedule", "production_strategy", "final_judgment", "task_state", "write_permissions", "network_permissions", "state_permissions", "evidence_gate", "evidence_gate_passed", "memoryhub_write", "memory_write", "portfolio_write", "positions_write", "orders_write", "schedule_write"})


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _normalized_request(value: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(value)
    provenance = dict(normalized.get("provenance") or {})
    if "timeout_seconds" in provenance:
        provenance["timeout_seconds"] = float(provenance["timeout_seconds"])
    provenance["retry_limit"] = int(provenance.get("retry_limit", 0))
    provenance.setdefault("provider", normalized["adapter_id"])
    provenance.setdefault("capabilities", [normalized["adapter_id"]])
    normalized["provenance"] = provenance
    permissions = normalized["permissions"]
    permissions.setdefault("network_permissions", [])
    permissions.setdefault("state_permissions", [])
    return normalized


def _bounded(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 200:
        raise ValueError(f"{field} must be bounded")
    return value.strip()


def _walk_forbidden(value: Any, path: str = "adapter") -> None:
    pending = [(value, path, 0)]
    visited = 0
    while pending:
        child, child_path, depth = pending.pop()
        visited += 1
        if depth > 64 or visited > 100_000:
            raise ValueError("AdapterContract payload limits exceeded")
        if isinstance(child, dict):
            for key, item in child.items():
                normalized_key = str(key).strip().casefold().replace("-", "_")
                # Multimodal contracts carry explicit empty read-only capability
                # descriptors inside their frozen input.  Empty lists grant no
                # authority; non-empty protected permissions remain forbidden.
                empty_read_only_descriptor = normalized_key in {
                    "write_permissions", "network_permissions", "state_permissions",
                } and item == []
                if normalized_key in _FORBIDDEN and not empty_read_only_descriptor:
                    raise ValueError(f"AdapterContract forbids protected field at {child_path}.{key}")
                pending.append((item, f"{child_path}.{key}", depth + 1))
        elif isinstance(child, (list, tuple)):
            pending.extend((item, f"{child_path}[{index}]", depth + 1) for index, item in enumerate(child))
        if visited + len(pending) > 100_000:
            raise ValueError("AdapterContract payload limits exceeded")


def validate_input(value: dict[str, Any]) -> None:
    required = {"contract", "version", "adapter_id", "adapter_version", "input_contract", "output_contract", "mode", "inputs", "permissions", "provenance"}
    if not isinstance(value, dict) or value.get("contract") != CONTRACT or set(value) != required:
        raise ValueError("invalid AdapterContract input fields")
    if value["version"] != VERSION or value["mode"] not in {"deterministic", "probabilistic"}:
        raise ValueError("invalid AdapterContract identity")
    for field in ("adapter_id", "adapter_version", "input_contract", "output_contract"):
        _bounded(value[field], field)
    if not isinstance(value["inputs"], dict) or not isinstance(value["permissions"], dict):
        raise ValueError("AdapterContract input must be bounded and read-only")
    permissions = value["permissions"]
    if permissions.get("write_permissions") != []:
        raise ValueError("AdapterContract input must not grant write permissions")
    for field in ("network_permissions", "state_permissions"):
        entries = permissions.get(field, [])
        if not isinstance(entries, list) or any(not isinstance(item, str) or not item.strip() for item in entries):
            raise ValueError(f"AdapterContract {field} are invalid")
        if field == "state_permissions" and any(not item.strip().casefold().startswith("read:") for item in entries):
            raise ValueError("AdapterContract state permissions must be read-only")
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
    if "fallback" in value["provenance"]:
        receipt = validate_receipt(value["provenance"]["fallback"])
        if receipt["input"]["source"]["input_sha256"] != value["provenance"]["input_sha256"] or receipt["input"]["attempts"] != value["attempts"]:
            raise ValueError("AdapterContract fallback provenance mismatch")
        if (receipt["continuation"] == "qualification_required") != (value["status"] == "succeeded" and value["data"].get("state") != "NOT_COMPUTABLE"):
            raise ValueError("AdapterContract fallback status mismatch")
    if "computation" in value["provenance"]:
        computation = value["provenance"]["computation"]
        if (computation != {"state": "NOT_COMPUTABLE", "value": None}
                or value["provenance"].get("computation_sha256") != sha256(computation)
                or value["provenance"].get("fallback", {}).get("state") != "NOT_COMPUTABLE"
                or value["provenance"].get("fallback", {}).get("input", {}).get("status") != "failed"
                or value["status"] == "succeeded"):
            raise ValueError("AdapterContract computation provenance mismatch")
    for field, payload in (("output_sha256", value["raw_output"]), ("data_sha256", value["data"])):
        expected = value["provenance"].get(field)
        if expected is not None and expected != sha256(payload):
            raise ValueError("AdapterContract output digest mismatch")
    _walk_forbidden(value["data"], "adapter.data")
    if value["status"] == "succeeded":
        _walk_forbidden(value["raw_output"], "adapter.raw_output")
        _walk_forbidden(value["qualification"], "adapter.qualification")


def _process_adapter(execute: Callable[[dict[str, Any]], dict[str, Any]], normalize: Callable[[dict[str, Any]], dict[str, Any]] | None, inputs: dict[str, Any], queue: Any) -> None:
    try:
        raw_output = execute(copy.deepcopy(inputs))
        if not isinstance(raw_output, dict):
            raise ValueError("adapter output must be an object")
        data = copy.deepcopy(raw_output)
        if normalize is not None:
            data = normalize(copy.deepcopy(data))
        if not isinstance(data, dict):
            raise ValueError("normalized adapter output must be an object")
        queue.put({"status": "succeeded", "data": data, "raw_output": raw_output})
    except TimeoutError:
        queue.put({"status": "timed_out", "error_code": "adapter_timeout", "data": {}, "raw_output": {}})
    except PermissionError:
        queue.put({"status": "untrusted_output", "error_code": "adapter_permission", "data": {}, "raw_output": {}})
    except ValueError as exc:
        queue.put({"status": "schema_mismatch", "error_code": str(exc)[:200], "data": {}, "raw_output": {}})
    except Exception as exc:
        queue.put({"status": "failed", "error_code": type(exc).__name__, "data": {}, "raw_output": {}})


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
    provider: str | None = None
    capabilities: tuple[str, ...] = ()
    network_permissions: tuple[str, ...] = ()
    state_permissions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.mode not in {"deterministic", "probabilistic"}:
            raise ValueError("unsupported adapter mode")
        for field in ("adapter_id", "adapter_version", "input_contract", "output_contract"):
            _bounded(getattr(self, field), field)
        if not all(callable(hook) for hook in (self.execute, self.output_validate, self.qualify)):
            raise ValueError("AdapterDefinition requires execute, output_validate and qualify")
        for hook in (self.validate, self.normalize, self.healthcheck):
            if hook is not None and not callable(hook):
                raise ValueError("AdapterDefinition hooks must be callable")
        if self.provider is not None:
            _bounded(self.provider, "provider")
        for name in ("capabilities", "network_permissions", "state_permissions"):
            values = getattr(self, name)
            if not isinstance(values, tuple):
                raise ValueError(f"{name} must be an immutable tuple")
            for value in values:
                _bounded(value, name)
        if any(not value.strip().casefold().startswith("read:") for value in self.state_permissions):
            raise ValueError("AdapterDefinition state permissions must be read-only")

    def declaration(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id, "adapter_version": self.adapter_version,
            "provider": self.provider or self.adapter_id,
            "capabilities": list(self.capabilities or (self.adapter_id,)),
            "input_contract": self.input_contract, "output_contract": self.output_contract,
            "mode": self.mode,
            "permissions": {"write_permissions": [], "network_permissions": list(self.network_permissions),
                            "state_permissions": list(self.state_permissions)},
        }


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
        if (not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool)
                or not math.isfinite(timeout_seconds) or timeout_seconds <= 0
                or not isinstance(retries, int) or isinstance(retries, bool) or retries < 0):
            raise ValueError("invalid adapter timeout or retry count")
        cache_key = sha256({
            "adapter": adapter_id, "candidates": candidates, "inputs": inputs,
            "as_of": as_of, "cycle_id": cycle_id, "request_id": request_id,
            "timeout_seconds": float(timeout_seconds), "retries": retries,
        })
        if cache_key in self._cache:
            return copy.deepcopy(self._cache[cache_key])
        attempts: list[str] = []
        final: dict[str, Any] | None = None
        requested = self.resolve(adapter_id)
        for candidate_id in candidates:
            if candidate_id not in self._adapters:
                attempts.append(f"{candidate_id}:unavailable")
                continue
            adapter = self.resolve(candidate_id)
            if not replacement_allowed(requested.declaration(), adapter.declaration()):
                attempts.append(f"{candidate_id}:fallback_rejected")
                continue
            for attempt in range(retries + 1):
                try:
                    health = self.healthcheck(candidate_id)
                except Exception as exc:
                    health = {"state": "unavailable", "error_code": type(exc).__name__}
                result = self._attempt(
                    adapter, inputs, as_of=as_of, timeout_seconds=timeout_seconds,
                    cycle_id=cycle_id, request_id=request_id, attempt=attempt,
                    attempts=attempts, health=health, retry_limit=retries,
                )
                result["provenance"]["health"] = copy.deepcopy(health)
                attempts.append(f"{candidate_id}:{result['status']}")
                final = result
                result["attempts"] = list(attempts)
                self._bind_fallback(result, requested.mode)
                if result["provenance"]["fallback"]["continuation"] == "qualification_required":
                    self._cache[cache_key] = copy.deepcopy(result)
                    return result
                if result["data"].get("state") == "NOT_COMPUTABLE":
                    return result
                if result["status"] not in _RETRYABLE:
                    break
        assert final is not None
        final["attempts"] = list(attempts)
        self._bind_fallback(final, requested.mode)
        if final["provenance"]["fallback"]["state"] == "NOT_COMPUTABLE":
            # Keep the legacy failed transport payload; expose semantics only
            # after every permitted retry/provider has been exhausted.
            computation = {"state": "NOT_COMPUTABLE", "value": None}
            final["provenance"]["computation"] = computation
            final["provenance"]["computation_sha256"] = sha256(computation)
            validate_output(final)
        return final

    @staticmethod
    def _bind_fallback(result: dict[str, Any], mode: str) -> None:
        provenance = result["provenance"]
        provenance["fallback"] = build_receipt(
            "Adapter", result["adapter_id"],
            status="succeeded" if result["status"] == "succeeded" and result["data"].get("state") != "NOT_COMPUTABLE" else "failed",
            as_of=provenance["as_of"], source_contract=RESULT_CONTRACT,
            source_version=result["adapter_version"], input_sha256=provenance["input_sha256"],
            deterministic=mode == "deterministic", attempts=result["attempts"], cycle_id=provenance["cycle_id"],
        )
        validate_output(result)

    def _attempt(self, adapter: AdapterDefinition, inputs: dict[str, Any], *, as_of: str, timeout_seconds: float, cycle_id: str | None, request_id: str | None, attempt: int, attempts: list[str], health: dict[str, Any], retry_limit: int) -> dict[str, Any]:
        request = {
            "contract": CONTRACT, "version": VERSION, "adapter_id": adapter.adapter_id,
            "adapter_version": adapter.adapter_version, "input_contract": adapter.input_contract,
            "output_contract": adapter.output_contract, "mode": adapter.mode, "inputs": copy.deepcopy(inputs),
            "permissions": {"write_permissions": [], "network_permissions": list(adapter.network_permissions), "state_permissions": list(adapter.state_permissions)},
            "provenance": {"as_of": as_of, "cycle_id": cycle_id, "timeout_seconds": float(timeout_seconds), "retry_limit": retry_limit, "request_id": request_id, "attempt": attempt, "provider": adapter.provider or adapter.adapter_id, "capabilities": list(adapter.capabilities or (adapter.adapter_id,))},
        }
        validate_input(request)
        if health["state"] == "unavailable":
            return self._result(adapter, "unavailable", {}, "adapter_unavailable", request, None, attempts)
        try:
            if adapter.validate is not None:
                adapter.validate(copy.deepcopy(inputs))
        except Exception as exc:
            return self._result(adapter, "schema_mismatch", {}, str(exc)[:200], request, None, attempts)
        context = mp.get_context("spawn")
        queue = context.Queue(1)
        process = context.Process(target=_process_adapter, args=(adapter.execute, adapter.normalize, copy.deepcopy(inputs), queue))
        try:
            deadline = time.monotonic() + timeout_seconds
            process.start()
            # Drain the queue before joining: a large output can otherwise block
            # the child's feeder thread and look like an execution timeout.
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._result(adapter, "timed_out", {}, "adapter_timeout", request, None, attempts)
                try:
                    payload = queue.get(timeout=min(remaining, 0.05))
                    break
                except Empty:
                    if not process.is_alive():
                        return self._result(adapter, "failed", {}, "adapter_no_result", request, None, attempts)
            process.join(max(0, deadline - time.monotonic()))
            if process.is_alive():
                return self._result(adapter, "timed_out", {}, "adapter_timeout", request, None, attempts)
        except Exception as exc:
            return self._result(adapter, "failed", {}, type(exc).__name__, request, None, attempts)
        finally:
            if process.is_alive():
                process.terminate()
                process.join(2)
                if process.is_alive():
                    process.kill()
                    process.join(2)
            queue.close()
            queue.join_thread()
        status, data, error = payload.get("status"), payload.get("data"), payload.get("error_code")
        raw_output = payload.get("raw_output", {})
        if status != "succeeded":
            return self._result(adapter, status, {}, error, request, None, attempts, raw_output=raw_output)
        try:
            _walk_forbidden(data, "adapter.data")
            _walk_forbidden(raw_output, "adapter.raw_output")
        except ValueError as exc:
            return self._result(adapter, "untrusted_output", {}, str(exc)[:200], request, None, attempts, raw_output=raw_output)
        try:
            adapter.output_validate(copy.deepcopy(data))
        except Exception as exc:
            return self._result(adapter, "schema_mismatch", {}, str(exc)[:200] or type(exc).__name__, request, None, attempts, raw_output=raw_output)
        try:
            qualification = adapter.qualify(copy.deepcopy(data))
            if not isinstance(qualification, dict) or qualification.get("passed") is not True:
                return self._result(adapter, "untrusted_output", {}, "adapter_qualification_failed", request, None, attempts, raw_output=raw_output)
            _walk_forbidden(qualification, "adapter.qualification")
            return self._result(adapter, "succeeded", data, None, request, qualification, attempts, raw_output=raw_output)
        except Exception as exc:
            return self._result(adapter, "untrusted_output", {}, type(exc).__name__, request, None, attempts, raw_output=raw_output)

    def _result(self, adapter: AdapterDefinition, status: str, data: dict[str, Any], error: str | None, request: dict[str, Any], qualification: dict[str, Any] | None, attempts: list[str], *, raw_output: dict[str, Any] | None = None) -> dict[str, Any]:
        result = {
            "contract": RESULT_CONTRACT, "version": VERSION, "adapter_id": adapter.adapter_id,
            "adapter_version": adapter.adapter_version, "status": status, "data": copy.deepcopy(data),
            "raw_output": copy.deepcopy(data if raw_output is None else raw_output),
            "provenance": {"input_sha256": sha256(_normalized_request(request)), "as_of": request["provenance"]["as_of"], "cycle_id": request["provenance"].get("cycle_id"), "request_id": request["provenance"].get("request_id"), "attempt": request["provenance"]["attempt"]},
            "permissions": {"write_permissions": []}, "error_code": error, "qualification": qualification, "attempts": list(attempts),
        }
        result["provenance"].update({
            "declaration": adapter.declaration(),
            "timeout_seconds": request["provenance"]["timeout_seconds"],
            "retry_limit": request["provenance"]["retry_limit"],
            "output_sha256": sha256(result["raw_output"]),
            "data_sha256": sha256(result["data"]),
            "evidence_gate": {"state": "not_evaluated", "owner": "EvidenceGate"},
        })
        validate_output(result)
        return result

    def healthcheck(self, adapter_id: str) -> dict[str, Any]:
        adapter = self.resolve(adapter_id)
        health = adapter.healthcheck() if adapter.healthcheck else {"state": "ready"}
        if not isinstance(health, dict) or health.get("state") not in {"ready", "degraded", "unavailable"}:
            raise ValueError("invalid adapter healthcheck")
        return {"adapter_id": adapter_id, "adapter_version": adapter.adapter_version, **health}

    def manifest(self) -> list[dict[str, Any]]:
        return [item.declaration() for item in self._adapters.values()]


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
