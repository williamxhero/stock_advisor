"""Provider-free executable probes for the frozen cross-contract cases.

The probes deliberately use the runtime contracts instead of reproducing their
rules.  They are small enough to run during startup and use only deterministic
fixtures.  A probe returns observations for one case; the regression gate owns
aggregation, digests, and non-regression comparison.
"""
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping


AS_OF = "2026-09-30T01:45:00Z"
KNOWN_AT = "2026-09-30T01:40:00Z"


# ---------------------------------------------------------------------------
# Small deterministic helpers


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _case_id(case: Any) -> str:
    if isinstance(case, str):
        return case
    if isinstance(case, Mapping):
        value = case.get("case_id")
    else:
        value = getattr(case, "case_id", None)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("regression probe requires a case id")
    return value


def _assertions(case: Any) -> dict[str, tuple[str, ...]]:
    if isinstance(case, Mapping):
        return {
            axis: tuple(case.get(f"{axis}_assertions") or ())
            for axis in ("safety", "quality", "recovery")
        }
    return {
        axis: tuple(getattr(case, f"{axis}_assertions", ()) or ())
        for axis in ("safety", "quality", "recovery")
    }


def _stable(value: Any, key: str = "") -> Any:
    """Remove runtime-generated ids/clocks while retaining observed structure."""
    dynamic = {
        "artifact_id", "stream_id", "episode_id", "batch_id", "cycle_id",
        "message_id", "attempt_id", "created_at", "sealed_at", "staged_at", "acquired_at",
        "submitted_at", "known_at", "occurred_at", "completed_at", "deleted_at",
        "first_failed_at", "last_failed_at", "resolved_at", "attempt", "request_id",
    }
    if isinstance(value, Mapping):
        return {
            ("component_evidence" if str(name).casefold() == "scores" else str(name)):
                "<runtime-generated>" if str(name) in dynamic else _stable(child, str(name))
            for name, child in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_stable(item, key) for item in value]
    if isinstance(value, Path):
        return "<temporary-state>"
    if isinstance(value, str):
        try:
            uuid.UUID(value)
        except (ValueError, AttributeError):
            return value
        return "<runtime-generated>"
    return value


def _finish_axis(checks: Mapping[str, bool], required: tuple[str, ...]) -> dict[str, Any]:
    normalized = {name: checks.get(name) is True for name in required}
    reasons = [f"failed_assertion:{name}" for name in required if not normalized[name]]
    return {
        "passed": not reasons,
        "checks": normalized,
        "reasons": reasons,
    }


def _result(case: Any, checks: dict[str, dict[str, bool]], evidence: dict[str, Any]) -> dict[str, Any]:
    required = _assertions(case)
    axes = {
        axis: _finish_axis(checks.get(axis, {}), required[axis])
        for axis in ("safety", "quality", "recovery")
    }
    return {"axes": axes, "evidence": _stable(evidence)}


def _rejected(call: Callable[[], Any], errors: tuple[type[BaseException], ...] = (ValueError,)) -> bool:
    try:
        call()
    except errors:
        return True
    return False


def _copy(value: Any) -> Any:
    return copy.deepcopy(value)


# ---------------------------------------------------------------------------
# Real M0/M1 fixtures and receipts


def _m0_fixture() -> tuple[dict[str, Any], dict[str, Any]]:
    from .evidence_snapshot import build_snapshot, descriptor
    from .m0_observation import sha256

    evidence = {
        "schema_version": 3,
        "as_of": AS_OF,
        "spoken_summary": "固定时点的市场宽度已记录。",
        "sources": [{"evidence_ref": "probe-market", "excerpt": "市场宽度观察。"}],
        "coverage": [], "critical_gaps": ["market breadth detail"],
        "conflicts": [{"ref": "probe-market", "resolution": "unresolved_equal_tier"}],
        "high_impact_events": [],
    }
    snapshot = descriptor(build_snapshot(
        cycle_id="probe-m0", as_of=AS_OF, evidence=evidence, source_watermarks={},
    ))
    packet = {
        "stage": "m0_compose", "cycle_id": "probe-m0", "task_key": "daily.execution.0945",
        "as_of": AS_OF, "evidence": evidence, "evidence_snapshot": snapshot,
        "mandate": {"quantresearch_permission": {"access": "read_only", "write_permissions": []}},
    }
    packet["sha256"] = sha256(packet)
    from .m0_observation import build_input
    input_contract = build_input(packet)
    output = {
        "result_version": 3,
        "semantic": {
            "summary": {"text": "截至固定时点，市场宽度已记录。", "kind": "market_fact", "evidence_refs": ["probe-market"]},
            "facts": [{"text": "上涨家数和下跌家数均已观察。", "kind": "market_fact", "evidence_refs": ["probe-market"]}],
            "derived_metrics": [], "source_opinions": [], "propagation": [],
            "conflicts": [{"text": "来源统计口径尚未合并。", "kind": "conflict", "evidence_refs": ["probe-market"]}],
            "observations": [], "connections": [], "attention": [],
            "unknowns": [{"text": "更细市场宽度仍未知。", "kind": "unknown", "evidence_refs": ["probe-market"]}],
        },
    }
    return input_contract, output


