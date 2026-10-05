"""Runtime-owned deterministic-chart and vision-interpretation contract.

The adapter receives structured facts owned by MarketHub (or deterministic
Runtime computation), plus a chart reference generated from those facts.  A
provider may explain the rendered structure, but it may not manufacture market
facts from pixels or mutate Runtime state.
"""
from __future__ import annotations

import copy
import hashlib
import html
import json
import math
import re
from datetime import datetime
from typing import Any

CONTRACT = "FinAgentMultimodalAdapterSpec/v1"
VERSION = 1
RESULT_CONTRACT = "FinAgentMultimodalAdapterResult/v1"
REPLAY_CONTRACT = "FinAgentMultimodalAdapterReplay/v1"
CHART_CONTRACT = "DeterministicChartReference/v1"
CHART_GENERATION_VERSION = "deterministic-chart-v1"
STAGES = frozenset({"m0_compose", "m1_judgment", "m2", "chat", "reflection"})
FACT_SOURCES = frozenset({"markethub", "deterministic_computation"})
_PERMISSIONS = {"write_permissions": []}
_QUANTRESEARCH = {"access": "read_only", "write_permissions": []}
_FORBIDDEN = frozenset({
    "runtime", "memoryhub", "memory", "portfolio", "positions", "orders", "schedule",
    "production_strategy", "final_judgment", "task_state", "write_permissions",
    "network_permissions", "state_permissions", "evidence_gate", "memoryhub_write",
    "memory_write", "portfolio_write", "positions_write", "orders_write", "schedule_write",
})
_MARKET_WORDS = frozenset({"price", "prices", "volume", "volumes", "indicator", "indicators", "ohlcv"})
_MAX_OUTPUT_BYTES = 32_768
_MAX_INTERPRETATION_BYTES = 16_384
_MAX_OUTPUT_NODES = 5_000
_MAX_OUTPUT_DEPTH = 12
_MAX_OUTPUT_STRING = 2_000
_MAX_OUTPUT_KEYS = 64
_MAX_OUTPUT_ITEMS = 200


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: Any, field: str, *, maximum: int = 240) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise ValueError(f"multimodal {field} must be bounded text")
    return value.strip()


def _timestamp(value: Any, field: str) -> str:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"multimodal {field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"multimodal {field} must be timezone-aware")
    return text


def _number(value: Any, field: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"multimodal {field} must be finite")
    if positive and value <= 0:
        raise ValueError(f"multimodal {field} must be positive")
    if not positive and value < 0:
        raise ValueError(f"multimodal {field} must be nonnegative")
    return float(value)


def _source(value: Any, field: str) -> str:
    source = _text(value, field, maximum=80).casefold().replace("-", "_")
    if source not in FACT_SOURCES:
        raise ValueError("market facts must come from MarketHub or deterministic computation")
    return source


def _validate_output_bounds(value: Any) -> None:
    """Keep provider output bounded before it enters a packet or artifact."""
    pending = [(value, 0)]
    nodes = 0
    while pending:
        child, depth = pending.pop()
        nodes += 1
        if nodes > _MAX_OUTPUT_NODES or depth > _MAX_OUTPUT_DEPTH:
            raise ValueError("multimodal interpretation payload limits exceeded")
        if isinstance(child, dict):
            if len(child) > _MAX_OUTPUT_KEYS:
                raise ValueError("multimodal interpretation has too many fields")
            for key, item in child.items():
                if not isinstance(key, str) or len(key) > 120:
                    raise ValueError("multimodal interpretation field names are not bounded")
                pending.append((item, depth + 1))
        elif isinstance(child, (list, tuple)):
            if len(child) > _MAX_OUTPUT_ITEMS:
                raise ValueError("multimodal interpretation list is not bounded")
            pending.extend((item, depth + 1) for item in child)
        elif isinstance(child, str):
            if len(child) > _MAX_OUTPUT_STRING:
                raise ValueError("multimodal interpretation text is not bounded")
        elif isinstance(child, float):
            if not math.isfinite(child):
                raise ValueError("multimodal interpretation numbers must be finite")
        elif not isinstance(child, (int, bool)) and child is not None:
            raise ValueError("multimodal interpretation contains an unsupported value")
    try:
        encoded = canonical_json(value).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("multimodal interpretation must be JSON-compatible") from exc
    if len(encoded) > _MAX_OUTPUT_BYTES:
        raise ValueError("multimodal interpretation payload is too large")
    interpretation = value.get("interpretation") if isinstance(value, dict) else None
    if isinstance(interpretation, dict) and len(canonical_json(interpretation).encode("utf-8")) > _MAX_INTERPRETATION_BYTES:
        raise ValueError("multimodal interpretation is too large")


