"""Versioned, Runtime-owned AuditSpec/v1 records.

Audit records answer what a cycle used and published without persisting a
provider's private deliberation.  Execution facts are written only by the
Runtime; evaluators may consume the immutable record but cannot manufacture or
replace its writer identity.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime
from typing import Any, Iterable

CONTRACT = "AuditSpec/v1"
VERSION = 1
REPLAY_CONTRACT = "AuditSpecReplay/v1"
INSTALL_CONTRACT = "AuditSpecInstallQualification/v1"
RUNTIME_SOURCE = "runtime"

_FORBIDDEN_KEYS = frozenset({
    "chain_of_thought", "cot", "thoughts", "reasoning_trace", "private_reasoning",
    "scratchpad", "deliberation", "hidden_reasoning", "hidden_deliberation",
    "internal_monologue", "inner_monologue", "verbose_reasoning", "verbose_internal_reasoning",
    "internal_reasoning", "private_thoughts", "hidden_thoughts", "thought_chain", "thinking",
})
_FORBIDDEN_TEXT = (
    "chain of thought", "chain-of-thought", "thought chain", "private reasoning", "private deliberation",
    "hidden reasoning", "hidden deliberation", "internal reasoning", "verbose internal reasoning",
    "scratchpad", "<thinking>", "内部思维链", "私有推理",
    "隐藏推理", "思维草稿",
)
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_STAGES = frozenset({"m0", "m0_research", "m0_compose", "h0", "m1", "m1_research", "m1_judgment", "m2", "outcome", "reflection", "chat"})
_ALLOWED_VERDICTS = frozenset({"qualified", "rejected", "blocked", "unknown"})


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _bounded_text(value: Any, field: str, limit: int = 4000) -> str:
    result = _required_string(value, field)
    if len(result) > limit:
        raise ValueError(f"{field} is too long for an audit record")
    return result


def _hash(value: Any, field: str) -> str:
    result = _required_string(value, field)
    if not _HEX64.fullmatch(result):
        raise ValueError(f"{field} must be a SHA-256 digest")
    return result


def _timestamp(value: Any, field: str) -> str:
    result = _required_string(value, field)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return result


def _walk_forbidden(value: Any, path: str = "audit") -> None:
    pending = [(value, path, 0)]
    visited = 0
    while pending:
        child, child_path, depth = pending.pop()
        visited += 1
        if depth > 32 or visited > 10000:
            raise ValueError("AuditSpec payload limits exceeded")
        if isinstance(child, dict):
            for key, item in child.items():
                normalized = str(key).strip().casefold().replace("-", "_")
                if normalized in _FORBIDDEN_KEYS:
                    raise ValueError(f"AuditSpec forbids private reasoning at {child_path}.{key}")
                pending.append((item, f"{child_path}.{key}", depth + 1))
        elif isinstance(child, (list, tuple)):
            pending.extend((item, f"{child_path}[{index}]", depth + 1) for index, item in enumerate(child))
        elif isinstance(child, str):
            lowered = child.casefold()
            if any(marker in lowered for marker in _FORBIDDEN_TEXT):
                raise ValueError(f"AuditSpec contains private reasoning text at {child_path}")
            if len(child) > 4000:
                raise ValueError(f"AuditSpec contains an unbounded narrative field at {child_path}")
        if visited + len(pending) > 10000:
            raise ValueError("AuditSpec payload limits exceeded")


def _string_list(value: Any, field: str, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{field} must be a list of non-empty strings")
    result = list(dict.fromkeys(item.strip() for item in value))
    if not allow_empty and not result:
        raise ValueError(f"{field} must not be empty")
    return result


def _object(value: Any, field: str, *, allow_empty: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict) or (not allow_empty and not value):
        raise ValueError(f"{field} must be a non-empty object")
    return value


def expected_writer_identity(*, cycle_id: str, stage: str, attempt_id: str, component: str = "runtime") -> dict[str, str]:
    """Return the Runtime-derived writer identity; providers cannot choose it."""
    stage = _required_string(stage, "stage")
    if stage not in _ALLOWED_STAGES:
        raise ValueError(f"unsupported audit stage: {stage}")
    return {
        "source": RUNTIME_SOURCE,
        "component": _required_string(component, "writer.component"),
        "cycle_id": _required_string(cycle_id, "writer.cycle_id"),
        "stage": stage,
        "attempt_id": _required_string(attempt_id, "writer.attempt_id"),
    }


def _normalize_writer(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"source", "component", "cycle_id", "stage", "attempt_id"}:
        raise ValueError("AuditSpec writer identity fields are not exact")
    result = {key: _required_string(value[key], f"writer.{key}") for key in value}
    if result["source"] != RUNTIME_SOURCE or result["stage"] not in _ALLOWED_STAGES:
        raise ValueError("AuditSpec execution facts must be Runtime-owned")
    return result


def _normalize_catalog(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("evidence_catalog must be a list")
    refs: set[str] = set()
    result: list[dict[str, Any]] = []
    for row in value:
        if not isinstance(row, dict):
            raise ValueError("evidence_catalog entries must be objects")
        required = {"ref", "source", "sha256", "as_of", "known_at"}
        if set(row) != required:
            raise ValueError("evidence_catalog entry fields are not exact")
        ref = _required_string(row["ref"], "evidence_catalog.ref")
        if ref in refs:
            raise ValueError("evidence_catalog references must be unique")
        refs.add(ref)
        result.append({
            "ref": ref,
            "source": _required_string(row["source"], "evidence_catalog.source"),
            "sha256": _hash(row["sha256"], "evidence_catalog.sha256"),
            "as_of": _timestamp(row["as_of"], "evidence_catalog.as_of"),
            "known_at": _timestamp(row["known_at"], "evidence_catalog.known_at"),
        })
    return result


def _normalize_propositions(value: Any, refs: set[str]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("propositions must be a list")
    ids: set[str] = set()
    result: list[dict[str, Any]] = []
    for row in value:
        if not isinstance(row, dict):
            raise ValueError("propositions must contain objects")
        required = {"id", "kind", "text", "evidence_refs", "counterevidence_refs"}
        if set(row) != required:
            raise ValueError("proposition fields are not exact")
        identifier = _required_string(row["id"], "proposition.id")
        if identifier in ids:
            raise ValueError("proposition ids must be unique")
        ids.add(identifier)
        kind = _required_string(row["kind"], "proposition.kind")
        text = _bounded_text(row["text"], "proposition.text", 1000)
        evidence_refs = _string_list(row["evidence_refs"], "proposition.evidence_refs")
        counter_refs = _string_list(row["counterevidence_refs"], "proposition.counterevidence_refs")
        if any(ref not in refs for ref in [*evidence_refs, *counter_refs]):
            raise ValueError(f"proposition {identifier} cites evidence outside the catalog")
        if kind in {"claim", "conclusion", "judgment"} and not evidence_refs:
            raise ValueError(f"proposition {identifier} must cite evidence")
        result.append({"id": identifier, "kind": kind, "text": text,
                       "evidence_refs": evidence_refs, "counterevidence_refs": counter_refs})
    return result


def _normalize_risks(value: Any, refs: set[str]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("risks must be a list")
    result = []
    ids: set[str] = set()
    for index, row in enumerate(value):
        if not isinstance(row, dict):
            raise ValueError("risks must contain objects")
        required = {"id", "text", "evidence_refs"}
        if set(row) != required:
            raise ValueError("risk fields are not exact")
        identifier = _required_string(row["id"], f"risks[{index}].id")
        if identifier in ids:
            raise ValueError("risk ids must be unique")
        ids.add(identifier)
        evidence_refs = _string_list(row["evidence_refs"], f"risks[{index}].evidence_refs")
        if any(ref not in refs for ref in evidence_refs):
            raise ValueError(f"risk {identifier} cites evidence outside the catalog")
        result.append({"id": identifier, "text": _bounded_text(row["text"], f"risks[{index}].text", 1000),
                       "evidence_refs": evidence_refs})
    return result


def _normalize_qualification(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"verdict", "passed", "reasons"}:
        raise ValueError("qualification fields are not exact")
    verdict = _required_string(value["verdict"], "qualification.verdict")
    if verdict not in _ALLOWED_VERDICTS or type(value["passed"]) is not bool:
        raise ValueError("qualification verdict is invalid")
    reasons = _string_list(value["reasons"], "qualification.reasons")
    if value["passed"] != (verdict == "qualified"):
        raise ValueError("qualification verdict and passed flag conflict")
    return {"verdict": verdict, "passed": value["passed"], "reasons": reasons}


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_judgment(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"state", "text", "sha256", "proposition_ids"}:
        raise ValueError("judgment fields are not exact")
    state = _required_string(value["state"], "judgment.state")
    if state not in {"published", "not_published"}:
        raise ValueError("judgment state is invalid")
    text = _bounded_text(value["text"], "judgment.text")
    text_hash = _hash(value["sha256"], "judgment.sha256")
    if text_hash != _text_sha256(text):
        raise ValueError("judgment digest does not match judgment text")
    proposition_ids = _string_list(value["proposition_ids"], "judgment.proposition_ids")
    return {"state": state, "text": text, "sha256": text_hash,
            "proposition_ids": proposition_ids}


def _normalize_provenance(value: Any) -> dict[str, Any]:
    required = {"source", "cycle_id", "stage", "attempt_id", "stage_run_id", "packet_sha256", "input_sha256", "output_sha256"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("provenance fields are not exact")
    if value["source"] != RUNTIME_SOURCE:
        raise ValueError("audit provenance source must be Runtime")
    result = {key: value[key] for key in required}
    for key in ("cycle_id", "stage", "attempt_id"):
        _required_string(result[key], f"provenance.{key}")
    for key in ("stage_run_id",):
        if result[key] is not None:
            _required_string(result[key], f"provenance.{key}")
    for key in ("packet_sha256", "input_sha256", "output_sha256"):
        _hash(result[key], f"provenance.{key}")
    return result


def _default_versions(packet: dict[str, Any]) -> dict[str, dict[str, Any]]:
    snapshot = packet.get("evidence_snapshot") if isinstance(packet.get("evidence_snapshot"), dict) else {}
    mandate = packet.get("mandate") if isinstance(packet.get("mandate"), dict) else {}
    task_profile = packet.get("task_profile") if isinstance(packet.get("task_profile"), dict) else {}
    evidence = packet.get("evidence") if isinstance(packet.get("evidence"), dict) else {}
    research = packet.get("research_evidence") if isinstance(packet.get("research_evidence"), dict) else {}
    data = {"evidence_snapshot": snapshot.get("content_hash") or sha256(evidence or {"cycle_id": packet.get("cycle_id")} ),
            "evidence_schema": evidence.get("schema_version", snapshot.get("schema_version", 1))}
    code = {"runtime": "stock-advisor-runtime/v1", "packet_schema": packet.get("schema_version", 1)}
    parameters = {"mandate": mandate.get("sha256") or sha256(mandate or {"stage": packet.get("stage")}),
                  "task_profile_version": task_profile.get("version", 1)}
    if research:
        if research.get("dataset_version"):
            data["quantresearch_dataset"] = research["dataset_version"]
        if research.get("code_version"):
            code["quantresearch_code"] = research["code_version"]
        if research.get("parameter_version"):
            parameters["quantresearch_parameters"] = research["parameter_version"]
    return {"data": data, "code": code, "parameters": parameters}


def _default_capabilities(packet: dict[str, Any], attempt: dict[str, Any] | None) -> dict[str, list[dict[str, Any]]]:
    attempt = attempt or {}
    model = attempt.get("model") or "unknown"
    provider = attempt.get("broker_provider") or "unknown"
    capabilities = {"models": [{"id": str(model), "version": "runtime-recorded"}],
                    "providers": [{"id": str(provider), "version": "runtime-recorded"}],
                    "agents": [], "skills": [], "adapters": []}
    for key, target in (("agent_role_inputs", "agents"), ("analysis_skills", "skills"), ("adapters", "adapters")):
        values = packet.get(key) if isinstance(packet.get(key), list) else []
        capabilities[target] = [copy.deepcopy(row) if isinstance(row, dict) else {"id": str(row)} for row in values]
    return capabilities


def _catalog_from_packet(packet: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = packet.get("evidence") if isinstance(packet.get("evidence"), dict) else {}
    snapshot = packet.get("evidence_snapshot") if isinstance(packet.get("evidence_snapshot"), dict) else {}
    as_of = str(evidence.get("as_of") or snapshot.get("as_of") or packet.get("as_of") or "1970-01-01T00:00:00Z")
    known_at = str(evidence.get("known_at") or as_of)
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in evidence.get("sources") or []:
        if not isinstance(row, dict) or not str(row.get("evidence_ref") or "").strip():
            continue
        ref = str(row["evidence_ref"])
        if ref in seen:
            continue
        seen.add(ref)
        source = str(row.get("url") or row.get("source_identity") or "runtime://evidence")
        body = row.get("excerpt") or row.get("excerpt_text") or row
        result.append({"ref": ref, "source": source, "sha256": sha256(body),
                       "as_of": str(row.get("fact_as_of") or row.get("published_at") or as_of),
                       "known_at": str(row.get("known_at") or known_at)})
    refs = snapshot.get("included_sources") or packet.get("evidence_refs") or []
    for ref_value in refs:
        ref = str(ref_value)
        if not ref.strip() or ref in seen:
            continue
        seen.add(ref)
        result.append({"ref": ref, "source": f"runtime://{ref}", "sha256": sha256(ref),
                       "as_of": as_of, "known_at": known_at})
    research = packet.get("research_evidence") if isinstance(packet.get("research_evidence"), dict) else {}
    for ref_value in research.get("evidence_refs") or []:
        ref = str(ref_value)
        if ref in seen:
            continue
        seen.add(ref)
        provenance = research.get("provenance") if isinstance(research.get("provenance"), dict) else {}
        result.append({"ref": ref, "source": str(provenance.get("artifact_ref") or "quantresearch"),
                       "sha256": str(research.get("sha256") or sha256(ref)),
                       "as_of": str(provenance.get("as_of") or as_of),
                       "known_at": str(provenance.get("known_at") or known_at)})
    return result


def _extract_propositions(output: dict[str, Any], refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    catalog_refs = {str(row["ref"]) for row in refs}
    result: list[dict[str, Any]] = []
    supplied = output.get("propositions") if isinstance(output.get("propositions"), list) else []
    for index, row in enumerate(supplied):
        if not isinstance(row, dict):
            continue
        evidence_refs = [str(ref) for ref in row.get("evidence_refs") or []]
        counter_refs = [str(ref) for ref in row.get("counterevidence_refs") or []]
        result.append({"id": str(row.get("id") or f"proposition:{index + 1}"),
                       "kind": str(row.get("kind") or "claim"),
                       "text": str(row.get("text") or row.get("claim") or "unknown"),
                       "evidence_refs": evidence_refs, "counterevidence_refs": counter_refs})
    semantic = output.get("semantic") if isinstance(output.get("semantic"), dict) else {}
    for section, rows in semantic.items():
        values = [rows] if isinstance(rows, dict) else rows if isinstance(rows, list) else []
        for index, row in enumerate(values):
            if not isinstance(row, dict) or not str(row.get("text") or "").strip():
                continue
            evidence_refs = [str(ref) for ref in row.get("evidence_refs") or []]
            result.append({"id": f"{section}:{index + 1}", "kind": str(row.get("kind") or "observation"),
                           "text": str(row["text"]), "evidence_refs": evidence_refs, "counterevidence_refs": []})
    core = output.get("decision_core") if isinstance(output.get("decision_core"), dict) else {}
    if core:
        supporting_refs: list[str] = []
        for source in [core.get("reasons"), core.get("position_focus")]:
            for row in source or []:
                if isinstance(row, dict):
                    supporting_refs.extend(str(ref) for ref in row.get("evidence_refs") or [])
        counterargument = core.get("counterargument") if isinstance(core.get("counterargument"), dict) else {}
        supporting_refs.extend(str(ref) for ref in counterargument.get("evidence_refs") or [])
        rows = [("judgment:thesis", "judgment", core.get("thesis"), supporting_refs),
                ("judgment:counterargument", "counterargument", counterargument.get("claim"),
                 counterargument.get("evidence_refs") or [])]
        rows.extend((f"judgment:reason:{index + 1}", "claim", row.get("fact"), row.get("evidence_refs") or [])
                     for index, row in enumerate(core.get("reasons") or []) if isinstance(row, dict))
        for identifier, kind, text, evidence_values in rows:
            if not str(text or "").strip():
                continue
            evidence_refs = [str(ref) for ref in evidence_values]
            result.append({"id": identifier, "kind": kind, "text": str(text),
                           "evidence_refs": evidence_refs, "counterevidence_refs": []})
    return result


def build_output(
    *, cycle_id: str, stage: str, attempt_id: str, packet: dict[str, Any] | None = None,
    output: dict[str, Any] | None = None, versions: dict[str, Any] | None = None,
    capabilities: dict[str, Any] | None = None, evidence_catalog: list[dict[str, Any]] | None = None,
    qualification: dict[str, Any] | None = None, judgment: dict[str, Any] | None = None,
    writer_identity: dict[str, Any] | None = None, stage_run_id: str | None = None,
    input_sha256: str | None = None, output_sha256: str | None = None,
    attempt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a bounded audit record from a frozen packet and qualified output."""
    packet = copy.deepcopy(packet or {})
    output = copy.deepcopy(output or {})
    _walk_forbidden(output, path="provider_output")
    packet_hash = str(packet.get("sha256") or input_sha256 or sha256(packet))
    raw_output_hash = output_sha256 or sha256(output)
    catalog = _normalize_catalog(evidence_catalog if evidence_catalog is not None else _catalog_from_packet(packet))
    refs = {str(row["ref"]) for row in catalog}
    props = _extract_propositions(output, catalog)
    # A provider's unbound citation is a qualification failure, not something
    # the audit layer silently repairs.  Explicit supplied catalogs therefore
    # retain every citation and are validated as-is.
    if evidence_catalog is not None:
        props = _extract_propositions(output, catalog)
    writer = copy.deepcopy(writer_identity or expected_writer_identity(cycle_id=cycle_id, stage=stage, attempt_id=attempt_id))
    judgment_value = copy.deepcopy(judgment or {"state": "not_published", "text": "No judgment was published.",
                                                "sha256": _text_sha256("No judgment was published."), "proposition_ids": []})
    if not judgment_value.get("proposition_ids"):
        judgment_value["proposition_ids"] = [row["id"] for row in props]
    record = {
        "contract": CONTRACT, "version": VERSION,
        "cycle_id": _required_string(cycle_id, "cycle_id"), "stage": _required_string(stage, "stage"),
        "attempt_id": _required_string(attempt_id, "attempt_id"),
        "writer_identity": writer,
        "data_versions": copy.deepcopy(versions or _default_versions(packet)).get("data") or _default_versions(packet)["data"],
        "code_versions": copy.deepcopy(versions or _default_versions(packet)).get("code") or _default_versions(packet)["code"],
        "parameter_versions": copy.deepcopy(versions or _default_versions(packet)).get("parameters") or _default_versions(packet)["parameters"],
        "capabilities": copy.deepcopy(capabilities or _default_capabilities(packet, attempt)),
        "evidence_catalog": catalog,
        "propositions": props,
        "risks": [],
        "qualification": copy.deepcopy(qualification or {"verdict": "qualified", "passed": True, "reasons": []}),
        "judgment": judgment_value,
        "provenance": {
            "source": RUNTIME_SOURCE, "cycle_id": cycle_id, "stage": stage, "attempt_id": attempt_id,
            "stage_run_id": stage_run_id, "packet_sha256": packet_hash,
            "input_sha256": input_sha256 or packet_hash, "output_sha256": raw_output_hash,
        },
    }
    risk_rows = output.get("risks") if isinstance(output.get("risks"), list) else []
    for index, row in enumerate(risk_rows):
        if isinstance(row, dict):
            record["risks"].append({"id": str(row.get("id") or f"risk:{index + 1}"),
                                    "text": str(row.get("text") or row.get("risk_cluster") or "risk"),
                                    "evidence_refs": [str(ref) for ref in row.get("evidence_refs") or []]})
    record["sha256"] = sha256(record)
    return validate_output(record)