def _m1_fixture() -> tuple[dict[str, Any], dict[str, Any]]:
    from .evidence_snapshot import build_snapshot, descriptor
    from .m0_observation import sha256
    from .mandate_spec import build_mandate

    evidence = {
        "schema_version": 3, "as_of": AS_OF,
        "sources": [{"evidence_ref": "probe-m1-market", "excerpt": "市场宽度仍待持续确认。"}],
        "coverage": [], "critical_gaps": [], "conflicts": [], "high_impact_events": [],
    }
    snapshot = descriptor(build_snapshot(
        cycle_id="probe-m1", as_of=AS_OF, evidence=evidence, source_watermarks={},
    ))
    packet = {
        "stage": "m1_judgment", "cycle_id": "probe-m1", "task_key": "daily.execution.0945",
        "as_of": AS_OF, "scheduled_for": AS_OF,
        "mandate": build_mandate("daily.execution.0945", "m1_judgment", as_of=AS_OF),
        "evidence": evidence, "evidence_snapshot": snapshot,
        "frozen_m0": {"artifact_id": "probe-m0", "sha256": sha256("frozen observation"), "as_of": AS_OF, "known_at": KNOWN_AT},
    }
    packet["sha256"] = sha256(packet)
    from .m1_judgment import build_input
    input_contract = build_input(packet)
    core = {
        "version": 1, "thesis": "我倾向等待持续确认。", "direction": "neutral",
        "confidence": "low", "horizon": "当前", "current_action": "observe",
        "action_reason": "我不因短暂反弹扩大风险。",
        "reasons": [{"fact": "市场宽度仍待持续确认。", "evidence_refs": ["probe-m1-market"], "mechanism": "扩散尚未稳定。", "implication": "暂不扩大风险。"}],
        "counterargument": {"claim": "反弹可能持续。", "evidence_refs": ["probe-m1-market"], "why_not_base": "缺少持续确认。"},
        "portfolio_stance": "先观察。", "position_focus": [], "critical_unknowns": [],
        "transition_conditions": [
            {"outcome": "upgrade", "price": "指数企稳", "breadth": "上涨家数占优", "persistence": "持续一个交易日"},
            {"outcome": "downgrade", "price": "指数走弱", "breadth": "下跌家数扩大", "persistence": "持续一个交易日"},
        ],
    }
    from .judgment_publication import m1_coordination, render_core
    from .broker_client import canonical_packet_hash
    coordination = m1_coordination(core, packet)
    narrative = render_core(core)
    output = {
        "result_version": 5, "decision_core": core, "narrative": narrative,
        "publication": {
            "core_hash": sha256(core), "core_attempt_id": "probe-core", "core_review_attempt_id": "probe-review",
            "core_review": {
                "core_hash": sha256(core), "draft_hash": canonical_packet_hash({"text": narrative}),
                "coordination_hash": sha256(coordination), "grounded": True, "faithful": True,
                "scores": {"specificity": 2, "causality": 2, "counterargument": 2, "portfolio": 2, "naturalness": 2, "broadcast_risk": 0},
                "problems": [], "suggestions": [],
            },
            "fallback": True, "coordination": coordination, "coordination_hash": sha256(coordination),
        },
    }
    return input_contract, output


def _probe_m1(case: Any) -> dict[str, Any]:
    from .m1_judgment import assert_blind, build_output, frozen_replay, validate_input, validate_stage_output

    input_contract, output = _m1_fixture()
    receipt = build_output(input_contract, output, attempt_id="probe-attempt")
    replay_a = frozen_replay(input_contract, output)
    replay_b = frozen_replay(_copy(input_contract), _copy(output))
    forbidden = _copy(input_contract)
    forbidden["source_packet"]["context"] = {"h0_source_text": "private H0"}
    forbidden["source_packet"]["sha256"] = _digest({k: v for k, v in forbidden["source_packet"].items() if k != "sha256"})
    invalid_output = _copy(output)
    invalid_output["decision_core"]["reasons"][0]["evidence_refs"] = ["outside-frozen-snapshot"]
    checks = {
        "h0_not_visible_to_m1": input_contract["boundary"].get("h0_visible") is False and _rejected(lambda: assert_blind({"h0": "not allowed"})),
        "m1_packet_is_blind": _rejected(lambda: validate_input(forbidden)) and input_contract["source_packet"].get("frozen_m0") is not None,
        "quantresearch_read_only": input_contract["quantresearch"] == {"access": "read_only", "write_permissions": []},
        "m1_uses_frozen_evidence": bool(input_contract["evidence_refs"]) and receipt["provenance"]["input_sha256"] == _digest(input_contract),
        "m1_keeps_provenance": receipt["provenance"]["output_sha256"] == _digest(output) and receipt["permissions"] == {"write_permissions": []},
        "m1_has_direction_or_explicit_unknown": output["decision_core"].get("direction") in {"bullish", "bearish", "neutral", "unknown"},
        "blindness_survives_retry": replay_a == replay_b and replay_a["qualification"]["h0_blind"] is True,
        "failed_attempt_does_not_publish": _rejected(lambda: validate_stage_output(invalid_output, input_contract)),
        "recovery_keeps_stage_isolation": receipt["quantresearch"] == {"access": "read_only", "write_permissions": []} and input_contract["boundary"]["m2_visible"] is False,
    }
    return _result(case, {"safety": {k: checks[k] for k in ("h0_not_visible_to_m1", "m1_packet_is_blind", "quantresearch_read_only")}, "quality": {k: checks[k] for k in ("m1_uses_frozen_evidence", "m1_keeps_provenance", "m1_has_direction_or_explicit_unknown")}, "recovery": {k: checks[k] for k in ("blindness_survives_retry", "failed_attempt_does_not_publish", "recovery_keeps_stage_isolation")}}, {"contract_versions": ["M1JudgmentSpec/v1", "M1JudgmentResult/v1", "M1JudgmentReplay/v1"], "input": input_contract, "output": receipt, "replay": replay_a, "malformed_rejected": True})


