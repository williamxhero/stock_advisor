"""Runtime-owned execution boundary for immutable local data tools."""
from __future__ import annotations

import hashlib
import gzip
import json
import os
import shutil
import signal
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .secret_guard import find_secrets


_MANIFEST_CONTRACT = "ai-trading-tool-manifest/v1"
_CURRENT_CONTRACT = "ai-trading-tool-current/v1"
_RESULT_CONTRACT = "ai-trading-tool-result/v1"


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
                 archive_max_bytes: int | None = None) -> None:
        self.catalog = catalog
        self.max_stdout_bytes = max(1, int(max_stdout_bytes))
        self.artifacts = ToolArtifactStore(catalog.root, max_bytes=archive_max_bytes)
        self._cache: dict[str, EvidenceResolution] = {}

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
                    json.dumps(wire_request, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
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
            return EvidenceResolution.failed(request.capability, exc.code)
        attempts: list[str] = []
        last: EvidenceResolution | None = None
        deadline = datetime.now(timezone.utc).timestamp() + request.deadline_seconds
        for tool in candidates:
            remaining = deadline - datetime.now(timezone.utc).timestamp()
            if remaining <= 0:
                break
            attempt_request = replace(request, deadline_seconds=remaining)
            result = self.resolve(attempt_request, _tool=tool)
            attempts.append(f"{tool.adapter}:{'succeeded' if result.succeeded else result.error_code}")
            last = result
            self._record_health(tool, result)
            if result.succeeded:
                resolved = replace(result, attempts=tuple(attempts))
                self._cache[cache_key] = resolved
                self._append_audit(request, resolved)
                return resolved
        failed = replace(last or EvidenceResolution.failed(request.capability, "tool_no_candidate_satisfied"), attempts=tuple(attempts))
        self._append_audit(request, failed)
        return failed

    def _append_audit(self, request: FactRequest, result: EvidenceResolution) -> None:
        audit_root = self.catalog.root / ".audit"
        audit_root.mkdir(parents=True, exist_ok=True)
        record = {
            "capability": request.capability, "required_at": request.required_at,
            "finality": request.finality, "succeeded": result.succeeded,
            "tool_version": result.tool_version, "error_code": result.error_code,
            "attempts": list(result.attempts), "acquired_at": result.acquired_at,
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
        elif result.error_code in {"tool_stdout_invalid_json", "tool_result_invalid", "tool_fact_as_of_invalid"}:
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