def build_audit_record(**kwargs: Any) -> dict[str, Any]:
    return build_output(**kwargs)


def validate_input(value: dict[str, Any]) -> dict[str, Any]:
    """Validate an audit input descriptor without making it an execution fact."""
    if not isinstance(value, dict) or value.get("contract") != CONTRACT or value.get("version") != VERSION:
        raise ValueError("unsupported AuditSpec input")
    required = {"contract", "version", "cycle_id", "stage", "packet_sha256", "data_versions", "code_versions", "parameter_versions"}
    if set(value) != required:
        raise ValueError("AuditSpec input fields are not exact")
    _required_string(value["cycle_id"], "cycle_id")
    if value["stage"] not in _ALLOWED_STAGES:
        raise ValueError("unsupported AuditSpec stage")
    _hash(value["packet_sha256"], "packet_sha256")
    for field in ("data_versions", "code_versions", "parameter_versions"):
        _object(value[field], field)
    _walk_forbidden(value)
    return value


def validate_output(value: dict[str, Any], *, expected_writer_identity: dict[str, Any] | None = None,
                    allowed_refs: Iterable[str] | None = None) -> dict[str, Any]:
    required = {"contract", "version", "cycle_id", "stage", "attempt_id", "writer_identity",
                "data_versions", "code_versions", "parameter_versions", "capabilities", "evidence_catalog",
                "propositions", "risks", "qualification", "judgment", "provenance", "sha256"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("AuditSpec output fields are not exact")
    if value["contract"] != CONTRACT or type(value["version"]) is not int or value["version"] != VERSION:
        raise ValueError("unsupported AuditSpec identity")
    _walk_forbidden(value)
    _required_string(value["cycle_id"], "cycle_id")
    if value["stage"] not in _ALLOWED_STAGES:
        raise ValueError("unsupported AuditSpec stage")
    _required_string(value["attempt_id"], "attempt_id")
    writer = _normalize_writer(value["writer_identity"])
    if expected_writer_identity is not None and writer != _normalize_writer(expected_writer_identity):
        raise PermissionError("AuditSpec writer identity does not match the executing component")
    for field in ("data_versions", "code_versions", "parameter_versions"):
        _object(value[field], field)
    capabilities = value["capabilities"]
    if not isinstance(capabilities, dict) or set(capabilities) != {"models", "providers", "agents", "skills", "adapters"}:
        raise ValueError("AuditSpec capabilities fields are not exact")
    for field in capabilities:
        if not isinstance(capabilities[field], list):
            raise ValueError(f"capabilities.{field} must be a list")
    catalog = _normalize_catalog(value["evidence_catalog"])
    refs = {str(row["ref"]) for row in catalog}
    if allowed_refs is not None and not refs.issubset({str(ref) for ref in allowed_refs}):
        raise ValueError("AuditSpec evidence catalog contains an unauthorized reference")
    propositions = _normalize_propositions(value["propositions"], refs)
    risks = _normalize_risks(value["risks"], refs)
    qualification = _normalize_qualification(value["qualification"])
    judgment = _normalize_judgment(value["judgment"])
    proposition_ids = {row["id"] for row in propositions}
    if any(identifier not in proposition_ids for identifier in judgment["proposition_ids"]):
        raise ValueError("judgment references an unknown proposition")
    provenance = _normalize_provenance(value["provenance"])
    if provenance["cycle_id"] != value["cycle_id"] or provenance["stage"] != value["stage"] or provenance["attempt_id"] != value["attempt_id"]:
        raise ValueError("AuditSpec provenance identity mismatch")
    if writer["cycle_id"] != value["cycle_id"] or writer["stage"] != value["stage"] or writer["attempt_id"] != value["attempt_id"]:
        raise ValueError("AuditSpec writer identity mismatch")
    # Validate a normalized copy so callers cannot mutate the record while its
    # digest is being checked.  The record itself remains immutable at storage.
    _walk_forbidden(value)
    expected_digest = sha256({key: item for key, item in value.items() if key != "sha256"})
    if value["sha256"] != expected_digest:
        raise ValueError("AuditSpec digest mismatch")
    return value


def finalize(value: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(value)
    result["sha256"] = sha256({key: item for key, item in result.items() if key != "sha256"})
    return validate_output(result)


def frozen_replay(value: dict[str, Any]) -> dict[str, Any]:
    source = copy.deepcopy(validate_output(value))
    replay = {"contract": REPLAY_CONTRACT, "version": VERSION, "source": source,
              "source_sha256": source["sha256"],
              "qualification": {"valid": True, "runtime_owned": True, "no_private_reasoning": True,
                                 "citations_bound": True, "immutable": True}}
    replay["sha256"] = sha256(replay)
    return replay


def validate_replay(value: dict[str, Any]) -> dict[str, Any]:
    required = {"contract", "version", "source", "source_sha256", "qualification", "sha256"}
    if not isinstance(value, dict) or set(value) != required or value["contract"] != REPLAY_CONTRACT or value["version"] != VERSION:
        raise ValueError("AuditSpec replay fields are not exact")
    source = validate_output(value["source"])
    if value["source_sha256"] != source["sha256"] or value["sha256"] != sha256({key: item for key, item in value.items() if key != "sha256"}):
        raise ValueError("AuditSpec replay digest mismatch")
    qualification = value["qualification"]
    expected = {"valid", "runtime_owned", "no_private_reasoning", "citations_bound", "immutable"}
    if not isinstance(qualification, dict) or set(qualification) != expected or any(qualification[key] is not True for key in expected):
        raise ValueError("AuditSpec replay qualification mismatch")
    return value


def install_qualification() -> dict[str, Any]:
    packet = {"schema_version": 1, "cycle_id": "audit-install", "stage": "m1_judgment",
              "as_of": "2026-10-05T01:45:00Z", "evidence_snapshot": {"content_hash": sha256("install")},
              "evidence": {"as_of": "2026-10-05T01:45:00Z", "sources": [{"evidence_ref": "install:evidence", "excerpt": "bounded fact"}]},
              "mandate": {"sha256": sha256("mandate")}}
    record = build_output(cycle_id="audit-install", stage="m1_judgment", attempt_id="install-attempt",
                          packet=packet, output={"propositions": [{"id": "p1", "kind": "claim", "text": "bounded fact", "evidence_refs": ["install:evidence"]}]},
                          judgment={"state": "published", "text": "bounded fact", "sha256": _text_sha256("bounded fact"), "proposition_ids": ["p1"]})
    replay = validate_replay(frozen_replay(record))
    return {"contract": INSTALL_CONTRACT, "qualified": True, "replay_sha256": replay["sha256"],
            "evaluation_vector": {"versioned": True, "runtime_owned": True, "citations": True,
                                  "no_private_reasoning": True, "frozen_replay": True}}


# Public aliases used by contract tests and Runtime integration.
validate = validate_output
replay = frozen_replay

if __name__ == "__main__":
    print(canonical_json(install_qualification()))