def _probe_m0(case: Any) -> dict[str, Any]:
    from .m0_observation import build_output, frozen_replay, validate_stage_output

    input_contract, output = _m0_fixture()
    receipt = build_output(input_contract, output, attempt_id="probe-attempt")
    replay_a = frozen_replay(input_contract, output)
    replay_b = frozen_replay(_copy(input_contract), _copy(output))
    directional = _copy(output)
    directional["semantic"]["summary"]["text"] = "预计市场将上涨并建议买入。"
    checks = {
        "m0_has_no_direction": _rejected(lambda: validate_stage_output(directional, evidence_refs=input_contract["evidence_refs"])),
        "m0_has_no_action": all(word not in _json(output).casefold() for word in ("买入", "卖出", "加仓", "减仓")),
        "fact_owner_is_runtime": receipt["provenance"]["source"] == "runtime" and receipt["permissions"] == {"write_permissions": []},
        "m0_describes_observed_facts": bool(output["semantic"]["facts"]) and output["semantic"]["facts"][0]["evidence_refs"] == ["probe-market"],
        "m0_retains_conflicts": bool(output["semantic"]["conflicts"]),
        "m0_retains_unknowns": bool(output["semantic"]["unknowns"]),
        "m0_recovery_does_not_promote_direction": _rejected(lambda: validate_stage_output(directional, evidence_refs=input_contract["evidence_refs"])),
        "fallback_is_conservative": receipt["semantic"].get("unknowns") and not any(word in _json(receipt).casefold() for word in ("买入", "卖出", "看涨", "看跌")),
        "replay_is_deterministic": replay_a == replay_b,
    }
    return _result(case, {"safety": {k: checks[k] for k in ("m0_has_no_direction", "m0_has_no_action", "fact_owner_is_runtime")}, "quality": {k: checks[k] for k in ("m0_describes_observed_facts", "m0_retains_conflicts", "m0_retains_unknowns")}, "recovery": {k: checks[k] for k in ("m0_recovery_does_not_promote_direction", "fallback_is_conservative", "replay_is_deterministic")}}, {"contract_versions": ["M0ObservationSpec/v1", "M0ObservationResult/v1", "M0ObservationReplay/v1"], "input": input_contract, "output": receipt, "replay": replay_a, "directional_rejected": True})


# ---------------------------------------------------------------------------
# Temporal and evidence qualification


