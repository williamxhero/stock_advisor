"""Local-only settings for optional integrations and recovery operations."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from urllib.parse import urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from pathlib import Path
from typing import Any, Callable

from .provider_client import current_codex_desktop_user_agent
from .provider_routes import (
    DEFAULT_WEIGHT, INITIAL_MODEL_CATALOG, SLOT_STAGES, build_provider_slots,
    catalog_entry, normalize_provider, recalculate_auto_tiers, redacted_provider, utc_now,
)


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
    "store": True,
    "hedge": {
        "enabled": True,
        # Formal Provider requests never exceed two concurrent live requests.
        "max_parallel": 2,
        "availability_probe_timeout_seconds": 5,
        "per_endpoint_timeout_seconds": 45,
        "per_endpoint_max_attempts": 1,
    },
    "retry": {
        "max_attempts": 5,
        "per_attempt_timeout_seconds": 90,
        "initial_backoff_seconds": 1,
        "max_backoff_seconds": 8,
        "circuit_breaker_failures": 5,
        "circuit_breaker_cooldown_seconds": 30,
        "probe_timeout_seconds": 180,
    },
    "efforts": {"research": "medium", "judgment": "medium", "fast": "medium"},
    "routing": {"family_mode": "auto", "default_weight": DEFAULT_WEIGHT,
                "model_catalog": INITIAL_MODEL_CATALOG, "price_catalog": {}},
    "endpoints": [
        {
            "id": "cpa", "enabled": False, "base_url": "http://yosef-server:8317/v1",
            "weight": DEFAULT_WEIGHT,
        },
        {
            "id": "direct-provider-example", "enabled": False, "base_url": "https://provider.example/v1",
            "weight": DEFAULT_WEIGHT,
        },
    ],
    "routes": [
        {"id": "cpa-research", "endpoint": "cpa", "model": "gpt-5.6-terra", "model_family": "openai",
         "enabled": False, "cost": {"tier": 0, "mode": "relative", "weight": DEFAULT_WEIGHT}, "preference": 0,
         "stages": ["research", "m0_research", "m1_research", "outcome_research", "chat_research"],
         "capabilities": ["stream", "json_schema", "race", "duel", "arbitration"]},
        {"id": "cpa-judgment", "endpoint": "cpa", "model": "gpt-5.6-sol", "model_family": "openai",
         "enabled": False, "cost": {"tier": 0, "mode": "relative", "weight": DEFAULT_WEIGHT}, "preference": 0,
         "stages": ["judgment", "m1_judgment", "m2", "reflection", "workflow_feedback"],
         "capabilities": ["stream", "json_schema", "race", "duel", "arbitration"]},
        {"id": "cpa-fast", "endpoint": "cpa", "model": "gpt-5.6-luna", "model_family": "openai",
         "enabled": False, "cost": {"tier": 0, "mode": "relative", "weight": DEFAULT_WEIGHT}, "preference": 0,
         "stages": ["fast", "m0_compose", "chat"],
         "capabilities": ["stream", "json_schema", "race", "duel", "arbitration"]},
        {"id": "example-claude-research", "endpoint": "direct-provider-example", "model": "claude-opus-5", "model_family": "anthropic",
         "enabled": False, "cost": {"tier": 100, "mode": "relative", "weight": DEFAULT_WEIGHT}, "preference": 0,
         "stages": ["research", "m0_research", "m1_research", "outcome_research", "chat_research"],
         "capabilities": ["stream", "json_schema", "race", "duel", "arbitration"]},
        {"id": "example-claude-judgment", "endpoint": "direct-provider-example", "model": "claude-opus-5", "model_family": "anthropic",
         "enabled": False, "cost": {"tier": 100, "mode": "relative", "weight": DEFAULT_WEIGHT}, "preference": 0,
         "stages": ["judgment", "m1_judgment", "m2", "reflection", "workflow_feedback"],
         "capabilities": ["stream", "json_schema", "race", "duel", "arbitration"]},
        {"id": "example-claude-fast", "endpoint": "direct-provider-example", "model": "claude-opus-5", "model_family": "anthropic",
         "enabled": False, "cost": {"tier": 100, "mode": "relative", "weight": DEFAULT_WEIGHT}, "preference": 0,
         "stages": ["fast", "m0_compose", "chat"],
         "capabilities": ["stream", "json_schema", "race", "duel", "arbitration"]},
        {"id": "direct-provider-example-research", "endpoint": "direct-provider-example", "model": "gpt-5.6-terra", "model_family": "openai",
         "enabled": False, "cost": {"tier": 200, "mode": "relative", "weight": DEFAULT_WEIGHT}, "preference": 0,
         "stages": ["research", "m0_research", "m1_research", "outcome_research", "chat_research"],
         "capabilities": ["stream", "json_schema", "race", "duel", "arbitration"]},
        {"id": "direct-provider-example-judgment", "endpoint": "direct-provider-example", "model": "gpt-5.6-sol", "model_family": "openai",
         "enabled": False, "cost": {"tier": 200, "mode": "relative", "weight": DEFAULT_WEIGHT}, "preference": 0,
         "stages": ["judgment", "m1_judgment", "m2", "reflection", "workflow_feedback"],
         "capabilities": ["stream", "json_schema", "race", "duel", "arbitration"]},
        {"id": "direct-provider-example-fast", "endpoint": "direct-provider-example", "model": "gpt-5.6-luna", "model_family": "openai",
         "enabled": False, "cost": {"tier": 200, "mode": "relative", "weight": DEFAULT_WEIGHT}, "preference": 0,
         "stages": ["fast", "m0_compose", "chat"],
         "capabilities": ["stream", "json_schema", "race", "duel", "arbitration"]},
    ],
}

DEFAULT_RESEARCH = {
    "web_access_gateway": {
        "mcp_url": "http://yosef-server:8930/mcp",
        "token": "",
        "search_timeout_seconds": 35,
        "read_timeout_seconds": 100,
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
    raw_provider = data.get("provider")
    if isinstance(raw_provider, dict) and not isinstance(raw_provider.get("routes"), list):
        # Legacy endpoint/global-slot settings are self-contained; adding the
        # normalized defaults before expansion would mask the compatibility path.
        provider = normalize_provider(_merge(_legacy_provider_defaults(), raw_provider))
    else:
        provider = normalize_provider(_merge(DEFAULT_PROVIDER, raw_provider))
    research = _merge(DEFAULT_RESEARCH, data.get("research"))
    if provider.get("enabled"):
        active_routes = [item for item in provider["routes"] if item.get("enabled", True)]
        if not active_routes:
            raise ValueError("provider configuration missing: enabled routes")
    return RuntimeSettings(
        backup=dict(data.get("backup") or {}), embedding=embedding,
        experiments=dict(data.get("experiments") or {}),
        provider=provider, research=research,
    )


def _legacy_provider_defaults() -> dict[str, Any]:
    return {
        "enabled": False,
        "base_url": "http://yosef-server:8317/v1",
        "store": True,
        "retry": DEFAULT_PROVIDER["retry"],
        "hedge": DEFAULT_PROVIDER["hedge"],
        "models": {
            "research": {"id": "gpt-5.6-terra", "effort": "medium"},
            "judgment": {"id": "gpt-5.6-sol", "effort": "medium"},
            "fast": {"id": "gpt-5.6-luna", "effort": "medium"},
        },
    }


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
    """Persist local Provider configuration (including local endpoint keys) atomically."""
    path = settings_path(home)
    data: dict[str, Any] = {}
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    data["provider"] = normalize_provider(provider, warn_legacy=False)
    if research is not None:
        data["research"] = research
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def provider_management(home: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Apply one validated Provider-management command atomically.

    This is intentionally the sole mutation seam for the desktop Provider page.
    It returns only a redacted configuration, so a local endpoint key can never
    round-trip into a UI response, audit event, or error body.
    """
    action = str(payload.get("action") or "").strip()
    current = load_settings(home).provider
    candidate = json.loads(json.dumps(current))
    endpoints = candidate["endpoints"]
    routes = candidate["routes"]

    def find(endpoint_id: str) -> dict[str, Any]:
        item = next((row for row in endpoints if row["id"] == endpoint_id), None)
        if item is None: raise ValueError("provider endpoint does not exist")
        return item

    if action == "list":
        return {"contract": "provider-management/v1", "provider": redacted_provider(candidate), "changes": []}
    if action == "preview":
        submitted = payload.get("provider")
        if not isinstance(submitted, dict): raise ValueError("provider preview requires a complete provider configuration")
        preview = normalize_provider(submitted, warn_legacy=False)
        tier_changes = recalculate_auto_tiers(preview)
        return {"contract": "provider-management/v1", "provider": redacted_provider(preview),
                "changes": [{"action": "preview", "tier_changes": tier_changes}]}
    if action == "probe":
        # A probe is strictly observational.  In particular, it must not touch
        # timestamps, replace the settings file, or turn an archived endpoint on.
        return {"contract": "provider-management/v1", "provider": redacted_provider(candidate), "changes": []}
    if action == "refresh_models":
        endpoint_id = _provider_id(payload.get("id")); endpoint = find(endpoint_id)
        discovered, state = _fetch_provider_models(endpoint)
        endpoint["available_models"] = discovered
        endpoint["model_directory_status"] = state
        _match_refreshed_models(candidate, endpoint_id)
        endpoint["models_updated_at"] = utc_now(); endpoint["updated_at"] = utc_now()
        candidate["last_model_refresh"] = {"endpoint_id": endpoint_id, "status": state}
    if action == "draft_slots":
        endpoint_id = _provider_id(payload.get("id"))
        families = _provider_families(payload.get("families"))
        return {"contract": "provider-management/v1", "draft_routes": build_provider_slots(endpoint_id, families, enabled=False)}
    if action == "clone":
        source_id = _provider_id(payload.get("source_id"))
        source = find(source_id)
        submitted = payload.get("endpoint")
        if not isinstance(submitted, dict): raise ValueError("copied Provider details are required")
        endpoint_id = _provider_id(submitted.get("id"))
        if endpoint_id == source_id or any(item["id"] == endpoint_id for item in endpoints):
            raise ValueError("copied Provider ID must be new")
        copied_endpoint = {key: value for key, value in source.items() if key not in {"id", "api_key", "updated_at"}}
        copied_endpoint.update(submitted); copied_endpoint["id"] = endpoint_id
        if isinstance(payload.get("api_key"), str):
            copied_endpoint["api_key"] = payload["api_key"]
        copied_routes = []
        for route in routes:
            if route["endpoint"] != source_id: continue
            copied = {key: value for key, value in route.items() if key != "endpoint"}
            slot = _route_slot(copied)
            copied["id"] = f"{endpoint_id}-{copied.get('model_family', '')}-{slot}"
            copied["slot"] = slot
            copied_routes.append(copied)
        result = provider_management(home, {
            "action": "upsert", "endpoint": copied_endpoint, "routes": copied_routes,
            "requires_new_key": True,
        })
        result["changes"][0]["action"] = "clone"
        return result
    if action == "upsert":
        submitted = payload.get("endpoint")
        if not isinstance(submitted, dict): raise ValueError("provider endpoint is required")
        endpoint_id = _provider_id(submitted.get("id"))
        existing = next((row for row in endpoints if row["id"] == endpoint_id), None)
        if existing is not None and payload.get("original_id") not in {None, endpoint_id}:
            raise ValueError("provider ID is immutable")
        if existing is None:
            existing = {"id": endpoint_id}
            endpoints.append(existing)
        existing["base_url"] = str(submitted.get("base_url") or "").strip().rstrip("/")
        parsed_url = urlparse(existing["base_url"])
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError("provider URL must be a valid http(s) API root or full endpoint URL")
        existing["enabled"] = bool(submitted.get("enabled", True))
        existing["archived"] = bool(submitted.get("archived", False))
        existing["weight"] = submitted.get("weight", candidate["routing"].get("default_weight", DEFAULT_WEIGHT))
        existing["families"] = _provider_families(submitted.get("families"))
        # Model discovery is metadata owned by the runtime.  UI saves must not
        # erase a successful prior /models refresh.
        if isinstance(submitted.get("available_models"), list):
            existing["available_models"] = submitted["available_models"]
        if submitted.get("models_updated_at"):
            existing["models_updated_at"] = submitted["models_updated_at"]
        existing["updated_at"] = utc_now()
        # Omitted / blank means retain the current secret.  Clones are forced
        # to supply one rather than borrowing the source Provider credential.
        submitted_key = submitted.get("api_key")
        if not (isinstance(submitted_key, str) and submitted_key.strip()):
            submitted_key = payload.get("api_key")
        if isinstance(submitted_key, str) and submitted_key.strip(): existing["api_key"] = submitted_key.strip()
        if payload.get("requires_new_key") and not (isinstance(submitted_key, str) and submitted_key.strip()):
            raise ValueError("copied Provider requires a new API key")
        submitted_routes = payload.get("routes")
        if not isinstance(submitted_routes, list) or not submitted_routes:
            raise ValueError("provider requires complete research, judgment, and fast model slots")
        slot_coverage: dict[str, set[str]] = {family: set() for family in existing["families"]}
        for raw in submitted_routes:
            if not isinstance(raw, dict): raise ValueError("provider route must be an object")
            family = str(raw.get("model_family") or "").lower()
            if family not in slot_coverage: raise ValueError("route family must be enabled on its Provider")
            slot = str(raw.get("slot") or _route_slot(raw))
            if slot not in SLOT_STAGES: raise ValueError("provider route must belong to research, judgment, or fast")
            slot_coverage[family].add(slot)
        missing_slots = [f"{family}:{slot}" for family, slots in slot_coverage.items()
                         for slot in SLOT_STAGES if slot not in slots]
        if missing_slots: raise ValueError("provider requires complete model slots: " + ", ".join(missing_slots))
        routes[:] = [row for row in routes if row["endpoint"] != endpoint_id]
        for raw in submitted_routes:
            if not isinstance(raw, dict): raise ValueError("provider route must be an object")
            route = dict(raw); route["endpoint"] = endpoint_id
            slot = str(route.pop("slot", "") or "")
            if slot and not route.get("stages"): route["stages"] = SLOT_STAGES.get(slot, [])
            if slot and not route.get("id"): route["id"] = f"{endpoint_id}-{route.get('model_family', '')}-{slot}"
            routes.append(route)
        if "family_mode" in payload:
            candidate["routing"]["family_mode"] = str(payload.get("family_mode") or "").lower()
        if isinstance(payload.get("model_catalog"), dict):
            candidate["routing"]["model_catalog"] = payload["model_catalog"]
    elif action == "refresh_models":
        # The refresh already updated the candidate above; normalize and save it
        # through the same atomic path as every other provider mutation.
        pass
    elif action == "archive":
        endpoint_id = _provider_id(payload.get("id")); endpoint = find(endpoint_id)
        endpoint["archived"] = True; endpoint["enabled"] = False; endpoint["updated_at"] = utc_now()
        for route in routes:
            if route["endpoint"] == endpoint_id: route["enabled"] = False
    elif action == "restore":
        endpoint_id = _provider_id(payload.get("id")); endpoint = find(endpoint_id)
        endpoint["archived"] = False; endpoint["enabled"] = bool(payload.get("enabled", True)); endpoint["updated_at"] = utc_now()
        for route in routes:
            if route["endpoint"] == endpoint_id: route["enabled"] = endpoint["enabled"]
    elif action == "permanent_delete":
        if payload.get("confirmed") is not True: raise ValueError("permanent Provider deletion requires confirmation")
        endpoint_id = _provider_id(payload.get("id")); find(endpoint_id)
        endpoints[:] = [row for row in endpoints if row["id"] != endpoint_id]
        routes[:] = [row for row in routes if row["endpoint"] != endpoint_id]
    elif action == "set_family_mode":
        candidate["routing"]["family_mode"] = str(payload.get("family_mode") or "").lower()
    elif action == "update_model_catalog":
        catalog = payload.get("model_catalog")
        if not isinstance(catalog, dict): raise ValueError("model catalog is required")
        candidate["routing"]["model_catalog"] = catalog
    else:
        raise ValueError("unsupported provider management action")

    # The legacy global switch remains for compatibility, but cannot leave the
    # local settings invalid after an archive/delete operation.  Endpoint state
    # is the operator-facing enablement control.
    candidate["enabled"] = any(
        endpoint.get("enabled", True) and not endpoint.get("archived", False)
        and any(route.get("endpoint") == endpoint["id"] and route.get("enabled", True) for route in routes)
        for endpoint in endpoints
    )

    normalized = normalize_provider(candidate, warn_legacy=False)
    tier_changes = recalculate_auto_tiers(normalized)
    save_provider_settings(home, normalized)
    return {
        "contract": "provider-management/v1", "provider": redacted_provider(normalized),
        "changes": [{"action": action, "endpoint_id": payload.get("id") or (payload.get("endpoint") or {}).get("id"),
                     "route_count": len(normalized["routes"]), "tier_changes": tier_changes}],
    }


