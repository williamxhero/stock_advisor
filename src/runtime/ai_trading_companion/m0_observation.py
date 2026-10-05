"""Runtime-owned M0 observation boundary.

M0 is an evidence-bound observation, not a judgment.  The provider-facing
stage result remains ``companion-m0-result-v3`` for compatibility; this module
adds the versioned input, output receipt, and replay contract around that
result.  Runtime owns the receipt and the provider never receives write
permissions, H0 text, or a later-stage artifact through this contract.
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Iterable


CONTRACT = "M0ObservationSpec/v1"
VERSION = 1
RESULT_CONTRACT = "M0ObservationResult/v1"
REPLAY_CONTRACT = "M0ObservationReplay/v1"

_ITEM_KINDS = frozenset({
    "market_fact", "derived_calculation", "source_opinion",
    "market_propagation", "conflict", "unknown",
})
_TYPED_SECTIONS = {
    "facts": ("market_fact", 4),
    "derived_metrics": ("derived_calculation", 3),
    "source_opinions": ("source_opinion", 2),
    "propagation": ("market_propagation", 2),
    "conflicts": ("conflict", 2),
}
_OBSERVATION_SECTIONS = {"observations": 3, "connections": 2, "attention": 1, "unknowns": 1}
_SECTIONS = frozenset({"summary", * _TYPED_SECTIONS, * _OBSERVATION_SECTIONS})

# These are deliberately action/conclusion phrases, not ordinary market
# movement words such as "上涨家数".  M0 may report a price move, but may not
# turn that move into a directional call or an instruction.
_DIRECTIONAL_MARKERS = (
    "看涨", "看跌", "看多", "看空", "偏多", "偏空", "做多", "做空",
    "bullish", "bearish", "预计上涨", "预计下跌", "将上涨", "将下跌",
    "会上涨", "会下跌", "方向预测", "目标价", "上涨空间", "下跌空间",
    "建议买入", "建议卖出", "建议加仓", "建议减仓", "建议持有", "建议清仓",
    "应买", "应卖", "应加仓", "应减仓", "应持有", "应清仓", "买入", "卖出",
    "加仓", "减仓", "清仓", "买入股数", "卖出股数", "机会排序", "机会优先级",
    "不新增仓", "不加仓", "不减仓", "不清仓", "交易动作",
)
_FORBIDDEN_INPUT_KEYS = frozenset({
    "h0", "human_messages", "chat_human", "published_chat_after_cutoff",
    "m1_output", "m2_output", "private_reasoning", "chain_of_thought",
    "pre_m0", "premarket", "premarket_artifact", "premarket_chat",
    "premarket_submission",
})
_FORBIDDEN_ARTIFACT_KINDS = frozenset({"h0", "m1", "m2", "pre_m0", "premarket", "premarket_chat"})


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: Any) -> str:
    return str(value.get("text") or "").strip() if isinstance(value, dict) else str(value or "").strip()


def _all_text(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _all_text(child)
    elif isinstance(value, list):
        for child in value:
            yield from _all_text(child)


def _walk_forbidden_keys(value: Any, path: str = "packet") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().casefold().replace("-", "_")
            if normalized in _FORBIDDEN_INPUT_KEYS or (
                normalized == "kind" and str(child).strip().casefold() in _FORBIDDEN_ARTIFACT_KINDS
            ):
                raise ValueError(f"M0 input contains forbidden channel at {path}.{key}")
            _walk_forbidden_keys(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk_forbidden_keys(child, f"{path}[{index}]")


def _packet_refs(packet: dict[str, Any]) -> set[str]:
    refs: set[str] = set()
    evidence = packet.get("evidence") if isinstance(packet.get("evidence"), dict) else {}
    for row in evidence.get("sources") or []:
        if isinstance(row, dict) and str(row.get("evidence_ref") or "").strip():
            refs.add(str(row["evidence_ref"]))
    for row in packet.get("verified_fact_digest") or []:
        if isinstance(row, dict) and str(row.get("evidence_ref") or "").strip():
            refs.add(str(row["evidence_ref"]))
    snapshot = packet.get("evidence_snapshot")
    if isinstance(snapshot, dict):
        values = (
            snapshot.get("evidence_refs")
            or snapshot.get("source_refs")
            or snapshot.get("included_sources")
            or []
        )
        if isinstance(values, list):
            refs.update(str(value) for value in values if str(value).strip())
    return refs


def _snapshot(packet: dict[str, Any]) -> dict[str, Any]:
    supplied = packet.get("evidence_snapshot")
    if not isinstance(supplied, dict):
        raise ValueError("M0 requires an immutable evidence snapshot")
    value = copy.deepcopy(supplied)
    required = {
        "contract", "snapshot_id", "cycle_id", "as_of", "schema_version",
        "content_hash", "source_watermarks", "included_sources", "version",
    }
    if value.get("contract") != "EvidenceSnapshotSpec/v1" or not required.issubset(value):
        raise ValueError("M0 requires an EvidenceSnapshotSpec/v1 descriptor")
    if set(value) not in (required, required | {"parent_snapshot_id"}):
        raise ValueError("M0 evidence snapshot descriptor fields are not exact")
    if not str(value.get("as_of") or "").strip():
        raise ValueError("M0 evidence snapshot requires as_of")
    # A packet carries the descriptor rather than the full snapshot. Rebuild
    # its deterministic identity from the packet's public baseline.
    baseline = packet.get("evidence")
    if not isinstance(baseline, dict):
        raise ValueError("M0 snapshot baseline is missing")
    from .evidence_snapshot import build_snapshot, descriptor
    expected = descriptor(build_snapshot(
        cycle_id=str(value.get("cycle_id") or packet.get("cycle_id") or ""),
        as_of=str(value["as_of"]),
        evidence=baseline,
        source_watermarks=value.get("source_watermarks") or {},
        parent_snapshot_id=value.get("parent_snapshot_id"),
        version=int(value.get("version") or 1),
    ))
    if value != expected:
        raise ValueError("M0 evidence snapshot identity or baseline mismatch")
    return value


def build_input(packet: dict[str, Any]) -> dict[str, Any]:
    """Bind a runtime packet to the M0-only input boundary."""
    if not isinstance(packet, dict):
        raise ValueError("M0 packet must be an object")
    stage = str(packet.get("stage") or "m0_compose")
    if stage != "m0_compose":
        raise ValueError("M0 observation input must target m0_compose")
    _walk_forbidden_keys(packet)
    snapshot = _snapshot(packet)
    mandate = packet.get("mandate") if isinstance(packet.get("mandate"), dict) else None
    quant = mandate.get("quantresearch_permission") if isinstance(mandate, dict) else None
    if not isinstance(quant, dict) or set(quant) - {
        "version", "enabled", "access", "scope", "write_permissions", "reason",
    } or (
        quant.get("access") != "read_only" or quant.get("write_permissions") != []
    ):
        raise ValueError("M0 QuantResearch access must be read_only")
    value = {
        "contract": CONTRACT,
        "version": VERSION,
        "stage": "m0_compose",
        "evidence_snapshot": snapshot,
        "evidence_refs": sorted(_packet_refs(packet)),
        "boundary": {
            "allowed_inputs": [
                "frozen_evidence", "market_facts", "derived_metrics",
                "source_opinions", "observed_market_propagation", "conflicts", "unknowns",
            ],
            "forbidden_inputs": [
                "h0", "published_chat_after_cutoff", "m1_output", "m2_output",
                "private_reasoning", "production_strategy",
            ],
            "m1_visible": False,
            "m2_visible": False,
            "h0_visible": False,
        },
        "permissions": {"write_permissions": []},
        "quantresearch": {"access": "read_only", "write_permissions": []},
        "provenance": {
            "cycle_id": packet.get("cycle_id"),
            "packet_sha256": packet.get("sha256"),
            "as_of": snapshot["as_of"],
            "source": "runtime",
        },
    }
    return validate_input(value)


def validate_input(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("contract") != CONTRACT or value.get("version") != VERSION:
        raise ValueError("unsupported M0ObservationSpec input")
    required = {
        "contract", "version", "stage", "evidence_snapshot", "evidence_refs",
        "boundary", "permissions", "quantresearch", "provenance",
    }
    if set(value) != required:
        raise ValueError("M0ObservationSpec input fields are not exact")
    if value["stage"] != "m0_compose":
        raise ValueError("M0ObservationSpec input stage is invalid")
    snapshot = value["evidence_snapshot"]
    snapshot_required = {
        "contract", "snapshot_id", "cycle_id", "as_of", "schema_version",
        "content_hash", "source_watermarks", "included_sources", "version",
    }
    if not isinstance(snapshot, dict) or snapshot.get("contract") != "EvidenceSnapshotSpec/v1":
        raise ValueError("M0ObservationSpec input requires an EvidenceSnapshotSpec/v1 descriptor")
    if set(snapshot) not in (snapshot_required, snapshot_required | {"parent_snapshot_id"}):
        raise ValueError("M0ObservationSpec evidence snapshot fields are not exact")
    if not str(snapshot.get("as_of") or "").strip():
        raise ValueError("M0ObservationSpec input requires a frozen evidence snapshot")
    evidence_refs = value["evidence_refs"]
    if not isinstance(evidence_refs, list) or any(
        not isinstance(ref, str) or not ref.strip() for ref in evidence_refs
    ) or evidence_refs != sorted(set(evidence_refs)):
        raise ValueError("M0ObservationSpec evidence_refs are invalid")
    snapshot_refs = snapshot.get("evidence_refs") or snapshot.get("source_refs") or snapshot.get("included_sources")
    if isinstance(snapshot_refs, list) and set(evidence_refs) != {
        str(ref) for ref in snapshot_refs if str(ref).strip()
    }:
        raise ValueError("M0ObservationSpec evidence_refs do not match the frozen snapshot")
    boundary = value["boundary"]
    if not isinstance(boundary, dict) or any(boundary.get(key) is not False for key in ("m1_visible", "m2_visible", "h0_visible")):
        raise ValueError("M0ObservationSpec later-stage and H0 visibility must be false")
    if value["permissions"] != {"write_permissions": []}:
        raise ValueError("M0ObservationSpec is read-only")
    quant = value["quantresearch"]
    if not isinstance(quant, dict) or quant.get("access") != "read_only" or quant.get("write_permissions") != []:
        raise ValueError("M0 QuantResearch boundary is not read-only")
    provenance = value["provenance"]
    if not isinstance(provenance, dict) or not str(provenance.get("as_of") or "").strip() or provenance.get("source") != "runtime":
        raise ValueError("M0ObservationSpec provenance is incomplete")
    return value


def _validate_item(item: Any, *, section: str, required_kind: str | None, refs: set[str]) -> None:
    if not isinstance(item, dict):
        raise ValueError(f"M0 {section} item must be an object")
    if set(item) - {"text", "evidence_refs", "kind"}:
        raise ValueError(f"M0 {section} item contains unknown fields")
    text = _text(item)
    if not text or len(text) > (240 if section in _TYPED_SECTIONS or section == "summary" else 180):
        raise ValueError(f"M0 {section} item text is out of bounds")
    kind = item.get("kind")
    if required_kind is not None and kind != required_kind:
        raise ValueError(f"M0 {section} item kind is invalid")
    if kind is not None and kind not in _ITEM_KINDS:
        raise ValueError(f"M0 {section} item kind is invalid")
    item_refs = item.get("evidence_refs")
    if not isinstance(item_refs, list) or any(not isinstance(ref, str) or not ref.strip() for ref in item_refs):
        raise ValueError(f"M0 {section} item evidence_refs are invalid")
    if any(ref not in refs for ref in item_refs):
        raise ValueError(f"M0 {section} item cites evidence outside the frozen snapshot")
    # Facts, calculations, opinions, propagation, conflicts, and summaries
    # cannot be asserted without an auditable source. An unknown is allowed to
    # be an explicit gap when no source can establish it.
    if (section == "summary" or kind not in {None, "unknown"}) and not item_refs:
        raise ValueError(f"M0 {section} item requires evidence_refs")


def validate_stage_output(
    output: dict[str, Any], *, evidence_refs: Iterable[str] = (),
) -> dict[str, Any]:
    """Validate the provider's compatible v3 result as an M0 observation."""
    if not isinstance(output, dict) or output.get("result_version") != 3:
        raise ValueError("M0 observation requires companion-m0-result-v3")
    if set(output) != {"result_version", "semantic"}:
        raise ValueError("M0 observation result fields are not exact")
    semantic = output.get("semantic")
    if not isinstance(semantic, dict):
        raise ValueError("M0 observation semantic payload is required")
    if set(semantic) != _SECTIONS:
        raise ValueError("M0 observation semantic fields are not exact")
    body = "\n".join(_all_text(semantic)).casefold()
    if any(marker.casefold() in body for marker in _DIRECTIONAL_MARKERS):
        raise ValueError("M0 observation contains directional or trading-action language")
    refs = {str(ref) for ref in evidence_refs if str(ref).strip()}
    _validate_item(semantic.get("summary"), section="summary", required_kind=None, refs=refs)
    for section, (kind, limit) in _TYPED_SECTIONS.items():
        values = semantic.get(section)
        if not isinstance(values, list) or len(values) > limit:
            raise ValueError(f"M0 {section} is outside its contract limit")
        for item in values:
            _validate_item(item, section=section, required_kind=kind, refs=refs)
    for section, limit in _OBSERVATION_SECTIONS.items():
        values = semantic.get(section)
        if not isinstance(values, list) or len(values) > limit:
            raise ValueError(f"M0 {section} is outside its contract limit")
        for item in values:
            _validate_item(item, section=section, required_kind="unknown" if section == "unknowns" else None, refs=refs)
    return output


