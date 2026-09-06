"""Runtime-owned execution boundary for immutable local data tools."""
from __future__ import annotations

import hashlib
import gzip
import json
import math
import os
import re
import shutil
import signal
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .secret_guard import find_secrets


_MANIFEST_CONTRACT = "ai-trading-tool-manifest/v1"
_CURRENT_CONTRACT = "ai-trading-tool-current/v1"
_RESULT_CONTRACT = "ai-trading-tool-result/v1"
_SHANGHAI = timezone(timedelta(hours=8))


class ToolLookupError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ArtifactCapacityError(RuntimeError):
    pass


@dataclass(frozen=True)
class FactRequest:
    """The caller-owned, versioned facts contract for one read-only capability."""

    contract_version: int
    capability: str
    required_at: str
    deadline_seconds: float
    inputs: dict[str, Any]
    context: dict[str, Any] = field(default_factory=dict)
    freshness_seconds: float = 0.0
    finality: str = "observed"

    def to_wire(self) -> dict[str, Any]:
        if self.contract_version != 1:
            raise ValueError("unsupported_fact_request_version")
        if not _safe_segment(self.capability):
            raise ValueError("invalid_tool_capability")
        if not isinstance(self.inputs, dict) or not isinstance(self.context, dict):
            raise ValueError("fact_request_inputs_must_be_object")
        _parse_timestamp(self.required_at)
        if self.deadline_seconds <= 0:
            raise ValueError("fact_request_deadline_must_be_positive")
        if self.freshness_seconds < 0 or not self.finality:
            raise ValueError("invalid_fact_request_freshness")
        return {
            "contract": "ai-trading-fact-request/v1",
            "version": self.contract_version,
            "capability": self.capability,
            "required_at": self.required_at,
            "inputs": self.inputs,
            "context": self.context,
            "freshness_seconds": self.freshness_seconds,
            "finality": self.finality,
        }


@dataclass(frozen=True)
class EvidenceResolution:
    """Technical outcome of one tool call; EvidenceGate still owns semantic qualification."""

    succeeded: bool
    capability: str
    tool_version: str | None
    fact_as_of: str | None
    acquired_at: str
    data: dict[str, Any] | None
    raw_artifact_ref: str | None
    diagnostic_artifact_ref: str | None
    technical_validation: tuple[str, ...]
    error_code: str | None = None
    exit_code: int | None = None
    attempts: tuple[str, ...] = ()
    route_adapter: str | None = None

    @classmethod
    def failed(cls, capability: str, code: str, *, tool_version: str | None = None,
               exit_code: int | None = None, raw_artifact_ref: str | None = None,
               diagnostic_artifact_ref: str | None = None) -> "EvidenceResolution":
        return cls(
            succeeded=False,
            capability=capability,
            tool_version=tool_version,
            fact_as_of=None,
            acquired_at=_now(),
            data=None,
            raw_artifact_ref=raw_artifact_ref,
            diagnostic_artifact_ref=diagnostic_artifact_ref,
            technical_validation=(),
            error_code=code,
            exit_code=exit_code,
        )


@dataclass(frozen=True)
class PublishedTool:
    capability: str
    version: str
    command: tuple[str, ...]
    version_root: Path
    adapter: str = "default"


