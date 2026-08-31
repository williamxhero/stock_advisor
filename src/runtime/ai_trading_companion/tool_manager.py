"""Runtime-owned Exchange projection and validated lifecycle commands for Tool Manager."""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .store import CompanionStore
from .tool_lifecycle import ToolLifecycleManager


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ToolManagerRuntime:
    def __init__(self, store: CompanionStore, tools_root: Path, exchange_root: Path) -> None:
        self.store, self.tools_root, self.exchange_root = store, Path(tools_root), Path(exchange_root)

    def projection(self) -> dict[str, Any]:
        tools: list[dict[str, Any]] = []
        if self.tools_root.exists():
            for directory in sorted(path for path in self.tools_root.iterdir() if path.is_dir() and not path.name.startswith(".")):
                try:
                    current = json.loads((directory / "current.json").read_text(encoding="utf-8"))
                    version = str(current.get("version") or "")
                except (FileNotFoundError, json.JSONDecodeError):
                    version = ""
                health_path = self.tools_root / ".health" / f"{directory.name}-default-{version}.json"
                try:
                    health = json.loads(health_path.read_text(encoding="utf-8"))
                except (FileNotFoundError, json.JSONDecodeError):
                    health = {}
                tools.append({"capability": directory.name, "version": version,
                              "health": "degraded" if health.get("degraded") else "healthy",
                              "degrade_reason": health.get("degrade_reason") or "",
                              "audit_reference": str(self.tools_root / ".lifecycle" / "audit.ndjson")})
        return {"contract": "ai-trading-tool-manager-projection/v1", "updated_at": _now(),
                "needs": self.store.list_capability_needs(), "tools": tools}

    def publish_projection(self) -> Path:
        target = self.exchange_root / "tool-manager" / "projection.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(self.projection(), ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(temporary, target)
        return target

    def command(self, command: dict[str, Any]) -> dict[str, Any]:
        if command.get("contract") != "ai-trading-tool-manager-command/v1" or not str(command.get("command_id") or ""):
            raise ValueError("invalid_tool_manager_command")
        self.store.initialize()
        command_id, action = str(command["command_id"]), str(command.get("type") or "")
        prior = self.store.receipt(command_id, command)
        if prior is not None:
            return prior
        if action in {"pause", "retry"}:
            result = {"need": self.store.transition_capability_need(str(command.get("need_id") or ""), action)}
        elif action == "disable":
            capability = str(command.get("capability") or "")
            marker = self.tools_root / capability / "disabled.json"; marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps({"contract": "ai-trading-tool-disabled/v1", "reason": "user_disabled", "at": _now()}), encoding="utf-8")
            result = {"capability": capability, "state": "disabled"}
        elif action == "rollback":
            result = {"tool": ToolLifecycleManager(self.tools_root).rollback(str(command.get("capability") or ""))}
        else:
            raise ValueError("unsupported_tool_manager_command")
        self.store.save_receipt(command_id, None, action, command, result)
        self.publish_projection()
        return result