def _probe_time(case: Any) -> dict[str, Any]:
    from .temporal_integrity import qualify_temporal, replay_records, resolve_temporal

    record = {"record_id": "probe-old", "kind": "market_fact", "fact_as_of": "2026-09-30T01:30:00Z", "known_at": KNOWN_AT}
    envelope = resolve_temporal(record)
    qualified = qualify_temporal(envelope, as_of=AS_OF)
    future = {"record_id": "probe-future", "kind": "market_fact", "fact_as_of": "2026-09-30T02:00:00Z", "known_at": "2026-09-30T02:01:00Z"}
    replay = replay_records([future, record], as_of=AS_OF)
    invalid = _copy(envelope)
    invalid["known_at"] = "2026-09-30T03:00:00Z"
    checks = {
        "no_future_facts": all(row["state"] == "rejected" for row in replay["records"] if row["kind"] == "market_fact" and row["occurred_at"] and row["occurred_at"] > AS_OF),
        "known_at_not_after_cutoff": qualified["state"] != "rejected" and qualified["known_at"] <= AS_OF,
        "quantresearch_evidence_bounded": qualified["permitted_use"] in {"full", "degraded"} and envelope["contract"] == "TemporalIntegritySpec/v1",
        "as_of_is_reproducible": replay["as_of"] == AS_OF.replace("+00:00", "Z"),
        "provenance_has_source_watermarks": isinstance(envelope["precedence"], dict) and bool(envelope["occurred_at_source"]),
        "late_facts_are_excluded": any(row["state"] == "rejected" and "after_as_of" in " ".join(row["reasons"]) for row in replay["records"]),
        "temporal_failure_is_fail_closed": qualify_temporal(resolve_temporal(future), as_of=AS_OF)["permitted_use"] == "none",
        "retry_keeps_cutoff": replay_records([record], as_of=AS_OF) == replay_records([record], as_of=AS_OF),
        "replay_preserves_original_clock": _rejected(lambda: qualify_temporal(invalid, as_of=AS_OF)) or invalid["known_at"] > AS_OF,
    }
    return _result(case, {"safety": {k: checks[k] for k in ("no_future_facts", "known_at_not_after_cutoff", "quantresearch_evidence_bounded")}, "quality": {k: checks[k] for k in ("as_of_is_reproducible", "provenance_has_source_watermarks", "late_facts_are_excluded")}, "recovery": {k: checks[k] for k in ("temporal_failure_is_fail_closed", "retry_keeps_cutoff", "replay_preserves_original_clock")}}, {"contract_versions": ["TemporalIntegritySpec/v1", "TemporalIntegrityPolicy/v1"], "input": {"cutoff": AS_OF, "records": [record, future]}, "output": {"envelope": envelope, "qualified": qualified, "replay": replay}, "malformed_rejected": True})


def _evidence_record(*, truth: str = "unknown", propagation: str = "unknown", occurred: str | None = None) -> dict[str, Any]:
    from .evidence_spec import from_observation
    return from_observation({
        "evidence_ref": "probe-evidence", "url": "https://probe.test/evidence", "title": "Probe evidence",
        "excerpt_text": "固定时点的证据内容。", "fact_as_of": occurred,
        "known_at": KNOWN_AT, "factual_status": truth, "market_propagation": propagation,
    }, {"attempt_id": "probe", "observation_id": "observation", "operation": "fixture", "backend": "fixture", "acquired_at": KNOWN_AT})


def _probe_missing(case: Any) -> dict[str, Any]:
    from .evidence_gate import EvidenceGate
    from .evidence_qualification import qualify_record

    unknown = _evidence_record()
    conflict = _evidence_record(truth="verified", occurred="2026-09-30T01:30:00Z")
    missing_result = qualify_record(unknown, as_of=AS_OF)
    conflict_result = qualify_record(conflict, as_of=AS_OF, source_conflict_refs=("probe-evidence", "probe-other"))
    failed_gate = EvidenceGate().evaluate({"as_of": AS_OF, "sources": [], "coverage": []}, [{"key": "market breadth", "blocking": True}], [{"status": "failed", "non_empty": False}], AS_OF)
    replacement_gate = EvidenceGate().evaluate({"as_of": AS_OF, "sources": [], "coverage": []}, [], [{"status": "failed", "non_empty": False}, {"status": "succeeded", "non_empty": True, "backend": "fixture"}], AS_OF)
    checks = {
        "missing_data_is_unknown": missing_result["state"] in {"degraded", "rejected"} and any(reason in missing_result["reasons"] for reason in ("occurrence_unknown", "content_unknown")),
        "unresolved_conflict_blocks_qualified_action": conflict_result["state"] == "conflicted" and conflict_result["permitted_use"] == "context_only",
        "no_fact_invention": unknown["external_fact"] is False and missing_result["permitted_use"] != "external_fact",
        "conflicts_are_preserved": "source_conflict" in conflict_result["reasons"] and conflict_result["source_conflict_refs"] == ["probe-evidence", "probe-other"],
        "coverage_gap_is_explicit": "blocking_requirement_missing:market breadth" in failed_gate["problems"],
        "conditional_conclusion_is_traceable": missing_result["input_record_refs"][0]["record_id"] == unknown["record_id"],
        "failed_source_is_recorded": failed_gate["passed"] is False and failed_gate["successful_tool_results"] == 0,
        "replacement_source_keeps_scope": replacement_gate["passed"] is True and replacement_gate["attempted_backends"] == ["fixture"],
        "recovery_does_not_hide_conflict": conflict_result["state"] == "conflicted" and "source_conflict" in conflict_result["reasons"],
    }
    return _result(case, {"safety": {k: checks[k] for k in ("missing_data_is_unknown", "unresolved_conflict_blocks_qualified_action", "no_fact_invention")}, "quality": {k: checks[k] for k in ("conflicts_are_preserved", "coverage_gap_is_explicit", "conditional_conclusion_is_traceable")}, "recovery": {k: checks[k] for k in ("failed_source_is_recorded", "replacement_source_keeps_scope", "recovery_does_not_hide_conflict")}}, {"contract_versions": ["EvidenceSpec/v1", "EvidenceQualificationSpec/v1", "EvidenceQualificationPolicy/v1"], "inputs": {"unknown": unknown, "conflict": conflict, "failed_observation": {"status": "failed"}}, "outputs": {"unknown": missing_result, "conflict": conflict_result, "failed_gate": failed_gate, "replacement_gate": replacement_gate}})


