"""Runtime-owned execution boundary for immutable local data tools."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_MANIFEST_CONTRACT = "ai-trading-tool-manifest/v1"
_CURRENT_CONTRACT = "ai-trading-tool-current/v1"
_RESULT_CONTRACT = "ai-trading-tool-result/v1"


class ToolLookupError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class FactRequest:
    """The caller-owned, versioned facts contract for one read-only capability."""

    contract_version: int
    capability: str
    required_at: str
    deadline_seconds: float
    inputs: dict[str, Any]

    def to_wire(self) -> dict[str, Any]:
        if self.contract_version != 1:
            raise ValueError("unsupported_fact_request_version")
        if not _safe_segment(self.capability):
            raise ValueError("invalid_tool_capability")
        if not isinstance(self.inputs, dict):
            raise ValueError("fact_request_inputs_must_be_object")
        _parse_timestamp(self.required_at)
        if self.deadline_seconds <= 0:
            raise ValueError("fact_request_deadline_must_be_positive")
        return {
            "contract": "ai-trading-fact-request/v1",
            "version": self.contract_version,
            "capability": self.capability,
            "required_at": self.required_at,
            "inputs": self.inputs,
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
    technical_validation: tuple[str, ...]
    error_code: str | None = None
    exit_code: int | None = None

    @classmethod
    def failed(cls, capability: str, code: str, *, tool_version: str | None = None,
               exit_code: int | None = None) -> "EvidenceResolution":
        return cls(
            succeeded=False,
            capability=capability,
            tool_version=tool_version,
            fact_as_of=None,
            acquired_at=_now(),
            data=None,
            raw_artifact_ref=None,
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


class ToolArtifactStore:
    """Opaque raw-output references. Retention and compression evolve behind this facade."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root) / ".artifacts"

    def write(self, raw: bytes) -> str:
        digest = hashlib.sha256(raw).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / f"{digest}.json"
        if not target.exists():
            temporary = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
            with temporary.open("xb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        return f"artifact:sha256:{digest}"

    def read(self, reference: str) -> bytes:
        prefix = "artifact:sha256:"
        digest = reference.removeprefix(prefix)
        if not reference.startswith(prefix) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("invalid_artifact_reference")
        return (self.root / f"{digest}.json").read_bytes()


class ToolRunner:
    """Deep execution facade: callers receive evidence, never process or package details."""

    def __init__(self, catalog: ToolCatalog, *, max_stdout_bytes: int = 1_000_000) -> None:
        self.catalog = catalog
        self.max_stdout_bytes = max(1, int(max_stdout_bytes))
        self.artifacts = ToolArtifactStore(catalog.root)

    def resolve(self, request: FactRequest) -> EvidenceResolution:
        try:
            wire_request = request.to_wire()
            tool = self.catalog.resolve(request.capability)
        except (ToolLookupError, ValueError) as exc:
            return EvidenceResolution.failed(request.capability, getattr(exc, "code", str(exc)))

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
                stdout, _stderr = process.communicate(
                    json.dumps(wire_request, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                    timeout=request.deadline_seconds,
                )
            except subprocess.TimeoutExpired:
                _terminate_process_tree(process)
                return EvidenceResolution.failed(request.capability, "tool_timeout", tool_version=tool.version)
            if process.returncode != 0:
                return EvidenceResolution.failed(
                    request.capability, "tool_process_failed", tool_version=tool.version, exit_code=process.returncode,
                )
            if len(stdout) > self.max_stdout_bytes:
                return EvidenceResolution.failed(request.capability, "tool_stdout_too_large", tool_version=tool.version)
            try:
                output = json.loads(stdout.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return EvidenceResolution.failed(request.capability, "tool_stdout_invalid_json", tool_version=tool.version)
            if not isinstance(output, dict):
                return EvidenceResolution.failed(request.capability, "tool_result_invalid", tool_version=tool.version)
            if set(output) != {"contract", "fact_as_of", "data"} or output.get("contract") != _RESULT_CONTRACT:
                return EvidenceResolution.failed(request.capability, "tool_result_invalid", tool_version=tool.version)
            if not isinstance(output.get("data"), dict):
                return EvidenceResolution.failed(request.capability, "tool_result_invalid", tool_version=tool.version)
            try:
                _parse_timestamp(str(output.get("fact_as_of") or ""))
            except ValueError:
                return EvidenceResolution.failed(request.capability, "tool_fact_as_of_invalid", tool_version=tool.version)
            return EvidenceResolution(
                succeeded=True,
                capability=request.capability,
                tool_version=tool.version,
                fact_as_of=str(output["fact_as_of"]),
                acquired_at=_now(),
                data=dict(output["data"]),
                raw_artifact_ref=self.artifacts.write(stdout),
                technical_validation=("tool_process_succeeded", "tool_result_schema_valid"),
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
