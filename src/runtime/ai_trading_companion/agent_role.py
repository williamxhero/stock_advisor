"""Runtime-owned AgentRoleSpec/v1.

Agent roles are internal capability boundaries, not user-facing personas.  A
role may read a bounded, versioned packet and return a bounded artifact, but
it never owns a market fact, portfolio fact, MemoryHub record, schedule,
production strategy, or published message.
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from .agent_contract import CONTRACT as AGENT_CONTRACT
from .agent_contract import VERSION as AGENT_CONTRACT_VERSION
from .agent_contract import validate_input as validate_agent_input

CONTRACT = "AgentRoleSpec/v1"
VERSION = 1
REPLAY_CONTRACT = "AgentRoleReplay/v1"
INTERNAL_VISIBILITY = "internal"
ROLE_IDS = (
    "fundamental",
    "market_structure",
    "news_event",
    "propagation_sentiment",
    "bull",
    "bear",
    "risk",
    "coordinator",
)
STAGES = frozenset({"m0_research", "m1_research", "m2", "outcome_research", "chat_research"})
STAGE_ROLES: dict[str, tuple[str, ...]] = {
    "m0_research": ("fundamental", "market_structure", "news_event", "propagation_sentiment", "risk", "coordinator"),
    "m1_research": ROLE_IDS,
    "m2": ("coordinator",),
    "outcome_research": ("fundamental", "market_structure", "news_event", "propagation_sentiment", "risk", "coordinator"),
    "chat_research": ("fundamental", "market_structure", "news_event", "propagation_sentiment", "risk", "coordinator"),
}
STATUSES = frozenset({"succeeded", "partial", "blocked", "failed", "unknown"})
EFFECTS = frozenset({"evidence_only", "support", "oppose", "block", "coordinate", "unknown"})

_FORBIDDEN_KEYS = frozenset({
    "chain_of_thought", "cot", "thoughts", "reasoning_trace", "private_reasoning",
    "scratchpad", "deliberation", "hidden_reasoning", "h0", "h0_text",
    "h0_propositions", "h0_actions", "m2_draft", "credentials", "api_key",
})
_FORBIDDEN_WRITES = frozenset({
    "markethub", "markethub_facts", "portfolio", "positions", "memoryhub",
    "memoryhub_formal_records", "schedule", "production_strategy", "published_messages",
    "exchange", "orders", "broker",
})
_M1_FORBIDDEN_KEYS = frozenset({
    "h0", "h0_text", "h0_propositions", "h0_actions", "m2", "m2_draft",
    "user_chat_after_cutoff", "post_h0_chat", "private_facts_after_h0",
})


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _walk_keys(value: Any, *, path: str = "artifact") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).casefold()
            if normalized in _FORBIDDEN_KEYS:
                raise ValueError(f"AgentRoleSpec forbids protected context at {path}.{key}")
            if normalized in _FORBIDDEN_WRITES:
                raise ValueError(f"AgentRoleSpec forbids fact-system writes at {path}.{key}")
            _walk_keys(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk_keys(child, path=f"{path}[{index}]")


def _find_forbidden(value: Any, names: frozenset[str]) -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).casefold() in names:
                return str(key)
            found = _find_forbidden(child, names)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_forbidden(child, names)
            if found:
                return found
    return None


def _refs(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{field} must be a list of non-empty references")
    return sorted(set(value))


def _definition(
    role: str,
    *,
    allowed_inputs: tuple[str, ...],
    forbidden_actions: tuple[str, ...],
    output_kinds: tuple[str, ...],
    effects: tuple[str, ...],
    may_block: bool = False,
) -> dict[str, Any]:
    return {
        "contract": CONTRACT,
        "version": VERSION,
        "role": role,
        "visibility": INTERNAL_VISIBILITY,
        "allowed_inputs": list(allowed_inputs),
        "forbidden_actions": list(forbidden_actions),
        "output_kinds": list(output_kinds),
        "allowed_effects": list(effects),
        "may_block": may_block,
        "write_permissions": [],
    }


_COMMON_FORBIDDEN = (
    "write_market_facts", "write_portfolio_facts", "write_memoryhub_records",
    "write_schedule", "write_production_strategy", "publish_user_message",
    "place_order", "change_permissions", "expose_internal_role",
)

ROLE_SPECS: dict[str, dict[str, Any]] = {
    "fundamental": _definition(
        "fundamental", allowed_inputs=("evidence_snapshot", "public_market_facts", "financial_evidence", "quantresearch_readonly"),
        forbidden_actions=_COMMON_FORBIDDEN + ("invent_missing_financial_fields",),
        output_kinds=("claim", "unknown", "risk"), effects=("evidence_only", "support", "unknown"),
    ),
    "market_structure": _definition(
        "market_structure", allowed_inputs=("evidence_snapshot", "public_market_facts", "price_structure", "market_breadth", "quantresearch_readonly"),
        forbidden_actions=_COMMON_FORBIDDEN + ("turn_one_quote_into_market_direction",),
        output_kinds=("claim", "unknown", "risk"), effects=("evidence_only", "support", "oppose", "unknown"),
    ),
    "news_event": _definition(
        "news_event", allowed_inputs=("evidence_snapshot", "public_market_facts", "news_evidence", "event_timeline"),
        forbidden_actions=_COMMON_FORBIDDEN + ("promote_unverified_news_to_fact",),
        output_kinds=("claim", "unknown", "risk"), effects=("evidence_only", "support", "oppose", "unknown"),
    ),
    "propagation_sentiment": _definition(
        "propagation_sentiment", allowed_inputs=("evidence_snapshot", "public_market_facts", "propagation_evidence", "sentiment_proxies"),
        forbidden_actions=_COMMON_FORBIDDEN + ("infer_truth_from_propagation",),
        output_kinds=("claim", "unknown", "risk"), effects=("evidence_only", "support", "oppose", "unknown"),
    ),
    "bull": _definition(
        "bull", allowed_inputs=("evidence_snapshot", "public_evidence", "role_outputs", "quantresearch_readonly"),
        forbidden_actions=_COMMON_FORBIDDEN + ("suppress_counterevidence", "publish_a_trade_instruction"),
        output_kinds=("claim", "reasoning", "risk", "unknown"), effects=("support", "unknown"),
    ),
    "bear": _definition(
        "bear", allowed_inputs=("evidence_snapshot", "public_evidence", "role_outputs", "quantresearch_readonly"),
        forbidden_actions=_COMMON_FORBIDDEN + ("suppress_supporting_evidence", "publish_a_trade_instruction"),
        output_kinds=("claim", "reasoning", "risk", "unknown"), effects=("oppose", "unknown"),
    ),
    "risk": _definition(
        "risk", allowed_inputs=("evidence_snapshot", "public_evidence", "role_outputs", "portfolio_snapshot_readonly", "risk_policy"),
        forbidden_actions=_COMMON_FORBIDDEN + ("change_risk_policy", "approve_missing_facts"),
        output_kinds=("risk", "unknown", "claim"), effects=("block", "oppose", "support", "unknown"), may_block=True,
    ),
    "coordinator": _definition(
        "coordinator", allowed_inputs=("evidence_snapshot", "public_evidence", "role_outputs", "risk_policy", "h0_frozen"),
        forbidden_actions=_COMMON_FORBIDDEN + ("rewrite_role_artifacts", "hide_unresolved_conflict"),
        output_kinds=("conclusion", "claim", "reasoning", "risk", "unknown"), effects=("coordinate", "block", "support", "oppose", "unknown"), may_block=True,
    ),
}


def role_definition(role: str) -> dict[str, Any]:
    try:
        return copy.deepcopy(ROLE_SPECS[role])
    except KeyError as exc:
        raise ValueError(f"unsupported AgentRoleSpec role: {role}") from exc


def _validate_common_identity(value: dict[str, Any], *, kind: str) -> None:
    if not isinstance(value, dict) or value.get("contract") != CONTRACT:
        raise ValueError(f"unsupported AgentRoleSpec {kind}")
    if value.get("version") != VERSION or value.get("visibility") != INTERNAL_VISIBILITY:
        raise ValueError(f"invalid AgentRoleSpec {kind} identity")
    role = value.get("role")
    if role not in ROLE_SPECS:
        raise ValueError("invalid AgentRoleSpec role")
    stage = value.get("stage")
    if stage not in STAGES:
        raise ValueError("invalid AgentRoleSpec stage")
    _walk_keys(value)


def validate_input(value: dict[str, Any]) -> None:
    _validate_common_identity(value, kind="input")
    required = {"contract", "version", "role", "stage", "visibility", "agent_contract_sha256", "input_refs", "provenance", "permissions"}
    missing = sorted(required - set(value))
    if missing:
        raise ValueError("AgentRoleSpec input missing: " + ", ".join(missing))
    unknown_fields = sorted(set(value) - required)
    if unknown_fields:
        raise ValueError("AgentRoleSpec input contains unsupported fields: " + ", ".join(unknown_fields))
    if not isinstance(value["agent_contract_sha256"], str) or not value["agent_contract_sha256"]:
        raise ValueError("AgentRoleSpec input requires AgentContractSpec provenance")
    refs = value["input_refs"]
    if not isinstance(refs, dict):
        raise TypeError("AgentRoleSpec input_refs must be an object")
    allowed = set(ROLE_SPECS[value["role"]]["allowed_inputs"])
    unknown = sorted(set(refs) - allowed)
    if unknown:
        raise ValueError(f"AgentRoleSpec input contains disallowed inputs: {', '.join(unknown)}")
    for key, rows in refs.items():
        _refs(rows, f"input_refs.{key}")
    if not isinstance(value["permissions"], dict) or value["permissions"].get("write_permissions") != []:
        raise ValueError("AgentRoleSpec roles are read-only")
    if not isinstance(value["provenance"], dict) or not str(value["provenance"].get("as_of") or ""):
        raise ValueError("AgentRoleSpec input requires provenance.as_of")
    if value["stage"] == "m1_research":
        forbidden = _find_forbidden(value, _M1_FORBIDDEN_KEYS)
        if forbidden:
            raise ValueError(f"M1 role input exposes forbidden context: {forbidden}")


def validate_output(value: dict[str, Any]) -> None:
    _validate_common_identity(value, kind="output")
    required = {"contract", "version", "role", "stage", "visibility", "status", "decision_effect", "propositions", "evidence_refs", "counterevidence_refs", "risks", "unknowns", "provenance", "permissions"}
    missing = sorted(required - set(value))
    if missing:
        raise ValueError("AgentRoleSpec output missing: " + ", ".join(missing))
    unknown_fields = sorted(set(value) - required)
    if unknown_fields:
        raise ValueError("AgentRoleSpec output contains unsupported fields: " + ", ".join(unknown_fields))
    if value["status"] not in STATUSES:
        raise ValueError("invalid AgentRoleSpec status")
    definition = ROLE_SPECS[value["role"]]
    if value["decision_effect"] not in definition["allowed_effects"]:
        raise ValueError("AgentRoleSpec decision effect is not allowed for role")
    if not isinstance(value["permissions"], dict) or value["permissions"].get("write_permissions") != []:
        raise ValueError("AgentRoleSpec outputs are read-only")
    for field in ("evidence_refs", "counterevidence_refs"):
        _refs(value[field], field)
    for field in ("propositions", "risks", "unknowns"):
        if not isinstance(value[field], list) or any(not isinstance(row, dict) for row in value[field]):
            raise ValueError(f"AgentRoleSpec {field} must contain structured objects")
    proposition_ids: set[str] = set()
    for proposition in value["propositions"]:
        identifier = str(proposition.get("id") or "")
        kind = str(proposition.get("kind") or "")
        if not identifier or identifier in proposition_ids or kind not in definition["output_kinds"]:
            raise ValueError("AgentRoleSpec propositions require unique ids and an allowed kind")
        proposition_ids.add(identifier)
        _refs(proposition.get("evidence_refs"), "proposition.evidence_refs")
        _refs(proposition.get("counterevidence_refs"), "proposition.counterevidence_refs")
        if kind in {"claim", "conclusion"} and not proposition["evidence_refs"]:
            raise ValueError(f"AgentRoleSpec proposition {identifier} must cite evidence")
    if not isinstance(value["provenance"], dict) or not str(value["provenance"].get("input_sha256") or ""):
        raise ValueError("AgentRoleSpec output requires input provenance")
    if any(len(text) > 4_000 for text in _strings(value)):
        raise ValueError("AgentRoleSpec contains an unbounded narrative field")


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def build_input(
    agent_contract: dict[str, Any],
    *,
    role: str,
    stage: str,
    input_refs: dict[str, list[str]],
    provenance: dict[str, Any],
) -> dict[str, Any]:
    validate_agent_input(agent_contract)
    if agent_contract.get("contract") != AGENT_CONTRACT or agent_contract.get("version") != AGENT_CONTRACT_VERSION:
        raise ValueError("AgentRoleSpec requires AgentContractSpec/v1 input")
    definition = role_definition(role)
    value = {
        "contract": CONTRACT,
        "version": VERSION,
        "role": role,
        "stage": stage,
        "visibility": INTERNAL_VISIBILITY,
        "agent_contract_sha256": sha256(agent_contract),
        "input_refs": copy.deepcopy(input_refs),
        "provenance": {**copy.deepcopy(provenance), "as_of": provenance.get("as_of") or agent_contract["controlled_context"]["as_of"]},
        "permissions": {"write_permissions": [], "read_permissions": list(definition["allowed_inputs"])},
    }
    validate_input(value)
    return value


def build_output(
    role_input: dict[str, Any],
    *,
    status: str,
    decision_effect: str,
    propositions: list[dict[str, Any]] | None = None,
    evidence_refs: list[str] | None = None,
    counterevidence_refs: list[str] | None = None,
    risks: list[dict[str, Any]] | None = None,
    unknowns: list[dict[str, Any]] | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_input(role_input)
    value = {
        "contract": CONTRACT,
        "version": VERSION,
        "role": role_input["role"],
        "stage": role_input["stage"],
        "visibility": INTERNAL_VISIBILITY,
        "status": status,
        "decision_effect": decision_effect,
        "propositions": copy.deepcopy(propositions or []),
        "evidence_refs": sorted(set(evidence_refs or [])),
        "counterevidence_refs": sorted(set(counterevidence_refs or [])),
        "risks": copy.deepcopy(risks or []),
        "unknowns": copy.deepcopy(unknowns or []),
        "provenance": {**copy.deepcopy(provenance or {}), "input_sha256": sha256(role_input)},
        "permissions": {"write_permissions": [], "read_permissions": list(ROLE_SPECS[role_input["role"]]["allowed_inputs"])},
    }
    validate_output(value)
    return value


def build_role_inputs(agent_contract: dict[str, Any], *, stage: str, input_refs: dict[str, list[str]], provenance: dict[str, Any]) -> list[dict[str, Any]]:
    """Create the deterministic internal roster for a stage.

    The roster contains references, not copied private facts.  M1 therefore
    cannot accidentally inherit H0 through a role bundle.
    """
    if stage not in STAGES:
        raise ValueError("unsupported AgentRoleSpec stage")
    roles: list[dict[str, Any]] = []
    for role in STAGE_ROLES[stage]:
        refs = {key: list(values) for key, values in input_refs.items() if key in ROLE_SPECS[role]["allowed_inputs"]}
        if stage != "m2":
            refs.pop("h0_frozen", None)
        roles.append(build_input(agent_contract, role=role, stage=stage, input_refs=refs, provenance=provenance))
    return roles


def attach_role_inputs(packet: dict[str, Any], *, stage: str) -> dict[str, Any]:
    """Attach the internal roster to a research packet and re-hash it.

    Only stable references are attached.  In particular this function does
    not copy H0 or any MemoryHub/portfolio payload into a role input.
    """
    value = copy.deepcopy(packet)
    agent_contract = value.get("agent_contract")
    if not isinstance(agent_contract, dict):
        raise TypeError("AgentRoleSpec requires an attached AgentContractSpec")
    refs: dict[str, list[str]] = {
        "evidence_snapshot": [str((agent_contract.get("evidence_snapshot") or {}).get("snapshot_id") or "snapshot:pending")],
        "public_evidence": [str(value.get("evidence_bundle_sha256") or value.get("sha256") or "packet:pending")],
        "public_market_facts": ["packet:public_market_facts"],
        "financial_evidence": ["packet:financial_evidence"],
        "news_evidence": ["packet:news_evidence"],
        "event_timeline": ["packet:event_timeline"],
        "propagation_evidence": ["packet:propagation_evidence"],
        "sentiment_proxies": ["packet:sentiment_proxies"],
        "price_structure": ["packet:price_structure"],
        "market_breadth": ["packet:market_breadth"],
        "quantresearch_readonly": ["packet:quantresearch_readonly"],
        "role_outputs": ["roles:pending"],
        "risk_policy": ["runtime:risk-policy-v1"],
    }
    if stage == "m2":
        refs["h0_frozen"] = ["cycle:h0-frozen"]
    value["agent_role_inputs"] = build_role_inputs(
        agent_contract, stage=stage, input_refs=refs,
        provenance={
            "cycle_id": value.get("cycle_id"), "attempt_id": value.get("attempt_id"),
            "as_of": agent_contract["controlled_context"]["as_of"],
        },
    )
    value.pop("sha256", None)
    value["sha256"] = sha256(value)
    return value


def internal_artifact(value: dict[str, Any]) -> dict[str, Any]:
    """Return a copy suitable for audit metadata; never a user-facing payload."""
    if value.get("visibility") != INTERNAL_VISIBILITY:
        raise ValueError("AgentRoleSpec artifact must remain internal")
    if value.get("contract") == CONTRACT:
        validate_output(value)
    else:
        validate_input(value)
    return copy.deepcopy(value)


def build_runtime_coordinator_output(
    role_inputs: list[dict[str, Any]],
    *,
    status: str,
    evidence_refs: list[str],
    unknowns: list[str],
    attempt_id: str,
    bundle_sha256: str | None,
) -> dict[str, Any]:
    """Close one runtime research attempt with a bounded coordinator artifact.

    Providers may return their domain-specific result shape, so the runtime
    records a deterministic coordinator envelope even when no provider-side
    role output was emitted.  This is a qualification/status artifact, not a
    replacement for the provider result and never a user-facing persona.
    """
    coordinator = next((item for item in role_inputs if item.get("role") == "coordinator"), None)
    if coordinator is None:
        raise ValueError("AgentRoleSpec coordinator input is missing")
    return build_output(
        coordinator,
        status=status,
        decision_effect="coordinate" if status == "succeeded" else "block",
        evidence_refs=evidence_refs,
        unknowns=[{"description": str(item)} for item in unknowns],
        provenance={"attempt_id": attempt_id, "bundle_sha256": bundle_sha256},
    )


def frozen_replay(role_input: dict[str, Any], role_output: dict[str, Any]) -> dict[str, Any]:
    """Re-qualify a frozen role artifact without changing either source value."""
    validate_input(role_input)
    validate_output(role_output)
    if role_input["role"] != role_output["role"] or role_input["stage"] != role_output["stage"]:
        raise ValueError("AgentRoleSpec replay identity mismatch")
    input_hash = sha256(role_input)
    output_hash = sha256(role_output)
    if role_output["provenance"].get("input_sha256") != input_hash:
        raise ValueError("AgentRoleSpec replay provenance mismatch")
    return {
        "contract": REPLAY_CONTRACT,
        "version": VERSION,
        "role": role_input["role"],
        "stage": role_input["stage"],
        "source_input_sha256": input_hash,
        "source_output_sha256": output_hash,
        "qualification": {
            "valid": True,
            "status": role_output["status"],
            "decision_effect": role_output["decision_effect"],
            "evidence_refs": list(role_output["evidence_refs"]),
            "counterevidence_refs": list(role_output["counterevidence_refs"]),
        },
        "evaluation_vector": {
            "delivery_speed": {"state": "not_measured_in_frozen_replay"},
            "qualification_probability": {"state": "not_estimated_in_frozen_replay"},
            "research_quality": {"structured": True, "evidence_ref_count": len(role_output["evidence_refs"])},
            "judgment_outcome": {"status": role_output["status"], "effect": role_output["decision_effect"]},
            "safety_reliability": {
                "read_only": role_output["permissions"]["write_permissions"] == [],
                "internal_visibility": role_output["visibility"] == INTERNAL_VISIBILITY,
                "m1_h0_blind": role_output["stage"] != "m1_research" or _find_forbidden(role_input, _M1_FORBIDDEN_KEYS) is None,
            },
        },
    }


def install_qualification() -> dict[str, Any]:
    """Run two deterministic, source-independent frozen replays."""
    from .agent_contract import build_input as build_agent_input

    packet = {
        "cycle_id": "agent-role-install", "stage": "m1_research", "as_of": "2026-01-01T00:00:00Z",
        "evidence_snapshot": {"contract": "evidence-snapshot-spec/v1", "snapshot_id": "install-snapshot", "as_of": "2026-01-01T00:00:00Z", "content_hash": "install-evidence"},
        "memories": [], "sha256": "install-packet",
    }
    agent_input = build_agent_input(packet, capability="research:m1_research")
    role_input = build_input(
        agent_input, role="coordinator", stage="m1_research",
        input_refs={"evidence_snapshot": ["install-snapshot"], "public_evidence": ["install-evidence"], "role_outputs": ["roles:install"]},
        provenance={"cycle_id": packet["cycle_id"], "as_of": packet["as_of"]},
    )
    role_output = build_output(
        role_input, status="succeeded", decision_effect="coordinate",
        evidence_refs=["install-evidence"], provenance={"attempt_id": "install-attempt"},
    )
    first = frozen_replay(role_input, role_output)
    second = frozen_replay(copy.deepcopy(role_input), copy.deepcopy(role_output))
    return {
        "contract": "AgentRoleInstallQualification/v1",
        "qualified": first == second,
        "replay_sha256": sha256(first),
        "source_input_sha256": first["source_input_sha256"],
        "source_output_sha256": first["source_output_sha256"],
        "evaluation_vector": first["evaluation_vector"],
    }


if __name__ == "__main__":
    print(canonical_json(install_qualification()))