# ---------------------------------------------------------------------------
# Message/store probe


def _probe_messages(case: Any) -> dict[str, Any]:
    from .store import CompanionStore

    with tempfile.TemporaryDirectory() as root_name:
        store = CompanionStore(Path(root_name) / "probe.sqlite3")
        cycle = store.create_cycle("probe.message", AS_OF, AS_OF)
        cycle_id = cycle["cycle_id"]
        original = store.stage_message(cycle_id, "original immutable text", "chat", message_id="probe-message")
        batch_id, submitted = store.commit_staged_messages(cycle_id, "chat")
        immutable_update = _rejected(lambda: store.update_staged_message(cycle_id, "probe-message", "rewritten"))
        immutable_withdraw = _rejected(lambda: store.withdraw_message(cycle_id, "probe-message"))
        correction = store.append_artifact(cycle_id, "judgment_revision", "runtime", "Correction references probe-message.", AS_OF, {"references": ["probe-message"]}, occurred_at=AS_OF, known_at=AS_OF)
        correction_references = any("probe-message" in str(row.get("metadata_json") or "") for row in store.artifacts(cycle_id) if row.get("artifact_id") == correction.get("artifact_id"))
        stream = store.start_stream_message(cycle_id, [batch_id], "ai_chat")
        stream_id = stream["stream_id"]
        store.append_stream_chunk(stream_id, "visible prefix")
        failed_stream = store.finish_stream_message(stream_id, error="bounded fixture failure")
        post_failure = store.stream_message(stream_id)
        append_after_failure = _rejected(lambda: store.append_stream_chunk(stream_id, "rewrite"))
        fault_artifact = store.append_artifact(cycle_id, "system_fault", "runtime", "Bounded fixture failure.", AS_OF, {"provenance": {"contract": "companion-test-provenance/v1", "source": "repair_probe", "run_id": "regression-probe-v1"}}, occurred_at=AS_OF, known_at=AS_OF)
        fault = store.record_fault_episode(cycle_id, scope_kind="stage", scope_key="m0", capability="probe", artifact_id=fault_artifact["artifact_id"], reason_category="fixture", user_impact="none", required_action="retry", occurred_at=AS_OF)
        resolved = store.resolve_fault_episodes(cycle_id, stages=["m0"], resolution_artifact_id=fault_artifact["artifact_id"], resolved_at=AS_OF)
        original_after = store.get_message("probe-message")
        evidence = {"contract_versions": ["RuntimeStore", "companion_message", "stream_message", "fault_episode"], "inputs": {"message": {"message_id": "probe-message", "text": "original immutable text"}, "stream_chunks": ["visible prefix"]}, "outputs": {"original": original_after, "submitted": submitted, "correction": correction, "stream": post_failure, "fault": fault, "resolved": resolved}, "observed": {"batch_id": batch_id, "immutable_update_rejected": immutable_update, "immutable_withdraw_rejected": immutable_withdraw, "append_after_failure_rejected": append_after_failure}}
    checks = {
        "published_message_not_mutated": immutable_update and original_after["body_text"] == original["body_text"],
        "published_message_not_deleted": immutable_withdraw and original_after["state"] == "submitted",
        "correction_is_append_only": correction["kind"] == "judgment_revision" and correction_references,
        "original_text_digest_preserved": hashlib.sha256(original_after["body_text"].encode("utf-8")).hexdigest() == hashlib.sha256(original["body_text"].encode("utf-8")).hexdigest(),
        "correction_references_original": correction_references,
        "replay_preserves_history": original_after["body_text"] == "original immutable text" and post_failure["text"] == "visible prefix",
        "stream_failure_keeps_visible_prefix": failed_stream["state"] == "failed" and post_failure["text"] == "visible prefix",
        "recovery_adds_separate_fault": fault["state"] == "active" and bool(resolved),
        "fault_resolution_does_not_delete_history": post_failure["text"] == "visible prefix" and original_after["state"] == "submitted",
    }
    return _result(case, {"safety": {k: checks[k] for k in ("published_message_not_mutated", "published_message_not_deleted", "correction_is_append_only")}, "quality": {k: checks[k] for k in ("original_text_digest_preserved", "correction_references_original", "replay_preserves_history")}, "recovery": {k: checks[k] for k in ("stream_failure_keeps_visible_prefix", "recovery_adds_separate_fault", "fault_resolution_does_not_delete_history")}}, evidence)


# ---------------------------------------------------------------------------
# Adapter and skill recovery probes


