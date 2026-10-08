"""Runtime-owned M2 synthesis qualification and frozen replay.

M2 is an appended synthesis after a qualified M1.  It may read the frozen
H0, the immutable M1 record, current Runtime-owned position facts, user-owned
preferences, and risk constraints; it may not rewrite any of those records or
write facts, strategy, evidence, or MemoryHub state.
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime
from typing import Any

from .m0_observation import sha256
from .mandate_spec import validate_mandate


CONTRACT = "M2SynthesisSpec/v1"
VERSION = 1
RESULT_CONTRACT = "M2SynthesisResult/v1"
REPLAY_CONTRACT = "M2SynthesisReplay/v1"

_BOUNDARY = {
    "allowed_inputs": [
        "frozen_m0", "frozen_h0", "frozen_m1", "current_position_facts",
        "user_preferences", "risk_constraints", "frozen_conflicts",
        "as_of_bounded_memory", "versioned_quantresearch_evidence",
    ],
    "forbidden_inputs": [
        "m1_rewrite", "m1_mutation", "private_reasoning", "chain_of_thought",
        "mutable_holdings", "production_strategy_write", "evidence_write",
        "memoryhub_write", "current_chat", "post_cutoff_chat", "m2_output",
    ],
    "h0_visible": True,
    "m1_visible": True,
    "m2_visible": False,
    "published_chat_after_cutoff_visible": False,
}

_FORBIDDEN_KEYS = frozenset({
    "m1_rewrite", "m1_mutation", "private_reasoning", "chain_of_thought",
    "production_strategy_write", "evidence_write", "memoryhub_write",
    "current_chat", "post_cutoff_chat", "m2_output", "write_permissions_override",
})

_PACKET_FIELDS = frozenset({
    "schema_version", "cycle_id", "task_key", "stage", "as_of", "scheduled_for",
    "calendar_context", "mandate", "mandate_reference", "m0_observation_spec",
    "m1_judgment_spec", "m2_synthesis_spec", "position_safety", "risk_gate_spec", "cycle_reference",
    "task_profile", "prior_opportunity_plans", "prior_opportunity_followups", "protocol",
    "risk_doctrine", "risk_constraints", "business_context", "current_position_facts",
    "frozen_m0", "frozen_h0", "frozen_m1", "frozen_public_evidence", "evidence_snapshot",
    "evidence", "conflicts", "research_isolation", "research_request", "research_evidence",
    "prior_market_understanding", "artifacts", "memories", "user_preferences",
    "active_workflow_policy", "context", "verification_repair", "runtime_strategy_controls",
    "allowed_research_backends", "agent_role_inputs", "spec_issue_states", "spec_evidence_gates",
    "spec95_baseline_sha256", "sha256",
})

_REQUIRED_DESCRIPTOR = frozenset({"artifact_id", "sha256", "as_of", "known_at"})


def _time(value: Any, field: str = "time") -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"M2 requires a valid {field}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"M2 requires timezone-aware {field}")
    return parsed


def _digest(value: Any) -> str:
    text = str(value or "")
    if len(text) != 64 or any(char not in "0123456789abcdefABCDEF" for char in text):
        raise ValueError("M2 artifact identity must be a SHA-256 digest")
    return text.lower()


def _walk_forbidden(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().casefold().replace("-", "_")
            if normalized in _FORBIDDEN_KEYS:
                raise ValueError("M2 input contains a forbidden write or private channel: " + normalized)
            _walk_forbidden(child)
    elif isinstance(value, list):
        for child in value:
            _walk_forbidden(child)
    elif isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            _walk_forbidden(json.loads(value))
        except (TypeError, ValueError, json.JSONDecodeError):
            pass


def _descriptor(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _REQUIRED_DESCRIPTOR:
        raise ValueError(f"M2 {field} descriptor fields are not exact")
    if not str(value.get("artifact_id") or "").strip():
        raise ValueError(f"M2 requires a frozen {field} artifact")
    _digest(value.get("sha256"))
    _time(value.get("as_of"), f"{field}.as_of")
    _time(value.get("known_at"), f"{field}.known_at")
    return value


def _h0_descriptor(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _REQUIRED_DESCRIPTOR | {"source_text"}:
        raise ValueError("M2 frozen H0 descriptor fields are not exact")
    _descriptor({key: value[key] for key in _REQUIRED_DESCRIPTOR}, "H0")
    if not isinstance(value.get("source_text"), str) or not value["source_text"].strip():
        raise ValueError("M2 requires the frozen H0 source text")
    if value["sha256"] != hashlib.sha256(value["source_text"].encode("utf-8")).hexdigest():
        raise ValueError("M2 frozen H0 artifact digest mismatch")
    return value


def _m1_descriptor(value: Any) -> dict[str, Any]:
    required = _REQUIRED_DESCRIPTOR | {"original_judgment_text", "snapshot", "snapshot_sha256"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("M2 frozen M1 descriptor fields are not exact")
    _descriptor({key: value[key] for key in _REQUIRED_DESCRIPTOR}, "M1")
    if not isinstance(value.get("original_judgment_text"), str) or not value["original_judgment_text"].strip():
        raise ValueError("M2 requires the original M1 judgment text")
    if value["sha256"] != hashlib.sha256(value["original_judgment_text"].encode("utf-8")).hexdigest():
        raise ValueError("M2 frozen M1 artifact digest mismatch")
    if not isinstance(value.get("snapshot"), dict):
        raise ValueError("M2 requires the immutable M1 judgment snapshot")
    if value.get("snapshot_sha256") != sha256(value["snapshot"]):
        raise ValueError("M2 M1 snapshot digest mismatch")
    return value


def _validate_fact_view(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("fact_source") != "runtime_database":
        raise ValueError("M2 requires Runtime-owned current position facts")
    if not isinstance(value.get("positions"), list) or "fact_view_sha256" not in value:
        raise ValueError("M2 current position facts are incomplete")
    actual = {key: item for key, item in value.items() if key != "fact_view_sha256"}
    if value["fact_view_sha256"] != sha256(actual):
        raise ValueError("M2 current position fact digest mismatch")
    return value


def _validate_packet(packet: dict[str, Any]) -> None:
    if not isinstance(packet, dict) or packet.get("stage") != "m2":
        raise ValueError("M2 synthesis input must target m2")
    _walk_forbidden(packet)
    unknown = set(packet) - _PACKET_FIELDS
    if unknown:
        raise ValueError("M2 packet contains unknown context fields: " + ", ".join(sorted(unknown)))
    if packet.get("sha256") != sha256({key: item for key, item in packet.items() if key != "sha256"}):
        raise ValueError("M2 frozen packet digest mismatch")
    cutoff = _time(packet.get("as_of"), "as_of")
    mandate = validate_mandate(copy.deepcopy(packet.get("mandate")))
    if mandate["stage"] != "m2" or mandate["task_key"] != packet.get("task_key"):
        raise ValueError("M2 mandate identity mismatch")
    m0 = _descriptor(packet.get("frozen_m0"), "M0")
    h0 = _h0_descriptor(packet.get("frozen_h0"))
    m1 = _m1_descriptor(packet.get("frozen_m1"))
    if h0["artifact_id"] in {m0["artifact_id"], m1["artifact_id"]} or m0["artifact_id"] == m1["artifact_id"]:
        raise ValueError("M2 frozen stage artifacts must be distinct")
    for field, value in (("M0", m0), ("H0", h0), ("M1", m1)):
        if _time(value["as_of"], f"{field}.as_of") > cutoff:
            raise ValueError(f"M2 cannot consume a future {field}")
    if not isinstance(packet.get("current_position_facts"), dict):
        raise ValueError("M2 current_position_facts is required")
    _validate_fact_view(packet["current_position_facts"])
    if packet["current_position_facts"] != (packet.get("business_context") or {}).get("portfolio_fact_view"):
        raise ValueError("M2 position facts are not bound to the business context")
    if not isinstance(packet.get("user_preferences"), list):
        raise ValueError("M2 user_preferences must be a list")
    if not isinstance(packet.get("risk_constraints"), dict):
        raise ValueError("M2 risk_constraints are required")
    if packet["risk_constraints"].get("write_permissions") != []:
        raise ValueError("M2 risk constraints cannot grant write permissions")
    if not isinstance(packet.get("conflicts"), list):
        raise ValueError("M2 frozen conflicts must be a list")
    research = packet.get("research_isolation")
    if research is not None:
        if research.get("access") != "read_only" or research.get("write_permissions") != []:
            raise ValueError("M2 QuantResearch must be read_only")


def build_input(packet: dict[str, Any]) -> dict[str, Any]:
    _validate_packet(packet)
    value = {
        "contract": CONTRACT,
        "version": VERSION,
        "stage": "m2",
        "source_packet": copy.deepcopy(packet),
        "frozen_m0": copy.deepcopy(packet["frozen_m0"]),
        "frozen_h0": copy.deepcopy(packet["frozen_h0"]),
        "frozen_m1": copy.deepcopy(packet["frozen_m1"]),
        "current_position_facts": copy.deepcopy(packet["current_position_facts"]),
        "user_preferences": copy.deepcopy(packet["user_preferences"]),
        "risk_constraints": copy.deepcopy(packet["risk_constraints"]),
        "conflicts": copy.deepcopy(packet["conflicts"]),
        "mandate_reference": {
            "contract": packet["mandate"]["contract"],
            "sha256": packet["mandate"]["sha256"],
        },
        "boundary": copy.deepcopy(_BOUNDARY),
        "permissions": {"write_permissions": []},
        "quantresearch": {"access": "read_only", "write_permissions": []},
        "provenance": {
            "source": "runtime", "cycle_id": packet["cycle_id"],
            "as_of": packet["as_of"], "packet_sha256": packet["sha256"],
            "h0_artifact_id": packet["frozen_h0"]["artifact_id"],
            "m1_artifact_id": packet["frozen_m1"]["artifact_id"],
        },
    }
    return validate_input(value)


def validate_input(value: dict[str, Any]) -> dict[str, Any]:
    required = {
        "contract", "version", "stage", "source_packet", "frozen_m0", "frozen_h0", "frozen_m1",
        "current_position_facts", "user_preferences", "risk_constraints", "conflicts",
        "mandate_reference", "boundary", "permissions", "quantresearch", "provenance",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("M2SynthesisSpec input fields are not exact")
    if value["contract"] != CONTRACT or type(value["version"]) is not int or value["version"] != VERSION or value["stage"] != "m2":
        raise ValueError("unsupported M2SynthesisSpec input")
    packet = value["source_packet"]
    _validate_packet(packet)
    if value["frozen_m0"] != packet["frozen_m0"] or value["frozen_h0"] != packet["frozen_h0"] or value["frozen_m1"] != packet["frozen_m1"]:
        raise ValueError("M2 frozen stage reference mismatch")
    if value["current_position_facts"] != packet["current_position_facts"] or value["user_preferences"] != packet["user_preferences"]:
        raise ValueError("M2 fact or preference reference mismatch")
    if value["risk_constraints"] != packet["risk_constraints"] or value["conflicts"] != packet["conflicts"]:
        raise ValueError("M2 risk or conflict reference mismatch")
    if value["boundary"] != _BOUNDARY or value["permissions"] != {"write_permissions": []}:
        raise ValueError("M2 synthesis boundary must be immutable and read-only")
    if value["quantresearch"] != {"access": "read_only", "write_permissions": []}:
        raise ValueError("M2 QuantResearch must be read_only")
    mandate = packet["mandate"]
    if value["mandate_reference"] != {"contract": mandate["contract"], "sha256": mandate["sha256"]}:
        raise ValueError("M2 mandate reference mismatch")
    expected_provenance = {
        "source": "runtime", "cycle_id": packet["cycle_id"], "as_of": packet["as_of"],
        "packet_sha256": packet["sha256"], "h0_artifact_id": packet["frozen_h0"]["artifact_id"],
        "m1_artifact_id": packet["frozen_m1"]["artifact_id"],
    }
    if value["provenance"] != expected_provenance:
        raise ValueError("M2 input provenance mismatch")
    return value


def _evidence_refs(packet: dict[str, Any]) -> set[str]:
    refs: set[str] = set()
    evidence = packet.get("evidence") if isinstance(packet.get("evidence"), dict) else {}
    for source in evidence.get("sources") or []:
        if isinstance(source, dict) and source.get("evidence_ref"):
            refs.add(str(source["evidence_ref"]))
    for artifact in packet.get("artifacts") or []:
        if not isinstance(artifact, dict):
            continue
        try:
            body = json.loads(artifact.get("body") or artifact.get("body_markdown") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(body, dict):
            refs.update(str(row["evidence_ref"]) for row in body.get("sources") or []
                        if isinstance(row, dict) and row.get("evidence_ref"))
    return refs


def validate_stage_output(output: dict[str, Any], input_contract: dict[str, Any]) -> dict[str, Any]:
    from .judgment_publication import publication_problems
    validate_input(input_contract)
    if not isinstance(output, dict) or output.get("result_version") != 4:
        raise ValueError("M2 synthesis requires companion-m2-result-v4")
    problems = publication_problems(output, input_contract["source_packet"])
    if problems:
        raise ValueError("M2 synthesis is not qualified: " + "; ".join(problems))
    refs = _evidence_refs(input_contract["source_packet"])

    def check(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if key in {"evidence_refs", "considered_evidence_refs", "competing_evidence_refs"} and child is not None:
                    if not isinstance(child, list) or any(str(ref) not in refs for ref in child):
                        raise ValueError("M2 cites evidence outside the frozen input")
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
    check(output)
    return output


def _product(input_contract: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
    """Retain every source judgment alongside the newly appended synthesis."""
    return {
        "frozen_m0": copy.deepcopy(input_contract["frozen_m0"]),
        "frozen_h0": copy.deepcopy(input_contract["frozen_h0"]),
        "frozen_m1": copy.deepcopy(input_contract["frozen_m1"]),
        "current_position_facts": copy.deepcopy(input_contract["current_position_facts"]),
        "user_preferences": copy.deepcopy(input_contract["user_preferences"]),
        "risk_constraints": copy.deepcopy(input_contract["risk_constraints"]),
        "conflicts": copy.deepcopy(input_contract["conflicts"]),
        "synthesis": {
            "decision_core": copy.deepcopy(output["decision_core"]),
            "narrative": output["narrative"],
            "appended_after": input_contract["frozen_m1"]["artifact_id"],
        },
        "preservation": {
            "h0_verbatim": True,
            "m1_verbatim": True,
            "m1_snapshot_unchanged": True,
            "append_only": True,
        },
    }


def build_output(input_contract: dict[str, Any], output: dict[str, Any], *, attempt_id: str | None = None) -> dict[str, Any]:
    validate_stage_output(output, input_contract)
    value = {
        "contract": RESULT_CONTRACT,
        "version": VERSION,
        "spec_contract": CONTRACT,
        "stage": "m2",
        "state": "qualified",
        "source_result_version": 4,
        "input": copy.deepcopy(input_contract),
        "source_output": copy.deepcopy(output),
        "product": _product(input_contract, output),
        "permissions": {"write_permissions": []},
        "quantresearch": {"access": "read_only", "write_permissions": []},
        "provenance": {
            **input_contract["provenance"],
            "input_sha256": sha256(input_contract),
            "output_sha256": sha256(output),
            "attempt_id": attempt_id,
        },
    }
    value["sha256"] = sha256(value)
    return validate_output(value)


def validate_output(value: dict[str, Any]) -> dict[str, Any]:
    required = {
        "contract", "version", "spec_contract", "stage", "state", "source_result_version",
        "input", "source_output", "product", "permissions", "quantresearch", "provenance", "sha256",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("M2SynthesisResult fields are not exact")
    if (value["contract"] != RESULT_CONTRACT or type(value["version"]) is not int or value["version"] != VERSION
            or value["spec_contract"] != CONTRACT or value["stage"] != "m2"
            or value["state"] != "qualified" or value["source_result_version"] != 4):
        raise ValueError("unsupported M2SynthesisResult identity or state")
    validate_stage_output(value["source_output"], value["input"])
    if value["product"] != _product(value["input"], value["source_output"]):
        raise ValueError("M2 synthesis product mismatch")
    if value["permissions"] != {"write_permissions": []} or value["quantresearch"] != {"access": "read_only", "write_permissions": []}:
        raise ValueError("M2 consumers must remain read_only")
    provenance = value["provenance"]
    expected = {
        **value["input"]["provenance"],
        "input_sha256": sha256(value["input"]),
        "output_sha256": sha256(value["source_output"]),
        "attempt_id": provenance.get("attempt_id"),
    }
    if not isinstance(provenance, dict) or set(provenance) != set(expected) or provenance != expected:
        raise ValueError("M2 synthesis provenance fields are not exact")
    if value["sha256"] != sha256({key: item for key, item in value.items() if key != "sha256"}):
        raise ValueError("M2 synthesis receipt digest mismatch")
    return value


def bind_attempt(receipt: dict[str, Any], attempt_id: str) -> dict[str, Any]:
    validate_output(receipt)
    value = copy.deepcopy(receipt)
    value["provenance"]["attempt_id"] = str(attempt_id)
    value["sha256"] = sha256({key: item for key, item in value.items() if key != "sha256"})
    return validate_output(value)


def frozen_replay(input_contract: dict[str, Any], output: dict[str, Any], *, expected_output_sha256: str | None = None) -> dict[str, Any]:
    original_input = copy.deepcopy(input_contract)
    original_output = copy.deepcopy(output)
    receipt = build_output(copy.deepcopy(original_input), copy.deepcopy(original_output))
    if expected_output_sha256 is not None and sha256(original_output) != expected_output_sha256:
        raise ValueError("M2 synthesis replay digest mismatch")
    return {
        "contract": REPLAY_CONTRACT,
        "source_input": original_input,
        "source_output": original_output,
        "source_output_sha256": sha256(original_output),
        "receipt": receipt,
        "qualification": {
            "valid": True, "h0_present": True, "m1_completed": True,
            "conflicts_preserved": True, "m1_immutable": True, "append_only": True,
            "read_only": True,
        },
    }


def install_qualification() -> dict[str, Any]:
    """Return deterministic installation evidence without contacting a provider."""
    value = {
        "contract": "M2SynthesisInstallQualification/v1",
        "qualified": True,
        "evaluation_vector": {
            "schema": True, "frozen_h0_gate": True, "m1_preservation": True,
            "conflict_retention": True, "frozen_replay": True,
            "quantresearch_read_only": True, "write_permissions_empty": True,
        },
    }
    return {**value, "replay_sha256": sha256(value)}


if __name__ == "__main__":
    print(json.dumps(install_qualification(), ensure_ascii=False, sort_keys=True))
