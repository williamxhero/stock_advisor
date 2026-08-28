"""Versioned, secret-safe Provider endpoint and route configuration."""
from __future__ import annotations

import copy
import re
import warnings
from datetime import datetime, timezone
from typing import Any

STAGES = frozenset({"research", "judgment", "fast", "m0_research", "m0_compose", "m1_research", "m1_judgment", "m2", "reflection", "workflow_feedback", "outcome_research", "chat_research", "chat"})
CAPABILITIES = frozenset({"stream", "json_schema", "race", "duel", "arbitration"})
KNOWN_FAMILIES = frozenset({"openai", "anthropic"})
FAMILY_MODES = frozenset({"auto", "openai", "anthropic"})
TRANSPORTS = frozenset({"responses", "chat_completions"})
DEFAULT_WEIGHT = 0.3
TIER_MODES = frozenset({"auto", "manual"})
AUTO_TIER_THRESHOLDS = (0.08, 0.15)
NEAR_COST_TOLERANCE = 0.15
REFERENCE_MODELS = {
    "research": ("openai", "gpt-5.6-terra"),
    "judgment": ("openai", "gpt-5.6-sol"),
    "fast": ("openai", "gpt-5.6-luna"),
}
INITIAL_MODEL_CATALOG = {
    "openai": {
        "gpt-5.6-sol": {"aliases": ["gpt-5.6-sol"], "price": {"currency": "USD", "input_per_million": 4, "output_per_million": 20}, "quality": {"research": 96, "judgment": 100, "fast": 70}},
        "gpt-5.6-terra": {"aliases": ["gpt-5.6-terra"], "price": {"currency": "USD", "input_per_million": 2, "output_per_million": 12}, "quality": {"research": 88, "judgment": 86, "fast": 84}},
        "gpt-5.6-luna": {"aliases": ["gpt-5.6-luna"], "price": {"currency": "USD", "input_per_million": .2, "output_per_million": 1.2}, "quality": {"research": 68, "judgment": 65, "fast": 92}},
    },
    "anthropic": {
        "claude-opus-5": {"aliases": ["claude-opus-5"], "price": {"currency": "USD", "input_per_million": 5, "output_per_million": 25}, "quality": {"research": 98, "judgment": 100, "fast": 68}},
        "claude-opus-4.8": {"aliases": ["claude-opus-4.8"], "price": {"currency": "USD", "input_per_million": 5, "output_per_million": 25}, "quality": {"research": 95, "judgment": 97, "fast": 65}},
        "claude-sonnet-5": {"aliases": ["claude-sonnet-5"], "price": {"currency": "USD", "input_per_million": 2, "output_per_million": 10}, "quality": {"research": 92, "judgment": 90, "fast": 88}},
    },
}
SLOT_STAGES = {
    "research": ["research", "m0_research", "m1_research", "outcome_research", "chat_research"],
    "judgment": ["judgment", "m1_judgment", "m2", "reflection", "workflow_feedback"],
    "fast": ["fast", "m0_compose", "chat"],
}
_FAMILY_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_SECRET_FIELD = re.compile(r"(?:authorization|bearer|secret|token)$", re.I)
_SHIFTED_PRODUCT_SEED_ROUTES = frozenset({"direct-provider-example-research", "direct-provider-example-judgment", "direct-provider-example-fast"})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def transport_for_family(family: str) -> str:
    """Keep the user-facing Codex/Claude choice tied to its wire protocol."""
    return "responses" if family == "openai" else "chat_completions"


def _positive(value: Any, label: str, default: float) -> float:
    value = default if value is None else value
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{label} must be positive")
    return float(value)


