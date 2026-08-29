"""Local settings for non-LLM integrations and recovery operations."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


DEFAULT_BROKER = {
    "url": "http://yosef-server:8817",
}


DEFAULT_RESEARCH = {
    "web_access_gateway": {
        "mcp_url": "http://yosef-server:8930/mcp",
        "token": "",
        "search_timeout_seconds": 35,
        "read_timeout_seconds": 100,
    },
}


@dataclass(frozen=True)
class RuntimeSettings:
    backup: dict[str, Any]
    embedding: dict[str, Any]
    experiments: dict[str, Any]
    broker: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_BROKER))
    research: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_RESEARCH))

    @property
    def cloud_embedding_enabled(self) -> bool:
        return bool(self.embedding.get("enabled", False))


def settings_path(home: Path) -> Path:
    return Path(home) / "config" / "settings.local.json"


def remove_legacy_provider_settings(home: Path) -> bool:
    """Remove obsolete direct-Provider configuration and local credentials atomically."""
    path = settings_path(home)
    if not path.exists():
        return False
    data = json.loads(path.read_text(encoding="utf-8"))
    changed = "provider" in data
    data.pop("provider", None)
    configured_broker = data.get("broker")
    configured_url = configured_broker.get("url") if isinstance(configured_broker, dict) else None
    broker = {"url": configured_url.strip() if isinstance(configured_url, str) and configured_url.strip() else DEFAULT_BROKER["url"]}
    if configured_broker != broker:
        data["broker"] = broker
        changed = True
    if not changed:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return True


def load_settings(home: Path) -> RuntimeSettings:
    remove_legacy_provider_settings(home)
    path = settings_path(home)
    if not path.exists():
        return RuntimeSettings(
            backup={}, embedding={"enabled": False}, experiments={},
            broker=dict(DEFAULT_BROKER), research=dict(DEFAULT_RESEARCH),
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    embedding = dict(data.get("embedding") or {})
    if embedding.get("enabled"):
        required = ("model", "max_concurrency", "timeout_seconds", "max_attempts_per_record")
        missing = [name for name in required if embedding.get(name) in (None, "")]
        if missing:
            raise ValueError("embedding configuration missing: " + ", ".join(missing))
    candidate = _merge(DEFAULT_BROKER, data.get("broker"))
    broker = {"url": str(candidate.get("url") or "").strip()}
    _validate_broker_url(broker["url"])
    return RuntimeSettings(
        backup=dict(data.get("backup") or {}), embedding=embedding,
        experiments=dict(data.get("experiments") or {}), broker=broker,
        research=_merge(DEFAULT_RESEARCH, data.get("research")),
    )


def save_research_settings(home: Path, research: dict[str, Any]) -> None:
    path = settings_path(home)
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data.pop("provider", None)
    data["research"] = research
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _merge(defaults: dict[str, Any], value: Any) -> dict[str, Any]:
    result = json.loads(json.dumps(defaults))
    if not isinstance(value, dict):
        return result
    for key, item in value.items():
        result[key] = _merge(result[key], item) if isinstance(item, dict) and isinstance(result.get(key), dict) else item
    return result


def _validate_broker_url(value: str) -> None:
    """The configurable endpoint is still a Broker base URL, never a provider API."""
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("broker configuration missing: url must be an absolute http(s) URL")
    if parsed.username is not None or parsed.password is not None or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("broker configuration invalid: url must be the unauthenticated Broker base URL without a path")