def _provider_id(value: Any) -> str:
    endpoint_id = str(value or "").strip()
    if not endpoint_id or any(char.isspace() for char in endpoint_id): raise ValueError("provider ID is required and cannot contain whitespace")
    return endpoint_id


def _provider_families(value: Any) -> list[str]:
    if not isinstance(value, list) or not value: raise ValueError("provider requires at least one supported family")
    families = sorted({str(item).lower() for item in value})
    if any(item not in {"openai", "anthropic"} for item in families): raise ValueError("provider family must be openai or anthropic")
    return families


def _models_url(base_url: str) -> str:
    value = base_url.rstrip("/")
    for suffix in ("/chat/completions", "/responses"):
        if value.endswith(suffix):
            return f"{value.removesuffix(suffix)}/models"
    return f"{value}/models"


def _fetch_provider_models(endpoint: dict[str, Any]) -> tuple[list[str], str]:
    """Fetch only model identifiers; credentials never leave this runtime call."""
    key = str(endpoint.get("api_key") or "").strip()
    if not key:
        raise ValueError("Provider has no API key; cannot refresh its model list")
    request = Request(
        _models_url(str(endpoint.get("base_url") or "")),
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json",
                 "User-Agent": current_codex_desktop_user_agent()}, method="GET",
    )
    try:
        with urlopen(request, timeout=8) as response:
            raw_bytes = response.read()
    except HTTPError as exc:
        if exc.code in {404, 405}:
            return [], f"unknown_http_{exc.code}"
        return [], f"error_http_{exc.code}"
    except TimeoutError:
        return [], "error_timeout"
    except URLError:
        return [], "error_network"
    try:
        document = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return [], "invalid_json"
    rows = document.get("data") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        return [], "invalid_response"
    models = sorted({str(row.get("id") or "").strip() for row in rows if isinstance(row, dict) and str(row.get("id") or "").strip()})
    return models, "available" if models else "empty"


