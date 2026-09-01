"""Deterministic promotion and recovery for immutable local CLI tool packages."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .tooling import FactRequest, ToolCatalog, ToolRunner


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ToolLifecycleManager:
    """Promote only sandbox-verified candidates; runtime owns calls to this manager."""

    def __init__(self, tools_root: Path, *, need_reporter: Callable[[dict[str, Any]], Any] | None = None) -> None:
        self.root = Path(tools_root)
        self.need_reporter = need_reporter

    def publish(self, candidate: Path, canary_request: FactRequest) -> dict[str, Any]:
        candidate = Path(candidate)
        try:
            manifest = self._validate_candidate(candidate, canary_request)
        except Exception as exc:
            return self._rejected(canary_request.capability, None, f"candidate_validation:{type(exc).__name__}")
        capability, version = str(manifest["capability"]), str(manifest["version"])
        current_path = self.root / capability / "current.json"
        previous = current_path.read_bytes() if current_path.exists() else None
        target = self.root / capability / "versions" / version
        if target.exists():
            return self._rejected(capability, version, "immutable_version_exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copytree(candidate, target)
            (target / "manifest.json").write_text(json.dumps({
                "contract": "ai-trading-tool-manifest/v1", "capability": capability, "version": version,
                "state": "promoted", "command": [sys.executable, "tool.py"],
            }, ensure_ascii=False, sort_keys=True), encoding="utf-8")
            self._write_current(current_path, version)
            canary = ToolRunner(ToolCatalog(self.root)).resolve_with_fallback(canary_request)
            if not canary.succeeded:
                self._restore_current(current_path, previous)
                return self._rejected(capability, version, f"canary:{canary.error_code}", canary.raw_artifact_ref)
        except Exception as exc:
            self._restore_current(current_path, previous)
            return self._rejected(capability, version, f"promotion:{type(exc).__name__}")
        event = {"event": "published", "capability": capability, "version": version, "at": _now()}
        self._audit(event)
        return {"state": "published", **event}

    def rollback(self, capability: str) -> dict[str, Any]:
        current = self.root / capability / "current.json"
        try:
            active = json.loads(current.read_text(encoding="utf-8"))["version"]
            versions = sorted(path.name for path in (self.root / capability / "versions").iterdir() if path.is_dir() and path.name != active)
            target = versions[-1]
        except (FileNotFoundError, KeyError, IndexError, json.JSONDecodeError):
            raise ValueError("tool_rollback_unavailable")
        self._write_current(current, target)
        event = {"event": "rolled_back", "capability": capability, "from_version": active, "version": target, "at": _now()}
        self._audit(event)
        return event

    def _validate_candidate(self, candidate: Path, request: FactRequest) -> dict[str, Any]:
        manifest = json.loads((candidate / "manifest.json").read_text(encoding="utf-8"))
        required = (candidate / "schema.json", candidate / "tool.py", candidate / "requirements.lock",
                    candidate / "fixtures" / "smoke-request.json", candidate / "tests" / "contract-check.json",
                    candidate / "build-record.json")
        if any(not path.is_file() for path in required):
            raise ValueError("candidate_package_incomplete")
        if (not isinstance(manifest, dict) or manifest.get("contract") != "ai-trading-tool-candidate-manifest/v1"
                or manifest.get("state") != "candidate" or manifest.get("capability") != request.capability):
            raise ValueError("candidate_manifest_invalid")
        version = str(manifest.get("version") or "")
        if not version or any(char not in "0123456789.-abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ" for char in version):
            raise ValueError("candidate_version_invalid")
        # An independent offline run validates the candidate before it is copied into the formal root.
        _sandbox_verify(candidate)
        return manifest

    def _write_current(self, path: Path, version: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps({"contract": "ai-trading-tool-current/v1", "version": version}, sort_keys=True), encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def _restore_current(path: Path, previous: bytes | None) -> None:
        if previous is None:
            path.unlink(missing_ok=True)
            return
        temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temporary.write_bytes(previous)
        temporary.replace(path)

    def _rejected(self, capability: str, version: str | None, reason: str, artifact: str | None = None) -> dict[str, Any]:
        event = {"event": "rejected", "capability": capability, "version": version, "reason": reason,
                 "raw_artifact_ref": artifact, "at": _now()}
        self._audit(event)
        if self.need_reporter is not None:
            try:
                self.need_reporter({
                    "contract": "ai-trading-capability-need/v1", "capability": capability,
                    "output_contract": {"result_contract": "ai-trading-tool-result/v1"}, "urgency": "high",
                    "examples": [], "failure_trace": {"error_code": reason, "raw_artifact_ref": artifact}, "source_hints": [],
                })
            except Exception:
                pass
        return {"state": "rejected", **event}

    def _audit(self, event: dict[str, Any]) -> None:
        path = self.root / ".lifecycle" / "audit.ndjson"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def _sandbox_verify(candidate: Path) -> None:
    request = (candidate / "fixtures" / "smoke-request.json").read_bytes()
    outcome = subprocess.run([sys.executable, "tool.py"], cwd=candidate, input=request, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, timeout=10, check=False)
    if outcome.returncode != 0:
        raise ValueError("candidate_sandbox_process_failed")
    try:
        output = json.loads(outcome.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("candidate_sandbox_invalid_json") from exc
    if not isinstance(output, dict) or set(output) != {"contract", "fact_as_of", "data"} or output.get("contract") != "ai-trading-tool-result/v1":
        raise ValueError("candidate_sandbox_contract_failed")
