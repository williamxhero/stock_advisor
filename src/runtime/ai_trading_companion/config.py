"""Local-only settings for optional integrations and recovery operations."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RuntimeSettings:
    backup: dict[str, Any]
    embedding: dict[str, Any]
    experiments: dict[str, Any]

    @property
    def cloud_embedding_enabled(self) -> bool:
        return bool(self.embedding.get("enabled", False))


def settings_path(home: Path) -> Path:
    return Path(home) / "config" / "settings.local.json"


def load_settings(home: Path) -> RuntimeSettings:
    path = settings_path(home)
    if not path.exists():
        return RuntimeSettings(backup={}, embedding={"enabled": False}, experiments={})
    data = json.loads(path.read_text(encoding="utf-8"))
    embedding = dict(data.get("embedding") or {})
    if embedding.get("enabled"):
        required = ("base_url", "model", "api_key", "max_concurrency", "timeout_seconds", "max_attempts_per_record")
        missing = [name for name in required if embedding.get(name) in (None, "")]
        if missing:
            raise ValueError("embedding configuration missing: " + ", ".join(missing))
    return RuntimeSettings(
        backup=dict(data.get("backup") or {}), embedding=embedding,
        experiments=dict(data.get("experiments") or {}),
    )