def _match_refreshed_models(provider: dict[str, Any], endpoint_id: str) -> None:
    """Map the endpoint's actual identifiers to catalog aliases slot by slot."""
    endpoint = next(item for item in provider["endpoints"] if item["id"] == endpoint_id)
    available = list(endpoint.get("available_models", []))
    normalized = {str(item).lower().replace("_", "-"): item for item in available}
    for route in provider["routes"]:
        if route.get("endpoint") != endpoint_id: continue
        desired = str(route.get("catalog_model") or route.get("model") or "")
        entry = catalog_entry(provider, str(route.get("model_family") or ""), desired)
        candidates = [desired]
        if entry:
            candidates = [entry["canonical_model"], *entry.get("aliases", [])]
        actual = next((normalized.get(item.lower().replace("_", "-")) for item in candidates if normalized.get(item.lower().replace("_", "-"))), None)
        if actual:
            route["model"] = actual
            route["catalog_model"] = entry["canonical_model"] if entry else desired
            route.pop("model_resolution", None)
        else:
            route["enabled"] = False
            route["model_resolution"] = "needs_selection"


def _route_slot(route: dict[str, Any]) -> str:
    explicit = str(route.get("slot") or "")
    if explicit in SLOT_STAGES: return explicit
    stages = set(route.get("stages") or [])
    for slot, slot_stages in SLOT_STAGES.items():
        if stages.intersection(slot_stages): return slot
    return ""


def migrate_embedded_provider_credentials(
    home: Path,
    *,
    credential_writer: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    """Compatibility migration retained for one release.

    Endpoint keys remain in the untracked local settings file.  ``credential_writer``
    is accepted only so older callers do not break; it is deliberately never used.
    """
    path = settings_path(home)
    if not path.exists():
        raise FileNotFoundError(f"Provider settings do not exist: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    provider = data.get("provider")
    if not isinstance(provider, dict):
        raise ValueError("provider settings must be an object")
    candidate = json.loads(json.dumps(provider))
    shared_secret = str(candidate.pop("api_key", "") or "").strip()
    endpoints = candidate.get("endpoints")
    if isinstance(endpoints, list) and shared_secret:
        for endpoint in endpoints:
            if isinstance(endpoint, dict) and not endpoint.get("api_key"):
                endpoint["api_key"] = shared_secret
    elif shared_secret:
        candidate["api_key"] = shared_secret
    normalized = normalize_provider(candidate, warn_legacy=False)

    data["provider"] = normalized
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return {
        "migrated_endpoint_count": 0,
        "endpoint_count": len(normalized["endpoints"]),
        "route_count": len(normalized["routes"]),
        "schema_version": normalized["schema_version"],
    }