class ToolCatalog:
    """Resolve only a promoted immutable version selected by an atomic current file."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def resolve(self, capability: str) -> PublishedTool:
        if not _safe_segment(capability):
            raise ToolLookupError("invalid_tool_capability")
        if (self.root / capability / "disabled.json").exists():
            raise ToolLookupError("tool_disabled")
        current_path = self.root / capability / "current.json"
        try:
            current = json.loads(current_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ToolLookupError("tool_not_found") from exc
        except json.JSONDecodeError as exc:
            raise ToolLookupError("tool_current_invalid") from exc
        if not isinstance(current, dict) or current.get("contract") != _CURRENT_CONTRACT:
            raise ToolLookupError("tool_current_invalid")
        version = str(current.get("version") or "")
        if not _safe_segment(version):
            raise ToolLookupError("tool_current_invalid")
        version_root = self.root / capability / "versions" / version
        manifest_path = version_root / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ToolLookupError("tool_manifest_missing") from exc
        except json.JSONDecodeError as exc:
            raise ToolLookupError("tool_manifest_invalid") from exc
        command = manifest.get("command") if isinstance(manifest, dict) else None
        if (
            manifest.get("contract") != _MANIFEST_CONTRACT
            or manifest.get("capability") != capability
            or manifest.get("version") != version
            or manifest.get("state") != "promoted"
        ):
            raise ToolLookupError("tool_not_published")
        if not isinstance(command, list) or not command or any(not isinstance(part, str) or not part for part in command):
            raise ToolLookupError("tool_manifest_invalid")
        return PublishedTool(capability, version, tuple(command), version_root)

    def resolve_candidates(self, capability: str) -> list[PublishedTool]:
        routing_path = self.root / capability / "routing.json"
        if not routing_path.exists():
            return [self.resolve(capability)]
        try:
            routing = json.loads(routing_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ToolLookupError("tool_routing_invalid") from exc
        candidates = routing.get("candidates") if isinstance(routing, dict) else None
        if routing.get("contract") != "ai-trading-tool-routing/v1" or not isinstance(candidates, list) or not candidates:
            raise ToolLookupError("tool_routing_invalid")
        resolved: list[PublishedTool] = []
        for candidate in candidates:
            adapter = str(candidate.get("adapter") or "") if isinstance(candidate, dict) else ""
            version = str(candidate.get("version") or "") if isinstance(candidate, dict) else ""
            if not _safe_segment(adapter) or not _safe_segment(version):
                continue
            if adapter == "default":
                tool = self.resolve(capability)
                if tool.version == version:
                    resolved.append(replace(tool, adapter=adapter))
                continue
            version_root = self.root / capability / "adapters" / adapter / "versions" / version
            try:
                manifest = json.loads((version_root / "manifest.json").read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                continue
            command = manifest.get("command") if isinstance(manifest, dict) else None
            if (
                manifest.get("contract") == _MANIFEST_CONTRACT
                and manifest.get("capability") == capability
                and manifest.get("version") == version
                and manifest.get("state") == "promoted"
                and isinstance(command, list) and command
                and all(isinstance(part, str) and part for part in command)
            ):
                resolved.append(PublishedTool(capability, version, tuple(command), version_root, adapter))
        if not resolved:
            raise ToolLookupError("tool_not_published")
        return resolved


class ToolArtifactStore:
    """Opaque raw-output references. Retention and compression evolve behind this facade."""

    def __init__(self, root: Path, *, max_bytes: int | None = None) -> None:
        self.root = Path(root) / ".artifacts"
        self.max_bytes = None if max_bytes is None else max(0, int(max_bytes))

    def can_accept_new_call(self) -> bool:
        return self.max_bytes is None or self._used_bytes() < self.max_bytes

    def write(self, raw: bytes) -> str:
        digest = hashlib.sha256(raw).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / f"{digest}.gz"
        if not target.exists():
            compressed = gzip.compress(raw)
            if self.max_bytes is not None and self._used_bytes() + len(compressed) > self.max_bytes:
                raise ArtifactCapacityError("tool_archive_capacity_exceeded")
            temporary = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
            with temporary.open("xb") as handle:
                handle.write(compressed)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        return f"artifact:sha256:{digest}"

    def read(self, reference: str) -> bytes:
        prefix = "artifact:sha256:"
        digest = reference.removeprefix(prefix)
        if not reference.startswith(prefix) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("invalid_artifact_reference")
        with gzip.open(self.root / f"{digest}.gz", "rb") as handle:
            return handle.read()

    def _used_bytes(self) -> int:
        if not self.root.exists():
            return 0
        return sum(path.stat().st_size for path in self.root.glob("*.gz"))


class ToolRunner:
    """Deep execution facade: callers receive evidence, never process or package details."""

    def __init__(self, catalog: ToolCatalog, *, max_stdout_bytes: int = 1_000_000,
                 archive_max_bytes: int | None = None,
                 need_reporter: Callable[[dict[str, Any]], Any] | None = None) -> None:
        self.catalog = catalog
        self.max_stdout_bytes = max(1, int(max_stdout_bytes))
        self.artifacts = ToolArtifactStore(catalog.root, max_bytes=archive_max_bytes)
        self._cache: dict[str, EvidenceResolution] = {}
        self._open_circuits: set[tuple[str, str, str, str]] = set()
        self.need_reporter = need_reporter

    def resolve(self, request: FactRequest, *, _tool: PublishedTool | None = None) -> EvidenceResolution:
        try:
            wire_request = request.to_wire()
            tool = _tool or self.catalog.resolve(request.capability)
        except (ToolLookupError, ValueError) as exc:
            return EvidenceResolution.failed(request.capability, getattr(exc, "code", str(exc)))
        if find_secrets(json.dumps(wire_request, ensure_ascii=False, sort_keys=True)):
            return EvidenceResolution.failed(request.capability, "tool_secret_rejected", tool_version=tool.version)
        if not self.artifacts.can_accept_new_call():
            return EvidenceResolution.failed(request.capability, "tool_archive_capacity_exceeded", tool_version=tool.version)

        run_root = self.catalog.root / ".runs"
        run_root.mkdir(parents=True, exist_ok=True)
        temporary_directory = Path(tempfile.mkdtemp(prefix="tool-", dir=run_root))
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                tool.command,
                cwd=tool.version_root,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=(os.name != "nt"),
                creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
            )
            try:
                stdout, stderr = process.communicate(
                    # ASCII escaping keeps malformed provider surrogates inside
                    # the JSON envelope until the capability can sanitize its
                    # public text fields deterministically.
                    json.dumps(wire_request, ensure_ascii=True, separators=(",", ":")).encode("utf-8"),
                    timeout=request.deadline_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                _terminate_process_tree(process)
                partial_stdout = exc.output if isinstance(exc.output, bytes) else b""
                partial_stderr = exc.stderr if isinstance(exc.stderr, bytes) else b""
                completed_stdout, completed_stderr = process.communicate()
                stdout = _merge_output(partial_stdout, completed_stdout)
                stderr = _merge_output(partial_stderr, completed_stderr)
                if _contains_secret(stdout) or _contains_secret(stderr):
                    return EvidenceResolution.failed(request.capability, "tool_secret_rejected", tool_version=tool.version)
                try:
                    raw_artifact_ref = self.artifacts.write(stdout)
                    diagnostic_artifact_ref = self.artifacts.write(stderr) if stderr else None
                except ArtifactCapacityError:
                    return EvidenceResolution.failed(request.capability, "tool_archive_capacity_exceeded", tool_version=tool.version)
                return EvidenceResolution.failed(
                    request.capability, "tool_timeout", tool_version=tool.version,
                    raw_artifact_ref=raw_artifact_ref, diagnostic_artifact_ref=diagnostic_artifact_ref,
                )
            if _contains_secret(stdout) or _contains_secret(stderr):
                return EvidenceResolution.failed(request.capability, "tool_secret_rejected", tool_version=tool.version)
            try:
                raw_artifact_ref = self.artifacts.write(stdout)
                diagnostic_artifact_ref = self.artifacts.write(stderr) if stderr else None
            except ArtifactCapacityError:
                return EvidenceResolution.failed(request.capability, "tool_archive_capacity_exceeded", tool_version=tool.version)
            if process.returncode != 0:
                return EvidenceResolution.failed(
                    request.capability,
                    "tool_access_restricted" if process.returncode == 64 else "tool_process_failed",
                    tool_version=tool.version, exit_code=process.returncode,
                    raw_artifact_ref=raw_artifact_ref, diagnostic_artifact_ref=diagnostic_artifact_ref,
                )
            if len(stdout) > self.max_stdout_bytes:
                return EvidenceResolution.failed(
                    request.capability, "tool_stdout_too_large", tool_version=tool.version,
                    raw_artifact_ref=raw_artifact_ref, diagnostic_artifact_ref=diagnostic_artifact_ref,
                )
            try:
                output = json.loads(stdout.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return EvidenceResolution.failed(
                    request.capability, "tool_stdout_invalid_json", tool_version=tool.version,
                    raw_artifact_ref=raw_artifact_ref, diagnostic_artifact_ref=diagnostic_artifact_ref,
                )
            if not isinstance(output, dict):
                return EvidenceResolution.failed(
                    request.capability, "tool_result_invalid", tool_version=tool.version,
                    raw_artifact_ref=raw_artifact_ref, diagnostic_artifact_ref=diagnostic_artifact_ref,
                )
            if set(output) != {"contract", "fact_as_of", "data"} or output.get("contract") != _RESULT_CONTRACT:
                return EvidenceResolution.failed(
                    request.capability, "tool_result_invalid", tool_version=tool.version,
                    raw_artifact_ref=raw_artifact_ref, diagnostic_artifact_ref=diagnostic_artifact_ref,
                )
            if not isinstance(output.get("data"), dict):
                return EvidenceResolution.failed(
                    request.capability, "tool_result_invalid", tool_version=tool.version,
                    raw_artifact_ref=raw_artifact_ref, diagnostic_artifact_ref=diagnostic_artifact_ref,
                )
            try:
                _parse_timestamp(str(output.get("fact_as_of") or ""))
            except ValueError:
                return EvidenceResolution.failed(
                    request.capability, "tool_fact_as_of_invalid", tool_version=tool.version,
                    raw_artifact_ref=raw_artifact_ref, diagnostic_artifact_ref=diagnostic_artifact_ref,
                )
            validation_error = _validate_capability_result(request, output)
            if validation_error:
                return EvidenceResolution.failed(
                    request.capability, validation_error, tool_version=tool.version,
                    raw_artifact_ref=raw_artifact_ref, diagnostic_artifact_ref=diagnostic_artifact_ref,
                )
            return EvidenceResolution(
                succeeded=True,
                capability=request.capability,
                tool_version=tool.version,
                fact_as_of=str(output["fact_as_of"]),
                acquired_at=_now(),
                data=dict(output["data"]),
                raw_artifact_ref=raw_artifact_ref,
                diagnostic_artifact_ref=diagnostic_artifact_ref,
                technical_validation=("tool_process_succeeded", "tool_result_schema_valid", "raw_output_archived"),
                exit_code=process.returncode,
            )
        except OSError:
            return EvidenceResolution.failed(request.capability, "tool_process_start_failed", tool_version=tool.version)
        finally:
            if process is not None and process.poll() is None:
                _terminate_process_tree(process)
            shutil.rmtree(temporary_directory, ignore_errors=True)

    def read_artifact(self, reference: str) -> bytes:
        return self.artifacts.read(reference)

    def cached_resolution_is_valid(self, request: FactRequest, cached: dict[str, Any]) -> bool:
        """Revalidate a persisted resolution against its immutable raw output."""
        try:
            request.to_wire()
            raw_artifact_ref = str(cached["raw_artifact_ref"])
            output = json.loads(self.read_artifact(raw_artifact_ref))
            if (
                not isinstance(output, dict)
                or set(output) != {"contract", "fact_as_of", "data"}
                or output.get("contract") != _RESULT_CONTRACT
                or not isinstance(output.get("data"), dict)
                or output.get("fact_as_of") != cached.get("fact_as_of")
                or output.get("data") != cached.get("data")
            ):
                return False
            _parse_timestamp(str(output["fact_as_of"]))
            if _validate_capability_result(request, output):
                return False
            if "technical_validation" in cached:
                checks = cached["technical_validation"]
                required = {"tool_process_succeeded", "tool_result_schema_valid", "raw_output_archived"}
                if not isinstance(checks, list) or not required.issubset(checks):
                    return False
            return True
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return False

    def resolve_with_fallback(self, request: FactRequest) -> EvidenceResolution:
        cache_key = json.dumps(request.to_wire(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        cached = self._cache.get(cache_key)
        if cached is not None and request.freshness_seconds > 0:
            age = datetime.now(timezone.utc) - _parse_timestamp(cached.acquired_at)
            if age.total_seconds() <= request.freshness_seconds:
                return replace(cached, attempts=("cache:succeeded",))
        try:
            candidates = self._ordered_candidates(self.catalog.resolve_candidates(request.capability))
        except ToolLookupError as exc:
            failed = EvidenceResolution.failed(request.capability, exc.code)
            self._report_capability_need(request, failed)
            return failed
        attempts: list[str] = []
        failures: list[EvidenceResolution] = []
        last: EvidenceResolution | None = None
        deadline = datetime.now(timezone.utc).timestamp() + request.deadline_seconds
        for tool in candidates:
            circuit_key = self._circuit_key(request, tool)
            if circuit_key is not None and circuit_key in self._open_circuits:
                attempts.append(f"{tool.adapter}:circuit_open")
                continue
            remaining = deadline - datetime.now(timezone.utc).timestamp()
            if remaining <= 0:
                break
            attempt_request = replace(request, deadline_seconds=remaining)
            result = self.resolve(attempt_request, _tool=tool)
            if result.route_adapter is None:
                result = replace(result, route_adapter=tool.adapter)
            attempts.append(f"{tool.adapter}:{'succeeded' if result.succeeded else result.error_code}")
            last = result
            if not result.succeeded:
                failures.append(result)
            self._record_health(tool, result)
            if not result.succeeded and self._is_deterministic_failure(result):
                if circuit_key is not None:
                    self._open_circuits.add(circuit_key)
            if result.succeeded:
                resolved = replace(result, attempts=tuple(attempts))
                self._cache[cache_key] = resolved
                self._append_audit(request, resolved)
                if request.context.get("capability_need_on_success") is True:
                    self._report_capability_need(request, resolved)
                return resolved
        failed = replace(
            last or EvidenceResolution.failed(
                request.capability,
                "tool_circuit_open" if attempts and all(item.endswith(":circuit_open") for item in attempts)
                else "tool_no_candidate_satisfied",
            ),
            attempts=tuple(attempts),
        )
        if (request.context.get("cycle_id") or request.context.get("attempt_id")) and failures and len(failures) == len(candidates) and all(self._is_deterministic_failure(item) for item in failures):
            failed = replace(failed, error_code="tool_routes_exhausted_deterministic")
        self._append_audit(request, failed)
        self._report_capability_need(request, failed)
        return failed

    def _report_capability_need(self, request: FactRequest, result: EvidenceResolution) -> None:
        if self.need_reporter is None:
            return
        urgency = str(request.context.get("capability_need_urgency") or "normal")
        if urgency not in {"low", "normal", "high", "critical"}:
            urgency = "normal"
        source_hints = [value for value in request.inputs.values() if isinstance(value, str) and value.startswith(("http://", "https://"))]
        payload = {
            "contract": "ai-trading-capability-need/v1", "capability": request.capability,
            "output_contract": {"result_contract": _RESULT_CONTRACT, "finality": request.finality,
                                "input_keys": sorted(request.inputs)},
            "urgency": urgency, "examples": [{"inputs": request.inputs, "required_at": request.required_at}],
            "failure_trace": {"succeeded": result.succeeded, "error_code": result.error_code,
                              "attempts": list(result.attempts), "tool_version": result.tool_version,
                              "exit_code": result.exit_code,
                              "diagnostic_artifact_ref": result.diagnostic_artifact_ref,
                              "route": {"adapter": result.route_adapter, "version": result.tool_version},
                              "raw_artifact_ref": result.raw_artifact_ref},
            "source_hints": source_hints,
        }
        try:
            self.need_reporter(payload)
        except Exception:
            # Development backlog collection must never extend or fail the caller's research deadline.
            return

    def _append_audit(self, request: FactRequest, result: EvidenceResolution) -> None:
        audit_root = self.catalog.root / ".audit"
        audit_root.mkdir(parents=True, exist_ok=True)
        record = {
            "capability": request.capability, "required_at": request.required_at,
            "finality": request.finality, "succeeded": result.succeeded,
            "tool_version": result.tool_version, "error_code": result.error_code,
            "attempts": list(result.attempts), "acquired_at": result.acquired_at,
            "inputs": request.inputs, "fact_as_of": result.fact_as_of,
            "source": (result.data or {}).get("source") or (result.data or {}).get("url"),
            "raw_artifact_ref": result.raw_artifact_ref,
            "diagnostic_artifact_ref": result.diagnostic_artifact_ref,
            "exit_code": result.exit_code,
            "route": {"adapter": result.route_adapter, "version": result.tool_version},
            "technical_validation": list(result.technical_validation),
        }
        with (audit_root / "resolutions.ndjson").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def _ordered_candidates(self, candidates: list[PublishedTool]) -> list[PublishedTool]:
        return sorted(candidates, key=lambda tool: bool(self._health(tool).get("degraded")))

    def _record_health(self, tool: PublishedTool, result: EvidenceResolution) -> None:
        health = self._health(tool)
        health["attempts"] = int(health.get("attempts") or 0) + 1
        if result.succeeded:
            health["successes"] = int(health.get("successes") or 0) + 1
        elif self._is_deterministic_failure(result):
            health["degraded"] = True
            health["degrade_reason"] = result.error_code
        else:
            health["transient_failures"] = int(health.get("transient_failures") or 0) + 1
        path = self._health_path(tool)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(health, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)

    def _health(self, tool: PublishedTool) -> dict[str, Any]:
        path = self._health_path(tool)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _health_path(self, tool: PublishedTool) -> Path:
        safe_name = f"{tool.capability}-{tool.adapter}-{tool.version}".replace("/", "_")
        return self.catalog.root / ".health" / f"{safe_name}.json"

    @staticmethod
    def _circuit_key(request: FactRequest, tool: PublishedTool) -> tuple[str, str, str, str] | None:
        cycle_id = str(request.context.get("cycle_id") or request.context.get("attempt_id") or "").strip()
        return (cycle_id, request.capability, tool.adapter, tool.version) if cycle_id else None

    def _is_deterministic_failure(self, result: EvidenceResolution) -> bool:
        code = result.error_code or ""
        if code in {"tool_timeout", "tool_network_transient"}:
            return False
        if code == "tool_process_failed" and result.diagnostic_artifact_ref:
            try:
                diagnostic = self.read_artifact(result.diagnostic_artifact_ref).decode("utf-8", errors="replace").lower()
            except Exception:
                diagnostic = ""
            if "network read failed" in diagnostic or "upstream http 5" in diagnostic:
                return False
        return True


def _validate_capability_result(request: FactRequest, output: dict[str, Any]) -> str | None:
    """Reject quote data whose identity, session date, or finality cannot meet the request."""
    if request.capability == "cn_market_turnover_compare":
        return _validate_market_turnover_compare(request, output["data"], str(output["fact_as_of"]))
    if request.capability == "cn_market_sector_snapshot":
        return _validate_market_sector_snapshot(request, output["data"], str(output["fact_as_of"]))
    if request.capability == "cn_market_fund_flow_snapshot":
        return _validate_market_fund_flow_snapshot(request, output["data"], str(output["fact_as_of"]))
    if request.capability == "cn_market_event_snapshot":
        return _validate_market_event_snapshot(request, output["data"], str(output["fact_as_of"]))
    if request.capability == "cn_equity_announcement_snapshot":
        return _validate_equity_announcement_snapshot(request, output["data"], str(output["fact_as_of"]))
    if request.capability == "cn_market_index_batch":
        return _validate_market_indices(request, output["data"])
    if request.capability == "cn_market_snapshot":
        return _validate_market_snapshot(request, output["data"], str(output["fact_as_of"]))
    if request.capability == "cn_market_breadth":
        return _validate_market_breadth(request, output["data"], str(output["fact_as_of"]))
    if request.capability == "cn_equity_current_bar":
        return _validate_current_equity_bars(request, output["data"])
    if request.capability != "cn_equity_quote_batch":
        return None
    data = output["data"]
    quotes = data.get("quotes")
    expected = request.inputs.get("symbols")
    if not isinstance(quotes, list) or not isinstance(expected, list) or not quotes:
        return "tool_quote_result_invalid"
    expected_symbols = [str(symbol).strip() for symbol in expected]
    if len(set(expected_symbols)) != len(expected_symbols):
        return "tool_quote_request_invalid"
    expected_date = _parse_timestamp(request.required_at).astimezone(_SHANGHAI).date().isoformat()
    seen: list[str] = []
    for quote in quotes:
        if not isinstance(quote, dict):
            return "tool_quote_result_invalid"
        symbol = quote.get("symbol")
        if not isinstance(symbol, str) or symbol not in expected_symbols:
            return "tool_quote_symbol_mismatch"
        if symbol in seen:
            return "tool_quote_symbol_mismatch"
        seen.append(symbol)
        if quote.get("trading_date") != expected_date:
            return "tool_quote_trading_date_mismatch"
        if quote.get("market") != "CN-A" or quote.get("exchange") not in {"SSE", "SZSE", "BSE"}:
            return "tool_quote_identity_invalid"
        if not isinstance(quote.get("name"), str) or not quote["name"].strip() or not isinstance(quote.get("source"), str):
            return "tool_quote_identity_invalid"
        try:
            quote_time = _parse_timestamp(str(quote.get("quote_at") or "")).astimezone(_SHANGHAI)
            price = float(quote.get("price"))
        except (TypeError, ValueError):
            return "tool_quote_result_invalid"
        if quote_time.date().isoformat() != expected_date or price <= 0:
            return "tool_quote_trading_date_mismatch" if quote_time.date().isoformat() != expected_date else "tool_quote_result_invalid"
        if quote_time.astimezone(timezone.utc) > _parse_timestamp(request.required_at):
            return "tool_quote_after_required_at"
        if request.finality in {"close", "official_close"}:
            if quote_time.time().hour < 15 or quote.get("status") != "closed":
                return "tool_quote_finality_invalid"
        try:
            previous_close = float(quote.get("previous_close"))
            change = float(quote.get("change"))
            change_percent = float(quote.get("change_percent"))
        except (TypeError, ValueError):
            return "tool_quote_calculation_invalid"
        expected_change = round(price - previous_close, 4)
        expected_percent = round((price - previous_close) / previous_close * 100, 4) if previous_close > 0 else 0.0
        if previous_close < 0 or not math.isclose(change, expected_change, abs_tol=1e-4) or not math.isclose(change_percent, expected_percent, abs_tol=1e-4):
            return "tool_quote_calculation_invalid"
    if set(seen) != set(expected_symbols) or data.get("finality") != request.finality:
        return "tool_quote_finality_invalid" if data.get("finality") != request.finality else "tool_quote_symbol_mismatch"
    return None


def validate_capability_data(request: FactRequest, fact_as_of: str, data: dict[str, Any]) -> str | None:
    """Apply the promoted-tool result contract to an alternate structured evidence source."""
    return _validate_capability_result(request, {"fact_as_of": fact_as_of, "data": data})


def _validate_market_turnover_compare(
    request: FactRequest, data: dict[str, Any], fact_as_of: str,
) -> str | None:
    expected_date = _parse_timestamp(request.required_at).astimezone(_SHANGHAI).date().isoformat()
    try:
        observed = _parse_timestamp(fact_as_of)
        previous_date = datetime.fromisoformat(str(data.get("previous_trading_date") or "")).date()
        current_amount = float(data.get("current_amount"))
        previous_amount = float(data.get("previous_amount"))
        change_amount = float(data.get("change_amount"))
        change_ratio = float(data.get("change_ratio"))
    except (TypeError, ValueError):
        return "tool_market_turnover_result_invalid"
    if (
        data.get("trading_date") != expected_date
        or observed.astimezone(_SHANGHAI).date().isoformat() != expected_date
        or observed > _parse_timestamp(request.required_at)
        or previous_date >= datetime.fromisoformat(expected_date).date()
    ):
        return "tool_market_turnover_trading_date_mismatch"
    if request.finality in {"close", "official_close"} and observed.astimezone(_SHANGHAI).time().hour < 15:
        return "tool_market_turnover_finality_invalid"
    if data.get("finality") != request.finality:
        return "tool_market_turnover_finality_invalid"
    if data.get("scope") != "SSE+SZSE" or data.get("unit") != "CNY":
        return "tool_market_turnover_scope_invalid"
    if current_amount < 0 or previous_amount <= 0:
        return "tool_market_turnover_result_invalid"
    if not math.isclose(change_amount, current_amount - previous_amount, rel_tol=1e-9, abs_tol=0.01):
        return "tool_market_turnover_calculation_invalid"
    if not math.isclose(change_ratio, change_amount / previous_amount, rel_tol=1e-9, abs_tol=1e-9):
        return "tool_market_turnover_calculation_invalid"
    if not str(data.get("scope_definition") or "").strip() or not str(data.get("scope_note") or "").strip():
        return "tool_market_turnover_scope_invalid"
    if not _valid_market_turnover_breakdown(data.get("current_markets"), current_amount):
        return "tool_market_turnover_scope_invalid"
    if not _valid_market_turnover_breakdown(data.get("previous_markets"), previous_amount):
        return "tool_market_turnover_scope_invalid"
    if not str(data.get("source") or "").strip() or not _valid_public_source_urls(data.get("source_urls")):
        return "tool_market_source_urls_invalid"
    return None


def _valid_market_turnover_breakdown(value: Any, expected_amount: float) -> bool:
    if not isinstance(value, list) or len(value) != 2:
        return False
    by_exchange: dict[str, float] = {}
    for row in value:
        if not isinstance(row, dict):
            return False
        exchange = str(row.get("exchange") or "")
        identity = str(row.get("market_total_id") or "").strip()
        if exchange not in {"SSE", "SZSE"} or exchange in by_exchange or not identity or row.get("unit") != "CNY":
            return False
        try:
            amount = float(row.get("amount"))
        except (TypeError, ValueError):
            return False
        if amount <= 0:
            return False
        by_exchange[exchange] = amount
    return set(by_exchange) == {"SSE", "SZSE"} and math.isclose(
        sum(by_exchange.values()), expected_amount, rel_tol=1e-9, abs_tol=0.01,
    )


def _validate_market_sector_snapshot(
    request: FactRequest, data: dict[str, Any], fact_as_of: str,
) -> str | None:
    expected_date = _parse_timestamp(request.required_at).astimezone(_SHANGHAI).date().isoformat()
    try:
        observed = _parse_timestamp(fact_as_of)
    except ValueError:
        return "tool_market_sector_result_invalid"
    if (
        data.get("trading_date") != expected_date
        or observed.astimezone(_SHANGHAI).date().isoformat() != expected_date
        or observed > _parse_timestamp(request.required_at)
    ):
        return "tool_market_sector_trading_date_mismatch"
    if request.finality in {"close", "official_close"} and observed.astimezone(_SHANGHAI).time().hour < 15:
        return "tool_market_sector_finality_invalid"
    if data.get("finality") != request.finality:
        return "tool_market_sector_finality_invalid"
    if not str(data.get("source") or "").strip() or not _valid_public_source_urls(data.get("source_urls")):
        return "tool_market_source_urls_invalid"
    leaders, laggards = data.get("leaders"), data.get("laggards")
    if not isinstance(leaders, list) or not leaders or not isinstance(laggards, list) or not laggards:
        return "tool_market_sector_result_invalid"
    seen: set[str] = set()
    for row in [*leaders, *laggards]:
        if not isinstance(row, dict):
            return "tool_market_sector_result_invalid"
        board_id = str(row.get("board_id") or "").strip()
        name = str(row.get("name") or "").strip()
        core = row.get("core")
        if not board_id or board_id in seen or not name or row.get("kind") not in {"industry", "theme"}:
            return "tool_market_sector_identity_invalid"
        seen.add(board_id)
        if not isinstance(core, dict) or not re.fullmatch(r"\d{6}", str(core.get("symbol") or "")):
            return "tool_market_sector_core_invalid"
        if not str(core.get("name") or "").strip():
            return "tool_market_sector_core_invalid"
        try:
            float(row.get("change_percent"))
            amount = float(core.get("amount"))
            float(core.get("change_percent"))
        except (TypeError, ValueError):
            return "tool_market_sector_result_invalid"
        if amount < 0:
            return "tool_market_sector_result_invalid"
    if request.inputs.get("require_distribution") is True:
        distribution = data.get("distribution")
        if not isinstance(distribution, dict):
            return "tool_market_sector_distribution_missing"
        for kind in ("industry", "theme"):
            row = distribution.get(kind)
            if not isinstance(row, dict):
                return "tool_market_sector_distribution_missing"
            total = row.get("total")
            counts = [row.get("up"), row.get("down"), row.get("flat")]
            if (
                not isinstance(total, int) or isinstance(total, bool) or total <= 0
                or any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in counts)
                or sum(counts) != total
            ):
                return "tool_market_sector_distribution_invalid"
            try:
                float(row.get("median_change_percent"))
            except (TypeError, ValueError):
                return "tool_market_sector_distribution_invalid"
    return None


def _validate_market_fund_flow_snapshot(
    request: FactRequest, data: dict[str, Any], fact_as_of: str,
) -> str | None:
    expected_date = _parse_timestamp(request.required_at).astimezone(_SHANGHAI).date().isoformat()
    try:
        observed = _parse_timestamp(fact_as_of)
    except ValueError:
        return "tool_market_fund_flow_time_invalid"
    if (
        data.get("trading_date") != expected_date
        or observed.astimezone(_SHANGHAI).date().isoformat() != expected_date
        or data.get("finality") != request.finality
        or data.get("scope") != "SSE+SZSE"
        or data.get("unit") != "CNY"
    ):
        return "tool_market_fund_flow_identity_invalid"
    if request.finality in {"close", "official_close"} and observed.astimezone(_SHANGHAI).time().hour < 15:
        return "tool_market_fund_flow_finality_invalid"
    if not str(data.get("source") or "").strip() or not _valid_public_source_urls(data.get("source_urls")):
        return "tool_market_source_urls_invalid"
    if data.get("coverage_level") == "directional_sector":
        inflows = data.get("sector_inflow_leaders")
        outflows = data.get("sector_outflow_leaders")
        if not isinstance(inflows, list) or len(inflows) < 3 or not isinstance(outflows, list) or not outflows:
            return "tool_market_fund_flow_result_invalid"
        try:
            if any(
                not isinstance(row, dict)
                or not str(row.get("name") or "").strip()
                or isinstance(row.get("net_inflow"), bool)
                or float(row["net_inflow"]) <= 0
                for row in inflows
            ):
                return "tool_market_fund_flow_result_invalid"
        except (KeyError, TypeError, ValueError):
            return "tool_market_fund_flow_result_invalid"
        if any(not isinstance(row, dict) or not str(row.get("name") or "").strip() for row in outflows):
            return "tool_market_fund_flow_result_invalid"
        limitations = data.get("limitations")
        if not isinstance(limitations, list) or not {
            "full_market_net_flow_unavailable", "order_size_breakdown_unavailable",
        }.issubset({str(value) for value in limitations}):
            return "tool_market_fund_flow_result_invalid"
        return None
    markets = data.get("markets")
    combined = data.get("combined")
    fields = ("main_net_inflow", "small_net_inflow", "medium_net_inflow", "large_net_inflow", "super_large_net_inflow")
    if not isinstance(markets, list) or len(markets) != 2 or not isinstance(combined, dict):
        return "tool_market_fund_flow_result_invalid"
    if {str(row.get("exchange") or "") for row in markets if isinstance(row, dict)} != {"SSE", "SZSE"}:
        return "tool_market_fund_flow_result_invalid"
    try:
        for field in fields:
            values = [float(row[field]) for row in markets]
            if abs(float(combined[field]) - sum(values)) > 0.01:
                return "tool_market_fund_flow_total_mismatch"
    except (KeyError, TypeError, ValueError):
        return "tool_market_fund_flow_result_invalid"
    return None


def _validate_equity_announcement_snapshot(
    request: FactRequest, data: dict[str, Any], fact_as_of: str,
) -> str | None:
    expected = [str(value).strip() for value in request.inputs.get("symbols") or []]
    checked = data.get("checked_symbols")
    if not expected or len(set(expected)) != len(expected) or checked != expected:
        return "tool_announcement_symbols_invalid"
    start_text = str(request.inputs.get("start_date") or "")
    end_text = str(request.inputs.get("end_date") or "")
    try:
        start = datetime.fromisoformat(start_text).date()
        end = datetime.fromisoformat(end_text).date()
        observed = _parse_timestamp(fact_as_of)
    except ValueError:
        return "tool_announcement_window_invalid"
    if start > end or data.get("start_date") != start_text or data.get("end_date") != end_text:
        return "tool_announcement_window_invalid"
    if observed > _parse_timestamp(request.required_at):
        return "tool_announcement_time_invalid"
    if not str(data.get("source") or "").strip() or not _valid_public_source_urls(data.get("source_urls")):
        return "tool_market_source_urls_invalid"
    proofs = data.get("enumeration_proofs")
    if not isinstance(proofs, list) or {
        str(row.get("query_symbol") or "") for row in proofs if isinstance(row, dict)
    } != set(expected):
        return "tool_announcement_enumeration_incomplete"
    if any(
        not isinstance(row, dict)
        or row.get("authority") not in {"cninfo", "sse", "szse"}
        or row.get("start_date") != start_text or row.get("end_date") != end_text
        or row.get("pagination_complete") is not True
        for row in proofs
    ):
        return "tool_announcement_enumeration_incomplete"
    rows = data.get("announcements")
    if not isinstance(rows, list):
        return "tool_announcement_result_invalid"
    for row in rows:
        if not isinstance(row, dict) or str(row.get("symbol") or "") not in expected:
            return "tool_announcement_result_invalid"
        try:
            announcement_date = datetime.fromisoformat(str(row.get("announcement_date") or "")).date()
        except ValueError:
            return "tool_announcement_result_invalid"
        try:
            published_at = _parse_timestamp(str(row.get("published_at") or ""))
        except ValueError:
            return "tool_announcement_result_invalid"
        if (
            not start <= announcement_date <= end
            or not str(row.get("title") or "").strip()
            or not str(row.get("issuer") or "").strip()
            or not str(row.get("source_url") or "").startswith(("http://", "https://"))
            or published_at > _parse_timestamp(request.required_at)
        ):
            return "tool_announcement_result_invalid"
    return None


def _validate_market_event_snapshot(
    request: FactRequest, data: dict[str, Any], fact_as_of: str,
) -> str | None:
    expected_sources = [
        "eastmoney_daily_topic_report", "cls_depth_article", "ths_important_news",
    ]
    start_text = str(request.inputs.get("start_at") or "")
    end_text = str(request.inputs.get("end_at") or "")
    try:
        start = _parse_timestamp(start_text)
        end = _parse_timestamp(end_text)
        observed = _parse_timestamp(fact_as_of)
    except ValueError:
        return "tool_market_event_window_invalid"
    if (
        start >= end or data.get("start_at") != start_text or data.get("end_at") != end_text
        or observed != end or observed > _parse_timestamp(request.required_at)
    ):
        return "tool_market_event_window_invalid"
    if data.get("checked_sources") != expected_sources:
        return "tool_market_event_sources_invalid"
    checks = data.get("source_checks")
    if (
        not isinstance(checks, list)
        or [row.get("source") for row in checks if isinstance(row, dict)] != expected_sources
    ):
        return "tool_market_event_sources_invalid"
    if not str(data.get("source") or "").strip() or not _valid_public_source_urls(data.get("source_urls")):
        return "tool_market_source_urls_invalid"
    articles = data.get("articles")
    if not isinstance(articles, list) or data.get("matched_count") != sum(
        int(row.get("matched_count") or 0) for row in checks if isinstance(row, dict)
    ):
        return "tool_market_event_result_invalid"
    for row in articles:
        if not isinstance(row, dict) or row.get("source") not in expected_sources:
            return "tool_market_event_result_invalid"
        try:
            published = _parse_timestamp(str(row.get("published_at") or ""))
        except ValueError:
            return "tool_market_event_result_invalid"
        if not start < published <= end or not str(row.get("title") or "").strip():
            return "tool_market_event_result_invalid"
        if not _valid_public_source_urls([row.get("source_url")]):
            return "tool_market_source_urls_invalid"
    return None


def _valid_public_source_urls(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    for url in value:
        try:
            parsed = urlsplit(str(url or ""))
        except ValueError:
            return False
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return False
    return True


def _validate_current_equity_bars(request: FactRequest, data: dict[str, Any]) -> str | None:
    bars = data.get("bars")
    expected = request.inputs.get("symbols")
    freq = request.inputs.get("freq")
    if not isinstance(bars, list) or not isinstance(expected, list) or not bars or freq not in {"1m", "30m"}:
        return "tool_current_bar_result_invalid"
    expected_symbols = [str(symbol).strip() for symbol in expected]
    if len(set(expected_symbols)) != len(expected_symbols):
        return "tool_current_bar_request_invalid"
    expected_date = _parse_timestamp(request.required_at).astimezone(_SHANGHAI).date().isoformat()
    required_at = _parse_timestamp(request.required_at)
    seen: set[str] = set()
    for bar in bars:
        if not isinstance(bar, dict) or not isinstance(bar.get("symbol"), str):
            return "tool_current_bar_result_invalid"
        symbol = bar["symbol"]
        if symbol not in expected_symbols or symbol in seen:
            return "tool_current_bar_symbol_mismatch"
        seen.add(symbol)
        if bar.get("freq") != freq or bar.get("market") != "CN-A" or bar.get("exchange") not in {"SSE", "SZSE", "BSE"}:
            return "tool_current_bar_identity_invalid"
        try:
            interval_start = _parse_timestamp(str(bar.get("interval_start") or ""))
            interval_end = _parse_timestamp(str(bar.get("interval_end") or ""))
            observed_at = _parse_timestamp(str(bar.get("observed_at") or ""))
            last_trade_at = _parse_timestamp(str(bar.get("last_trade_at") or ""))
            open_price, high, low, close = (float(bar[field]) for field in ("open", "high", "low", "close"))
            volume, amount = float(bar.get("volume")), float(bar.get("amount"))
            freshness_ms = int(bar.get("freshness_ms"))
        except (KeyError, TypeError, ValueError):
            return "tool_current_bar_result_invalid"
        if (interval_start >= interval_end or observed_at > required_at or last_trade_at > observed_at
                or observed_at.astimezone(_SHANGHAI).date().isoformat() != expected_date):
            return "tool_current_bar_after_required_at" if observed_at > required_at else "tool_current_bar_time_invalid"
        if min(open_price, high, low, close) <= 0 or not (low <= open_price <= high and low <= close <= high) or volume < 0 or amount < 0:
            return "tool_current_bar_values_invalid"
        if freshness_ms < 0 or freshness_ms > 300_000:
            return "tool_current_bar_stale"
        if not isinstance(bar.get("is_final"), bool) or not isinstance(bar.get("degraded"), bool):
            return "tool_current_bar_status_invalid"
        if request.finality in {"close", "official_close"} and not bar["is_final"]:
            return "tool_current_bar_finality_invalid"
        if bar.get("source_semantics") not in {"native", "derived"} or not str(bar.get("provider") or "").strip():
            return "tool_current_bar_source_invalid"
    if seen != set(expected_symbols) or data.get("finality") != request.finality:
        return "tool_current_bar_symbol_mismatch"
    return None


def _validate_market_indices(request: FactRequest, data: dict[str, Any]) -> str | None:
    indices = data.get("indices")
    expected = request.inputs.get("symbols")
    if not isinstance(indices, list) or not isinstance(expected, list) or not indices:
        return "tool_market_result_invalid"
    expected_date = _parse_timestamp(request.required_at).astimezone(_SHANGHAI).date().isoformat()
    seen: list[str] = []
    for index in indices:
        if not isinstance(index, dict) or not isinstance(index.get("symbol"), str):
            return "tool_market_result_invalid"
        symbol = index["symbol"]
        if symbol not in expected or symbol in seen:
            return "tool_market_identity_invalid"
        seen.append(symbol)
        if index.get("trading_date") != expected_date or index.get("exchange") not in {"SSE", "SZSE"}:
            return "tool_market_trading_date_mismatch"
        if not isinstance(index.get("name"), str) or not index["name"].strip() or not isinstance(index.get("source"), str):
            return "tool_market_identity_invalid"
        try:
            moment = _parse_timestamp(str(index.get("quote_at") or "")).astimezone(_SHANGHAI)
            price = float(index.get("price"))
        except (TypeError, ValueError):
            return "tool_market_result_invalid"
        if moment.date().isoformat() != expected_date or price <= 0:
            return "tool_market_trading_date_mismatch"
        if moment.astimezone(timezone.utc) > _parse_timestamp(request.required_at):
            return "tool_market_after_required_at"
        if request.finality in {"close", "official_close"} and (moment.time().hour < 15 or index.get("status") != "closed"):
            return "tool_market_finality_invalid"
        try:
            previous_close = float(index.get("previous_close"))
            change = float(index.get("change"))
            change_percent = float(index.get("change_percent"))
        except (TypeError, ValueError):
            return "tool_market_calculation_invalid"
        expected_change = round(price - previous_close, 4)
        expected_percent = round((price - previous_close) / previous_close * 100, 4) if previous_close > 0 else 0.0
        if previous_close < 0 or not math.isclose(change, expected_change, abs_tol=1e-4) or not math.isclose(change_percent, expected_percent, abs_tol=1e-4):
            return "tool_market_calculation_invalid"
    if set(seen) != {str(value) for value in expected} or data.get("finality") != request.finality:
        return "tool_market_identity_invalid"
    return None


def _validate_market_snapshot(request: FactRequest, data: dict[str, Any], fact_as_of: str) -> str | None:
    expected_date = _parse_timestamp(request.required_at).astimezone(_SHANGHAI).date().isoformat()
    if data.get("is_trading_day") is False:
        return "tool_market_non_trading_day"
    if data.get("trading_date") != expected_date or data.get("finality") != request.finality:
        return "tool_market_trading_date_mismatch"
    try:
        observed = _parse_timestamp(fact_as_of).astimezone(_SHANGHAI)
    except ValueError:
        return "tool_market_result_invalid"
    if observed.date().isoformat() != expected_date:
        return "tool_market_trading_date_mismatch"
    if observed.astimezone(timezone.utc) > _parse_timestamp(request.required_at):
        return "tool_market_after_required_at"
    if request.finality in {"close", "official_close"} and observed.time().hour < 15:
        return "tool_market_finality_invalid"
    if not isinstance(data.get("source"), str) or not data["source"].strip():
        return "tool_market_identity_invalid"
    breadth = data.get("breadth")
    if not isinstance(breadth, dict):
        return "tool_market_result_invalid"
    try:
        if any(float(breadth[field]) < 0 for field in ("up", "down", "flat", "limit_up", "limit_down")):
            return "tool_market_result_invalid"
    except (KeyError, TypeError, ValueError):
        return "tool_market_result_invalid"
    for field in ("indices", "industries", "themes"):
        entries = data.get(field)
        if not isinstance(entries, list):
            return "tool_market_result_invalid"
    for field in ("industries", "themes"):
        for entry in data[field]:
            if not isinstance(entry, dict) or not str(entry.get("id") or "").strip() or not str(entry.get("name") or "").strip():
                return "tool_market_identity_invalid"
            try:
                float(entry.get("strength"))
            except (TypeError, ValueError):
                return "tool_market_result_invalid"
    return None


def _validate_market_breadth(request: FactRequest, data: dict[str, Any], fact_as_of: str) -> str | None:
    expected_date = _parse_timestamp(request.required_at).astimezone(_SHANGHAI).date().isoformat()
    if data.get("is_trading_day") is False:
        return "tool_market_non_trading_day"
    if data.get("trading_date") != expected_date or data.get("finality") != request.finality:
        return "tool_market_trading_date_mismatch"
    try:
        observed = _parse_timestamp(fact_as_of).astimezone(_SHANGHAI)
    except ValueError:
        return "tool_market_result_invalid"
    if observed.date().isoformat() != expected_date:
        return "tool_market_trading_date_mismatch"
    if observed.astimezone(timezone.utc) > _parse_timestamp(request.required_at):
        return "tool_market_after_required_at"
    if request.finality in {"close", "official_close"} and observed.time().hour < 15:
        return "tool_market_finality_invalid"
    if not isinstance(data.get("source"), str) or not data["source"].strip():
        return "tool_market_identity_invalid"
    breadth = data.get("breadth")
    if not isinstance(breadth, dict):
        return "tool_market_result_invalid"
    try:
        if any(float(breadth[field]) < 0 for field in ("up", "down", "flat")):
            return "tool_market_result_invalid"
        if any(float(breadth[field]) < 0 for field in ("limit_up", "limit_down") if field in breadth):
            return "tool_market_result_invalid"
    except (KeyError, TypeError, ValueError):
        return "tool_market_result_invalid"
    return None


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=5)


def _safe_segment(value: str) -> bool:
    return bool(value) and value not in {".", ".."} and all(char.isalnum() or char in {"-", "_", "."} for char in value)


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp_missing_timezone")
    return parsed.astimezone(timezone.utc)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _contains_secret(value: bytes) -> bool:
    return bool(find_secrets(value.decode("utf-8", errors="replace")))


def _merge_output(partial: bytes, completed: bytes) -> bytes:
    if completed.startswith(partial):
        return completed
    return partial + completed
