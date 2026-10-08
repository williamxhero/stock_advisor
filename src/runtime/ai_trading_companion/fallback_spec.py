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


def _build_receipt(request: dict[str, Any]) -> dict[str, Any]:
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
    value = _build_receipt(request)
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
    if value != _build_receipt(request):
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


def install_qualification() -> dict[str, Any]:
    """Measure deterministic fallback boundaries; do not imply live quality."""
    def fixture(component: str, operation: str, status: str, **kwargs: Any) -> dict[str, Any]:
        return build_receipt(
            component, operation, status=status, as_of="2026-09-21T01:45:00Z",
            source_contract="FallbackInstallFixture/v1", source_version="1",
            input_sha256="a" * 64, **kwargs,
        )

    failed = fixture("Adapter", "financial_calculation", "failed", deterministic=True)
    unavailable = fixture("MemoryHub", "read_episode", "unavailable")
    retried = fixture("Skill", "market_analysis", "succeeded", attempts=("attempt-1", "attempt-2"))
    recovered = fixture("MemoryHub", "read_episode", "succeeded", attempts=("recovery-1",))
    requested = {
        "adapter_id": "primary", "mode": "read", "input_contract": "Input/v1",
        "output_contract": "Output/v1", "capabilities": ["market.read"],
        "permissions": {"network_permissions": ["market.read"],
                        "state_permissions": ["cycle.read"], "write_permissions": []},
    }
    reduced = {**requested, "adapter_id": "replacement", "permissions": {
        "network_permissions": [], "state_permissions": ["cycle.read"], "write_permissions": [],
    }}
    expanded = {**reduced, "permissions": {
        **reduced["permissions"], "write_permissions": ["runtime.write"],
    }}
    checks = {
        "deterministic_failure_not_computable": (
            failed["state"] == "NOT_COMPUTABLE" and failed["substitute_value"] is None
            and failed["continuation"] == "blocked"
        ),
        "memory_unavailable_without_local_fallback": (
            unavailable["state"] == "unavailable" and not unavailable["boundaries"]["local_memory_fallback"]
        ),
        "retry_degraded_and_recovery_available": retried["state"] == "degraded" and recovered["state"] == "available",
        "replacement_cannot_expand_permissions": (
            replacement_allowed(requested, reduced) and not replacement_allowed(requested, expanded)
        ),
        "ownership_and_qualification_boundaries_preserved": (
            failed["boundaries"]["rollback_committed_facts"] is False
            and failed["boundaries"]["blind_m1"] is True
            and failed["boundaries"]["fact_ownership_preserved"] is True
            and failed["boundaries"]["quantresearch_access"] == "read_only"
            and failed["qualification"]["state"] == "not_evaluated"
        ),
    }
    unmeasured_reasons = {
        "delivery_speed": "offline_fixtures_have_no_live_latency_baseline",
        "qualification_probability": "fixed_fallback_fixtures_are_not_a_population",
        "research_quality": "fallback_smoke_does_not_measure_research_quality",
        "judgment_outcome": "no_realized_judgment_outcome_is_available_offline",
    }
    evaluation_vector = {
        axis: {"status": "not_measured", "reason": reason}
        for axis, reason in unmeasured_reasons.items()
    }
    evaluation_vector["safety_reliability"] = {
        "status": "pass" if all(checks.values()) else "fail",
        "scope": "deterministic_fallback_fixtures", "measurements": checks,
    }
    return {
        "contract": "FallbackSpecInstallQualification/v1",
        "qualified": evaluation_vector["safety_reliability"]["status"] == "pass",
        "evaluation_vector": evaluation_vector,
        "receipts": {"deterministic_failure": failed, "memory_unavailable": unavailable,
                     "retried_skill": retried, "recovered_memory": recovered},
    }


if __name__ == "__main__":
    print(json.dumps(install_qualification(), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
