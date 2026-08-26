"""Local-only settings for optional integrations and recovery operations."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RuntimeSettings:
    backup: dict[str, Any]
    embedding: dict[str, Any]
    experiments: dict[str, Any]
    provider: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_PROVIDER))
    research: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_RESEARCH))

    @property
    def cloud_embedding_enabled(self) -> bool:
        return bool(self.embedding.get("enabled", False))

    @property
    def provider_enabled(self) -> bool:
        return bool(self.provider.get("enabled", False))


DEFAULT_PROVIDER = {
    "enabled": False,
    "base_url": "http://yosef-server:8317/v1",
    "credential_target": "AITradingCompanion/CPA",
    "store": True,
    "retry": {
        "max_attempts": 5,
        "initial_backoff_seconds": 1,
        "max_backoff_seconds": 8,
        "circuit_breaker_failures": 5,
        "circuit_breaker_cooldown_seconds": 30,
        "probe_timeout_seconds": 180,
    },
    "models": {
        "research": {"id": "gpt-5.6-terra", "effort": "medium"},
        "judgment": {"id": "gpt-5.6-sol", "effort": "medium"},
        "fast": {"id": "gpt-5.6-terra", "effort": "medium"},
    },
}

DEFAULT_RESEARCH = {
    "searxng": {"base_url": "http://yosef-server:8801", "timeout_seconds": 20},
    "playwright": {
        "edge_profile": "Profile 2",
        "profile_directory": "browser-profile",
        "download_limit_mb": 50,
    },
}


def settings_path(home: Path) -> Path:
    return Path(home) / "config" / "settings.local.json"


def load_settings(home: Path) -> RuntimeSettings:
    path = settings_path(home)
    if not path.exists():
        return RuntimeSettings(
            backup={}, embedding={"enabled": False}, experiments={},
            provider=dict(DEFAULT_PROVIDER), research=dict(DEFAULT_RESEARCH),
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    embedding = dict(data.get("embedding") or {})
    if embedding.get("enabled"):
        required = ("model", "max_concurrency", "timeout_seconds", "max_attempts_per_record")
        missing = [name for name in required if embedding.get(name) in (None, "")]
        if missing:
            raise ValueError("embedding configuration missing: " + ", ".join(missing))
    provider = _merge(DEFAULT_PROVIDER, data.get("provider"))
    research = _merge(DEFAULT_RESEARCH, data.get("research"))
    if provider.get("enabled"):
        if not str(provider.get("base_url") or "").strip():
            raise ValueError("provider configuration missing: base_url")
        if not str(provider.get("credential_target") or "").strip():
            raise ValueError("provider configuration missing: credential_target")
        models = provider.get("models") if isinstance(provider.get("models"), dict) else {}
        for slot in ("research", "judgment", "fast"):
            item = models.get(slot) if isinstance(models.get(slot), dict) else {}
            if not str(item.get("id") or "").strip():
                raise ValueError(f"provider configuration missing: models.{slot}.id")
    return RuntimeSettings(
        backup=dict(data.get("backup") or {}), embedding=embedding,
        experiments=dict(data.get("experiments") or {}),
        provider=provider, research=research,
    )


def _merge(defaults: dict[str, Any], value: Any) -> dict[str, Any]:
    result = json.loads(json.dumps(defaults))
    if not isinstance(value, dict):
        return result
    for key, item in value.items():
        if isinstance(item, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], item)
        else:
            result[key] = item
    return result


def save_provider_settings(home: Path, provider: dict[str, Any], research: dict[str, Any] | None = None) -> None:
    """Persist non-secret Provider settings atomically; credentials live in Windows Credential Manager."""
    path = settings_path(home)
    data: dict[str, Any] = {}
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    data["provider"] = provider
    if research is not None:
        data["research"] = research
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
