"""Runtime-owned AgentContractSpec/v1 for internal research capabilities.

The contract is an auditable decision artifact.  It deliberately contains
references and short structured propositions, never a model's private chain
of thought or an unbounded reasoning transcript.
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any


CONTRACT = "AgentContractSpec/v1"
VERSION = 1
_FORBIDDEN_KEYS = frozenset({
    "chain_of_thought", "cot", "thoughts", "reasoning_trace",
    "private_reasoning", "scratchpad", "deliberation", "hidden_reasoning",
})
_PROPOSITION_KINDS = frozenset({"conclusion", "claim", "reasoning", "unknown"})
_STATUSES = frozenset({"succeeded", "partial", "blocked", "failed", "unknown"})


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _walk_forbidden(value: Any, path: str = "artifact") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).casefold() in _FORBIDDEN_KEYS:
                raise ValueError(f"AgentContractSpec forbids private reasoning at {path}.{key}")
            _walk_forbidden(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk_forbidden(child, f"{path}[{index}]")


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def _refs(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{field} must be a list of non-empty references")
    return sorted(set(value))


def _structured_rows(value: list[Any] | None) -> list[dict[str, Any]]:
    return [copy.deepcopy(row) if isinstance(row, dict) else {"description": str(row)} for row in (value or [])]


def validate_input(value: dict[str, Any]) -> None:
    if not isinstance(value, dict) or value.get("contract") != CONTRACT:
        raise ValueError("unsupported AgentContractSpec input")
    required = {"contract", "version", "capability", "evidence_snapshot", "mandate",
                "controlled_context", "memory_references", "provenance"}
    missing = sorted(required - set(value))
    if missing:
        raise ValueError("AgentContractSpec input missing: " + ", ".join(missing))
    if value["version"] != VERSION or not str(value["capability"]).strip():
        raise ValueError("invalid AgentContractSpec input identity")
    if not isinstance(value["evidence_snapshot"], dict) or not str(value["evidence_snapshot"].get("as_of") or ""):
        raise ValueError("AgentContractSpec input requires an evidence snapshot reference")
    if not isinstance(value["mandate"], dict) or not str(value["mandate"].get("goal") or "").strip():
        raise ValueError("AgentContractSpec input mandate.goal is required")
    if not isinstance(value["controlled_context"], dict) or not str(value["controlled_context"].get("as_of") or ""):
        raise ValueError("AgentContractSpec input controlled_context.as_of is required")
    if not isinstance(value["memory_references"], list):
        raise ValueError("AgentContractSpec memory_references must be a list")
    for row in value["memory_references"]:
        if not isinstance(row, dict) or not str(row.get("reference") or "").strip():
            raise ValueError("memory references require stable reference ids")
    _walk_forbidden(value)


def validate_output(value: dict[str, Any]) -> None:
    if not isinstance(value, dict) or value.get("contract") != CONTRACT:
        raise ValueError("unsupported AgentContractSpec output")
    required = {"contract", "version", "capability", "status", "propositions",
                "evidence", "counterevidence", "risks", "unknowns", "provenance"}
    missing = sorted(required - set(value))
    if missing:
        raise ValueError("AgentContractSpec output missing: " + ", ".join(missing))
    if value["version"] != VERSION or value["status"] not in _STATUSES:
        raise ValueError("invalid AgentContractSpec output identity or status")
    for field in ("propositions", "evidence", "counterevidence", "risks", "unknowns"):
        if not isinstance(value[field], list):
            raise ValueError(f"AgentContractSpec output {field} must be a list")
        if field != "propositions" and any(not isinstance(row, dict) for row in value[field]):
            raise ValueError(f"AgentContractSpec output {field} must contain structured objects")
    proposition_ids: set[str] = set()
    for proposition in value["propositions"]:
        if not isinstance(proposition, dict):
            raise ValueError("propositions must contain objects")
        identifier = str(proposition.get("id") or "")
        kind = str(proposition.get("kind") or "")
        if not identifier or identifier in proposition_ids or kind not in _PROPOSITION_KINDS:
            raise ValueError("propositions require unique ids and a supported kind")
        proposition_ids.add(identifier)
        evidence_refs = _refs(proposition.get("evidence_refs"), "proposition.evidence_refs")
        counter_refs = _refs(proposition.get("counterevidence_refs"), "proposition.counterevidence_refs")
        if kind in {"conclusion", "claim"} and not evidence_refs:
            raise ValueError(f"proposition {identifier} must cite evidence")
        if kind == "unknown" and not str(proposition.get("text") or "").strip():
            raise ValueError(f"unknown proposition {identifier} requires a description")
        proposition["evidence_refs"] = evidence_refs
        proposition["counterevidence_refs"] = counter_refs
    _walk_forbidden(value)
    # A long free-form explanation is not a contract artifact, even if it is
    # hidden under an otherwise acceptable field name.
    if any(len(text) > 4_000 for text in _strings(value)):
        raise ValueError("AgentContractSpec contains an unbounded narrative field")


def build_input(packet: dict[str, Any], *, capability: str) -> dict[str, Any]:
    snapshot = copy.deepcopy(packet.get("evidence_snapshot") or {
        "contract": "evidence-snapshot-spec/v1", "snapshot_id": None,
        "as_of": packet.get("as_of"), "content_hash": None, "state": "pending",
    })
    value = {
        "contract": CONTRACT, "version": VERSION, "capability": capability,
        "evidence_snapshot": snapshot,
        "mandate": {
            "goal": "Acquire and qualify evidence for the internal research capability.",
            "constraints": ["runtime_owned", "as_of_bounded", "structured_artifact_only"],
        },
        "controlled_context": {
            "cycle_id": packet.get("cycle_id"), "stage": packet.get("stage"),
            "as_of": packet.get("as_of"), "allowed_sources": ["packet", "configured_research_backends"],
            "forbidden_channels": ["h0", "published_chat_after_cutoff", "credentials"],
        },
        "memory_references": [
            {"reference": str(row.get("artifact_id") or row.get("memory_id")),
             "kind": "runtime_memory", "as_of": row.get("as_of")}
            for row in packet.get("memories") or []
            if isinstance(row, dict) and (row.get("artifact_id") or row.get("memory_id"))
        ],
        "provenance": {"cycle_id": packet.get("cycle_id"), "packet_sha256": packet.get("sha256")},
    }
    validate_input(value)
    return value


def build_output(input_contract: dict[str, Any], *, capability: str, status: str,
                 propositions: list[dict[str, Any]] | None = None,
                 evidence: list[dict[str, Any]] | None = None,
                 counterevidence: list[dict[str, Any]] | None = None,
                 risks: list[Any] | None = None, unknowns: list[Any] | None = None,
                 provenance: dict[str, Any] | None = None) -> dict[str, Any]:
    validate_input(input_contract)
    value = {
        "contract": CONTRACT, "version": VERSION, "capability": capability,
        "status": status, "propositions": copy.deepcopy(propositions or []),
        "evidence": _structured_rows(evidence), "counterevidence": _structured_rows(counterevidence),
        "risks": _structured_rows(risks), "unknowns": _structured_rows(unknowns),
        "provenance": {**(provenance or {}), "input_contract_sha256": sha256(input_contract)},
    }
    validate_output(value)
    return value


def attach_input(packet: dict[str, Any], *, capability: str) -> dict[str, Any]:
    value = copy.deepcopy(packet)
    value.pop("sha256", None)
    value["agent_contract"] = build_input(value, capability=capability)
    value["sha256"] = sha256(value)
    return value