def _walk_forbidden(value: Any, path: str = "multimodal") -> None:
    pending = [(value, path, 0)]
    visited = 0
    while pending:
        child, child_path, depth = pending.pop()
        visited += 1
        if depth > 64 or visited > 100_000:
            raise ValueError("multimodal payload limits exceeded")
        if isinstance(child, dict):
            for key, item in child.items():
                normalized = str(key).strip().casefold().replace("-", "_")
                if normalized in _FORBIDDEN:
                    raise ValueError(f"multimodal contract forbids protected field at {child_path}.{key}")
                pending.append((item, f"{child_path}.{key}", depth + 1))
        elif isinstance(child, (list, tuple)):
            pending.extend((item, f"{child_path}[{index}]", depth + 1) for index, item in enumerate(child))
        if visited + len(pending) > 100_000:
            raise ValueError("multimodal payload limits exceeded")


def _reject_image_market_facts(value: Any, path: str = "output") -> None:
    """Reject market facts whose declared authority is an image or vision model."""
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().casefold().replace("-", "_")
            compact = normalized.replace(" ", "")
            if ("image" in compact or "vision" in compact) and any(word in compact for word in _MARKET_WORDS):
                raise ValueError("image-derived price, volume, and indicator facts are forbidden")
            if normalized in {"price", "prices", "volume", "volumes", "indicator", "indicators", "ohlcv"} and path == "output":
                raise ValueError("market facts must be returned as sourced facts, not image claims")
            if normalized in {"source", "fact_source", "authority"} and isinstance(child, str):
                source = child.casefold().replace("-", "_")
                if source not in FACT_SOURCES and any(word in source for word in ("image", "vision", "pixel")):
                    raise ValueError("image-derived price, volume, and indicator facts are forbidden")
            _reject_image_market_facts(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_image_market_facts(child, f"{path}[{index}]")


def _validate_ohlcv(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list) or not rows or len(rows) > 10_000:
        raise ValueError("multimodal OHLCV must be a non-empty bounded list")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"timestamp", "open", "high", "low", "close", "volume", "source"}:
            raise ValueError("multimodal OHLCV fields are not exact")
        timestamp = _timestamp(row["timestamp"], "OHLCV.timestamp")
        if timestamp in seen:
            raise ValueError("conflicting duplicate OHLCV timestamps")
        seen.add(timestamp)
        source = _source(row["source"], "OHLCV.source")
        values = {key: _number(row[key], f"OHLCV.{key}", positive=key != "volume") for key in ("open", "high", "low", "close", "volume")}
        if values["high"] < max(values["open"], values["close"]) or values["low"] > min(values["open"], values["close"]):
            raise ValueError("invalid OHLCV high/low bounds")
        normalized.append({"timestamp": timestamp, **values, "source": source})
    return normalized


def _validate_fact_rows(rows: Any, field: str) -> list[dict[str, Any]]:
    if rows is None:
        return []
    if not isinstance(rows, list) or len(rows) > 500:
        raise ValueError(f"multimodal {field} must be a bounded list")
    result: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"name", "value", "source"}:
            raise ValueError(f"multimodal {field} fields are not exact")
        result.append({
            "name": _text(row["name"], f"{field}.name", maximum=120),
            "value": _number(row["value"], f"{field}.value", positive=field == "quotes"),
            "source": _source(row["source"], f"{field}.source"),
        })
    return result