def _tier(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value % 100:
        raise ValueError("provider route cost.tier must be a non-negative multiple of 100")
    return value


def _catalog(raw: Any) -> dict[str, dict[str, dict[str, float | str]]]:
    if raw is None: return {}
    if not isinstance(raw, dict): raise ValueError("provider routing.price_catalog must be an object")
    result: dict[str, dict[str, dict[str, float | str]]] = {}
    for family, models in raw.items():
        name = str(family).lower()
        if not _FAMILY_NAME.fullmatch(name) or not isinstance(models, dict): raise ValueError("provider routing.price_catalog family is invalid")
        result[name] = {}
        for model, price in models.items():
            if not str(model).strip() or not isinstance(price, dict): raise ValueError("provider routing.price_catalog model is invalid")
            currency = str(price.get("currency", "USD")).upper()
            if currency != "USD": raise ValueError("provider routing.price_catalog currency must be USD")
            normalized: dict[str, float | str] = {"currency": currency}
            for field in ("input_per_million", "cached_input_per_million", "output_per_million"):
                # A provider without a published cached-input rate must be
                # charged at the normal input rate, never treated as free.
                value = price.get(field, price.get("input_per_million", 0))
                if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0: raise ValueError(f"provider routing.price_catalog.{field} must be non-negative")
                normalized[field] = float(value)
            result[name][str(model)] = normalized
    return result


def _model_catalog(raw: Any, legacy_prices: dict[str, dict[str, dict[str, float | str]]]) -> dict[str, dict[str, dict[str, Any]]]:
    """Normalize the editable catalog while accepting the one-release price-only shape."""
    source = copy.deepcopy(INITIAL_MODEL_CATALOG)
    if isinstance(raw, dict):
        for family, rows in raw.items():
            if not isinstance(rows, dict):
                source[family] = rows
            else:
                source.setdefault(family, {}).update(rows)
    if raw is not None and not isinstance(raw, dict):
        raise ValueError("provider routing.model_catalog must be an object")
    result: dict[str, dict[str, dict[str, Any]]] = {}
    families = set(source) | set(legacy_prices)
    for family in families:
        name = str(family).lower()
        rows = source.get(family, {})
        if not _FAMILY_NAME.fullmatch(name) or not isinstance(rows, dict):
            raise ValueError("provider routing.model_catalog family is invalid")
        result[name] = {}
        for model, item in rows.items():
            if not str(model).strip() or not isinstance(item, dict):
                raise ValueError("provider routing.model_catalog model is invalid")
            price_source = item.get("price") if isinstance(item.get("price"), dict) else item
            price = _catalog({name: {str(model): price_source}})[name][str(model)]
            aliases = item.get("aliases", [])
            if not isinstance(aliases, list) or any(not str(alias).strip() for alias in aliases):
                raise ValueError("provider routing.model_catalog aliases must be a list of model IDs")
            quality_source = item.get("quality", {})
            if quality_source is None: quality_source = {}
            if not isinstance(quality_source, dict):
                raise ValueError("provider routing.model_catalog quality must be an object")
            quality: dict[str, int] = {}
            for stage in ("research", "judgment", "fast"):
                value = quality_source.get(stage)
                if value is None: continue
                if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
                    raise ValueError("provider routing.model_catalog quality must be an integer from 0 to 100")
                quality[stage] = value
            result[name][str(model)] = {"aliases": sorted({str(alias).strip() for alias in aliases}), "price": price, "quality": quality}
        for model, price in legacy_prices.get(name, {}).items():
            result[name].setdefault(model, {"aliases": [], "price": price, "quality": {}})
    return result


def catalog_entry(provider: dict[str, Any], family: str, model: str) -> dict[str, Any] | None:
    """Resolve a real provider model ID through its canonical catalog aliases."""
    catalog = provider.get("routing", {}).get("model_catalog", {})
    rows = catalog.get(family, {}) if isinstance(catalog, dict) else {}
    target = _model_id(model)
    for canonical, item in rows.items():
        if target in {_model_id(canonical), *(_model_id(alias) for alias in item.get("aliases", []))}:
            return {"canonical_model": canonical, **item}
    return None


def _model_id(value: Any) -> str:
    return re.sub(r"[_. ]+", "-", str(value or "").strip().lower())


def route_cost_index(provider: dict[str, Any], route: dict[str, Any]) -> float | None:
    """Return the fixed standard-load cost index used for automatic tiers."""
    entry = catalog_entry(provider, str(route.get("model_family") or ""), str(route.get("catalog_model") or route.get("model") or ""))
    if not entry: return None
    slot = str(route.get("slot") or _slot_for_stages(route.get("stages", [])))
    reference = REFERENCE_MODELS.get(slot)
    if reference is None: return None
    reference_entry = catalog_entry(provider, *reference)
    if not reference_entry: return None
    price = entry["price"]; reference_price = reference_entry["price"]
    standard = float(price["input_per_million"]) + .2 * float(price["output_per_million"])
    reference_standard = float(reference_price["input_per_million"]) + .2 * float(reference_price["output_per_million"])
    if reference_standard <= 0: return None
    endpoint_id = route.get("endpoint")
    endpoint = next((item for item in provider.get("endpoints", []) if item.get("id") == endpoint_id), {})
    return float(endpoint.get("weight", provider.get("routing", {}).get("default_weight", DEFAULT_WEIGHT))) * standard / reference_standard


def auto_tier(index: float) -> int:
    return 0 if index <= AUTO_TIER_THRESHOLDS[0] else (100 if index <= AUTO_TIER_THRESHOLDS[1] else 200)


def recalculate_auto_tiers(provider: dict[str, Any]) -> list[dict[str, Any]]:
    """Mutate a normalized provider only for calibrated automatic routes."""
    changes: list[dict[str, Any]] = []
    for route in provider.get("routes", []):
        if route.get("tier_mode") != "auto": continue
        index = route_cost_index(provider, route)
        route["cost_index"] = index
        route["tier_calibrated"] = index is not None
        if index is None:
            continue
        before = int(route["cost"]["tier"])
        after = auto_tier(index)
        route["cost"]["tier"] = after
        if before != after:
            changes.append({"route_id": route["id"], "from_tier": before, "to_tier": after, "cost_index": index})
    return changes


def _slot_for_stages(stages: Any) -> str:
    values = set(stages if isinstance(stages, list) else [])
    for slot, slot_stages in SLOT_STAGES.items():
        if values.intersection(slot_stages): return slot
    return "fast"


def normalize_provider(provider: dict[str, Any], *, warn_legacy: bool = True) -> dict[str, Any]:
    source = copy.deepcopy(provider if isinstance(provider, dict) else {})
    schema = int(source.get("schema_version") or 0)
    routing_source = source.get("routing") if isinstance(source.get("routing"), dict) else {}
    family_mode = str(routing_source.get("family_mode") or "auto").lower()
    if family_mode not in FAMILY_MODES: raise ValueError("provider routing.family_mode must be auto, openai, or anthropic")
    default_weight = _positive(routing_source.get("default_weight"), "provider routing.default_weight", DEFAULT_WEIGHT)
    legacy_prices = _catalog(routing_source.get("price_catalog"))
    routing = {
        "family_mode": family_mode,
        "default_weight": default_weight,
        "near_cost_tolerance": float(routing_source.get("near_cost_tolerance", NEAR_COST_TOLERANCE)),
        "price_catalog": legacy_prices,
        "model_catalog": _model_catalog(routing_source.get("model_catalog"), legacy_prices),
    }
    if not 0 <= routing["near_cost_tolerance"] <= 1:
        raise ValueError("provider routing.near_cost_tolerance must be from 0 to 1")
    endpoints_raw = source.get("endpoints")
    if not isinstance(endpoints_raw, list): endpoints_raw = [{"id": "legacy-default", "enabled": bool(source.get("enabled", False)), "base_url": source.get("base_url"), "api_key": source.get("api_key"), "weight": default_weight}]
    endpoints: list[dict[str, Any]] = []; endpoint_ids: set[str] = set()
    for index, raw in enumerate(endpoints_raw):
        if not isinstance(raw, dict): raise ValueError(f"provider endpoint {index} must be an object")
        endpoint_id = str(raw.get("id") or f"endpoint-{index + 1}").strip()
        if not endpoint_id or endpoint_id in endpoint_ids: raise ValueError(f"provider configuration has duplicate endpoint id: {endpoint_id}")
        endpoint_ids.add(endpoint_id)
        for key, value in raw.items():
            if _SECRET_FIELD.search(str(key)) and value not in (None, ""): raise ValueError(f"provider endpoint {endpoint_id} embeds forbidden credential field: {key}")
        archived = bool(raw.get("archived", False))
        # Before schema v3 every relative route was initialized to 1.0.  That
        # was a placeholder, not an operator supplied endpoint multiplier.
        # Preserve explicitly calibrated v3 values while migrating old files to
        # the safe default multiplier.
        raw_weight = raw.get("weight")
        if schema < 3 and raw_weight == 1.0:
            raw_weight = default_weight
        endpoint = {"id": endpoint_id, "enabled": bool(raw.get("enabled", True)) and not archived, "archived": archived, "base_url": str(raw.get("base_url") or "").rstrip("/"), "weight": _positive(raw_weight, f"provider endpoint {endpoint_id} weight", default_weight), "updated_at": str(raw.get("updated_at") or utc_now())}
        families = raw.get("families", [])
        if families:
            if not isinstance(families, list) or any(str(f).lower() not in KNOWN_FAMILIES for f in families): raise ValueError("provider endpoint families must be openai or anthropic")
            endpoint["families"] = sorted({str(f).lower() for f in families})
        key = str(raw.get("api_key") or "").strip()
        if key: endpoint["api_key"] = key
        raw_models = raw.get("available_models", [])
        if raw_models is not None and not isinstance(raw_models, list):
            raise ValueError(f"provider endpoint {endpoint_id} available_models must be a list")
        endpoint["available_models"] = sorted({str(item).strip() for item in raw_models or [] if str(item).strip()})
        if raw.get("model_directory_status"):
            endpoint["model_directory_status"] = str(raw["model_directory_status"])
        if raw.get("models_updated_at"):
            endpoint["models_updated_at"] = str(raw["models_updated_at"])
        if endpoint["enabled"] and not endpoint["base_url"]: raise ValueError(f"provider endpoint {endpoint_id} requires base_url")
        endpoints.append(endpoint)
    endpoint_by_id = {item["id"]: item for item in endpoints}
    routes_raw = source.get("routes")
    if not isinstance(routes_raw, list):
        routes_raw = _expand_legacy_routes(source, endpoints_raw, default_weight)
        if warn_legacy and routes_raw: warnings.warn("legacy Provider settings were normalized; save to migrate", DeprecationWarning)
    routes: list[dict[str, Any]] = []; route_ids: set[str] = set()
    for index, raw in enumerate(routes_raw):
        if not isinstance(raw, dict): raise ValueError(f"provider route {index} must be an object")
        route_id = str(raw.get("id") or f"route-{index + 1}").strip()
        if not route_id or route_id in route_ids: raise ValueError(f"provider configuration has duplicate route id: {route_id}")
        route_ids.add(route_id); endpoint_id = str(raw.get("endpoint") or "")
        if endpoint_id not in endpoint_by_id: raise ValueError(f"provider route {route_id} references missing endpoint: {endpoint_id}")
        model = str(raw.get("model") or "").strip(); family = str(raw.get("model_family") or _infer_family(model)).lower()
        if not model or not _FAMILY_NAME.fullmatch(family): raise ValueError(f"provider route {route_id} requires model and stable model_family")
        stages = [str(item) for item in raw.get("stages", [])]; caps = [str(item) for item in raw.get("capabilities", ["stream", "json_schema", "race"])]
        if not stages or any(s not in STAGES for s in stages): raise ValueError(f"provider route {route_id} has unsupported stages")
        if any(c not in CAPABILITIES for c in caps): raise ValueError(f"provider route {route_id} has unsupported capabilities")
        legacy_priority = raw.get("priority"); raw_cost = raw.get("cost") if isinstance(raw.get("cost"), dict) else {}
        tier = _tier(raw_cost.get("tier", (legacy_priority or 0) // 100 * 100))
        if schema < 2 and route_id in _SHIFTED_PRODUCT_SEED_ROUTES and tier in {100, 200}: tier += 100
        preference = raw.get("preference", legacy_priority % 100 if isinstance(legacy_priority, int) else 0)
        if isinstance(preference, bool) or not isinstance(preference, int): raise ValueError(f"provider route {route_id} preference must be an integer")
        cost_raw = raw_cost; mode = str(cost_raw.get("mode") or "relative")
        if mode == "relative":
            # The multiplier belongs to the endpoint. Route copies are kept only
            # for backwards-compatible readers and are always synchronized here.
            weight = endpoint_by_id[endpoint_id]["weight"]
            cost: dict[str, Any] = {"tier": tier, "mode": "relative", "weight": weight}
        elif mode == "token":
            currency = str(cost_raw.get("currency") or "").upper()
            if not re.fullmatch(r"[A-Z]{3}", currency): raise ValueError("provider route token cost.currency must be an ISO currency")
            cost = {"tier": tier, "mode": mode, "currency": currency}
            for field in ("input_per_million", "output_per_million", "fixed_request"):
                value = cost_raw.get(field, 0)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0: raise ValueError(f"provider route token cost.{field} must be non-negative")
                cost[field] = float(value)
        else: raise ValueError("provider route cost.mode must be relative or token")
        transport = str(raw.get("transport") or transport_for_family(family)).lower()
        if transport not in TRANSPORTS:
            raise ValueError("provider route transport must be responses or chat_completions")
        if transport != transport_for_family(family):
            raise ValueError("provider route transport must match its model family")
        tier_mode = str(raw.get("tier_mode") or "manual").lower()
        if tier_mode not in TIER_MODES:
            raise ValueError("provider route tier_mode must be auto or manual")
        catalog_model = str(raw.get("catalog_model") or model).strip()
        route = {"id": route_id, "endpoint": endpoint_id, "model": model, "catalog_model": catalog_model,
                 "model_family": family, "transport": transport,
                 "enabled": bool(raw.get("enabled", True)) and endpoint_by_id[endpoint_id]["enabled"] and not endpoint_by_id[endpoint_id]["archived"],
                 "cost": cost, "tier_mode": tier_mode, "preference": preference, "stages": stages,
                 "capabilities": caps, "effort": str(raw.get("effort") or "medium")}
        resolution = str(raw.get("model_resolution") or "").strip()
        if resolution: route["model_resolution"] = resolution
        routes.append(route)
    # v2 did not persist endpoint families.  Derive them from their routes so
    # the Provider page can edit a legacy endpoint without losing its slots.
    for endpoint in endpoints:
        if "families" not in endpoint:
            endpoint["families"] = sorted({route["model_family"] for route in routes if route["endpoint"] == endpoint["id"] and route["model_family"] in KNOWN_FAMILIES})
    result = {key: copy.deepcopy(value) for key, value in source.items() if key not in {"base_url", "credential_target", "api_key", "models", "routes", "endpoints", "routing", "schema_version"}}
    hedge = result.get("hedge")
    if isinstance(hedge, dict):
        parallel = hedge.get("max_parallel", 2)
        if isinstance(parallel, bool) or not isinstance(parallel, int):
            raise ValueError("provider hedge.max_parallel must be an integer")
        # The formal broker always caps live Provider requests at two.  Legacy
        # zero meant unbounded fan-out and is migrated to the safe formal cap.
        hedge["max_parallel"] = 2 if parallel <= 0 else min(parallel, 2)
    result.update({"schema_version": 4, "routing": routing, "endpoints": endpoints, "routes": routes})
    recalculate_auto_tiers(result)
    return result


def _expand_legacy_routes(provider: dict[str, Any], endpoints: list[Any], weight: float) -> list[dict[str, Any]]:
    models = provider.get("models") if isinstance(provider.get("models"), dict) else {}; output: list[dict[str, Any]] = []
    stage_map = {"research": ["research", "m0_research", "m1_research", "outcome_research", "chat_research"], "judgment": ["judgment", "m1_judgment", "m2", "reflection", "workflow_feedback"], "fast": ["fast", "m0_compose", "chat"]}
    for i, endpoint in enumerate(endpoints):
        if not isinstance(endpoint, dict): continue
        endpoint_id = str(endpoint.get("id") or f"endpoint-{i + 1}"); priority = endpoint.get("priority", 0)
        if isinstance(priority, bool) or not isinstance(priority, int) or priority < 0: raise ValueError("legacy endpoint priority must be non-negative integer")
        for slot, stages in stage_map.items():
            item = models.get(slot) if isinstance(models.get(slot), dict) else {}; model = str(endpoint.get(f"{slot}_model") or item.get("id") or "").strip()
            if model: output.append({"id": f"{endpoint_id}-{slot}", "endpoint": endpoint_id, "model": model, "model_family": endpoint.get("model_family") or _infer_family(model), "enabled": bool(endpoint.get("enabled", True)), "cost": {"tier": priority // 100 * 100, "mode": "relative", "weight": weight}, "preference": priority % 100, "stages": stages, "capabilities": ["stream", "json_schema", "race", "duel", "arbitration"], "effort": str(item.get("effort") or "medium")})
    return output


def _infer_family(model: str) -> str: return "anthropic" if "claude" in model.lower() else "openai"


def redacted_provider(provider: dict[str, Any]) -> dict[str, Any]:
    value = normalize_provider(provider, warn_legacy=False)
    for endpoint in value["endpoints"]:
        key = str(endpoint.pop("api_key", "") or "")
        endpoint["api_key_hint"] = f"{key[:3]}...{key[-3:]}" if len(key) > 6 else ("已配置" if key else "")
    return value


def build_provider_slots(endpoint_id: str, families: list[str], *, enabled: bool = True) -> list[dict[str, Any]]:
    """Create the editable research/judgment/fast slots for a new Provider.

    Models deliberately start blank.  A caller must fill them before this draft
    is persisted, which prevents a Provider from becoming live accidentally.
    """
    result: list[dict[str, Any]] = []
    for family in sorted({str(item).lower() for item in families}):
        if family not in KNOWN_FAMILIES:
            raise ValueError("provider endpoint families must be openai or anthropic")
        for slot, stages in SLOT_STAGES.items():
            result.append({
                "id": f"{endpoint_id}-{family}-{slot}", "endpoint": endpoint_id,
                "slot": slot, "model": "", "model_family": family, "transport": transport_for_family(family), "enabled": enabled,
                "cost": {"tier": 0, "mode": "relative"}, "tier_mode": "auto", "preference": 0,
                "stages": list(stages), "capabilities": ["stream", "json_schema", "race", "duel", "arbitration"],
                "effort": "medium",
            })
    return result
