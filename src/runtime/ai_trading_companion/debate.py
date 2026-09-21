"""Runtime-owned DebateSpec/v1 for bounded internal research debate.

Debate artifacts are internal, append-only qualification inputs for the
coordinator. They contain references to frozen claims and evidence, never
copied fact payloads, private reasoning, or write capabilities.
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from .agent_contract import validate_input as validate_agent_input


CONTRACT = "DebateSpec/v1"
VERSION = 1
REPLAY_CONTRACT = "DebateReplay/v1"
STATUSES = frozenset({
    "succeeded", "partial", "conflict_unresolved", "evidence_insufficient",
    "timed_out", "failed", "unknown",
})
COORDINATION_STATES = frozenset({
    "ready", "conflict_unresolved", "evidence_insufficient", "timed_out",
    "failed", "unknown",
})
POSITIONS = frozenset({"support", "oppose", "unknown"})
_FORBIDDEN_KEYS = frozenset({
    "chain_of_thought", "cot", "thoughts", "reasoning_trace", "private_reasoning",
    "scratchpad", "deliberation", "hidden_reasoning", "h0", "h0_text", "credentials",
    "memoryhub", "portfolio", "positions", "markethub", "exchange", "orders",
})


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _walk_forbidden(value: Any, path: str = "artifact") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).casefold() in _FORBIDDEN_KEYS:
                raise ValueError(f"DebateSpec forbids protected data at {path}.{key}")
            _walk_forbidden(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk_forbidden(child, f"{path}[{index}]")


def _refs(value: Any, field: str, *, required: bool = False) -> list[str]:
    if value is None:
        value = []
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{field} must be a list of non-empty references")
    result = sorted(set(value))
    if required and not result:
        raise ValueError(f"{field} must cite at least one evidence_id")
    return result


def _bounded_text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a bounded non-empty string")
    text = value.strip()
    if not text or len(text) > 4_000:
        raise ValueError(f"{field} must be a bounded non-empty string")
    return text


def validate_input(value: dict[str, Any]) -> None:
    if not isinstance(value, dict) or value.get("contract") != CONTRACT:
        raise ValueError("unsupported DebateSpec input")
    required = {"contract", "version", "stage", "visibility", "evidence_snapshot_refs",
                "role_input_refs", "provenance", "permissions"}
    missing = sorted(required - set(value))
    if missing:
        raise ValueError("DebateSpec input missing: " + ", ".join(missing))
    unknown = sorted(set(value) - required)
    if unknown:
        raise ValueError("DebateSpec input contains unsupported fields: " + ", ".join(unknown))
    if value["version"] != VERSION or value.get("visibility") != "internal":
        raise ValueError("invalid DebateSpec input identity")
    _bounded_text(value.get("stage"), "stage")
    if not isinstance(value["evidence_snapshot_refs"], list) or not value["evidence_snapshot_refs"]:
        raise ValueError("DebateSpec input requires frozen evidence snapshot references")
    _refs(value["evidence_snapshot_refs"], "evidence_snapshot_refs", required=True)
    if not isinstance(value["role_input_refs"], list) or not value["role_input_refs"]:
        raise ValueError("DebateSpec input requires AgentRole references")
    _refs(value["role_input_refs"], "role_input_refs", required=True)
    if not isinstance(value["provenance"], dict) or not isinstance(value["provenance"].get("as_of"), str) or not value["provenance"]["as_of"].strip():
        raise ValueError("DebateSpec input requires provenance.as_of")
    if not isinstance(value["permissions"], dict) or value["permissions"].get("write_permissions") != []:
        raise ValueError("DebateSpec is read-only")
    if "read_permissions" in value["permissions"] and not isinstance(value["permissions"]["read_permissions"], list):
        raise ValueError("DebateSpec permissions.read_permissions must be a list")
    _walk_forbidden(value)


def _validate_argument(row: dict[str, Any], field: str) -> None:
    required = {"argument_id", "role", "claim_id", "position", "statement", "evidence_ids"}
    missing = sorted(required - set(row))
    if missing:
        raise ValueError(f"{field} missing: {', '.join(missing)}")
    _bounded_text(row["argument_id"], f"{field}.argument_id")
    _bounded_text(row["claim_id"], f"{field}.claim_id")
    _bounded_text(row["statement"], f"{field}.statement")
    if row["position"] not in POSITIONS:
        raise ValueError(f"{field}.position is invalid")
    _refs(row["evidence_ids"], f"{field}.evidence_ids", required=True)


def validate_output(value: dict[str, Any]) -> None:
    if not isinstance(value, dict) or value.get("contract") != CONTRACT:
        raise ValueError("unsupported DebateSpec output")
    required = {"contract", "version", "stage", "visibility", "status", "arguments",
                "counterarguments", "conflicts", "coordination", "provenance", "permissions"}
    missing = sorted(required - set(value))
    if missing:
        raise ValueError("DebateSpec output missing: " + ", ".join(missing))
    unknown = sorted(set(value) - required)
    if unknown:
        raise ValueError("DebateSpec output contains unsupported fields: " + ", ".join(unknown))
    if value["version"] != VERSION or value["visibility"] != "internal" or value["status"] not in STATUSES:
        raise ValueError("invalid DebateSpec output identity or status")
    _bounded_text(value.get("stage"), "stage")
    if not isinstance(value["arguments"], list) or not isinstance(value["counterarguments"], list):
        raise ValueError("DebateSpec arguments must be lists")
    argument_ids: set[str] = set()
    claim_ids: set[str] = set()
    for row in value["arguments"]:
        if not isinstance(row, dict):
            raise ValueError("DebateSpec arguments must contain objects")
        _validate_argument(row, "argument")
        _bounded_text(row["role"], "argument.role")
        if row["argument_id"] in argument_ids:
            raise ValueError("DebateSpec argument ids must be unique")
        argument_ids.add(row["argument_id"])
        claim_ids.add(row["claim_id"])
    counter_ids: set[str] = set()
    for row in value["counterarguments"]:
        if not isinstance(row, dict):
            raise ValueError("DebateSpec counterarguments must contain objects")
        required_counter = {"counterargument_id", "target_claim_id", "statement", "evidence_ids", "status"}
        missing_counter = sorted(required_counter - set(row))
        if missing_counter:
            raise ValueError("counterargument missing: " + ", ".join(missing_counter))
        if row["counterargument_id"] in counter_ids:
            raise ValueError("DebateSpec counterargument ids must be unique")
        counter_ids.add(row["counterargument_id"])
        _bounded_text(row["counterargument_id"], "counterargument.counterargument_id")
        _bounded_text(row["target_claim_id"], "counterargument.target_claim_id")
        _bounded_text(row["statement"], "counterargument.statement")
        _refs(row["evidence_ids"], "counterargument.evidence_ids", required=True)
        if row["status"] not in {"supports", "refutes", "unresolved"}:
            raise ValueError("counterargument.status is invalid")
        if row["target_claim_id"] not in claim_ids:
            raise ValueError("counterargument must target a known claim_id")
    if not isinstance(value["conflicts"], list):
        raise ValueError("DebateSpec conflicts must be a list")
    for row in value["conflicts"]:
        if not isinstance(row, dict) or not str(row.get("claim_id") or ""):
            raise ValueError("DebateSpec conflicts must target claim_id")
        _bounded_text(row["claim_id"], "conflict.claim_id")
        if row["claim_id"] not in claim_ids:
            raise ValueError("DebateSpec conflict must target a known claim_id")
        _refs(row.get("evidence_ids"), "conflict.evidence_ids", required=True)
        if row.get("state") != "unresolved":
            raise ValueError("DebateSpec conflicts must be unresolved")
        _bounded_text(row.get("reason"), "conflict.reason")
    coordination = value["coordination"]
    if not isinstance(coordination, dict) or coordination.get("state") not in COORDINATION_STATES:
        raise ValueError("DebateSpec coordination state is invalid")
    if not isinstance(coordination.get("issues"), list) or any(not isinstance(item, dict) for item in coordination["issues"]):
        raise ValueError("DebateSpec coordination.issues must be a list")
    if not isinstance(value["provenance"], dict) or not isinstance(value["provenance"].get("input_sha256"), str) or not value["provenance"]["input_sha256"].strip():
        raise ValueError("DebateSpec output requires input provenance")
    if not isinstance(value["permissions"], dict) or value["permissions"].get("write_permissions") != []:
        raise ValueError("DebateSpec output is read-only")
    if "read_permissions" in value["permissions"] and not isinstance(value["permissions"]["read_permissions"], list):
        raise ValueError("DebateSpec permissions.read_permissions must be a list")
    _walk_forbidden(value)


def build_input(packet: dict[str, Any], *, stage: str) -> dict[str, Any]:
    agent_contract = packet.get("agent_contract")
    if not isinstance(agent_contract, dict):
        raise ValueError("DebateSpec requires AgentContractSpec input")
    validate_agent_input(agent_contract)
    roles = packet.get("agent_role_inputs") or []
    if not isinstance(roles, list) or not roles:
        raise ValueError("DebateSpec requires AgentRoleSpec inputs")
    snapshot = (agent_contract.get("evidence_snapshot") or {}).get("snapshot_id") or "snapshot:pending"
    value = {
        "contract": CONTRACT, "version": VERSION, "stage": stage, "visibility": "internal",
        "evidence_snapshot_refs": [str(snapshot)],
        "role_input_refs": [sha256(row) for row in roles],
        "provenance": {"cycle_id": packet.get("cycle_id"), "packet_sha256": packet.get("sha256"),
                       "as_of": agent_contract["controlled_context"]["as_of"]},
        "permissions": {"write_permissions": [], "read_permissions": ["frozen_evidence", "agent_role_outputs"]},
    }
    validate_input(value)
    return value


def _evidence_ids(row: dict[str, Any]) -> list[str]:
    values = row.get("evidence_ids") or row.get("evidence_refs") or []
    if not values and row.get("evidence_id"):
        values = [row["evidence_id"]]
    if not values and row.get("evidence_ref"):
        values = [row["evidence_ref"]]
    return sorted({str(item) for item in values if str(item).strip()})


def _position(row: dict[str, Any], role: str) -> str:
    position = str(row.get("position") or row.get("effect") or "").lower()
    if position in POSITIONS:
        return position
    if role == "bull":
        return "support"
    if role == "bear":
        return "oppose"
    return "unknown"


def build_output(input_contract: dict[str, Any], *, status: str, arguments: list[dict[str, Any]],
                 counterarguments: list[dict[str, Any]] | None = None,
                 conflicts: list[dict[str, Any]] | None = None,
                 coordination: dict[str, Any] | None = None,
                 provenance: dict[str, Any] | None = None) -> dict[str, Any]:
    validate_input(input_contract)
    value = {
        "contract": CONTRACT, "version": VERSION, "stage": input_contract["stage"],
        "visibility": "internal", "status": status, "arguments": copy.deepcopy(arguments),
        "counterarguments": copy.deepcopy(counterarguments or []), "conflicts": copy.deepcopy(conflicts or []),
        "coordination": copy.deepcopy(coordination or {"state": "unknown", "issues": []}),
        "provenance": {**copy.deepcopy(provenance or {}), "input_sha256": sha256(input_contract)},
        "permissions": {"write_permissions": [], "read_permissions": ["frozen_evidence", "agent_role_outputs"]},
    }
    validate_output(value)
    return value


def from_stage(packet: dict[str, Any], data: dict[str, Any], verifier: dict[str, Any], *,
               attempt_id: str, status_override: str | None = None) -> dict[str, Any]:
    """Build the bounded debate envelope from a verified stage result."""
    input_contract = build_input(packet, stage=str(packet.get("stage") or "unknown"))
    arguments: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for index, row in enumerate(data.get("propositions") or []):
        if not isinstance(row, dict):
            continue
        claim_id = str(row.get("claim_id") or row.get("id") or "").strip()
        evidence_ids = _evidence_ids(row)
        if not claim_id or not evidence_ids:
            issues.append({"state": "evidence_insufficient", "claim_id": claim_id or None,
                           "evidence_ids": evidence_ids, "reason": "argument_requires_claim_and_evidence"})
            continue
        role = str(row.get("role") or "coordinator")
        arguments.append({
            "argument_id": str(row.get("argument_id") or f"argument-{index + 1}"),
            "role": role, "claim_id": claim_id, "position": _position(row, role),
            "statement": str(row.get("text") or row.get("statement") or claim_id),
            "evidence_ids": evidence_ids,
        })
    claim_ids = {row["claim_id"] for row in arguments}
    counterarguments: list[dict[str, Any]] = []
    for index, row in enumerate(data.get("counterarguments") or data.get("counterevidence") or []):
        if not isinstance(row, dict):
            continue
        target = str(row.get("target_claim_id") or row.get("claim_id") or "").strip()
        evidence_ids = _evidence_ids(row)
        if not target or target not in claim_ids or not evidence_ids:
            issues.append({"state": "evidence_insufficient", "claim_id": target or None,
                           "evidence_ids": evidence_ids, "reason": "counterargument_requires_target_and_evidence"})
            continue
        counterarguments.append({
            "counterargument_id": str(row.get("counterargument_id") or f"counterargument-{index + 1}"),
            "target_claim_id": target,
            "statement": str(row.get("statement") or row.get("text") or "counterevidence"),
            "evidence_ids": evidence_ids,
            "status": str(row.get("status") or "unresolved"),
        })
    conflicts: list[dict[str, Any]] = []
    for row in data.get("conflicts") or []:
        if not isinstance(row, dict):
            continue
        claim_id = str(row.get("claim_id") or "").strip()
        refs = _evidence_ids(row) or [str(item) for item in row.get("competing_evidence_refs") or []]
        if claim_id not in claim_ids or not refs:
            issues.append({"state": "conflict_unresolved", "claim_id": claim_id or None,
                           "evidence_ids": refs, "reason": "conflict_requires_specific_claim_and_evidence"})
            continue
        conflicts.append({"claim_id": claim_id, "evidence_ids": sorted(set(refs)), "state": "unresolved",
                          "reason": str(row.get("reason") or row.get("materiality") or "competing evidence")})
    if status_override:
        status = status_override
    elif conflicts:
        status = "conflict_unresolved"
    elif issues or not arguments:
        status = "evidence_insufficient"
    elif not verifier.get("passed"):
        status = "partial"
    else:
        status = "succeeded"
    state = status if status in COORDINATION_STATES else ("ready" if status == "succeeded" else "unknown")
    return build_output(
        input_contract, status=status, arguments=arguments, counterarguments=counterarguments,
        conflicts=conflicts,
        coordination={"state": state, "issues": issues},
        provenance={"attempt_id": attempt_id, "packet_sha256": packet.get("sha256"),
                    "source_output_sha256": sha256(data), "verifier_passed": bool(verifier.get("passed"))},
    )


def failure(packet: dict[str, Any], *, stage: str, status: str, attempt_id: str, reason: str) -> dict[str, Any] | None:
    if not isinstance(packet.get("agent_contract"), dict) or not packet.get("agent_role_inputs"):
        return None
    return build_output(
        build_input(packet, stage=stage), status=status, arguments=[], coordination={
            "state": status, "issues": [{"state": status, "reason": reason, "evidence_ids": []}],
        }, provenance={"attempt_id": attempt_id, "packet_sha256": packet.get("sha256")},
    )


def frozen_replay(input_contract: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
    validate_input(input_contract)
    validate_output(output)
    input_hash = sha256(input_contract)
    if output["provenance"].get("input_sha256") != input_hash:
        raise ValueError("DebateSpec replay provenance mismatch")
    result = {
        "contract": REPLAY_CONTRACT, "version": VERSION, "stage": output["stage"],
        "source_input_sha256": input_hash, "source_output_sha256": sha256(output),
        "qualification": {"valid": True, "status": output["status"],
                           "coordination_state": output["coordination"]["state"],
                           "argument_count": len(output["arguments"]),
                           "counterargument_count": len(output["counterarguments"])},
        "evaluation_vector": {
            "delivery_speed": {"state": "not_measured_in_frozen_replay"},
            "qualification_probability": {"state": "not_estimated_in_frozen_replay"},
            "research_quality": {"argument_count": len(output["arguments"]), "conflict_count": len(output["conflicts"])},
            "judgment_outcome": {"status": output["status"], "coordination_state": output["coordination"]["state"]},
            "safety_reliability": {"read_only": output["permissions"]["write_permissions"] == [],
                                    "internal_visibility": output["visibility"] == "internal",
                                    "claim_evidence_bound": all(row["evidence_ids"] for row in output["arguments"])},
        },
    }
    return result


def install_qualification() -> dict[str, Any]:
    from .agent_contract import build_input as build_agent_input
    packet = {"cycle_id": "debate-install", "stage": "m1_research", "as_of": "2026-01-01T00:00:00Z",
              "evidence_snapshot": {"snapshot_id": "install-snapshot", "as_of": "2026-01-01T00:00:00Z"},
              "memories": [], "sha256": "install-packet"}
    agent = build_agent_input(packet, capability="research:m1_research")
    role = {"contract": "AgentRoleSpec/v1", "version": 1, "role": "coordinator", "stage": "m1_research",
            "visibility": "internal", "agent_contract_sha256": sha256(agent),
            "input_refs": {"evidence_snapshot": ["install-snapshot"], "public_evidence": ["install-evidence"]},
            "provenance": {"as_of": packet["as_of"]}, "permissions": {"write_permissions": [], "read_permissions": []}}
    # The install fixture is intentionally small but still exercises the full
    # claim -> evidence -> coordinator chain.
    frozen = {**packet, "agent_contract": agent, "agent_role_inputs": [role]}
    output = from_stage(frozen, {"propositions": [{"id": "claim-install", "text": "fixture", "evidence_refs": ["evidence-install"]}]}, {"passed": True}, attempt_id="install-attempt")
    first = frozen_replay(build_input(frozen, stage="m1_research"), output)
    second = frozen_replay(copy.deepcopy(build_input(frozen, stage="m1_research")), copy.deepcopy(output))
    return {"contract": "DebateInstallQualification/v1", "qualified": first == second,
            "replay_sha256": sha256(first), "evaluation_vector": first["evaluation_vector"],
            "source_input_sha256": first["source_input_sha256"], "source_output_sha256": first["source_output_sha256"]}


if __name__ == "__main__":
    print(canonical_json(install_qualification()))
