"""Secret-safe Provider + AWG smoke chain composed from formal runtime seams.

It never opens the product database, Exchange, scheduler, or UI. Evidence and
prompts remain in memory; the persisted report contains metadata and hashes.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .local_research import freeze_evidence_bundle
from .provider_broker import ChatCompletionsTransport, ProviderBroker, ProviderOutcome, StageRequest, canonical_packet_hash
from .provider_client import ProviderError
from .provider_routes import normalize_provider
from .web_access_gateway import WebAccessGatewayClient, WebAccessGatewayError


class SmokeFailure(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message); self.code = code


MINIMAL_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}
PLAN_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["research_question", "query", "checks"], "properties": {"research_question": {"type": "string"}, "query": {"type": "string"}, "checks": {"type": "array", "items": {"type": "string"}}}}
FAST_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["summary", "caveats", "evidence_ids"], "properties": {"summary": {"type": "string"}, "caveats": {"type": "array", "items": {"type": "string"}}, "evidence_ids": {"type": "array", "items": {"type": "string"}}}}
M1_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["direction", "thesis", "risks", "evidence_ids", "confidence"], "properties": {"direction": {"type": "string", "enum": ["bullish", "bearish", "neutral"]}, "thesis": {"type": "string"}, "risks": {"type": "array", "items": {"type": "string"}}, "evidence_ids": {"type": "array", "items": {"type": "string"}}, "confidence": {"type": "number"}}}

_FORBIDDEN_REPORT_KEYS = {"api_key", "authorization", "gateway_token", "token", "prompt", "prompt_text", "evidence_body", "request_body", "response_body", "output_text", "input_text", "packet", "result", "excerpt_text", "body", "markdown", "content"}


def normalize_usage(usage: dict[str, Any]) -> dict[str, int | None]:
    def nonzero(*names: str) -> int | None:
        values = [int(usage[name]) for name in names if isinstance(usage.get(name), (int, float))]
        return next((value for value in values if value), values[0] if values else None)
    prompt = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
    input_detail = usage.get("input_tokens_details") if isinstance(usage.get("input_tokens_details"), dict) else {}
    return {"input_tokens": nonzero("input_tokens", "prompt_tokens"), "output_tokens": nonzero("output_tokens", "completion_tokens"), "cached_input_tokens": nonzero("cached_input_tokens") or int(prompt.get("cached_tokens") or input_detail.get("cached_tokens") or 0)}


def _redact(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, dict):
        return {str(key): _redact(item, secrets) for key, item in value.items() if str(key).casefold() not in _FORBIDDEN_REPORT_KEYS}
    if isinstance(value, list): return [_redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret: value = value.replace(secret, "[REDACTED]")
    return value


def write_smoke_report(path: Path, report: dict[str, Any], *, forbidden_values: list[str]) -> None:
    encoded = json.dumps(_redact(report, forbidden_values), ensure_ascii=False, indent=2, sort_keys=True)
    if any(secret and secret in encoded for secret in forbidden_values):
        raise SmokeFailure("SECRET_GUARD", "A credential reached the smoke report boundary")
    path.parent.mkdir(parents=True, exist_ok=True); path.write_text(encoded + "\n", encoding="utf-8")


def _utc_now() -> str: return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _broker(provider: dict[str, Any], research: dict[str, Any], home: Path, *, family: str | None = None, hedge_seconds: float = 8.0, probe_seconds: float = 10.0) -> ProviderBroker:
    scoped = provider if family is None else {**provider, "routing": {**provider.get("routing", {}), "family_mode": family}}
    return ProviderBroker(scoped, ChatCompletionsTransport(home, research, retry=provider.get("retry") or {}), hedge_seconds=hedge_seconds, probe_seconds=probe_seconds)


def _invoke(broker: ProviderBroker, *, stage: str, packet: dict[str, Any], schema: dict[str, Any], timeout: float, mode: str = "race", verifier: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> ProviderOutcome:
    request = StageRequest(stage=stage, packet=packet, packet_sha256=canonical_packet_hash(packet), effort="medium", schema=schema, mode=mode, required_capabilities=("duel",) if mode == "duel" else ("race",), absolute_deadline=time.monotonic() + timeout, route_timeout_seconds=timeout, verifier_name="provider-awg-smoke/v2", verifier=verifier, h0_forbidden=stage == "m1_judgment")
    try: return broker.invoke(request)
    except ProviderError as exc:
        code = "PROVIDER_OUTAGE" if exc.category == "PROVIDER_OUTAGE" else "PROVIDER_INVOCATION_FAILED"
        raise SmokeFailure(code, "Provider runtime stopped the smoke chain") from None


def _hash_result(value: Any) -> str | None:
    if value is None: return None
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _outcome(role: str, outcome: ProviderOutcome) -> dict[str, Any]:
    winner = next((item for item in outcome.attempts if item.winner), None)
    return {"role": role, "status": "passed" if outcome.winner_route else "failed", "endpoint_id": outcome.endpoint_id, "route_id": outcome.winner_route, "model": outcome.model, "model_family": outcome.model_family, "requested_level": outcome.requested_level, "actual_level": outcome.actual_level, "upgrade_reason": outcome.upgrade_reason, "protocol": ("responses" if winner and winner.runner_fingerprint == "provider-broker/responses-sse-v1" else "chat_completions" if winner else None), "tier": winner.tier if winner else None, "multiplier": outcome.multiplier, "ttft_ms": round(outcome.ttft_seconds * 1000, 3) if outcome.ttft_seconds is not None else None, "duration_ms": round((winner.completed_at - winner.started_at) * 1000, 3) if winner and winner.completed_at is not None else None, "usage": normalize_usage(outcome.usage), "cost": {"estimated": outcome.estimated_cost, "actual": outcome.actual_cost, "currency": outcome.currency, "basis": outcome.cost_basis}, "output_sha256": _hash_result(outcome.result), "attempts": [{"route_id": item.route_id, "endpoint_id": item.endpoint_id, "model": item.model, "model_family": item.model_family, "tier": item.tier, "requested_level": item.requested_level, "actual_level": item.actual_level, "upgrade_reason": item.upgrade_reason, "runner_fingerprint": item.runner_fingerprint, "delayed_start": item.delayed_start, "terminal_error": item.terminal_error, "winner": item.winner, "cancellation_class": item.cancellation_class} for item in outcome.attempts]}


def _probes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"round": row.get("probe_round"), "scope": row.get("probe_scope"), "endpoint_id": row.get("endpoint_id"), "status": row.get("status"), "available_models": list(row.get("models") or []), "model_count": row.get("model_count"), "duration_ms": round((row["completed_at"] - row["started_at"]) * 1000, 3) if row.get("started_at") is not None and row.get("completed_at") is not None else None} for row in rows]


def _evidence(item: dict[str, Any]) -> dict[str, Any]:
    url, text = str(item.get("url") or ""), str(item.get("excerpt_text") or "")
    return {"evidence_id": "ev-" + hashlib.sha256((url + "\n" + text).encode()).hexdigest()[:16], "url": url, "title": str(item.get("title") or ""), "text": text, "fact_as_of": item.get("fact_as_of"), "published_at": item.get("published_at")}


def _verify_evidence(allowed: set[str]) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def verify(output: dict[str, Any]) -> dict[str, Any]:
        cited = output.get("evidence_ids") if isinstance(output, dict) else None
        passed = isinstance(cited, list) and bool(cited) and set(map(str, cited)).issubset(allowed)
        return {"passed": passed, "problems": [] if passed else ["evidence_ids_not_covered"]}
    return verify


def missing_luna_terra_endpoints(provider: dict[str, Any], probes: list[dict[str, Any]]) -> list[str]:
    """Find endpoint scopes that can prove a real L1 inventory miss and L2 hit."""
    latest_round = max((int(row.get("probe_round") or 0) for row in probes), default=0)
    inventories = {
        str(row.get("endpoint_id")): {str(model).casefold().replace("_", "-") for model in row.get("models") or []}
        for row in probes
        if row.get("probe_scope") == "health_gate"
        and int(row.get("probe_round") or 0) == latest_round
        and row.get("status") == "available"
    }
    candidates: list[tuple[int, float, str]] = []
    for endpoint_id, models in inventories.items():
        if any("luna" in model for model in models):
            continue
        terra_routes = [
            route for route in provider.get("routes", [])
            if route.get("enabled", True) and route.get("endpoint") == endpoint_id
            and "terra" in str(route.get("model") or "").casefold()
            and str(route.get("model") or "").casefold().replace("_", "-") in models
        ]
        if terra_routes:
            best = min(terra_routes, key=lambda route: (
                int((route.get("cost") or {}).get("tier") or 0),
                float((route.get("cost") or {}).get("weight") or 1), str(route.get("id") or ""),
            ))
            candidates.append((int((best.get("cost") or {}).get("tier") or 0),
                               float((best.get("cost") or {}).get("weight") or 1), endpoint_id))
    return [endpoint_id for _tier, _weight, endpoint_id in sorted(candidates)]


def run_smoke(settings: dict[str, Any], output_dir: Path, *, query: str, probe_timeout: float, provider_timeout: float) -> dict[str, Any]:
    provider_raw = settings.get("provider") if isinstance(settings.get("provider"), dict) else {}
    research = settings.get("research") if isinstance(settings.get("research"), dict) else {}
    awg_config = research.get("web_access_gateway") if isinstance(research.get("web_access_gateway"), dict) else {}
    secrets = [*[str(item.get("api_key") or "") for item in provider_raw.get("endpoints", []) if isinstance(item, dict)], str(awg_config.get("token") or "")]
    report: dict[str, Any] = {"contract": "provider-awg-smoke-report/v2", "run_id": str(uuid.uuid4()), "started_at": _utc_now(), "completed_at": None, "status": "running", "failure_code": None, "providers": {"probes": []}, "invocations": [], "upgrade_trace": [], "awg": {"status": "not_started", "calls": []}, "evidence": None, "duel": None}
    try:
        provider = normalize_provider(provider_raw, warn_legacy=False)
        if not provider.get("enabled") or not any(item.get("enabled") for item in provider.get("endpoints", [])): raise SmokeFailure("PROVIDER_OUTAGE", "No enabled Provider")
        if not str(awg_config.get("mcp_url") or "").startswith(("http://", "https://")) or not awg_config.get("token"): raise SmokeFailure("AWG_OUTAGE", "AWG is not configured")
        hedge = provider.get("hedge") if isinstance(provider.get("hedge"), dict) else {}; hedge_seconds = float(hedge.get("first_token_seconds") or 8)
        inventory_probes: list[dict[str, Any]] = []
        for family in ("openai", "anthropic"):
            result = _invoke(_broker(provider, research, output_dir, family=family, hedge_seconds=hedge_seconds, probe_seconds=probe_timeout), stage="research", packet={"instruction": "Return only JSON with ok=true."}, schema=MINIMAL_SCHEMA, timeout=provider_timeout)
            inventory_probes.extend(result.probes)
            report["providers"]["probes"].extend(_probes(result.probes)); report["invocations"].append(_outcome(f"strict_schema_probe_{family}", result))
            if not result.winner_route: raise SmokeFailure("PROVIDER_OUTAGE", f"{family} has no real-inventory usable model")
        broker = _broker(provider, research, output_dir, hedge_seconds=hedge_seconds, probe_seconds=probe_timeout)
        plan = _invoke(broker, stage="research", packet={"topic": query, "instruction": "Return a concise read-only web research plan."}, schema=PLAN_SCHEMA, timeout=provider_timeout)
        report["invocations"].append(_outcome("structured_research_plan", plan))
        if not isinstance(plan.result, dict) or not str(plan.result.get("query") or "").strip(): raise SmokeFailure("PROVIDER_INVOCATION_FAILED", "Research plan failed")
        awg = WebAccessGatewayClient(research); awg_started = time.monotonic()
        try:
            discovery = awg.search(str(plan.result["query"])); rows = [row for row in discovery.get("results") or [] if isinstance(row, dict)]
            if not rows: raise WebAccessGatewayError("web_search returned no results")
            read = None
            for row in rows[:5]:
                try:
                    candidate = awg.read(str(row.get("url") or ""))
                    if candidate.get("results"): read = candidate; break
                except WebAccessGatewayError: continue
            if read is None: raise WebAccessGatewayError("web_read returned no results")
        except WebAccessGatewayError as exc:
            report["awg"] = {
                "status": "failed", "failure_code": "AWG_OUTAGE",
                "duration_ms": round((time.monotonic() - awg_started) * 1000, 3),
                "calls": list(awg.call_history),
            }
            raise SmokeFailure("AWG_OUTAGE", "AWG web_search/web_read failed") from exc
        report["awg"] = {"status": "passed", "duration_ms": round((time.monotonic() - awg_started) * 1000, 3), "calls": list(awg.call_history)}
        search_rows, read_rows = [_evidence(row) for row in rows], [_evidence(row) for row in read.get("results") or [] if isinstance(row, dict)]
        if not search_rows or not read_rows or not read_rows[0]["text"].strip(): raise SmokeFailure("AWG_OUTAGE", "AWG evidence was empty")
        bundle = {"contract": "provider-awg-smoke-evidence/v2", "frozen_at": _utc_now(), "research_question": plan.result.get("research_question"), "query": plan.result.get("query"), "checks": plan.result.get("checks"), "search_results": search_rows, "read_results": read_rows}
        bundle_bytes, bundle_hash = freeze_evidence_bundle(bundle); evidence_ids = {row["evidence_id"] for row in [*search_rows, *read_rows]}
        report["evidence"] = {"bundle_sha256": bundle_hash, "bundle_bytes": len(bundle_bytes), "coverage": {"search_results": len(search_rows), "read_results": len(read_rows), "unique_urls": len({row["url"] for row in [*search_rows, *read_rows] if row["url"]})}}
        fast_packet = {"frozen_evidence": bundle, "evidence_bundle_sha256": bundle_hash, "instruction": "Write an evidence-grounded note using only bundle evidence_ids."}
        fast = _invoke(broker, stage="fast", packet=fast_packet, schema=FAST_SCHEMA, timeout=provider_timeout, verifier=_verify_evidence(evidence_ids))
        report["invocations"].append(_outcome("fast_compose", fast)); report["upgrade_trace"].append({"role": "fast_compose", "requested_level": fast.requested_level, "actual_level": fast.actual_level, "upgrade_reason": fast.upgrade_reason})
        if not fast.winner_route: raise SmokeFailure("PROVIDER_INVOCATION_FAILED", "Fast composition failed")
        upgrade = fast
        if fast.actual_level != "L2" or "terra" not in str(fast.model or "").casefold() or not fast.upgrade_reason:
            upgrade = None
            for endpoint_id in missing_luna_terra_endpoints(provider, inventory_probes):
                scoped = {
                    **provider,
                    "endpoints": [row for row in provider.get("endpoints", []) if row.get("id") == endpoint_id],
                    "routes": [row for row in provider.get("routes", []) if row.get("endpoint") == endpoint_id],
                }
                candidate = _invoke(
                    _broker(scoped, research, output_dir, hedge_seconds=hedge_seconds, probe_seconds=probe_timeout),
                    stage="fast", packet=fast_packet, schema=FAST_SCHEMA, timeout=provider_timeout,
                    verifier=_verify_evidence(evidence_ids),
                )
                report["invocations"].append(_outcome("fast_upgrade_verification", candidate))
                if candidate.winner_route and candidate.actual_level == "L2" and "terra" in str(candidate.model or "").casefold() and candidate.upgrade_reason:
                    upgrade = candidate
                    report["upgrade_trace"].append({"role": "fast_upgrade_verification", "scope": "single_provider_real_inventory", "endpoint_id": endpoint_id, "requested_level": candidate.requested_level, "actual_level": candidate.actual_level, "upgrade_reason": candidate.upgrade_reason})
                    break
            if upgrade is None:
                raise SmokeFailure("FAST_UPGRADE_NOT_PROVABLE", "No real missing-Luna inventory completed Terra fast composition")
        m1_packet = {"frozen_evidence": bundle, "evidence_bundle_sha256": bundle_hash, "instruction": "Make an independent M1 judgment using only the frozen evidence bundle."}
        duel = _invoke(broker, stage="m1_judgment", packet=m1_packet, schema=M1_SCHEMA, timeout=provider_timeout, mode="duel", verifier=_verify_evidence(evidence_ids))
        report["invocations"].append(_outcome("m1_duel", duel)); families = {item.model_family for item in duel.attempts if item.product_success and item.actual_level == "L3"}
        if not duel.winner_route or families != {"openai", "anthropic"}: raise SmokeFailure("M1_DUEL_FAILED", "M1 needs qualified real L3 results from both families")
        report["duel"] = {"status": (duel.duel or {}).get("status"), "bundle_sha256": bundle_hash, "families": sorted(families), "arbitration": duel.arbitration}; report["status"] = "passed"
    except SmokeFailure as exc: report["status"], report["failure_code"] = "failed", exc.code
    except Exception: report["status"], report["failure_code"] = "failed", "UNEXPECTED_ERROR"
    finally:
        report["completed_at"] = _utc_now(); write_smoke_report(output_dir / "smoke-report.json", report, forbidden_values=secrets)
    return report