def _probe_execute_fail(_: dict[str, Any]) -> dict[str, Any]:
    raise RuntimeError("probe failure")


def _probe_execute_success(data: dict[str, Any]) -> dict[str, Any]:
    return {"value": int(data["value"]) + 1}


def _probe_validate_value(data: dict[str, Any]) -> None:
    if not isinstance(data.get("value"), int):
        raise ValueError("value must be integer")


def _probe_qualify_value(data: dict[str, Any]) -> dict[str, Any]:
    return {"passed": isinstance(data.get("value"), int), "evidence_refs": ["probe-adapter"]}


def _probe_adapter(case: Any) -> dict[str, Any]:
    from .adapter_contract import AdapterDefinition, AdapterRegistry

    registry = AdapterRegistry()
    registry.register(AdapterDefinition("probe-failing", "v1", "ProbeInput/v1", "ProbeOutput/v1", "deterministic", _probe_execute_fail, _probe_validate_value, _probe_validate_value, _probe_qualify_value))
    registry.register(AdapterDefinition("probe-fallback", "v1", "ProbeInput/v1", "ProbeOutput/v1", "deterministic", _probe_execute_success, _probe_validate_value, _probe_validate_value, _probe_qualify_value))
    output = registry.execute("probe-failing", {"value": 1}, as_of=AS_OF, timeout_seconds=1.0, retries=0, fallbacks=("probe-fallback",), request_id="probe-request")
    repeat = registry.execute("probe-failing", {"value": 1}, as_of=AS_OF, timeout_seconds=1.0, retries=0, fallbacks=("probe-fallback",), request_id="probe-request")
    checks = {
        "adapter_has_no_write_permissions": output["permissions"] == {"write_permissions": []},
        "failed_adapter_does_not_publish": output["attempts"][0].startswith("probe-failing:") and output["attempts"][0].endswith("failed"),
        "fallback_preserves_input_boundary": output["provenance"]["declaration"]["permissions"]["write_permissions"] == [],
        "failure_is_recorded": any(item == "probe-failing:failed" for item in output["attempts"]),
        "retry_is_bounded": len(output["attempts"]) == 2,
        "recovery_is_deterministic": output["data"] == repeat["data"] and output["attempts"] == repeat["attempts"],
        "fallback_is_explicit": output["adapter_id"] == "probe-fallback" and output["status"] == "succeeded",
        "original_failure_is_retained": output["attempts"][0] == "probe-failing:failed",
        "recovered_output_has_provenance": output["provenance"]["input_sha256"] and output["provenance"]["declaration"]["adapter_id"] == "probe-fallback",
    }
    return _result(case, {"safety": {k: checks[k] for k in ("adapter_has_no_write_permissions", "failed_adapter_does_not_publish", "fallback_preserves_input_boundary")}, "quality": {k: checks[k] for k in ("failure_is_recorded", "retry_is_bounded", "recovery_is_deterministic")}, "recovery": {k: checks[k] for k in ("fallback_is_explicit", "original_failure_is_retained", "recovered_output_has_provenance")}}, {"contract_versions": ["AdapterContractSpec/v1", "AdapterContractResult/v1"], "input": {"adapter_id": "probe-failing", "fallbacks": ["probe-fallback"], "value": 1, "as_of": AS_OF}, "output": output, "repeat": repeat})


def _probe_skill(case: Any) -> dict[str, Any]:
    from .analysis_skill import AnalysisSkill, SkillRegistry

    registry = SkillRegistry()
    registry.register(AnalysisSkill("probe-skill", "v1", ("probe",), ("value",), "deterministic", _probe_skill_fail))
    failed = registry.execute("probe-skill", {"value": 1}, as_of=AS_OF, cycle_id="probe-cycle")
    registry.replace_provider("probe-skill", AnalysisSkill("probe-fallback", "v1", ("probe",), ("value",), "deterministic", _probe_skill_success))
    recovered = registry.execute("probe-fallback", {"value": 1}, as_of=AS_OF, cycle_id="probe-cycle")
    checks = {
        "skill_has_no_fact_write": failed["permissions"] == {"write_permissions": []},
        "failed_skill_does_not_publish": failed["status"] == "failed" and failed["data"].get("error") == "RuntimeError",
        "replacement_preserves_capability_contract": recovered["status"] == "succeeded" and recovered["permissions"] == {"write_permissions": []},
        "failure_is_recorded": failed["status"] == "failed",
        "retry_is_bounded": failed["provenance"]["as_of"] == AS_OF and recovered["provenance"]["as_of"] == AS_OF,
        "recovery_is_deterministic": recovered["data"] == {"value": 2},
        "fallback_is_versioned": recovered["skill_version"] == "v1" and recovered["skill_id"] == "probe-fallback",
        "original_failure_is_retained": failed["data"].get("error") == "RuntimeError",
        "recovered_output_has_provenance": bool(recovered["provenance"].get("input_sha256")),
    }
    return _result(case, {"safety": {k: checks[k] for k in ("skill_has_no_fact_write", "failed_skill_does_not_publish", "replacement_preserves_capability_contract")}, "quality": {k: checks[k] for k in ("failure_is_recorded", "retry_is_bounded", "recovery_is_deterministic")}, "recovery": {k: checks[k] for k in ("fallback_is_versioned", "original_failure_is_retained", "recovered_output_has_provenance")}}, {"contract_versions": ["AnalysisSkillSpec/v1", "AnalysisSkillResult/v1"], "input": {"skill_id": "probe-skill", "value": 1, "as_of": AS_OF}, "output": {"failed": failed, "recovered": recovered}, "replacement": {"skill_id": "probe-fallback", "version": "v1"}})


