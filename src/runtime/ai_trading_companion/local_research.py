"""Structured, local-only acquisition followed by an immutable evidence seal."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .acquisition import AcquisitionBoundary
from .evidence_gate import EvidenceGate
from .broker_client import BrokerError, BrokerRequest, ProviderBrokerClient, canonical_packet_hash
from .market_breadth_cache import MarketBreadthSnapshotCache
from .tooling import FactRequest, ToolRunner, validate_capability_data


class ResearchPlanError(ValueError):
    pass


RESEARCH_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["version", "operations"],
    "properties": {
        "version": {"type": "integer", "const": 1},
        "operations": {"type": "array", "maxItems": 24, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["requirement_key", "backend", "operation", "arguments", "fallback_backends"],
            "properties": {
                "requirement_key": {"type": "string", "minLength": 1},
                "backend": {"type": "string", "enum": ["market", "gateway"]},
                "operation": {"type": "string", "enum": [
                    "market_snapshot", "market_breadth", "turnover_compare", "sector_snapshot",
                    "sentiment_snapshot", "holding_snapshot", "current_bar",
                    "web_search", "web_read", "web_browser",
                ]},
                "arguments": {
                    "type": "object", "additionalProperties": False,
                    "required": ["query", "categories", "url", "symbol", "render", "session_id", "actions"],
                    "properties": {
                        "query": {"type": ["string", "null"]},
                        "categories": {"type": ["string", "null"]},
                        "url": {"type": ["string", "null"]},
                        "symbol": {"type": ["string", "null"]},
                        "render": {"type": ["string", "null"]},
                        "session_id": {"type": ["string", "null"]},
                        "actions": {"type": ["array", "null"], "items": {
                            "type": "object", "additionalProperties": False,
                            "required": ["type", "url", "ref", "element", "ms", "pixels"],
                            "properties": {
                                "type": {"type": "string", "enum": [
                                    "navigate", "click", "wait", "scroll", "snapshot", "screenshot", "close",
                                ]},
                                "url": {"type": ["string", "null"]},
                                "ref": {"type": ["string", "null"]},
                                "element": {"type": ["string", "null"]},
                                "ms": {"type": ["integer", "null"]},
                                "pixels": {"type": ["integer", "null"]},
                            },
                        }},
                    },
                },
                "fallback_backends": {"type": "array", "items": {
                    "type": "string", "enum": ["market", "gateway"],
                }},
            },
        }},
    },
}


def _research_plan_schema(evidence_contract: dict[str, Any]) -> dict[str, Any]:
    """Bind planner output to the contract's canonical requirement vocabulary."""
    schema = copy.deepcopy(RESEARCH_PLAN_SCHEMA)
    requirement_keys = sorted({
        str(row.get("key") or "")
        for row in evidence_contract.get("requirements") or []
        if isinstance(row, dict) and str(row.get("key") or "")
    })
    if requirement_keys:
        schema["properties"]["operations"]["items"]["properties"]["requirement_key"]["enum"] = requirement_keys
    return schema


_OPERATIONS = {
    "market": {
        "market_snapshot", "market_breadth", "turnover_compare", "sector_snapshot",
        "sentiment_snapshot", "holding_snapshot", "current_bar",
    },
    "gateway": {"web_search", "web_read", "web_browser"},
}
_BACKEND_ORDER = {"market": 0, "gateway": 1}
_SHANGHAI = ZoneInfo("Asia/Shanghai")


class BrokerResearchPlanner:
    """Ask Broker only for declarative JSON; acquisition stays local."""

    def __init__(self, broker: ProviderBrokerClient, *, intellect: str, effort: str,
                 deadline: Callable[[], float] | None = None, market_tool_available: bool = False) -> None:
        self.broker = broker
        self.deadline = deadline or (lambda: math.inf)
        self.intellect = intellect
        self.effort = effort
        self.market_tool_available = market_tool_available
        self.outcomes: list[Any] = []

    def __call__(self, packet: dict[str, Any], gaps: list[str], round_number: int) -> dict[str, Any]:
        attempted_urls = {
            str(url) for url in packet.get("attempted_research_urls") or [] if str(url)
        }
        discoveries = _merge_discoveries(
            list(packet.get("research_discoveries") or []),
            _public_market_close_discoveries(packet),
            _public_intraday_market_discoveries(packet),
        )
        discoveries = [
            row for row in discoveries if str(row.get("url") or "") not in attempted_urls
        ]
        discovery_repair = _discovery_read_repair_plan(
            packet.get("evidence_contract") or {}, discoveries, gaps, round_number,
        )
        if discovery_repair is not None:
            return discovery_repair
        planning_packet = {
            "task_key": packet.get("task_key"),
            "stage": "research_plan",
            "as_of": packet.get("as_of"),
            "evidence_contract": packet.get("evidence_contract"),
            "market_time_context": _planner_time_context(packet),
            "research_scope": _planner_research_scope(packet.get("public_research_scope")),
            "coverage_gaps": list(gaps),
            "repair_round": int(round_number),
            "research_discoveries": discoveries,
            "available_backends": [
                backend for backend in ("gateway", "market")
                if backend in set(packet.get("allowed_research_backends") or ("gateway", "market"))
                and (backend != "market" or packet.get("deterministic_market_facts") or self.market_tool_available)
            ],
            "deterministic_requirement_keys": _deterministic_requirement_keys(packet.get("evidence_contract") or {})
            if packet.get("deterministic_injection") is True else [],
            "instruction": (
                "Return only a version 1 research plan. Copy requirement_key exactly from evidence_contract.requirements; "
                "never invent, rename, split, or broaden requirement keys. Deterministic index, breadth, portfolio-quote and "
                "per-holding event searches are injected locally from the frozen contract; do not substitute "
                "or broaden their symbols. Use gateway web_search only for discovery and "
                "gateway web_read for source verification. Use market operations only when market appears in "
                "available_backends; they read frozen caller-supplied facts when available, otherwise use promoted local public-market tools. "
                "All timestamps in the evidence contract are UTC. For Chinese-market search terms, convert them to the "
                "Asia/Shanghai local timestamps supplied in market_time_context. An exact 15:00 local market-state "
                "requirement means the closing state: search for 收盘/闭市 evidence, never 早盘 or 开盘. "
                "For an exact closing market state, read the supplied deterministic_public_market URLs first; they are "
                "public historical daily data fetched through the local gateway and already bounded to the frozen date. "
                "When research_discoveries is non-empty, prioritize web_read for 4 to 8 distinct candidate URLs that cover "
                "the remaining gaps; do not repeat discovery searches unless no candidate URL can address a gap."
            ),
        }
        request = BrokerRequest(
            stage="research", packet=planning_packet, packet_sha256=canonical_packet_hash(planning_packet),
            intellect=self.intellect, effort=self.effort,
            schema=_research_plan_schema(planning_packet["evidence_contract"] or {}),
            visible_stream=False, absolute_deadline=float(self.deadline()), verifier_name="research-plan/v1",
            verifier=lambda output: _verify_research_plan(planning_packet, output),
        )
        outcome = self.broker.invoke(request)
        self.outcomes.append(outcome)
        if not isinstance(outcome.result, dict):
            raise ResearchPlanError("Broker did not return a qualified research plan")
        return outcome.result


