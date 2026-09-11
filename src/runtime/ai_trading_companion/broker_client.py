"""The sole LLM transport: yosef-server's Provider Broker."""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .secret_guard import assert_safe


class BrokerError(RuntimeError):
    def __init__(self, message: str, *, category: str = "broker_error", status: int | None = None,
                 attempts: list[dict[str, Any]] | None = None, request_id: str | None = None,
                 verifier: dict[str, Any] | None = None, tool_trace: list[dict[str, Any]] | None = None,
                 metadata: dict[str, Any] | None = None, output: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.category, self.status = category, status
        self.attempts = attempts or []
        self.request_id, self.verifier = request_id, verifier
        self.tool_trace = tool_trace or []
        self.metadata = metadata or {}
        self.output = output


@dataclass(frozen=True)
class BrokerRequest:
    stage: str
    packet: dict[str, Any]
    packet_sha256: str
    intellect: str
    effort: str
    schema: dict[str, Any] | None = None
    visible_stream: bool = False
    on_delta: Callable[[str], None] | None = None
    absolute_deadline: float = math.inf
    idle_timeout_seconds: float = 30.0
    output_token_limit: int = 2_000
    verifier_name: str = "none/v1"
    verifier: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    h0_forbidden: bool = False

    def __post_init__(self) -> None:
        if self.intellect not in {"standard", "smart", "expert"}:
            raise ValueError("Broker intellect must be standard, smart, or expert")
        if self.effort not in {"medium", "high", "xhigh"}:
            raise ValueError("Broker effort must be a capability-contract value")
        if self.idle_timeout_seconds <= 0:
            raise ValueError("Broker idle timeout must be positive")
        if self.packet_sha256 != canonical_packet_hash(self.packet):
            raise ValueError("Broker request packet hash does not match canonical frozen input")
        if self.h0_forbidden and _contains_h0(self.packet):
            raise ValueError("M1 packet contains H0 or human-message material")


@dataclass(frozen=True)
class BrokerResponse:
    output_text: str
    result: Any
    actual_model: str | None
    provider: str | None
    intellect: str
    fulfilled_intellect: str | None
    request_id: str | None
    fulfilled_effort: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    cost_estimate: float | None = None
    ttft_ms: float | None = None

    def audit_metadata(self) -> dict[str, Any]:
        return {
            "kind": "broker_call",
            "intellect": self.intellect,
            "fulfilled_intellect": self.fulfilled_intellect,
            "fulfilled_effort": self.fulfilled_effort,
            "actual_model": self.actual_model,
            "provider": self.provider,
            "request_id": self.request_id,
            "attempts": _safe_attempts(self.attempts),
            "cost_estimate": self.cost_estimate,
            "ttft_ms": self.ttft_ms,
        }


class ProviderBrokerClient:
    """One request in, one Broker invocation out; no local routing or fallback."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def invoke(self, request: BrokerRequest) -> BrokerResponse:
        payload = self._payload(request)
        if request.visible_stream:
            raw, streamed = self._stream(payload, request)
            if streamed != str(raw.get("output_text") or ""):
                raise BrokerError("Broker stream final text does not match deltas", category="broker_stream_incomplete")
        else:
            raw = self._post(
                "/v1/generate", payload, request.absolute_deadline, request.idle_timeout_seconds,
            )
        response = self._response(raw, request)
        return response

    def _payload(self, request: BrokerRequest) -> dict[str, Any]:
        remaining = request.absolute_deadline - time.monotonic()
        if math.isfinite(request.absolute_deadline) and remaining <= 0:
            raise BrokerError("Broker request deadline expired", category="broker_timeout")
        deadline_ms = max(1_000, int((remaining if math.isfinite(remaining) else 60) * 1_000))
        prompt = _structured_prompt(request.packet, request.schema)
        assert_safe(prompt, boundary="Broker prompt")
        return {
            "prompt": prompt,
            "intellect": request.intellect,
            "effort": request.effort,
            "deadline_ms": deadline_ms,
            "output_token_limit": max(1, int(request.output_token_limit)),
        }

    def _post(
        self, path: str, payload: dict[str, Any], deadline: float, idle_timeout_seconds: float,
    ) -> dict[str, Any]:
        request = Request(
            self.base_url + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urlopen(request, timeout=_timeout(deadline)) as response:
                raw = b"".join(_response_chunks(response, deadline, idle_timeout_seconds)).decode("utf-8")
        except HTTPError as exc:
            raise _http_error(exc) from exc
        except TimeoutError as exc:
            raise BrokerError("Broker request timed out", category="broker_timeout") from exc
        except URLError as exc:
            raise BrokerError("Broker network request failed", category="broker_network") from exc
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BrokerError("Broker returned invalid JSON", category="broker_protocol") from exc
        if not isinstance(value, dict):
            raise BrokerError("Broker returned an invalid response", category="broker_protocol")
        return value

    def _stream(self, payload: dict[str, Any], request: BrokerRequest) -> tuple[dict[str, Any], str]:
        raw_request = Request(
            self.base_url + "/v1/generate/stream",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        )
        event, data, chunks, final = None, [], [], None
        try:
            with urlopen(raw_request, timeout=_timeout(request.absolute_deadline)) as response:
                for line in _response_lines(
                    response, request.absolute_deadline, request.idle_timeout_seconds,
                ):
                    if not line:
                        if event == "delta":
                            value = _event_json(data)
                            text = value.get("text") if isinstance(value, dict) else None
                            if not isinstance(text, str):
                                raise BrokerError("Broker delta is invalid", category="broker_protocol")
                            assert_safe(text, boundary="Broker stream delta")
                            chunks.append(text)
                            if request.on_delta:
                                request.on_delta(text)
                        elif event == "final":
                            value = _event_json(data)
                            if not isinstance(value, dict):
                                raise BrokerError("Broker final event is invalid", category="broker_protocol")
                            final = value
                        event, data = None, []
                        continue
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        data.append(line[5:].strip())
        except HTTPError as exc:
            raise _http_error(exc) from exc
        except TimeoutError as exc:
            raise BrokerError("Broker stream timed out", category="broker_timeout") from exc
        except (URLError, UnicodeDecodeError) as exc:
            raise BrokerError("Broker stream failed", category="broker_network") from exc
        if final is None:
            raise BrokerError("Broker stream ended without final event", category="broker_stream_incomplete")
        return final, "".join(chunks)

    def _response(self, raw: dict[str, Any], request: BrokerRequest) -> BrokerResponse:
        text = raw.get("output_text")
        if raw.get("status") != "completed" or not isinstance(text, str) or not text.strip():
            raise BrokerError("Broker response is incomplete", category="broker_protocol",
                              attempts=_safe_attempts(raw.get("attempts")), request_id=_text(raw.get("request_id")),
                              metadata=_metadata(raw, request))
        assert_safe(text, boundary="Broker output")
        try:
            result = _parse_structured_json(text) if request.schema is not None else text
        except json.JSONDecodeError as exc:
            raise BrokerError("Broker output is not valid JSON", category="broker_output_invalid",
                              attempts=_safe_attempts(raw.get("attempts")), request_id=_text(raw.get("request_id")),
                              metadata=_metadata(raw, request)) from exc
        schema = _validate_schema(result, request.schema)
        business = request.verifier(result) if request.verifier and isinstance(result, dict) else {"passed": True, "problems": []}
        verifier = {"schema": schema, "business": business, "name": request.verifier_name,
                    "passed": bool(schema["passed"] and business.get("passed"))}
        if not verifier["passed"]:
            raise BrokerError("Broker output did not pass local verification", category="broker_output_invalid",
                              attempts=_safe_attempts(raw.get("attempts")), request_id=_text(raw.get("request_id")), verifier=verifier,
                              metadata=_metadata(raw, request), output=result if isinstance(result, dict) else None)
        usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
        return BrokerResponse(
            output_text=text, result=result, actual_model=_text(raw.get("actual_model")), provider=_text(raw.get("provider")),
            intellect=request.intellect, fulfilled_intellect=_text(raw.get("fulfilled_intellect")),
            request_id=_text(raw.get("request_id")), fulfilled_effort=_text(raw.get("fulfilled_effort")),
            usage=usage, attempts=_safe_attempts(raw.get("attempts")),
            cost_estimate=float(raw["cost_estimate"]) if isinstance(raw.get("cost_estimate"), (int, float)) else None,
            ttft_ms=float(raw["ttft_ms"]) if isinstance(raw.get("ttft_ms"), (int, float)) else None,
        )


def canonical_packet_hash(packet: dict[str, Any]) -> str:
    raw = json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _structured_prompt(packet: dict[str, Any], schema: dict[str, Any] | None) -> str:
    if schema is None:
        return json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return json.dumps({
        "instruction": "Return only one JSON object matching output_schema. Do not add prose or Markdown fences.",
        "output_schema": schema,
        "input": packet,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _timeout(deadline: float) -> float:
    if not math.isfinite(deadline):
        return 65.0
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise BrokerError("Broker request deadline expired", category="broker_timeout")
    return max(0.1, remaining)


def _response_chunks(response: Any, deadline: float, idle_timeout_seconds: float = 30.0):
    """Read an HTTP response without allowing trickle traffic to extend its deadline."""
    reader = getattr(response, "read1", None)
    if not callable(reader):
        reader = response.read
    while True:
        timeout = min(_timeout(deadline), max(0.1, float(idle_timeout_seconds)))
        _set_response_timeout(response, timeout)
        chunk = reader(64 * 1024)
        _timeout(deadline)
        if not chunk:
            return
        yield chunk


def _response_lines(response: Any, deadline: float, idle_timeout_seconds: float = 30.0):
    pending = b""
    for chunk in _response_chunks(response, deadline, idle_timeout_seconds):
        pending += chunk
        while b"\n" in pending:
            raw_line, pending = pending.split(b"\n", 1)
            yield raw_line.rstrip(b"\r").decode("utf-8", errors="strict")
    if pending:
        yield pending.rstrip(b"\r").decode("utf-8", errors="strict")


def _set_response_timeout(response: Any, timeout: float) -> None:
    fp = getattr(response, "fp", None)
    raw = getattr(fp, "raw", None)
    socket = getattr(raw, "_sock", None)
    if socket is not None:
        socket.settimeout(timeout)


def _http_error(error: HTTPError) -> BrokerError:
    raw = error.read().decode("utf-8", errors="replace")
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        body = {}
    attempts = _safe_attempts(body.get("attempts")) if isinstance(body, dict) else []
    error_text = json.dumps(body, ensure_ascii=False, sort_keys=True).lower()
    effort_rejected = (
        error.code == 400 and "effort" in error_text
        and any(marker in error_text for marker in ("unsupported", "not supported", "invalid", "not allowed"))
    )
    category = (
        "broker_effort_unsupported" if effort_rejected
        else "broker_timeout" if error.code == 504
        else "broker_unavailable" if error.code == 503
        else "broker_protocol" if error.code == 400
        else "broker_http"
    )
    return BrokerError(
        f"Broker HTTP {error.code}", category=category, status=error.code, attempts=attempts,
        metadata={"capability": "effort"} if effort_rejected else None,
    )


def _event_json(lines: list[str]) -> Any:
    try:
        return json.loads("\n".join(lines))
    except json.JSONDecodeError as exc:
        raise BrokerError("Broker SSE data is invalid JSON", category="broker_protocol") from exc


def _safe_attempts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if not isinstance(item, dict):
            continue
        result.append({key: item[key] for key in ("provider", "status", "actual_model", "fulfilled") if key in item})
    return result


def _metadata(raw: dict[str, Any], request: BrokerRequest) -> dict[str, Any]:
    return {
        "provider": _text(raw.get("provider")), "intellect": request.intellect,
        "fulfilled_intellect": _text(raw.get("fulfilled_intellect")), "request_id": _text(raw.get("request_id")),
        "fulfilled_effort": _text(raw.get("fulfilled_effort")),
        "actual_model": _text(raw.get("actual_model")),
        "cost_estimate": raw.get("cost_estimate") if isinstance(raw.get("cost_estimate"), (int, float)) else None,
        "attempts": _safe_attempts(raw.get("attempts")),
    }


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _parse_structured_json(text: str) -> Any:
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as direct_error:
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.IGNORECASE | re.DOTALL)
        if fenced is None:
            raise direct_error
        return json.loads(fenced.group(1))


def _contains_h0(value: Any) -> bool:
    forbidden = {"h0", "human_messages", "chat_human", "human_message", "h0_propositions", "h0_actions"}
    if isinstance(value, dict):
        return any(str(key).lower() in forbidden or _contains_h0(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_h0(item) for item in value)
    return False


def _validate_schema(
    value: Any,
    schema: dict[str, Any] | None,
    path: str = "$",
    *,
    _root: dict[str, Any] | None = None,
    _references: tuple[str, ...] = (),
) -> dict[str, Any]:
    if schema is None:
        return {"passed": True, "problems": []}
    root = schema if _root is None else _root
    reference = schema.get("$ref")
    if isinstance(reference, str):
        if reference in _references:
            return {"passed": False, "problems": [f"{path}: recursive schema reference {reference}"]}
        resolved = _resolve_local_schema_reference(root, reference)
        if resolved is None:
            return {"passed": False, "problems": [f"{path}: unresolved schema reference {reference}"]}
        referenced = _validate_schema(
            value, resolved, path, _root=root, _references=(*_references, reference),
        )
        siblings = {key: item for key, item in schema.items() if key != "$ref"}
        if not siblings:
            return referenced
        sibling_result = _validate_schema(
            value, siblings, path, _root=root, _references=_references,
        )
        problems = [*referenced["problems"], *sibling_result["problems"]]
        return {"passed": not problems, "problems": list(dict.fromkeys(problems))}
    if isinstance(schema.get("oneOf"), list):
        alternatives = [
            _validate_schema(value, item, path, _root=root, _references=_references)
            for item in schema["oneOf"] if isinstance(item, dict)
        ]
        matched = [item for item in alternatives if item["passed"]]
        if len(matched) == 1:
            return {"passed": True, "problems": []}
        if not matched:
            details = [problem for item in alternatives for problem in item["problems"]]
            return {"passed": False, "problems": list(dict.fromkeys(details))}
        return {"passed": False, "problems": [f"{path}: ambiguous oneOf"]}
    if isinstance(schema.get("anyOf"), list):
        alternatives = [
            _validate_schema(value, item, path, _root=root, _references=_references)
            for item in schema["anyOf"] if isinstance(item, dict)
        ]
        if any(item["passed"] for item in alternatives):
            return {"passed": True, "problems": []}
        details = [problem for item in alternatives for problem in item["problems"]]
        return {"passed": False, "problems": list(dict.fromkeys(details))}
    problems: list[str] = []
    expected = schema.get("type")
    valid = {"object": isinstance(value, dict), "array": isinstance(value, list), "string": isinstance(value, str),
             "boolean": isinstance(value, bool), "integer": isinstance(value, int) and not isinstance(value, bool),
             "number": isinstance(value, (int, float)) and not isinstance(value, bool), "null": value is None}
    expected_types = expected if isinstance(expected, list) else [expected]
    recognized = [item for item in expected_types if item in valid]
    if recognized and not any(valid[item] for item in recognized):
        return {"passed": False, "problems": [f"{path}: expected {' or '.join(recognized)}"]}
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        problems.extend(f"{path}.{key}: required" for key in schema.get("required", []) if key not in value)
        if schema.get("additionalProperties") is False:
            problems.extend(f"{path}.{key}: additional property" for key in value if key not in properties)
        for key, child in properties.items():
            if key in value and isinstance(child, dict):
                problems.extend(_validate_schema(
                    value[key], child, f"{path}.{key}", _root=root, _references=_references,
                )["problems"])
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            problems.extend(_validate_schema(
                item, schema["items"], f"{path}[{index}]", _root=root, _references=_references,
            )["problems"])
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{path}: not in enum")
    if "const" in schema and value != schema["const"]:
        problems.append(f"{path}: const mismatch")
    if isinstance(value, str):
        if isinstance(schema.get("minLength"), int) and len(value) < schema["minLength"]:
            problems.append(f"{path}: shorter than minLength")
        if isinstance(schema.get("maxLength"), int) and len(value) > schema["maxLength"]:
            problems.append(f"{path}: longer than maxLength")
    if isinstance(value, list):
        if isinstance(schema.get("minItems"), int) and len(value) < schema["minItems"]:
            problems.append(f"{path}: fewer than minItems")
        if isinstance(schema.get("maxItems"), int) and len(value) > schema["maxItems"]:
            problems.append(f"{path}: more than maxItems")
        if schema.get("uniqueItems") is True:
            fingerprints = [json.dumps(item, ensure_ascii=False, sort_keys=True) for item in value]
            if len(fingerprints) != len(set(fingerprints)):
                problems.append(f"{path}: duplicate items")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(schema.get("minimum"), (int, float)) and value < schema["minimum"]:
            problems.append(f"{path}: below minimum")
        if isinstance(schema.get("maximum"), (int, float)) and value > schema["maximum"]:
            problems.append(f"{path}: above maximum")
    return {"passed": not problems, "problems": problems}


def _resolve_local_schema_reference(root: dict[str, Any], reference: str) -> dict[str, Any] | None:
    if not reference.startswith("#/"):
        return None
    current: Any = root
    for raw_part in reference[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current if isinstance(current, dict) else None