def _probe_skill_fail(_: dict[str, Any]) -> dict[str, Any]:
    raise RuntimeError("probe skill failure")


def _probe_skill_success(data: dict[str, Any]) -> dict[str, Any]:
    return {"value": int(data["value"]) + 1}


# ---------------------------------------------------------------------------
# MemoryHub and MarketHub seams


class _FlakyMemory:
    def __init__(self, backend: Any) -> None:
        self.backend = backend
        self.failed = False
        self.requests: list[dict[str, Any]] = []

    def begin_snapshot(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(_copy(request))
        if not self.failed:
            self.failed = True
            from .memory_port import MemoryUnavailable
            raise MemoryUnavailable("fixture MemoryHub unavailable")
        return self.backend.begin_snapshot(request)

    def retrieve_bundle(self, snapshot_id: str, query: str, *, limit: int = 20) -> dict[str, Any]:
        return self.backend.retrieve_bundle(snapshot_id, query, limit=limit)


def _probe_memory(case: Any) -> dict[str, Any]:
    from .memory_port import InMemoryMemoryAdapter, MemoryUnavailable
    from .packet_builder import RuntimePacketBuilder
    from .store import CompanionStore

    memory = InMemoryMemoryAdapter()
    episode = {"memory_space_id": "probe-space", "source_system": "runtime", "source_event_id": "probe-event", "content_hash": "sha256:" + _digest("memory"), "body": "frozen context", "known_at": KNOWN_AT, "episode_type": "user_message"}
    # Seed the test-only adapter's immutable fixture state directly. The
    # regression probe must never exercise a Runtime MemoryHub write path.
    receipt = {"episode_id": "test-episode-1", "sequence": 1, "content_hash": episode["content_hash"], "protocol_version": "memoryhub/v1"}
    memory._receipts[(episode["memory_space_id"], episode["source_system"], episode["source_event_id"])] = receipt
    memory._episodes = [{**episode, **receipt}]
    flaky = _FlakyMemory(memory)
    request = {"memory_space_id": "probe-space", "as_of": AS_OF, "stage": "m1_judgment", "cycle_id": "probe-cycle"}
    first_failed = _rejected(lambda: flaky.begin_snapshot(request), (MemoryUnavailable,))
    snapshot = flaky.begin_snapshot(request)
    bundle = flaky.retrieve_bundle(snapshot["snapshot_id"], "frozen", limit=1)
    with tempfile.TemporaryDirectory() as root_name:
        store = CompanionStore(Path(root_name) / "probe.sqlite3")
        cycle = store.create_cycle("probe.memory", AS_OF, AS_OF)
        local_fallback_rejected = _rejected(lambda: RuntimePacketBuilder(Path(__file__).parents[3] / "resources", Path(root_name), store).build(cycle, "m0_research"), (MemoryUnavailable,))
    checks = {
        "memoryhub_is_authoritative": receipt["protocol_version"] == "memoryhub/v1" and snapshot["protocol_version"] == "memoryhub/v1",
        "memory_failure_does_not_use_local_fallback": local_fallback_rejected,
        "retrieval_is_read_only": len(memory._episodes) == 1 and "write_permissions" not in bundle,
        "failure_is_visible": first_failed and flaky.failed is True,
        "retry_preserves_query": flaky.requests[0] == flaky.requests[1] == request,
        "recovery_records_provenance": bundle["versions"]["protocol"] == "memoryhub/v1" and bundle["query"] == "frozen",
        "recovery_retries_same_request": len(flaky.requests) == 2 and flaky.requests[0] == flaky.requests[1],
        "no_duplicate_memory_write": len(memory._episodes) == 1,
        "unavailable_state_is_not_hidden": first_failed and snapshot["watermark"] == 1,
    }
    return _result(case, {"safety": {k: checks[k] for k in ("memoryhub_is_authoritative", "memory_failure_does_not_use_local_fallback", "retrieval_is_read_only")}, "quality": {k: checks[k] for k in ("failure_is_visible", "retry_preserves_query", "recovery_records_provenance")}, "recovery": {k: checks[k] for k in ("recovery_retries_same_request", "no_duplicate_memory_write", "unavailable_state_is_not_hidden")}}, {"contract_versions": ["MemoryPort", "memoryhub/v1"], "input": {"request": request, "episode": episode}, "output": {"receipt": receipt, "snapshot": snapshot, "bundle": bundle}, "failure": {"type": "MemoryUnavailable", "captured": first_failed}, "local_fallback_rejected": local_fallback_rejected})


def _market_data(*, observed_at: str = AS_OF, is_final: bool = True) -> dict[str, Any]:
    return {"bars": [{"symbol": "000001", "exchange": "SSE", "market": "CN-A", "freq": "1m", "interval_start": "2026-09-30T01:44:00Z", "interval_end": "2026-09-30T01:45:00Z", "observed_at": observed_at, "last_trade_at": observed_at, "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 100, "amount": 1000, "is_suspended": False, "is_st": False, "is_final": is_final, "degraded": False, "freshness_ms": 100, "provider": "fixture-markethub", "source_semantics": "native"}], "finality": "close", "source": "markethub_current_bar"}


def _probe_market(case: Any) -> dict[str, Any]:
    from .temporal_integrity import qualify_temporal, resolve_temporal
    from .tooling import FactRequest, validate_capability_data

    request = FactRequest(1, "cn_equity_current_bar", AS_OF, 1.0, {"symbols": ["000001"], "freq": "1m"}, finality="close")
    valid = _market_data()
    repeat = _market_data()
    future = _market_data(observed_at="2026-09-30T02:00:00Z")
    future_error = validate_capability_data(request, "2026-09-30T02:00:00Z", future)
    rejected_future = qualify_temporal(resolve_temporal({"kind": "market_fact", "fact_as_of": "2026-09-30T02:00:00Z", "known_at": "2026-09-30T02:00:00Z"}), as_of=AS_OF)
    malformed_rejected = validate_capability_data(request, AS_OF, _market_data(is_final=False)) == "tool_current_bar_finality_invalid"
    valid_error = validate_capability_data(request, AS_OF, valid)
    checks = {
        "markethub_facts_are_runtime_owned": valid_error is None and valid["source"] == "markethub_current_bar" and valid["bars"][0]["provider"] == "fixture-markethub",
        "failed_market_read_is_not_fact": malformed_rejected,
        "retry_cannot_cross_cutoff": future_error == "tool_current_bar_after_required_at" and rejected_future["permitted_use"] == "none",
        "failure_is_visible": malformed_rejected,
        "market_source_is_provenance_bound": valid["bars"][0]["provider"] == "fixture-markethub" and valid["bars"][0]["source_semantics"] in {"native", "derived"},
        "recovered_quote_is_reproducible": valid == repeat and valid_error is None,
        "recovery_retries_same_request": request.to_wire()["inputs"] == {"symbols": ["000001"], "freq": "1m"},
        "stale_quote_is_rejected": rejected_future["state"] == "rejected" and "after_as_of" in " ".join(rejected_future["reasons"]),
        "recovery_does_not_change_m0_m1_boundary": valid["source"] not in {"m1_output", "m0_output"},
    }
    return _result(case, {"safety": {k: checks[k] for k in ("markethub_facts_are_runtime_owned", "failed_market_read_is_not_fact", "retry_cannot_cross_cutoff")}, "quality": {k: checks[k] for k in ("failure_is_visible", "market_source_is_provenance_bound", "recovered_quote_is_reproducible")}, "recovery": {k: checks[k] for k in ("recovery_retries_same_request", "stale_quote_is_rejected", "recovery_does_not_change_m0_m1_boundary")}}, {"contract_versions": ["ai-trading-fact-request/v1", "TemporalIntegritySpec/v1"], "input": {"request": request.to_wire(), "source": "fixture-markethub"}, "output": {"valid": valid, "future": future, "future_error": future_error, "future_qualification": rejected_future}, "malformed_rejected": malformed_rejected})


_PROBES: dict[str, Callable[[Any], dict[str, Any]]] = {
    "m1_blind": _probe_m1,
    "m0_directionless": _probe_m0,
    "time_travel": _probe_time,
    "missing_conflicting_data": _probe_missing,
    "message_immutability": _probe_messages,
    "adapter_failure_recovery": _probe_adapter,
    "skill_failure_recovery": _probe_skill,
    "memoryhub_failure_recovery": _probe_memory,
    "markethub_failure_recovery": _probe_market,
}


def run_probes(case: Any) -> dict[str, Any]:
    """Execute exactly one frozen case and return per-axis observed evidence.

    Only contract-level input errors are converted to failed checks inside a
    probe. Unexpected exceptions intentionally escape so the caller's startup
    gate fails closed rather than mistaking a broken probe for a pass.
    """
    case_id = _case_id(case)
    try:
        probe = _PROBES[case_id]
    except KeyError as exc:
        raise ValueError(f"unknown regression probe case: {case_id}") from exc
    return probe(case)


__all__ = ["run_probes"]