class WebAccessGatewayBackend:
    """Narrow adapter exposing only gateway read operations."""

    def __init__(self, tools: Any, *, as_of: str | None = None) -> None:
        self.tools = tools
        self.as_of = as_of

    def __call__(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if operation == "web_search": return self.tools.search(str(arguments.get("query") or ""), str(arguments.get("categories") or "news"))
        if operation == "web_read": return self.tools.read(
            str(arguments.get("url") or ""), str(arguments.get("render") or "auto"), self.as_of,
        )
        if operation == "web_browser": return self.tools.browser(arguments.get("session_id"), list(arguments.get("actions") or []))
        raise ValueError(f"unsupported read-only research operation: {operation}")


class ToolCatalogResearchBackend:
    """Compatibility projection from a research plan to promoted local CLI capabilities."""

    def __init__(self, runner: ToolRunner, *, as_of: str, deadline: Callable[[], float],
                 contract: dict[str, Any] | None = None) -> None:
        self.runner = runner
        self.as_of = as_of
        self.deadline = deadline
        self.requirements = {
            str(row.get("key") or ""): row
            for row in (contract or {}).get("requirements") or [] if isinstance(row, dict)
        }

    def __call__(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        requirement_key = str(arguments.pop("_requirement_key", "") or "")
        requirement = self.requirements.get(requirement_key) or {}
        window = requirement.get("window") if isinstance(requirement.get("window"), dict) else {}
        required_at = str(window.get("end") or self.as_of)
        capability, inputs = self._request_for(operation, arguments)
        resolution = self.runner.resolve_with_fallback(FactRequest(
            contract_version=1, capability=capability, required_at=required_at,
            deadline_seconds=max(0.1, min(15.0, float(self.deadline()))), inputs=inputs,
            context={}, freshness_seconds=0.0, finality="observed",
        ))
        if not resolution.succeeded or resolution.data is None:
            raise ToolResolutionError(capability, resolution)
        return self._project(operation, resolution)

    @staticmethod
    def _request_for(operation: str, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if operation == "web_search":
            return "generic_web_search", {"query": str(arguments.get("query") or "")}
        if operation == "web_read":
            return "generic_web_read", {"url": str(arguments.get("url") or "")}
        if operation == "web_browser":
            actions = arguments.get("actions") if isinstance(arguments.get("actions"), list) else []
            url = str(arguments.get("url") or "")
            for action in actions:
                if isinstance(action, dict) and action.get("type") == "navigate" and action.get("url"):
                    url = str(action["url"])
            return "generic_browser_capture", {"url": url}
        raise ValueError(f"unsupported ToolCatalog research operation: {operation}")

    @staticmethod
    def _project(operation: str, resolution: Any) -> dict[str, Any]:
        data = resolution.data
        artifact = resolution.raw_artifact_ref
        if operation == "web_search":
            results = [
                {"url": str(item.get("url") or ""), "title": str(item.get("title") or ""),
                 "excerpt_text": str(item.get("title") or ""), "fact_as_of": resolution.fact_as_of,
                 "raw_artifact_ref": artifact}
                for item in data.get("results") or [] if isinstance(item, dict) and item.get("url")
            ]
            return {"url": data.get("url"), "results": results, "raw_artifact_ref": artifact}
        url, text = str(data.get("url") or ""), str(data.get("text") or "")
        return {
            "url": url, "text": text, "raw_artifact_ref": artifact,
            "results": [{"url": url, "title": url, "excerpt_text": text, "fact_as_of": resolution.fact_as_of,
                         "raw_artifact_ref": artifact}],
        }


class ToolCatalogMarketBackend:
    """Resolve public market facts through promoted tools and expose evidence-shaped results."""

    def __init__(self, runner: ToolRunner, *, contract: dict[str, Any], deadline: Callable[[], float],
                 cycle_id: str = "", daily_ledger: list[dict[str, Any]] | None = None) -> None:
        self.runner = runner
        self.contract = contract
        self.deadline = deadline
        self.cycle_id = cycle_id
        self.daily_ledger = json.loads(json.dumps(daily_ledger or [], ensure_ascii=False))
        self.requirements = {
            str(row.get("key") or ""): row
            for row in contract.get("requirements") or [] if isinstance(row, dict)
        }

    def __call__(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        requirement_key = str(arguments.pop("_requirement_key", "") or "")
        requirement = self.requirements.get(requirement_key) or {}
        capability = {
            "market_snapshot": "cn_market_index_batch",
            "market_breadth": "cn_market_breadth",
            "turnover_compare": "cn_market_turnover_compare",
            "sector_snapshot": "cn_market_sector_snapshot",
            "sentiment_snapshot": "cn_market_breadth",
            "holding_snapshot": "cn_equity_quote_batch",
            "current_bar": "cn_equity_current_bar",
        }.get(operation)
        if capability is None:
            raise ValueError(f"unsupported live market operation: {operation}")
        window = requirement.get("window") if isinstance(requirement.get("window"), dict) else {}
        required_at = str(window.get("end") or self.contract.get("as_of") or "")
        mode = str(window.get("mode") or "")
        declared_finality = str(requirement.get("finality") or "")
        finality = declared_finality or (
            "official_close"
            if (mode == "exact" and required_at[11:16] == "07:00")
            or operation in {"sector_snapshot", "sentiment_snapshot"}
            else "intraday"
        )
        if operation in {"holding_snapshot", "current_bar"}:
            symbols = [str(value) for value in requirement.get("required_entities") or [] if str(value)]
            if not symbols:
                raise ValueError(f"{operation} requires frozen portfolio entities")
            inputs = {"symbols": symbols, **({"freq": "1m"} if operation == "current_bar" else {})}
        elif operation == "market_snapshot":
            inputs = {"symbols": ["000001", "399001", "399006"]}
        else:
            inputs = {}
        if operation == "market_breadth":
            cached = self._cached_breadth(required_at, str(window.get("start") or ""), finality)
            if cached is not None:
                return cached
        request = FactRequest(
            contract_version=1, capability=capability, required_at=required_at,
            deadline_seconds=max(0.1, min(25.0, float(self.deadline()))), inputs=inputs,
            context={"window_start": str(window.get("start") or required_at), "cycle_id": self.cycle_id},
            freshness_seconds=900.0 if finality == "intraday" else 0.0, finality=finality,
        )
        resolution = self.runner.resolve_with_fallback(request)
        if not resolution.succeeded or resolution.data is None:
            ledger = self._exact_close_ledger_result(request, requirement, operation)
            if ledger is not None:
                return ledger
            raise ToolResolutionError(capability, resolution)
        source_rows = [row for row in resolution.data.get("source_evidence") or []
                       if isinstance(row, dict) and str(row.get("url") or "").startswith(("http://", "https://"))]
        if not source_rows:
            source_rows = [{"url": url, "fact_as_of": resolution.fact_as_of, "data": resolution.data}
                           for url in resolution.data.get("source_urls") or []
                           if str(url).startswith(("http://", "https://"))]
        if not source_rows:
            raise RuntimeError(f"tool resolution has no public source URLs: {capability}")
        results = []
        for row in source_rows:
            excerpt = json.dumps(row.get("data") if isinstance(row.get("data"), dict) else {}, ensure_ascii=False, sort_keys=True)[:8000]
            results.append({"url": str(row["url"]), "title": str(resolution.data.get("source") or capability),
                            "excerpt_text": excerpt, "fact_as_of": str(row.get("fact_as_of") or resolution.fact_as_of),
                            "raw_artifact_ref": resolution.raw_artifact_ref})
        return {
            "url": results[0]["url"], "text": results[0]["excerpt_text"],
            "raw_artifact_ref": resolution.raw_artifact_ref, "results": results,
        }

    def _exact_close_ledger_result(
        self, request: FactRequest, requirement: dict[str, Any], operation: str,
    ) -> dict[str, Any] | None:
        """Revalidate qualified ledger JSON through the same contract as a live tool result."""
        if operation not in {"market_snapshot", "market_breadth", "holding_snapshot"} or request.finality != "official_close":
            return None
        window = requirement.get("window") if isinstance(requirement.get("window"), dict) else {}
        if (
            window.get("mode") != "exact"
            or str(window.get("start") or "") != request.required_at
            or str(window.get("end") or "") != request.required_at
        ):
            return None
        try:
            packet_as_of = datetime.fromisoformat(str(self.contract.get("as_of") or "").replace("Z", "+00:00"))
            if packet_as_of.tzinfo is None:
                return None
        except ValueError:
            return None
        if operation == "market_breadth":
            return self._exact_close_breadth_ledger_result(request, packet_as_of)
        field = "indices" if operation == "market_snapshot" else "quotes"
        expected = {str(value) for value in request.inputs.get("symbols") or [] if str(value)}
        if not expected:
            return None

        variants: dict[str, dict[str, dict[str, Any]]] = {symbol: {} for symbol in expected}
        candidates: list[tuple[dict[str, Any], str, str, list[dict[str, Any]]]] = []
        for entry in self.daily_ledger:
            if not isinstance(entry, dict) or entry.get("coverage_state") != "observed":
                continue
            url = str(entry.get("url") or "")
            parsed_url = urlsplit(url)
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname or parsed_url.username or parsed_url.password:
                continue
            try:
                known_at = datetime.fromisoformat(str(entry.get("known_at") or "").replace("Z", "+00:00"))
                payload = json.loads(str(entry.get("text") or ""))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if known_at.tzinfo is None or known_at > packet_as_of or not isinstance(payload, dict):
                continue
            if payload.get("finality") != request.finality or not isinstance(payload.get(field), list):
                continue
            selected: list[dict[str, Any]] = []
            for value in payload[field]:
                if not isinstance(value, dict) or str(value.get("symbol") or "") not in expected:
                    continue
                row = json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))
                symbol = str(row["symbol"])
                signature = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                variants[symbol][signature] = row
                selected.append(row)
            if selected:
                candidates.append((entry, url, str(entry.get("title") or "daily evidence ledger"), selected))
        if any(len(rows) != 1 for rows in variants.values()):
            return None

        rows = [next(iter(variants[symbol].values())) for symbol in request.inputs["symbols"]]
        data = {
            field: rows, "finality": request.finality, "source": "daily_evidence_ledger",
            "source_urls": list(dict.fromkeys(url for _, url, _, _ in candidates)),
        }
        if validate_capability_data(request, request.required_at, data) is not None:
            return None
        selected_signatures = {
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for row in rows
        }
        results = []
        for _, url, title, source_rows in candidates:
            source_rows = [
                row for row in source_rows
                if json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) in selected_signatures
            ]
            if not source_rows:
                continue
            excerpt = json.dumps({field: source_rows, "finality": request.finality}, ensure_ascii=False, sort_keys=True)
            results.append({
                "url": url, "title": title, "excerpt_text": excerpt,
                "fact_as_of": request.required_at, "raw_artifact_ref": None,
            })
        if not results:
            return None
        return {
            "url": results[0]["url"], "text": results[0]["excerpt_text"],
            "source": "daily_evidence_ledger", "raw_artifact_ref": None, "results": results,
        }

    def _exact_close_breadth_ledger_result(
        self, request: FactRequest, packet_as_of: datetime,
    ) -> dict[str, Any] | None:
        variants: dict[str, dict[str, Any]] = {}
        candidates: list[tuple[str, str, dict[str, Any]]] = []
        for entry in self.daily_ledger:
            if not isinstance(entry, dict) or entry.get("coverage_state") != "observed":
                continue
            url = str(entry.get("url") or "")
            parsed_url = urlsplit(url)
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname or parsed_url.username or parsed_url.password:
                continue
            try:
                known_at = datetime.fromisoformat(str(entry.get("known_at") or "").replace("Z", "+00:00"))
                payload = json.loads(str(entry.get("text") or ""))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if known_at.tzinfo is None or known_at > packet_as_of or not isinstance(payload, dict):
                continue
            core = {
                "trading_date": payload.get("trading_date"), "breadth": payload.get("breadth"),
                "finality": payload.get("finality"),
            }
            if core["finality"] != request.finality or not isinstance(core["breadth"], dict):
                continue
            signature = json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            variants[signature] = core
            candidates.append((url, str(entry.get("title") or "daily evidence ledger"), core))
        if len(variants) != 1:
            return None
        core = next(iter(variants.values()))
        data = {
            **core, "source": "daily_evidence_ledger",
            "source_urls": list(dict.fromkeys(url for url, _, _ in candidates)),
        }
        if validate_capability_data(request, request.required_at, data) is not None:
            return None
        results = [{
            "url": url, "title": title,
            "excerpt_text": json.dumps(payload, ensure_ascii=False, sort_keys=True),
            "fact_as_of": request.required_at, "raw_artifact_ref": None,
        } for url, title, payload in candidates]
        return {
            "url": results[0]["url"], "text": results[0]["excerpt_text"],
            "source": "daily_evidence_ledger", "raw_artifact_ref": None, "results": results,
        }

    def _cached_breadth(self, required_at: str, window_start: str, finality: str) -> dict[str, Any] | None:
        """Use only a runtime prefetch whose fact time is inside this contract."""
        # The prefetch is runtime state under the user's Companion home, not a
        # release asset under the immutable tool catalog. Looking beside tools
        # made a valid pre-freeze snapshot invisible and forced a later live
        # read, which the frozen-time gate correctly rejected.
        home = os.environ.get("AI_TRADING_COMPANION_HOME")
        snapshot_name = (
            "market-breadth-official-close-snapshot.json"
            if finality == "official_close" else "market-breadth-snapshot.json"
        )
        path = (
            Path(home) / "runtime" / snapshot_name
            if home else self.runner.catalog.root.parent / snapshot_name
        )
        try:
            cached = MarketBreadthSnapshotCache(path).select(
                required_at=required_at, window_start=window_start, finality=finality,
            )
            if cached is None:
                return None
            fact_as_of = str(cached["fact_as_of"])
            data = dict(cached["data"])
            urls = [str(url) for url in data.get("source_urls") or [] if str(url).startswith(("http://", "https://"))]
            if not urls:
                return None
            excerpt = json.dumps(data, ensure_ascii=False, sort_keys=True)[:8000]
            results = [{"url": url, "title": str(data.get("source") or "cn_market_breadth"),
                        "excerpt_text": excerpt, "fact_as_of": fact_as_of,
                        "raw_artifact_ref": cached.get("raw_artifact_ref")} for url in urls]
            return {"url": urls[0], "text": excerpt, "raw_artifact_ref": cached.get("raw_artifact_ref"), "results": results}
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None


class ToolResolutionError(RuntimeError):
    """Carry immutable resolution facts across the read-only research boundary."""

    def __init__(self, capability: str, resolution: Any) -> None:
        self.capability = capability
        self.resolution = resolution
        super().__init__(f"tool resolution failed: {capability}:{resolution.error_code}")


class DeterministicMarketBackend:
    """Serve market facts frozen by the caller; never performs network I/O."""

    def __init__(self, facts: dict[str, dict[str, Any]]) -> None:
        self.facts = json.loads(json.dumps(facts, ensure_ascii=False))

    def __call__(self, operation: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        result = self.facts.get(operation)
        if not isinstance(result, dict):
            raise ValueError(f"deterministic market fact not present: {operation}")
        return json.loads(json.dumps(result, ensure_ascii=False))


@dataclass(frozen=True)
class FrozenResearchResult:
    qualified: bool
    evidence: dict[str, Any]
    verifier: dict[str, Any]
    observations: list[dict[str, Any]]
    bundle_bytes: bytes
    bundle_sha256: str
    repair_rounds: int
    stage_failures: list[dict[str, Any]] = field(default_factory=list)


class ReadOnlyResearchExecutor:
    """Dispatch a finite plan to local read-only adapters."""

    def __init__(self, backends: dict[str, Callable[[str, dict[str, Any]], dict[str, Any]]], *, max_operations: int = 24) -> None:
        self.backends = backends
        self.max_operations = max(0, min(24, int(max_operations)))

    def validate_plan(self, plan: dict[str, Any]) -> list[dict[str, Any]]:
        if not isinstance(plan, dict) or set(plan) != {"version", "operations"} or plan.get("version") != 1:
            raise ResearchPlanError("research plan must match version 1 JSON object")
        operations = plan.get("operations")
        if not isinstance(operations, list) or len(operations) > self.max_operations:
            raise ResearchPlanError(f"research plan operations must be an array of at most {self.max_operations} items")
        required = {"requirement_key", "backend", "operation", "arguments", "fallback_backends"}
        for row in operations:
            if not isinstance(row, dict) or set(row) != required:
                raise ResearchPlanError("research operation has unsupported fields")
            backend, operation = row.get("backend"), row.get("operation")
            if backend not in _OPERATIONS or operation not in _OPERATIONS[backend]:
                raise ResearchPlanError("research plan requests a mutating or unsupported operation")
            if not str(row.get("requirement_key") or "").strip() or not isinstance(row.get("arguments"), dict):
                raise ResearchPlanError("research operation requires a requirement key and object arguments")
            fallbacks = row.get("fallback_backends")
            if not isinstance(fallbacks, list) or any(item not in _OPERATIONS for item in fallbacks):
                raise ResearchPlanError("research operation has invalid fallback backends")
        return sorted(operations, key=lambda row: _BACKEND_ORDER[row["backend"]])

    def execute(self, row: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        candidates = [row["backend"], *[item for item in row["fallback_backends"] if item != row["backend"]]]
        failures: list[str] = []
        resolution_failure: ToolResolutionError | None = None
        for backend in candidates:
            adapter = self.backends.get(backend)
            if adapter is None:
                failures.append(f"{backend}:not_configured")
                continue
            operation = row["operation"] if row["operation"] in _OPERATIONS[backend] else _fallback_operation(backend)
            try:
                result = adapter(operation, {**row["arguments"], "_requirement_key": row["requirement_key"]})
                if not isinstance(result, dict):
                    raise TypeError("backend result is not an object")
                return backend, {**result, "backend": backend}
            except Exception as exc:
                failures.append(f"{backend}:{type(exc).__name__}")
                if isinstance(exc, ToolResolutionError):
                    resolution_failure = exc
        if resolution_failure is not None:
            raise resolution_failure
        raise RuntimeError("all research backends failed: " + ",".join(failures))


class LocalResearchChain:
    def __init__(self, planner: Callable[[dict[str, Any], list[str], int], dict[str, Any]],
                 executor: ReadOnlyResearchExecutor, *, gate: EvidenceGate | None = None,
                 max_repairs: int | None = 2,
                 deadline: Callable[[], float] | None = None) -> None:
        self.planner = planner
        self.executor = executor
        self.gate = gate or EvidenceGate()
        self.max_repairs = None if max_repairs is None else max(0, int(max_repairs))
        self.deadline = deadline

    def run(self, packet: dict[str, Any], contract: dict[str, Any], *, attempt_id: str) -> FrozenResearchResult:
        boundary = AcquisitionBoundary(attempt_id)
        observations: list[dict[str, Any]] = []
        verifier: dict[str, Any] = {"passed": False, "problems": ["not_evaluated"], "missing_requirements": []}
        evidence: dict[str, Any] = {}
        # Acquire every deterministic blocker before the first Broker round.
        # These facts are time-sensitive; waiting for a planning response first
        # can turn an otherwise valid 09:45 snapshot into future evidence.
        preflight = _merge_mandatory_operations(
            {"version": 1, "operations": []}, contract,
            max_operations=self.executor.max_operations,
        )
        for row in self.executor.validate_plan(preflight):
            try:
                backend, result = self.executor.execute(row)
                observation, _ = boundary.observe(
                    row["operation"], {**row["arguments"], "requirement_key": row["requirement_key"]},
                    result, bool(result.get("results") or result.get("url") or result.get("text")),
                )
                _normalize_exact_close_fact_time(observation, contract, row["requirement_key"])
                observation["backend"] = backend
                observations.append(observation)
            except Exception as exc:
                failure = {
                    "attempt_id": attempt_id, "observation_id": f"failure-{len(observations) + 1}",
                    "tool": row["operation"], "backend": row["backend"], "operation": row["operation"],
                    "status": "failed", "ok": False, "non_empty": False,
                    "arguments": {**row["arguments"], "requirement_key": row["requirement_key"]},
                    "error_category": type(exc).__name__,
                }
                _attach_tool_resolution_failure(failure, exc)
                observations.append(failure)
        evidence = _compile_evidence(packet, contract, observations)
        verifier = self.gate.evaluate(
            evidence, contract, observations, str(packet.get("as_of") or contract.get("as_of") or ""),
            attempt_id=attempt_id,
        )
        if verifier.get("passed"):
            normalized = verifier.get("normalized_evidence") or evidence
            bundle_bytes, bundle_hash = freeze_evidence_bundle(normalized)
            return FrozenResearchResult(True, normalized, verifier, observations, bundle_bytes, bundle_hash, 0)
        round_number = 0
        while self.deadline is None or self.deadline() > 1.0:
            round_observation_start = len(observations)
            gaps = list(verifier.get("missing_requirements") or verifier.get("problems") or [])
            planning_packet = {
                **packet,
                "deterministic_injection": True,
                "research_discoveries": _discovery_digest(observations, contract),
                "attempted_research_urls": sorted({
                    str((item.get("arguments") or {}).get("url") or "")
                    for item in observations
                    if str((item.get("arguments") or {}).get("url") or "")
                }),
            }
            try:
                plan = self.planner(planning_packet, gaps, round_number)
            except BrokerError as exc:
                # Keep the deterministic observations from this frozen attempt
                # alive while a transient Planner/Broker fault is retried.
                # Re-entering the outer M0 stage would otherwise require a new
                # live breadth read, which can only be later than the frozen
                # time and must correctly be rejected by the tool gate.
                if exc.category in {
                    "broker_effort_unsupported", "broker_authentication", "broker_forbidden",
                    "broker_secret_rejected",
                }:
                    raise
                if exc.category == "broker_output_invalid":
                    broker_verifier = exc.verifier if isinstance(exc.verifier, dict) else {}
                    business_verifier = broker_verifier.get("business")
                    if isinstance(business_verifier, dict):
                        verifier = {
                            "passed": False,
                            "problems": list(business_verifier.get("problems") or ["broker_output_invalid"]),
                            "missing_requirements": list(business_verifier.get("missing_requirements") or []),
                        }
                    else:
                        verifier = {
                            "passed": False,
                            "problems": ["broker_output_invalid"],
                            "missing_requirements": [],
                        }
                observations.append({
                    "attempt_id": attempt_id, "observation_id": f"failure-{len(observations) + 1}",
                    "tool": "research_plan", "backend": "broker", "operation": "research_plan",
                    "status": "failed", "ok": False, "non_empty": False,
                    "arguments": {}, "error_category": exc.category,
                    "broker_request_id": exc.request_id,
                })
                round_number += 1
                if self.max_repairs is not None and round_number > self.max_repairs:
                    raise
                continue
            plan = _merge_mandatory_operations(
                plan, contract, max_operations=self.executor.max_operations,
                observations=observations, gaps=gaps,
            )
            operations = self.executor.validate_plan(plan)
            for row in operations:
                try:
                    backend, result = self.executor.execute(row)
                    observation, _ = boundary.observe(
                        row["operation"], {**row["arguments"], "requirement_key": row["requirement_key"]},
                        result, bool(result.get("results") or result.get("url") or result.get("text")),
                    )
                    _normalize_exact_close_fact_time(observation, contract, row["requirement_key"])
                    observation["backend"] = backend
                    observations.append(observation)
                except Exception as exc:
                    failure = {
                        "attempt_id": attempt_id, "observation_id": f"failure-{len(observations) + 1}",
                        "tool": row["operation"], "backend": row["backend"], "operation": row["operation"],
                        "status": "failed", "ok": False, "non_empty": False,
                        "arguments": {**row["arguments"], "requirement_key": row["requirement_key"]},
                        "error_category": type(exc).__name__,
                    }
                    _attach_tool_resolution_failure(failure, exc)
                    observations.append(failure)
            evidence = _compile_evidence(packet, contract, observations)
            verifier = self.gate.evaluate(
                evidence, contract, observations, str(packet.get("as_of") or contract.get("as_of") or ""),
                attempt_id=attempt_id,
            )
            current_round = observations[round_observation_start:]
            if not verifier.get("passed") and any(
                item.get("operation") == "web_read" and item.get("status") == "failed"
                for item in current_round
            ):
                for row in _fallback_read_rows(observations, contract, limit=6):
                    try:
                        backend, result = self.executor.execute(row)
                        observation, _ = boundary.observe(
                            row["operation"], {**row["arguments"], "requirement_key": row["requirement_key"]},
                            result, bool(result.get("results") or result.get("url") or result.get("text")),
                        )
                        _normalize_exact_close_fact_time(observation, contract, row["requirement_key"])
                        observation["backend"] = backend
                        observations.append(observation)
                    except Exception as exc:
                        observations.append({
                            "attempt_id": attempt_id, "observation_id": f"failure-{len(observations) + 1}",
                            "tool": "web_read", "backend": "gateway", "operation": "web_read",
                            "status": "failed", "ok": False, "non_empty": False,
                            "arguments": {**row["arguments"], "requirement_key": row["requirement_key"]},
                            "error_category": type(exc).__name__,
                        })
                evidence = _compile_evidence(packet, contract, observations)
                verifier = self.gate.evaluate(
                    evidence, contract, observations, str(packet.get("as_of") or contract.get("as_of") or ""),
                    attempt_id=attempt_id,
                )
            if verifier.get("passed"):
                normalized = verifier.get("normalized_evidence") or evidence
                bundle_bytes, bundle_hash = freeze_evidence_bundle(normalized)
                return FrozenResearchResult(True, normalized, verifier, observations, bundle_bytes, bundle_hash, round_number)
            round_number += 1
            if self.max_repairs is not None and round_number > self.max_repairs:
                break
        bundle_bytes, bundle_hash = freeze_evidence_bundle(evidence)
        verifier = {
            **verifier,
            "attempted_backends": sorted({route for item in observations for route in item.get("tool_attempts", [])}),
            "safe_boundary": "no_trading_action_qualified",
        }
        failure = {
            "type": "stage_failure", "stage": str(packet.get("stage") or "research"),
            "category": "evidence_insufficient",
            "stop_reason": "reliability_deadline" if self.deadline is not None else "configured_test_rounds",
            "problems": list(verifier.get("problems") or []),
        }
        return FrozenResearchResult(False, evidence, verifier, observations, bundle_bytes, bundle_hash,
                                    round_number, [failure])


def freeze_evidence_bundle(evidence: dict[str, Any]) -> tuple[bytes, str]:
    payload = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return payload, hashlib.sha256(payload).hexdigest()


def _verify_research_plan(packet: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
    operations = output.get("operations") if isinstance(output, dict) else None
    if not isinstance(operations, list):
        return {"passed": False, "problems": ["research_plan_operations_missing"]}
    requirements = {
        str(row.get("key") or ""): row
        for row in (packet.get("evidence_contract") or {}).get("requirements") or []
        if isinstance(row, dict) and str(row.get("key") or "")
    }
    gap_text = "\n".join(str(item) for item in packet.get("coverage_gaps") or [])
    required = {
        key for key, row in requirements.items()
        if row.get("blocking", True)
        and row.get("evidence_class") != "internal_runtime"
        and not (row.get("evidence_class") == "public_if_present" and not row.get("required_entities"))
        and (not gap_text or key in gap_text)
    }
    required -= {str(value) for value in packet.get("deterministic_requirement_keys") or []}
    planned = {
        str(row.get("requirement_key") or "")
        for row in operations if isinstance(row, dict)
    }
    problems = [f"research_plan_missing_requirement:{key}" for key in sorted(required - planned)]
    problems.extend(f"research_plan_unknown_requirement:{key}" for key in sorted(planned - set(requirements)))
    available_backends = set(packet.get("available_backends") or [])
    for row in operations:
        if not isinstance(row, dict):
            continue
        backend = str(row.get("backend") or "")
        operation = str(row.get("operation") or "")
        arguments = row.get("arguments") if isinstance(row.get("arguments"), dict) else {}
        key = str(row.get("requirement_key") or "")
        if operation == "web_search" and not str(arguments.get("query") or "").strip():
            problems.append(f"research_plan_operation_argument_missing:{key}:web_search:query")
        if operation in {"web_read", "web_browser"} and not str(arguments.get("url") or "").strip():
            problems.append(f"research_plan_operation_argument_missing:{key}:{operation}:url")
        if backend and backend not in available_backends:
            problems.append(f"research_plan_backend_unavailable:{backend}")
        for fallback in row.get("fallback_backends") or []:
            if fallback not in available_backends:
                problems.append(f"research_plan_backend_unavailable:{fallback}")
    discoveries = {
        str(row.get("requirement_key") or "")
        for row in packet.get("research_discoveries") or []
        if isinstance(row, dict) and row.get("url")
    }
    read_requirements = {
        str(row.get("requirement_key") or "")
        for row in operations
        if isinstance(row, dict) and row.get("operation") in {"web_read", "web_browser"}
    }
    for key in sorted(required.intersection(discoveries) - read_requirements):
        problems.append(f"research_plan_missing_verification_read:{key}")
    counts: dict[str, int] = {}
    for row in operations:
        if isinstance(row, dict):
            key = str(row.get("requirement_key") or "")
            counts[key] = counts.get(key, 0) + 1
    problems.extend(f"research_plan_excessive_operations:{key}" for key, count in sorted(counts.items()) if count > 8)
    time_rows = {
        str(row.get("requirement_key") or ""): row
        for row in (packet.get("market_time_context") or {}).get("requirements") or []
        if isinstance(row, dict)
    }
    close_markers = ("收盘", "闭市", "收市", "market close", "closing")
    open_markers = ("早盘", "开盘", "盘前", "pre-market", "opening")
    for row in operations:
        if not isinstance(row, dict) or row.get("operation") != "web_search":
            continue
        key = str(row.get("requirement_key") or "")
        time_row = time_rows.get(key) or {}
        query = str((row.get("arguments") or {}).get("query") or "").casefold()
        if key == "current_market_state" and time_row.get("is_local_market_close"):
            if not any(marker in query for marker in close_markers):
                problems.append("research_plan_market_close_query_missing_close_semantics")
            if any(marker in query for marker in open_markers):
                problems.append("research_plan_market_close_query_uses_open_semantics")
            utc_clock = str(time_row.get("start_utc") or "")[11:16]
            local_clock = str(time_row.get("start_local") or "")[11:16]
            if utc_clock and local_clock and utc_clock != local_clock and utc_clock in query and local_clock not in query:
                problems.append("research_plan_market_query_uses_utc_clock_as_local")
    frozen_market_urls = {
        str(row.get("url") or "")
        for row in packet.get("research_discoveries") or []
        if isinstance(row, dict) and str(row.get("source_kind") or "").startswith("deterministic_public_market")
    }
    if frozen_market_urls and "current_market_state" in required:
        planned_urls = {
            str((row.get("arguments") or {}).get("url") or "")
            for row in operations if isinstance(row, dict) and row.get("operation") == "web_read"
        }
        if not frozen_market_urls.intersection(planned_urls):
            problems.append("research_plan_missing_frozen_public_market_read")
    return {"passed": not problems, "problems": problems}


def _planner_time_context(packet: dict[str, Any]) -> dict[str, Any]:
    contract = packet.get("evidence_contract") or {}
    rows: list[dict[str, Any]] = []
    for requirement in contract.get("requirements") or []:
        if not isinstance(requirement, dict):
            continue
        window = requirement.get("window") or {}
        start, end = _parse_utc(window.get("start")), _parse_utc(window.get("end"))
        start_local = start.astimezone(_SHANGHAI) if start else None
        end_local = end.astimezone(_SHANGHAI) if end else None
        rows.append({
            "requirement_key": str(requirement.get("key") or ""),
            "window_mode": str(window.get("mode") or ""),
            "start_utc": start.isoformat().replace("+00:00", "Z") if start else None,
            "end_utc": end.isoformat().replace("+00:00", "Z") if end else None,
            "start_local": start_local.isoformat() if start_local else None,
            "end_local": end_local.isoformat() if end_local else None,
            "is_local_market_close": bool(
                window.get("mode") == "exact" and start_local and end_local
                and start_local == end_local and start_local.hour == 15 and start_local.minute == 0
            ),
        })
    frozen = _parse_utc(packet.get("as_of") or contract.get("as_of"))
    return {
        "timezone": "Asia/Shanghai",
        "frozen_utc": frozen.isoformat().replace("+00:00", "Z") if frozen else None,
        "frozen_local": frozen.astimezone(_SHANGHAI).isoformat() if frozen else None,
        "requirements": rows,
    }


def _public_market_close_discoveries(packet: dict[str, Any]) -> list[dict[str, Any]]:
    context = _planner_time_context(packet)
    close = next((
        row for row in context["requirements"]
        if row.get("requirement_key") == "current_market_state" and row.get("is_local_market_close")
    ), None)
    if not close:
        return []
    local_date = str(close["start_local"])[:10]
    compact = local_date.replace("-", "")
    rows = []
    for symbol, title in (
        ("sh000001", "上证指数"), ("sz399001", "深证成指"), ("sz399006", "创业板指"),
    ):
        rows.append({
            "requirement_key": "current_market_state",
            "url": (
                "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
                f"?param={symbol},day,{local_date},{local_date},1,qfq"
            ),
            "title": f"{title} {local_date} 公开历史日线",
            "excerpt": f"{title} frozen close {compact}",
            "fact_as_of": close["start_utc"],
            "published_at": None,
            "primary": False,
            "source_kind": "deterministic_public_market",
        })
    return rows


def _public_intraday_market_discoveries(packet: dict[str, Any]) -> list[dict[str, Any]]:
    context = _planner_time_context(packet)
    requirement = next((
        row for row in context["requirements"]
        if row.get("requirement_key") == "current_market_state" and not row.get("is_local_market_close")
    ), None)
    if not requirement:
        return []
    return [{
        "requirement_key": "current_market_state",
        "url": f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={symbol}",
        "title": f"Tencent public intraday minute series {symbol}",
        "excerpt": "Timestamped public intraday index minute series",
        "fact_as_of": requirement.get("end_utc"),
        "published_at": None,
        "primary": False,
        "source_kind": "deterministic_public_market_intraday",
    } for symbol in ("sh000001", "sz399001", "sz399006")]


def _merge_discoveries(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in groups:
        for row in group:
            if not isinstance(row, dict):
                continue
            url = str(row.get("url") or "")
            if not url or url in seen:
                continue
            seen.add(url)
            merged.append(row)
    return merged


def _discovery_read_repair_plan(
    contract: dict[str, Any], discoveries: list[dict[str, Any]], gaps: list[str], round_number: int,
) -> dict[str, Any] | None:
    """Deterministically verify known candidate URLs instead of asking the model to rediscover them."""
    if round_number <= 0 or not discoveries or not gaps:
        return None
    requirement_keys = [
        str(row.get("key") or "")
        for row in contract.get("requirements") or []
        if isinstance(row, dict) and str(row.get("key") or "")
    ]
    targets = {
        key for key in requirement_keys
        if any(gap == key or key in str(gap) for gap in gaps)
    }
    if not targets:
        return None
    operations: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    seen_urls: set[str] = set()
    for discovery in discoveries:
        key = str(discovery.get("requirement_key") or "")
        url = str(discovery.get("url") or "")
        if key not in targets or not url.startswith(("http://", "https://")) or url in seen_urls:
            continue
        if counts.get(key, 0) >= 4 or len(operations) >= 24:
            continue
        seen_urls.add(url)
        counts[key] = counts.get(key, 0) + 1
        operations.append({
            "requirement_key": key,
            "backend": "gateway",
            "operation": "web_read",
            "arguments": {
                "query": None,
                "categories": None,
                "url": url,
                "symbol": None,
                "render": "auto",
                "session_id": None,
                "actions": None,
            },
            "fallback_backends": [],
        })
    return {"version": 1, "operations": operations} if operations else None


def _planner_research_scope(value: Any) -> dict[str, Any]:
    """Expose only public search intent, not memories, account context, or credentials."""
    if not isinstance(value, dict):
        return {}
    allowed = {
        "task_name", "mode", "from_as_of", "categories", "standing_questions",
        "prior_public_context", "portfolio_research_context", "validation_context",
    }
    return {key: value[key] for key in allowed if key in value}


def _compile_evidence(packet: dict[str, Any], contract: dict[str, Any], observations: list[dict[str, Any]]) -> dict[str, Any]:
    sources: list[dict[str, Any]] = []
    refs_by_requirement: dict[str, list[str]] = {}
    requirements = {
        str(row.get("key") or ""): row
        for row in contract.get("requirements") or [] if isinstance(row, dict)
    }
    for observation in observations:
        if observation.get("operation") == "web_search":
            # Listings discover URLs only; a read/browser observation must supply proof.
            continue
        requirement = str((observation.get("arguments") or {}).get("requirement_key") or "")
        for item in observation.get("evidence_items") or []:
            ref = str(item.get("evidence_ref") or "")
            excerpt = str(item.get("excerpt_text") or "")
            if ref and excerpt and _item_in_requirement_window(item, requirements.get(requirement) or {}):
                sources.append({"evidence_ref": ref, "excerpt": excerpt, "analysis": f"支持 {requirement}"})
                refs_by_requirement.setdefault(requirement, []).append(ref)
    coverage = []
    for requirement in contract.get("requirements") or []:
        key = str(requirement.get("key") or "")
        refs = refs_by_requirement.get(key, [])
        allowed = set(requirement.get("allowed_coverage") or ["covered"])
        if requirement.get("evidence_class") == "internal_runtime":
            status = "covered" if int(requirement.get("internal_record_count") or 0) > 0 else "checked_no_change"
        elif requirement.get("evidence_class") == "public_if_present" and not requirement.get("required_entities"):
            status = "checked_no_change"
        elif refs:
            status = "covered"
        elif "checked_no_change" in allowed and _has_matching_negative_search(observations, requirement):
            status = "checked_no_change"
        else:
            status = "missing"
        coverage.append({"requirement_key": key, "status": status, "evidence_refs": refs})
    return {
        "schema_version": 3, "as_of": str(packet.get("as_of") or contract.get("as_of") or ""),
        "spoken_summary": "本地研究证据已按冻结合同采集。", "sources": sources, "coverage": coverage,
        "critical_gaps": [row["requirement_key"] for row in coverage if row["status"] == "missing"],
        "conflicts": [], "high_impact_events": [],
    }


def _has_matching_negative_search(observations: list[dict[str, Any]], requirement: dict[str, Any]) -> bool:
    key = str(requirement.get("key") or "")
    terms = [str(term).casefold() for term in requirement.get("negative_query_terms") or []]
    if not terms:
        return False
    queries = [
        str((observation.get("arguments") or {}).get("query") or "").casefold()
        for observation in observations
        if observation.get("operation") == "web_search"
        and observation.get("status") == "succeeded"
        and str((observation.get("arguments") or {}).get("requirement_key") or "") == key
        and all(term in str((observation.get("arguments") or {}).get("query") or "").casefold() for term in terms)
    ]
    return bool(queries) and all(
        any(entity.casefold() in query for query in queries)
        for entity in [str(value) for value in requirement.get("required_entities") or [] if str(value)]
    ) and any(
        observation.get("operation") == "web_search"
        and observation.get("status") == "succeeded"
        and str((observation.get("arguments") or {}).get("requirement_key") or "") == key
        and all(term in str((observation.get("arguments") or {}).get("query") or "").casefold() for term in terms)
        for observation in observations
    )


def _operation(
    requirement_key: str, backend: str, operation: str, *, query: str | None = None,
) -> dict[str, Any]:
    return {
        "requirement_key": requirement_key, "backend": backend, "operation": operation,
        "arguments": {
            "query": query, "categories": "news" if query else None, "url": None,
            "symbol": None, "render": None, "session_id": None, "actions": None,
        },
        "fallback_backends": [],
    }


def _merge_mandatory_operations(
    plan: dict[str, Any], contract: dict[str, Any], *, max_operations: int,
    observations: list[dict[str, Any]] | None = None, gaps: list[str] | None = None,
) -> dict[str, Any]:
    """The model may supplement research but never omit deterministic blocker reads."""
    proposed = list(plan.get("operations") or []) if isinstance(plan, dict) else []
    requirements = {
        str(item.get("key") or ""): item for item in contract.get("requirements") or []
        if isinstance(item, dict)
    }
    required: list[dict[str, Any]] = []
    if "current_market_state" in requirements:
        required.append(_operation("current_market_state", "market", "market_snapshot"))
    if "indices_close" in requirements:
        required.append(_operation("indices_close", "market", "market_snapshot"))
    if "market_breadth" in requirements:
        required.append(_operation("market_breadth", "market", "market_breadth"))
    if "turnover_compare" in requirements:
        required.append(_operation("turnover_compare", "market", "turnover_compare"))
    if "themes_and_capacity_cores" in requirements:
        required.append(_operation("themes_and_capacity_cores", "market", "sector_snapshot"))
    if "forum_and_sentiment" in requirements:
        required.extend((
            _operation("forum_and_sentiment", "market", "sentiment_snapshot"),
            _operation(
                "forum_and_sentiment", "gateway", "web_search",
                query="A股 收盘 论坛 股吧 市场情绪",
            ),
        ))
    if requirements.get("portfolio_market_state", {}).get("required_entities"):
        required.append(_operation("portfolio_market_state", "market", "holding_snapshot"))
    if requirements.get("portfolio_current_bar", {}).get("required_entities"):
        required.append(_operation("portfolio_current_bar", "market", "current_bar"))
    material_events = requirements.get("material_events_and_counterevidence") or {}
    if "checked_no_change" in set(material_events.get("allowed_coverage") or []):
        required.append(_operation(
            "material_events_and_counterevidence", "gateway", "web_search",
            query="A股 公告 政策 风险",
        ))
    event_requirement = requirements.get("portfolio_events_and_counterevidence") or {}
    for entity in [str(value) for value in event_requirement.get("required_entities") or [] if str(value)]:
        required.append(_operation(
            "portfolio_events_and_counterevidence", "gateway", "web_search",
            query=f"{entity} 公告 停复牌 财报 风险",
        ))
    completed = {
        (
            str((item.get("arguments") or {}).get("requirement_key") or ""),
            str(item.get("operation") or ""),
            str((item.get("arguments") or {}).get("query") or "") if item.get("operation") == "web_search" else "",
        )
        for item in observations or []
        if item.get("status") == "succeeded"
    }

    def needs_retry(item: dict[str, Any]) -> bool:
        key = str(item["requirement_key"])
        identity = (
            key, str(item["operation"]),
            str((item.get("arguments") or {}).get("query") or "") if item["operation"] == "web_search" else "",
        )
        if identity not in completed:
            failed_attempts = sum(
                1 for observation in observations or []
                if observation.get("status") == "failed"
                and str((observation.get("arguments") or {}).get("requirement_key") or "") == key
                and observation.get("operation") == identity[1]
                and (
                    identity[1] != "web_search"
                    or str((observation.get("arguments") or {}).get("query") or "") == identity[2]
                )
            )
            if failed_attempts >= 2:
                return False
            if any(
                item.get("status") == "failed"
                and str((item.get("arguments") or {}).get("requirement_key") or "") == key
                and item.get("operation") == identity[1]
                and item.get("tool_error_code") in {"tool_circuit_open", "tool_routes_exhausted_deterministic"}
                for item in observations or []
            ):
                return False
            return True
        # A technically successful call may still have failed semantic coverage
        # (for example an incomplete quote batch). Repeat only that named
        # requirement, never every already-qualified mandatory fact.
        return any(key in str(gap) for gap in (gaps or []))

    mandatory_operation_keys = {(item["requirement_key"], item["operation"]) for item in required}
    required = [item for item in required if needs_retry(item)]
    retained = [
        item for item in proposed
        if (str(item.get("requirement_key") or ""), str(item.get("operation") or "")) not in mandatory_operation_keys
    ]
    return {"version": 1, "operations": [*required, *retained][:max_operations]}


def _attach_tool_resolution_failure(observation: dict[str, Any], exc: Exception) -> None:
    if not isinstance(exc, ToolResolutionError):
        return
    resolution = exc.resolution
    observation["tool_error_code"] = resolution.error_code
    observation["tool_attempts"] = list(resolution.attempts)
    observation["tool_exit_code"] = resolution.exit_code
    observation["tool_diagnostic_artifact_ref"] = resolution.diagnostic_artifact_ref


def _deterministic_requirement_keys(contract: dict[str, Any]) -> list[str]:
    keys = {str(item.get("key") or "") for item in contract.get("requirements") or [] if isinstance(item, dict)}
    return sorted(keys.intersection({
        "current_market_state", "indices_close", "market_breadth", "portfolio_market_state",
        "portfolio_current_bar", "portfolio_events_and_counterevidence",
    }))


def _fallback_operation(backend: str) -> str:
    return {"market": "market_snapshot", "gateway": "web_search"}[backend]


def _discovery_digest(observations: list[dict[str, Any]], contract: dict[str, Any]) -> list[dict[str, Any]]:
    """Expose a small, inert URL shortlist to repair planning; never raw page bodies."""
    requirements = {
        str(row.get("key") or ""): row
        for row in contract.get("requirements") or [] if isinstance(row, dict)
    }
    candidates: list[tuple[tuple[int, int, int], dict[str, Any]]] = []
    seen: set[str] = set()
    for observation in reversed(observations):
        requirement = str((observation.get("arguments") or {}).get("requirement_key") or "")
        for item in observation.get("evidence_items") or []:
            url = str(item.get("url") or "")
            if not url or url in seen:
                continue
            if not _item_in_requirement_window(item, requirements.get(requirement) or {}, allow_undated=True):
                continue
            seen.add(url)
            candidate = {
                "requirement_key": requirement,
                "url": url,
                "title": str(item.get("title") or "")[:300],
                "excerpt": str(item.get("excerpt_text") or "")[:600],
                "fact_as_of": item.get("fact_as_of"),
                "published_at": item.get("published_at"),
                "primary": bool(item.get("primary")),
            }
            candidates.append((
                (
                    0 if item.get("fact_as_of") else 1,
                    0 if item.get("primary") else 1,
                    len(candidates),
                ),
                candidate,
            ))
    candidates.sort(key=lambda row: row[0])
    return [candidate for _, candidate in candidates[:16]]


def _fallback_read_rows(
    observations: list[dict[str, Any]], contract: dict[str, Any], *, limit: int,
) -> list[dict[str, Any]]:
    attempted = {
        str((item.get("arguments") or {}).get("url") or "")
        for item in observations
        if item.get("operation") == "web_read"
    }
    rows: list[dict[str, Any]] = []
    for candidate in _discovery_digest(observations, contract):
        url = str(candidate.get("url") or "")
        if not url or url in attempted:
            continue
        rows.append({
            "requirement_key": str(candidate.get("requirement_key") or ""),
            "backend": "gateway", "operation": "web_read",
            "arguments": {
                "query": None, "categories": None, "url": url, "symbol": None,
                "render": "auto", "session_id": None, "actions": None,
            },
            "fallback_backends": [],
        })
        if len(rows) >= max(0, limit):
            break
    return rows


def _normalize_exact_close_fact_time(
    observation: dict[str, Any], contract: dict[str, Any], requirement_key: str,
) -> None:
    # A fetched page may contain dynamic quotes newer than the frozen contract.
    # Never relabel that page as an earlier market fact; only tools that return a
    # timestamped historical observation may satisfy an exact window.
    return
    requirement = next((
        row for row in contract.get("requirements") or []
        if str(row.get("key") or "") == str(requirement_key)
    ), None)
    window = requirement.get("window") if isinstance(requirement, dict) else None
    if not isinstance(window, dict) or window.get("mode") != "exact" or window.get("start") != window.get("end"):
        return
    exact = _parse_utc(window.get("start"))
    as_of = _parse_utc(contract.get("as_of"))
    if exact is None or as_of is None:
        return
    close_markers = ("收盘", "闭市", "收市", "market close", "closed at", "closing")
    for item in observation.get("evidence_items") or []:
        fact = _parse_utc(item.get("fact_as_of"))
        excerpt = str(item.get("excerpt_text") or "").casefold()
        if (
            fact is not None and exact <= fact <= as_of and fact.date() == exact.date()
            and any(marker in excerpt for marker in close_markers)
        ):
            item["published_at"] = item.get("published_at") or item.get("fact_as_of")
            item["fact_as_of"] = exact.isoformat().replace("+00:00", "Z")


def _item_in_requirement_window(
    item: dict[str, Any], requirement: dict[str, Any], *, allow_undated: bool = False,
) -> bool:
    fact = _parse_utc(item.get("fact_as_of"))
    if fact is None:
        return allow_undated
    window = requirement.get("window") or {}
    start, end = _parse_utc(window.get("start")), _parse_utc(window.get("end"))
    if start is None or end is None:
        return False
    if window.get("mode") == "exact":
        return fact == start == end
    return start < fact <= end


def _parse_utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)
