from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
import uuid


def handle_memory_command(
    memory: Any, memory_space_id: str, command: dict[str, Any], export_directory: Path,
) -> dict[str, Any]:
    command_id = str(command.get("command_id") or "")
    command_type = str(command.get("type") or "")
    if not command_id:
        raise ValueError("memory command requires command_id")
    if command_type == "memory.export":
        exported = memory.export_space(memory_space_id)
        export_directory.mkdir(parents=True, exist_ok=True)
        stem = export_directory / f"memory-export-{command_id}"
        machine_path = stem.with_suffix(".json")
        human_path = stem.with_suffix(".md")
        _atomic_write(machine_path, json.dumps(exported, ensure_ascii=False, indent=2, sort_keys=True))
        _atomic_write(human_path, exported["human_markdown"])
        confirmation = memory.prepare_clear(memory_space_id, exported["export_sha256"])
        return {
            "command_id": command_id, "state": "exported", "export_sha256": exported["export_sha256"],
            "machine_export_path": str(machine_path), "human_export_path": str(human_path),
            "confirmation_token": confirmation["confirmation_token"],
            "confirmation_required": True,
        }
    if command_type == "memory.clear":
        if command.get("confirmed") is not True:
            return {"command_id": command_id, "state": "cancelled", "cleared": False}
        result = memory.clear_space(memory_space_id, str(command.get("confirmation_token") or ""))
        return {"command_id": command_id, "cleared": True, **result}
    raise ValueError(f"unsupported memory command: {command_type}")


def _atomic_write(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    with temporary.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