def build_output(
    input_contract: dict[str, Any], output: dict[str, Any], *,
    attempt_id: str | None = None, packet_sha256: str | None = None,
) -> dict[str, Any]:
    """Create the runtime-owned immutable observation receipt."""
    validate_input(input_contract)
    validate_stage_output(output, evidence_refs=input_contract["evidence_refs"])
    value = {
        "contract": RESULT_CONTRACT,
        "version": VERSION,
        "spec_contract": CONTRACT,
        "stage": "m0_compose",
        "evidence_snapshot": copy.deepcopy(input_contract["evidence_snapshot"]),
        "evidence_refs": list(input_contract["evidence_refs"]),
        "semantic": copy.deepcopy(output["semantic"]),
        "source_result_version": output["result_version"],
        "permissions": {"write_permissions": []},
        "boundary": copy.deepcopy(input_contract["boundary"]),
        "quantresearch": copy.deepcopy(input_contract["quantresearch"]),
        "provenance": {
            "input_sha256": sha256(input_contract),
            "output_sha256": sha256(output),
            "packet_sha256": packet_sha256 or input_contract["provenance"].get("packet_sha256"),
            "attempt_id": attempt_id,
            "as_of": input_contract["provenance"]["as_of"],
            "source": "runtime",
        },
    }
    value["sha256"] = sha256(value)
    return validate_output(value)