def validate_market_data(value: dict[str, Any]) -> dict[str, Any]:
    required = {"source", "source_ref", "as_of", "instrument", "interval", "ohlcv", "quotes", "indicators"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("multimodal market data fields are not exact")
    source = _source(value["source"], "market_data.source")
    source_ref = _text(value["source_ref"], "market_data.source_ref", maximum=300)
    as_of = _timestamp(value["as_of"], "market_data.as_of")
    instrument = _text(value["instrument"], "market_data.instrument", maximum=80)
    interval = _text(value["interval"], "market_data.interval", maximum=40)
    ohlcv = _validate_ohlcv(value["ohlcv"])
    quotes = _validate_fact_rows(value["quotes"], "quotes")
    indicators = _validate_fact_rows(value["indicators"], "indicators")
    if any(row["source"] != source for row in ohlcv + quotes):
        raise ValueError("quote and OHLCV fact sources conflict with MarketHub provenance")
    return {
        "source": source, "source_ref": source_ref, "as_of": as_of,
        "instrument": instrument, "interval": interval, "ohlcv": ohlcv,
        "quotes": quotes, "indicators": indicators,
    }


def _render_parameters(value: dict[str, Any] | None) -> dict[str, Any]:
    value = copy.deepcopy(value or {})
    defaults = {"width": 960, "height": 540, "theme": "light", "show_volume": True, "indicator_names": []}
    if set(value) - set(defaults):
        raise ValueError("multimodal render parameters contain unknown fields")
    defaults.update(value)
    if type(defaults["width"]) is not int or not 160 <= defaults["width"] <= 4096:
        raise ValueError("multimodal chart width is invalid")
    if type(defaults["height"]) is not int or not 120 <= defaults["height"] <= 4096:
        raise ValueError("multimodal chart height is invalid")
    if defaults["theme"] not in {"light", "dark"} or type(defaults["show_volume"]) is not bool:
        raise ValueError("multimodal render parameters are invalid")
    if not isinstance(defaults["indicator_names"], list) or any(not isinstance(item, str) or not item.strip() for item in defaults["indicator_names"]):
        raise ValueError("multimodal indicator_names are invalid")
    defaults["indicator_names"] = list(defaults["indicator_names"])
    return defaults


def render_chart(market_data: dict[str, Any], render_parameters: dict[str, Any] | None = None) -> str:
    """Render a small deterministic SVG without a graphics/runtime dependency."""
    market_data = validate_market_data(market_data)
    params = _render_parameters(render_parameters)
    width, height = params["width"], params["height"]
    bars = market_data["ohlcv"]
    margin = 32
    plot_height = height - (92 if params["show_volume"] else 48)
    lows = [row["low"] for row in bars]
    highs = [row["high"] for row in bars]
    low, high = min(lows), max(highs)
    span = high - low or 1.0
    candle_width = max(2.0, (width - margin * 2) / max(len(bars), 1) * 0.62)
    background, foreground, up, down = (("#ffffff", "#111827", "#16a34a", "#dc2626") if params["theme"] == "light" else ("#111827", "#f9fafb", "#4ade80", "#f87171"))

    def y(price: float) -> float:
        return margin + (high - price) / span * plot_height

    elements = [f'<rect width="{width}" height="{height}" fill="{background}"/>']
    title = html.escape(f'{market_data["instrument"]} {market_data["interval"]}')
    elements.append(f'<text x="{margin}" y="20" fill="{foreground}" font-size="14">{title}</text>')
    for index, row in enumerate(bars):
        x = margin + (index + 0.5) * (width - margin * 2) / len(bars)
        colour = up if row["close"] >= row["open"] else down
        elements.append(f'<line x1="{x:.3f}" x2="{x:.3f}" y1="{y(row["high"]):.3f}" y2="{y(row["low"]):.3f}" stroke="{colour}"/>')
        top, bottom = sorted((y(row["open"]), y(row["close"])))
        elements.append(f'<rect x="{x - candle_width / 2:.3f}" y="{top:.3f}" width="{candle_width:.3f}" height="{max(bottom - top, 1):.3f}" fill="{colour}"/>')
        if params["show_volume"]:
            volume_max = max((item["volume"] for item in bars), default=1.0) or 1.0
            volume_top = plot_height + margin + 20 + (1 - row["volume"] / volume_max) * 36
            elements.append(f'<rect x="{x - candle_width / 2:.3f}" y="{volume_top:.3f}" width="{candle_width:.3f}" height="{max(1, 36 - (volume_top - (plot_height + margin + 20))):.3f}" fill="{colour}" opacity="0.55"/>')
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">' + "".join(elements) + "</svg>"


def build_chart_reference(
    market_data: dict[str, Any], *, render_parameters: dict[str, Any] | None = None,
    generation_version: str = CHART_GENERATION_VERSION,
) -> dict[str, Any]:
    market_data = validate_market_data(market_data)
    params = _render_parameters(render_parameters)
    generation_version = _text(generation_version, "chart.generation_version", maximum=80)
    svg = render_chart(market_data, params)
    image_reference = {
        "kind": "deterministic_chart",
        "uri": "generated://chart/pending.svg",
        "media_type": "image/svg+xml",
        "content_sha256": hashlib.sha256(svg.encode("utf-8")).hexdigest(),
        "chart_sha256": None,
        "market_data_sha256": sha256(market_data),
        "render_parameters_sha256": sha256(params),
        "generation_version": generation_version,
    }
    chart_body = {
        "contract": CHART_CONTRACT, "version": VERSION,
        "generation_version": generation_version,
        "market_data_sha256": sha256(market_data),
        "ohlcv_sha256": sha256(market_data["ohlcv"]),
        "render_parameters": params,
        "render_parameters_sha256": sha256(params),
        "image_content_sha256": image_reference["content_sha256"],
    }
    chart_body["chart_sha256"] = sha256(chart_body)
    image_reference["chart_sha256"] = chart_body["chart_sha256"]
    image_reference["uri"] = f'generated://chart/{chart_body["chart_sha256"]}.svg'
    chart_body["image_reference"] = image_reference
    return chart_body


def _validate_image_reference(value: dict[str, Any], chart: dict[str, Any]) -> dict[str, Any]:
    required = {"kind", "uri", "media_type", "content_sha256", "chart_sha256", "market_data_sha256", "render_parameters_sha256", "generation_version"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("image reference provenance is incomplete")
    if value["kind"] != "deterministic_chart" or value["media_type"] != "image/svg+xml":
        raise ValueError("image reference is not a deterministic chart")
    _text(value["uri"], "image_reference.uri", maximum=500)
    _text(value["generation_version"], "image_reference.generation_version", maximum=80)
    for field in ("content_sha256", "chart_sha256", "market_data_sha256", "render_parameters_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(value[field])):
            raise ValueError("image reference digest is invalid")
    if value["chart_sha256"] != chart["chart_sha256"] or value["market_data_sha256"] != chart["market_data_sha256"] or value["render_parameters_sha256"] != chart["render_parameters_sha256"] or value["generation_version"] != chart["generation_version"]:
        raise ValueError("image reference is not bound to chart provenance")
    if value["uri"] != f'generated://chart/{chart["chart_sha256"]}.svg':
        raise ValueError("image reference URI is not bound to chart")
    return value


def validate_chart_reference(value: dict[str, Any]) -> dict[str, Any]:
    required = {"contract", "version", "generation_version", "market_data_sha256", "ohlcv_sha256", "render_parameters", "render_parameters_sha256", "image_content_sha256", "chart_sha256", "image_reference"}
    if not isinstance(value, dict) or set(value) != required or value.get("contract") != CHART_CONTRACT or value.get("version") != VERSION:
        raise ValueError("invalid deterministic chart reference")
    _text(value["generation_version"], "chart.generation_version", maximum=80)
    for field in ("market_data_sha256", "ohlcv_sha256", "render_parameters_sha256", "image_content_sha256", "chart_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(value[field])):
            raise ValueError("chart provenance digest is invalid")
    params = _render_parameters(value["render_parameters"])
    if params != value["render_parameters"] or value["render_parameters_sha256"] != sha256(params):
        raise ValueError("chart render parameter provenance mismatch")
    expected = {key: value[key] for key in ("contract", "version", "generation_version", "market_data_sha256", "ohlcv_sha256", "render_parameters", "render_parameters_sha256", "image_content_sha256")}
    if value["chart_sha256"] != sha256(expected):
        raise ValueError("chart digest mismatch")
    _validate_image_reference(value["image_reference"], value)
    if value["image_reference"]["content_sha256"] != value["image_content_sha256"]:
        raise ValueError("image content is not bound to deterministic chart")
    return value


def build_input(
    market_data: dict[str, Any], *, stage: str = "m0_compose", as_of: str | None = None,
    cycle_id: str | None = None, request_id: str | None = None,
    render_parameters: dict[str, Any] | None = None,
    generation_version: str = CHART_GENERATION_VERSION,
    image_reference: dict[str, Any] | None = None,
) -> dict[str, Any]:
    market_data = validate_market_data(copy.deepcopy(market_data))
    if stage not in STAGES:
        raise ValueError("unsupported multimodal stage")
    chart = build_chart_reference(market_data, render_parameters=render_parameters, generation_version=generation_version)
    if image_reference is not None:
        _validate_image_reference(copy.deepcopy(image_reference), chart)
        chart["image_reference"] = copy.deepcopy(image_reference)
    value = {
        "contract": CONTRACT, "version": VERSION, "stage": stage,
        "market_data": market_data, "chart": chart,
        "image_reference": copy.deepcopy(chart["image_reference"]),
        "permissions": copy.deepcopy(_PERMISSIONS), "quantresearch": copy.deepcopy(_QUANTRESEARCH),
        "provenance": {
            "source": "runtime", "market_data_source": market_data["source"],
            "as_of": _timestamp(as_of or market_data["as_of"], "provenance.as_of"),
            "cycle_id": cycle_id, "request_id": request_id,
        },
    }
    value["sha256"] = sha256(value)
    return validate_input(value)


def validate_input(value: dict[str, Any]) -> dict[str, Any]:
    required = {"contract", "version", "stage", "market_data", "chart", "image_reference", "permissions", "quantresearch", "provenance", "sha256"}
    if not isinstance(value, dict) or set(value) != required or value.get("contract") != CONTRACT or value.get("version") != VERSION or value.get("stage") not in STAGES:
        raise ValueError("invalid multimodal adapter input fields")
    market_data = validate_market_data(value["market_data"])
    if market_data != value["market_data"]:
        raise ValueError("market data normalization/provenance mismatch")
    chart = validate_chart_reference(value["chart"])
    if chart["market_data_sha256"] != sha256(market_data):
        raise ValueError("chart is not bound to market data")
    if chart["ohlcv_sha256"] != sha256(market_data["ohlcv"]):
        raise ValueError("chart is not bound to OHLCV data")
    if value["image_reference"] != value["chart"]["image_reference"]:
        raise ValueError("image reference differs from chart-bound reference")
    _validate_image_reference(value["image_reference"], chart)
    if value["permissions"] != _PERMISSIONS or value["quantresearch"] != _QUANTRESEARCH:
        raise ValueError("multimodal adapter permissions must be read-only")
    provenance = value["provenance"]
    if not isinstance(provenance, dict) or set(provenance) != {"source", "market_data_source", "as_of", "cycle_id", "request_id"}:
        raise ValueError("multimodal input provenance fields are not exact")
    if provenance["source"] != "runtime" or provenance["market_data_source"] != market_data["source"]:
        raise ValueError("multimodal input must use Runtime-owned provenance")
    _timestamp(provenance["as_of"], "provenance.as_of")
    if provenance["as_of"] != market_data["as_of"]:
        raise ValueError("multimodal provenance cutoff does not match market data")
    for field in ("cycle_id", "request_id"):
        if provenance[field] is not None:
            _text(provenance[field], f"provenance.{field}", maximum=200)
    _walk_forbidden(value["market_data"], "multimodal.market_data")
    if value["sha256"] != sha256({key: item for key, item in value.items() if key != "sha256"}):
        raise ValueError("multimodal input digest mismatch")
    return value


def validate_vision_output(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("multimodal vision output must be an object")
    _validate_output_bounds(value)
    if value.get("output_kind") != "interpretation":
        raise ValueError("vision output must be explicitly marked as interpretation")
    interpretation = value.get("interpretation")
    if not isinstance(interpretation, dict) or not interpretation:
        raise ValueError("interpretation output is required")
    facts = value.get("facts", [])
    if not isinstance(facts, list) or len(facts) > 100:
        raise ValueError("multimodal interpretation facts are invalid")
    for fact in facts:
        if not isinstance(fact, dict) or not isinstance(fact.get("kind"), str) or not isinstance(fact.get("source"), str):
            raise ValueError("market facts require an explicit permitted source")
        kind = fact["kind"].casefold()
        image_kind = any(token in kind for token in ("image", "vision", "pixel", "screenshot"))
        if image_kind and any(word in kind for word in _MARKET_WORDS):
            raise ValueError("image-derived price, volume, and indicator facts are forbidden")
        source_name = fact["source"].casefold()
        if source_name not in FACT_SOURCES and any(token in source_name for token in ("image", "vision", "pixel", "screenshot")):
            raise ValueError("image-derived price, volume, and indicator facts are forbidden")
        _source(fact["source"], "vision fact source")
        if any(word in kind for word in _MARKET_WORDS):
            if "value" not in fact:
                raise ValueError("sourced market facts require a value")
            _number(
                fact["value"], "vision fact value",
                positive=any(word in fact["kind"].casefold() for word in ("price", "volume")),
            )
    _reject_image_market_facts(value)
    _walk_forbidden(value, "multimodal.output")
    return value


def build_output(input_contract: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
    input_contract = validate_input(copy.deepcopy(input_contract))
    output = validate_vision_output(copy.deepcopy(output))
    receipt = {
        "contract": RESULT_CONTRACT, "version": VERSION, "spec_contract": CONTRACT,
        "state": "qualified", "input": input_contract,
        "output_kind": "interpretation", "interpretation": copy.deepcopy(output["interpretation"]),
        "facts": copy.deepcopy(output.get("facts", [])), "source_output": output,
        "permissions": copy.deepcopy(_PERMISSIONS), "quantresearch": copy.deepcopy(_QUANTRESEARCH),
        "provenance": {
            "source": "runtime", "input_sha256": input_contract["sha256"],
            "output_sha256": sha256(output), "chart_sha256": input_contract["chart"]["chart_sha256"],
            "image_reference_sha256": sha256(input_contract["image_reference"]),
        },
    }
    receipt["sha256"] = sha256(receipt)
    return receipt


def validate_output(receipt: dict[str, Any]) -> dict[str, Any]:
    required = {"contract", "version", "spec_contract", "state", "input", "output_kind", "interpretation", "facts", "source_output", "permissions", "quantresearch", "provenance", "sha256"}
    if not isinstance(receipt, dict) or set(receipt) != required or receipt.get("contract") != RESULT_CONTRACT or receipt.get("version") != VERSION or receipt.get("spec_contract") != CONTRACT or receipt.get("state") != "qualified":
        raise ValueError("invalid multimodal adapter result fields")
    expected = build_output(receipt["input"], receipt["source_output"])
    if receipt != expected:
        raise ValueError("multimodal result qualification or digest mismatch")
    return receipt


def frozen_replay(input_contract: dict[str, Any], output: dict[str, Any], *, expected_output_sha256: str | None = None) -> dict[str, Any]:
    input_contract = validate_input(copy.deepcopy(input_contract))
    output = validate_vision_output(copy.deepcopy(output))
    if expected_output_sha256 is not None and sha256(output) != expected_output_sha256:
        raise ValueError("multimodal replay output digest mismatch")
    receipt = build_output(input_contract, output)
    replay = {
        "contract": REPLAY_CONTRACT, "version": VERSION,
        "source_input": input_contract, "source_output": output,
        "source_input_sha256": input_contract["sha256"], "source_output_sha256": sha256(output),
        "receipt": receipt,
        "qualification": {"valid": True, "interpretation_only": True, "read_only": True, "chart_bound": True, "fact_sources_enforced": True},
        "evaluation_vector": {
            "delivery_speed": {"status": "not_measured_in_frozen_replay"},
            "qualification_probability": {"status": "not_estimated_in_frozen_replay"},
            "research_quality": {"status": "not_measured_in_frozen_replay"},
            "judgment_outcome": {"status": "adapter_not_a_judgment"},
            "safety_reliability": {"status": "pass", "write_permissions": [], "image_facts_rejected": True},
        },
    }
    return replay


def publication_receipt(packet: dict[str, Any], output: dict[str, Any]) -> dict[str, Any] | None:
    value = packet.get("multimodal_adapter") if isinstance(packet, dict) else None
    if value is None:
        return None
    validate_input(value)
    return build_output(value, output)


def install_qualification() -> dict[str, Any]:
    as_of = "2026-10-05T01:45:00Z"
    market_data = {
        "source": "markethub", "source_ref": "markethub:install:000001",
        "as_of": as_of, "instrument": "000001", "interval": "1d",
        "ohlcv": [{"timestamp": as_of, "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 1000, "source": "markethub"}],
        "quotes": [{"name": "last_price", "value": 10.5, "source": "markethub"}],
        "indicators": [{"name": "sma_1", "value": 10.5, "source": "deterministic_computation"}],
    }
    input_contract = build_input(market_data, cycle_id="multimodal-install")
    output = {"output_kind": "interpretation", "interpretation": {"summary": "The latest candle has a narrow observed range."}, "facts": []}
    first = frozen_replay(input_contract, output)
    second = frozen_replay(copy.deepcopy(input_contract), copy.deepcopy(output))
    checks = {
        "frozen_replay": first == second,
        "chart_bound": first["qualification"]["chart_bound"],
        "interpretation_marked": True,
        "fact_sources_enforced": first["qualification"]["fact_sources_enforced"],
        "read_only": first["qualification"]["read_only"],
    }
    return {
        "contract": "FinAgentMultimodalAdapterInstallQualification/v1",
        "qualified": all(checks.values()), "checks": checks,
        "replay_sha256": sha256(first), "evaluation_vector": first["evaluation_vector"],
    }


def execute_deterministic_chart_interpretation(input_contract: dict[str, Any]) -> dict[str, Any]:
    """Provide a bounded interpretation for installations without a vision provider.

    The output deliberately contains no market facts.  Facts remain in the
    Runtime-owned input; this adapter only describes the rendered structure.
    """
    input_contract = validate_input(input_contract)
    bars = input_contract["market_data"]["ohlcv"]
    rising = sum(1 for row in bars if row["close"] >= row["open"])
    falling = len(bars) - rising
    return {
        "output_kind": "interpretation",
        "interpretation": {
            "summary": f"The deterministic chart contains {len(bars)} candles with {rising} rising and {falling} falling bodies.",
            "basis": "rendered_deterministic_chart",
        },
        "facts": [],
    }


def validate_multimodal_adapter_input(value: dict[str, Any]) -> None:
    validate_input(value)


def validate_multimodal_adapter_output(value: dict[str, Any]) -> None:
    validate_vision_output(value)


def qualify_multimodal_adapter_output(value: dict[str, Any]) -> dict[str, Any]:
    validate_vision_output(value)
    return {
        "passed": True,
        "interpretation_only": True,
        "read_only": True,
        "chart_bound": True,
        "fact_sources_enforced": True,
    }


# Descriptive aliases keep the Runtime seam readable for callers that do not
# need to know the generic contract function names used by other specs.
build_multimodal_input = build_input
build_chart = build_chart_reference
validate_interpretation = validate_vision_output


if __name__ == "__main__":
    print(canonical_json(install_qualification()))
