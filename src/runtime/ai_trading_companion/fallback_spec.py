"""Runtime-owned honest degradation; this contract never grants qualification."""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

CONTRACT = "FallbackSpec/v1"
INPUT_CONTRACT = "FallbackInput/v1"
VERSION = 1
_COMPONENTS = {"Adapter", "Skill", "Orchestration", "MarketHub", "MemoryHub", "QuantResearch"}
_BOUNDARIES = {
    "local_memory_fallback": False, "llm_numeric_substitution": False,
    "rollback_committed_facts": False, "blind_m1": True,
    "fact_ownership_preserved": True, "quantresearch_access": "read_only",
    "write_permissions": [],
}


def sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _output(request: dict[str, Any]) -> dict[str, Any]:
    status = request["status"]
    if status == "succeeded":
        state = "degraded" if len(request["attempts"]) > 1 else "available"
    elif status == "degraded":
        state = "degraded"
    elif request["deterministic"] and request["component"] in {"Adapter", "Skill"}:
        state = "NOT_COMPUTABLE"
    else:
        state = "terminated" if status == "terminated" else "unavailable"
    value = {
        "contract": CONTRACT, "version": VERSION, "input": copy.deepcopy(request),
        "state": state, "substitute_value": None,
        "continuation": "qualification_required" if state in {"available", "degraded"} else "blocked",
        "boundaries": copy.deepcopy(_BOUNDARIES),
        "qualification": {"state": "not_evaluated", "required_gates": ["EvidenceGate", "RiskGate"]},
        "provenance": {"source": "runtime", "spec_refs": ["github:issue:316", "github:issue:106", "github:issue:82"]},
    }
    value["sha256"] = sha256(value)
    return value


def build_receipt(component: str, operation: str, *, status: str, as_of: str,
                  source_contract: str, source_version: str, input_sha256: str,
                  deterministic: bool = False, attempts: tuple[str, ...] | list[str] = (),
                  cycle_id: str | None = None) -> dict[str, Any]:
    request = {
        "contract": INPUT_CONTRACT, "version": VERSION, "component": component,
        "operation": operation, "status": status, "deterministic": deterministic,
        "as_of": as_of, "cycle_id": cycle_id, "attempts": list(attempts),
        "source": {"contract": source_contract, "version": source_version, "input_sha256": input_sha256},
    }
    value = _output(request)
    validate_receipt(value)
    return value


def validate_receipt(value: dict[str, Any]) -> dict[str, Any]:
    request = value.get("input") if isinstance(value, dict) else None
    fields = {"contract", "version", "component", "operation", "status", "deterministic",
              "as_of", "cycle_id", "attempts", "source"}
    if not isinstance(request, dict) or set(request) != fields:
        raise ValueError("invalid FallbackInput fields")
    if request["contract"] != INPUT_CONTRACT or type(request["version"]) is not int or request["version"] != VERSION:
        raise ValueError("invalid FallbackInput version")
    if request["component"] not in _COMPONENTS or request["status"] not in {"succeeded", "degraded", "failed", "unavailable", "terminated"}:
        raise ValueError("invalid FallbackInput state")
    if type(request["deterministic"]) is not bool:
        raise ValueError("invalid FallbackInput mode")
    if any(not isinstance(request[key], str) or not request[key].strip() for key in ("operation", "as_of")):
        raise ValueError("FallbackInput requires operation and as_of")
    if request["cycle_id"] is not None and not isinstance(request["cycle_id"], str):
        raise ValueError("invalid FallbackInput cycle")
    if not isinstance(request["attempts"], list) or any(not isinstance(item, str) for item in request["attempts"]):
        raise ValueError("invalid FallbackInput attempts")
    source = request["source"]
    if not isinstance(source, dict) or set(source) != {"contract", "version", "input_sha256"}:
        raise ValueError("invalid FallbackInput source")
    if any(not isinstance(source[key], str) or not source[key].strip() for key in source):
        raise ValueError("FallbackInput requires versioned source")
    if len(source["input_sha256"]) != 64 or any(char not in "0123456789abcdef" for char in source["input_sha256"]):
        raise ValueError("invalid FallbackInput source digest")
    if value != _output(request):
        raise ValueError("FallbackSpec receipt differs from deterministic policy")
    return value


def replacement_allowed(requested: dict[str, Any], candidate: dict[str, Any]) -> bool:
    """A replacement may change provider, not computation semantics or authority."""
    if any(requested[key] != candidate[key] for key in ("mode", "input_contract", "output_contract")):
        return False
    # Legacy adapters declare their identity as the default capability. Explicit
    # capability declarations must agree; default identities remain replaceable.
    def capabilities(declaration: dict[str, Any]) -> set[str]:
        entries = set(declaration["capabilities"])
        return set() if entries == {declaration["adapter_id"]} else entries
    if capabilities(requested) != capabilities(candidate):
        return False
    return all(set(candidate["permissions"][key]).issubset(requested["permissions"][key])
               for key in ("network_permissions", "state_permissions", "write_permissions"))