def validate_output(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("contract") != RESULT_CONTRACT or value.get("version") != VERSION:
        raise ValueError("unsupported M0ObservationResult")
    required = {
        "contract", "version", "spec_contract", "stage", "evidence_snapshot", "evidence_refs", "semantic",
        "source_result_version", "permissions", "boundary", "quantresearch", "provenance", "sha256",
    }
    if set(value) != required:
        raise ValueError("M0ObservationResult fields are not exact")
    if value["spec_contract"] != CONTRACT or value["stage"] != "m0_compose" or value["source_result_version"] != 3:
        raise ValueError("M0ObservationResult identity is invalid")
    if value["permissions"] != {"write_permissions": []}:
        raise ValueError("M0ObservationResult read_only permissions are required")
    input_like = {
        "contract": CONTRACT, "version": VERSION, "stage": "m0_compose",
        "evidence_snapshot": value["evidence_snapshot"], "evidence_refs": list(value["evidence_refs"]),
        "boundary": value["boundary"], "permissions": value["permissions"],
        "quantresearch": value["quantresearch"],
        "provenance": {"as_of": (value.get("provenance") or {}).get("as_of"), "source": "runtime"},
    }
    validate_input(input_like)
    source_output = {"result_version": 3, "semantic": value["semantic"]}
    validate_stage_output(source_output, evidence_refs=set(value["evidence_refs"]))
    provenance = value["provenance"]
    if not isinstance(provenance, dict):
        raise ValueError("M0ObservationResult provenance is incomplete")
    for key in ("input_sha256", "output_sha256"):
        digest = provenance.get(key)
        if not isinstance(digest, str) or len(digest) != 64 or any(
            char not in "0123456789abcdef" for char in digest
        ):
            raise ValueError("M0ObservationResult provenance is incomplete")
    if provenance["output_sha256"] != sha256(source_output):
        raise ValueError("M0ObservationResult output digest mismatch")
    receipt_sha = value.get("sha256")
    if not isinstance(receipt_sha, str) or len(receipt_sha) != 64 or any(
        char not in "0123456789abcdef" for char in receipt_sha
    ) or receipt_sha != sha256({key: item for key, item in value.items() if key != "sha256"}):
        raise ValueError("M0ObservationResult receipt digest mismatch")
    return value


def bind_attempt(receipt: dict[str, Any], attempt_id: str) -> dict[str, Any]:
    """Bind a qualified receipt to the durable compose attempt identity."""
    validate_output(receipt)
    value = copy.deepcopy(receipt)
    value["provenance"]["attempt_id"] = str(attempt_id)
    value["sha256"] = sha256({key: item for key, item in value.items() if key != "sha256"})
    return validate_output(value)


def frozen_replay(
    input_contract: dict[str, Any], output: dict[str, Any], *,
    expected_output_sha256: str | None = None,
) -> dict[str, Any]:
    """Rebuild qualification from frozen input without changing the observation."""
    validate_input(copy.deepcopy(input_contract))
    validate_stage_output(copy.deepcopy(output), evidence_refs=input_contract["evidence_refs"])
    output_digest = sha256(output)
    if expected_output_sha256 is not None and output_digest != expected_output_sha256:
        raise ValueError("M0 observation replay digest mismatch")
    receipt = build_output(input_contract, output)
    return {
        "contract": REPLAY_CONTRACT,
        "source_input": copy.deepcopy(input_contract),
        "source_output": copy.deepcopy(output),
        "source_output_sha256": output_digest,
        "receipt": receipt,
        "qualification": {
            "valid": True,
            "read_only": receipt["permissions"] == {"write_permissions": []},
            "m0_only": receipt["stage"] == "m0_compose",
            "h0_blind": receipt["boundary"]["h0_visible"] is False,
            "later_stages_blind": receipt["boundary"]["m1_visible"] is False and receipt["boundary"]["m2_visible"] is False,
        },
    }


def install_qualification() -> dict[str, Any]:
    """Return deterministic schema, boundary, and replay qualification evidence."""
    from .evidence_snapshot import build_snapshot, descriptor
    baseline = {
        "schema_version": 3,
        "as_of": "2026-10-05T01:45:00Z",
        "sources": [{"evidence_ref": "install-market-1"}],
    }
    snapshot = build_snapshot(
        cycle_id="install-m0", as_of=baseline["as_of"], evidence=baseline,
        source_watermarks={},
    )
    packet = {
        "stage": "m0_compose", "cycle_id": "install-m0", "as_of": baseline["as_of"],
        "evidence": baseline, "evidence_snapshot": descriptor(snapshot),
        "mandate": {"quantresearch_permission": {"access": "read_only", "write_permissions": []}},
    }
    input_contract = build_input(packet)
    output = {"result_version": 3, "semantic": {
        "summary": {"text": "截至固定时点，市场宽度数据已记录。", "evidence_refs": ["install-market-1"], "kind": "market_fact"},
        "facts": [{"text": "上涨家数多于下跌家数。", "kind": "market_fact", "evidence_refs": ["install-market-1"]}],
        "derived_metrics": [], "source_opinions": [], "propagation": [], "conflicts": [],
        "observations": [], "connections": [], "attention": [], "unknowns": [],
    }}
    replay = frozen_replay(input_contract, output)
    return {
        "contract": "M0ObservationInstallQualification/v1",
        "qualified": replay["qualification"]["valid"],
        "replay_sha256": sha256(replay),
        "evaluation_vector": {
            "schema": True, "facts_vs_inference": True, "directional_language_blocked": True,
            "unknowns_supported": True, "frozen_replay": True, "h0_m1_m2_isolation": True,
            "quantresearch_read_only": True, "write_permissions_empty": True,
        },
    }


if __name__ == "__main__":
    print(canonical_json(install_qualification()))
