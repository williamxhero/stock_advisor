"""OpenAI-compatible Chat Completions client with local research tools."""
from __future__ import annotations

import json
import os
import platform
import random
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

from .credentials import read_secret
from .acquisition import AcquisitionBoundary
from .research_tools import ResearchTools, ResearchToolError
from .secret_guard import assert_safe


_FALLBACK_CPA_USER_AGENT = "AITradingCompanion/1.0 (Windows; x64)"
_SYNTHESIZE_FROM_EXISTING_EVIDENCE = (
    "停止调用工具，基于本轮已经取得的证据立即输出要求的结构化 JSON；"
    "不得把空结果、缺失事实或未核实推测写成事实。"
)


def _synthesis_reserve_seconds(stage_timeout: int) -> int:
    """Keep enough of a research stage for one bounded schema-only response."""
    return min(180, max(15, stage_timeout // 5), max(1, stage_timeout // 2))


def _has_usable_evidence(trace: list[dict[str, Any]]) -> bool:
    return any(item.get("status") == "succeeded" and item.get("non_empty") for item in trace)


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, category: str = "provider_error", request_id: str | None = None, status: int | None = None, retry_after: float | None = None, retry_attempts: int = 1, tool_trace: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.category, self.request_id, self.status = category, request_id, status
        self.retry_after, self.retry_attempts = retry_after, retry_attempts
        self.tool_trace = list(tool_trace or [])

    def __str__(self) -> str:
        suffix = f" (attempts={self.retry_attempts})" if self.retry_attempts > 1 else ""
        return f"{self.category}: {super().__str__()}{suffix}"


class ResearchDeadlineExceeded(ProviderError, TimeoutError):
    pass


class CurrentInformationUnavailable(ProviderError):
    pass


@dataclass(frozen=True)
class ProviderResult:
    text: str
    response_id: str | None
    events: list[dict[str, Any]]
    usage: dict[str, Any]
    request_id: str | None
    tool_trace: list[dict[str, Any]]
    validation: dict[str, Any] | None = None


@dataclass
class _CircuitState:
    failures: int = 0
    open_until: float = 0.0


class ProviderClient:
    _circuits: dict[str, _CircuitState] = {}
    _circuit_lock = threading.Lock()

    def __init__(self, provider: dict[str, Any], research: dict[str, Any], home: Path) -> None:
        self.provider, self.research, self.home = provider, research, home
        # Version detection is local-only and runs once for each runtime process.
        # It never invokes Codex as an LLM backend.
        self.user_agent = current_codex_desktop_user_agent()

    def probe(self) -> dict[str, Any]:
        """Verify the Chat Completions subset used by the runtime."""
        fast = self._model("fast")
        timeout = int(self._retry_settings()["probe_timeout_seconds"])
        normal = self._request(self._payload("Reply with OK.", fast["id"], "medium"), timeout)
        streamed, deltas = self._request_stream(self._payload("Reply with OK.", fast["id"], "medium", stream=True), timeout, lambda _delta: None)
        if not deltas and not _message_content(streamed):
            raise ProviderError("provider capability probe did not return streamed output", category="capability_missing")
        structured_payload = self._payload("Return the requested object.", fast["id"], "medium")
        structured_payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "capability_probe", "strict": True, "schema": {"type": "object", "additionalProperties": False, "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}},
        }
        structured = self._request(structured_payload, timeout)
        function = {"type": "function", "function": {"name": "capability_probe", "description": "Return a fixed probe value.", "parameters": {"type": "object", "additionalProperties": False, "properties": {}}}}
        called_payload = self._payload("Call capability_probe.", fast["id"], "medium")
        called_payload.update({"tools": [function], "tool_choice": {"type": "function", "function": {"name": "capability_probe"}}})
        called = self._request(called_payload, timeout)
        calls = _tool_calls(called)
        if not calls:
            raise ProviderError("provider capability probe did not return a tool call", category="capability_missing")
        continuation = self._payload_from_messages([
            {"role": "user", "content": "Call capability_probe."},
            {"role": "assistant", "tool_calls": [calls[0]]},
            {"role": "tool", "tool_call_id": calls[0]["id"], "content": "{\"ok\":true}"},
        ], fast["id"], "medium")
        continuation["tools"] = [function]
        self._request(continuation, timeout)
        return {"available": True, "base_url": self._base_url(), "response_id": normal.get("id"), "stream_response_id": streamed.get("id"), "structured_response_id": structured.get("id"), "function_response_id": called.get("id")}

    def run(
        self, prompt: str, schema: Path | None, *, slot: str, effort: str,
        search: bool, timeout: int, on_delta: Callable[[str], None] | None = None,
        research_validator: Callable[[dict[str, Any], list[dict[str, Any]]], dict[str, Any]] | None = None,
        max_coverage_repairs: int | None = None,
        retry_stream_after_delta: bool = False,
        attempt_id: str | None = None,
        max_model_turns: int | None = None,
        max_tool_calls: int | None = None,
        max_empty_tool_results: int = 3,
    ) -> ProviderResult:
        assert_safe(prompt, boundary="Provider input")
        started = time.monotonic()
        deadline = started + timeout
        tools = ResearchTools(self.home, self.research) if search else None
        acquisition = AcquisitionBoundary(attempt_id or str(uuid.uuid4())) if search else None
        trace: list[dict[str, Any]] = []
        seen_tool_calls: set[str] = set()
        model = self._model(slot)
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        deltas: list[str] = []
        coverage_repairs = 0
        forced_tool: str | None = None
        model_turns = 0
        tool_calls = 0
        empty_tool_results = 0
        synthesis_reserve = _synthesis_reserve_seconds(timeout) if search and schema is not None else 0
        while True:
            if time.monotonic() >= deadline:
                raise ResearchDeadlineExceeded(
                    "research hard deadline exceeded",
                    category="provider_timeout",
                    tool_trace=trace,
                )
            if max_model_turns is not None and model_turns >= max_model_turns:
                raise ProviderError("research model turn limit exceeded", category="research_loop_limit", tool_trace=trace)
            model_turns += 1
            remaining = max(1, int(deadline - time.monotonic()))
            has_evidence = _has_usable_evidence(trace)
            if tools is not None and schema is not None and has_evidence and remaining <= synthesis_reserve:
                messages.append({
                    "role": "user",
                    "content": _SYNTHESIZE_FROM_EXISTING_EVIDENCE,
                })
                tools = None
                deltas = []
                continue
            # CPA accepts tools and JSON Schema independently, but its upstream
            # translation rejects the combination. Keep the research loop in
            # ordinary Chat Completions mode, then make one schema-only pass.
            payload_schema = schema if tools is None else None
            payload = self._payload_from_messages(
                messages, model["id"], effort or model.get("effort") or "medium",
                schema=payload_schema, tools=tools, stream=on_delta is not None,
                forced_tool=forced_tool,
            )
            forced_tool = None
            request_timeout = remaining
            if tools is not None and schema is not None and has_evidence:
                request_timeout = max(1, remaining - synthesis_reserve)
            try:
                response, new_deltas = self._request_stream(
                    payload, request_timeout, on_delta, retry_after_delta=retry_stream_after_delta,
                ) if on_delta else (self._request(payload, request_timeout), [])
            except ProviderError as exc:
                if (
                    exc.category == "provider_timeout"
                    and tools is not None
                    and schema is not None
                    and _has_usable_evidence(trace)
                    and time.monotonic() < deadline
                ):
                    messages.append({"role": "user", "content": _SYNTHESIZE_FROM_EXISTING_EVIDENCE})
                    tools = None
                    deltas = []
                    continue
                if trace and not getattr(exc, "tool_trace", None):
                    exc.tool_trace = list(trace)
                raise
            except Exception as exc:
                if trace and not getattr(exc, "tool_trace", None):
                    exc.tool_trace = list(trace)
                raise
            deltas.extend(new_deltas)
            calls = _tool_calls(response)
            if not calls:
                text = _message_content(response) or "".join(deltas)
                if schema is not None and tools is not None:
                    messages.extend([
                        {"role": "assistant", "content": text},
                        {"role": "user", "content": "将以上研究结果整理为要求的结构化 JSON。只返回 JSON，不要添加 Markdown 或解释。"},
                    ])
                    tools = None
                    deltas = []
                    continue
                validation = None
                if research_validator is not None:
                    try:
                        structured = json.loads(text)
                    except json.JSONDecodeError as exc:
                        raise ProviderError("Provider returned invalid structured research JSON", category="invalid_response", tool_trace=trace) from exc
                    validation = research_validator(structured, trace)
                    if not validation.get("passed"):
                        if max_coverage_repairs is not None and coverage_repairs >= max_coverage_repairs:
                            assert_safe(text, boundary="Provider output")
                            return ProviderResult(text, response.get("id"), [], response.get("usage") or {}, _request_id(response), trace, validation)
                        coverage_repairs += 1
                        messages.extend([
                            {"role": "assistant", "content": text},
                            {"role": "user", "content": self._coverage_repair_message(validation)},
                        ])
                        tools = ResearchTools(self.home, self.research)
                        forced_tool = self._repair_tool(validation, trace)
                        deltas = []
                        continue
                assert_safe(text, boundary="Provider output")
                return ProviderResult(text, response.get("id"), [], response.get("usage") or {}, _request_id(response), trace, validation)
            if tools is None:
                raise ProviderError("Provider attempted an unavailable tool", category="tool_policy", request_id=_request_id(response), tool_trace=trace)
            messages.append(_assistant_message(response))
            usable_tool_result = False
            for call in calls:
                if max_tool_calls is not None and tool_calls >= max_tool_calls:
                    raise ProviderError("research tool call limit exceeded", category="research_loop_limit", request_id=_request_id(response), tool_trace=trace)
                tool_calls += 1
                name = str(call.get("function", {}).get("name") or "")
                call_id = str(call.get("id") or "")
                arguments: dict[str, Any] = {}
                try:
                    arguments = json.loads(str(call.get("function", {}).get("arguments") or "{}"))
                    if not isinstance(arguments, dict):
                        raise ValueError("tool arguments must be an object")
                    fingerprint = _hash_json({"tool": name, "arguments": arguments})
                    if fingerprint in seen_tool_calls:
                        raise ResearchToolError("provider repeated an identical research tool call")
                    seen_tool_calls.add(fingerprint)
                    result = tools.call(name, arguments)
                    non_empty = bool(result.get("results") or result.get("text") or result.get("content_sha256"))
                    usable_tool_result = usable_tool_result or non_empty
                    assert acquisition is not None
                    observation, result = acquisition.observe(name, arguments, result, non_empty)
                    trace.append(observation)
                except (ValueError, ResearchToolError) as exc:
                    result = {"error": str(exc), "backend": name}
                    trace.append({
                        "tool": name, "backend": name, "operation": name, "ok": False,
                        "status": "failed", "non_empty": False, "arguments": arguments if "arguments" in locals() else {},
                        "attempt_id": attempt_id, "evidence_items": [], "observation_id": str(uuid.uuid4()),
                        "error_code": type(exc).__name__, "error": str(exc),
                    })
                messages.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps(result, ensure_ascii=False)})
            if usable_tool_result:
                empty_tool_results = 0
            else:
                empty_tool_results += 1
                if empty_tool_results >= max_empty_tool_results:
                    if any(item.get("status") == "succeeded" and item.get("non_empty") for item in trace):
                        messages.append({
                            "role": "user",
                            "content": _SYNTHESIZE_FROM_EXISTING_EVIDENCE,
                        })
                        tools = None
                        deltas = []
                        continue
                    raise ProviderError("research empty tool result limit exceeded", category="research_loop_limit", tool_trace=trace)

    @staticmethod
    def _tool_observation(name: str, arguments: dict[str, Any], result: dict[str, Any], non_empty: bool) -> dict[str, Any]:
        urls: list[str] = []
        if result.get("url"):
            urls.append(str(result["url"]))
        for row in result.get("results") or []:
            if isinstance(row, dict) and row.get("url"):
                urls.append(str(row["url"]))
        result_items: list[dict[str, Any]] = []
        rows_by_url = {
            str(row.get("url")): row for row in result.get("results") or []
            if isinstance(row, dict) and row.get("url")
        }
        for url in dict.fromkeys(urls):
            row = rows_by_url.get(url) or result
            evidence_text = str(row.get("snippet") or row.get("text") or row.get("title") or "")[:8000]
            result_items.append({
                "url": url,
                "result_item_hash": _hash_json(row),
                "title": str(row.get("title") or result.get("title") or ""),
                "evidence_text": evidence_text,
                "published_at": row.get("published_at"),
                "acquired_at": result.get("acquired_at"),
                "source_family": _source_family(url),
                "upstream_id": urlsplit(url).netloc.lower(),
            })
        return {
            "observation_id": str(uuid.uuid4()),
            "tool": name,
            "backend": str(result.get("backend") or name),
            "operation": name,
            "ok": True,
            "status": "succeeded",
            "non_empty": non_empty,
            "arguments": arguments,
            "acquired_at": result.get("acquired_at"),
            "result_urls": list(dict.fromkeys(urls)),
            "result_items": result_items,
            "content_sha256": result.get("content_sha256") or _hash_json(result),
            "result_sha256": _hash_json(result),
        }

    @staticmethod
    def _coverage_repair_message(validation: dict[str, Any]) -> str:
        return (
            "EvidenceGate 拒绝结束研究。请只针对以下结构化缺口继续调用研究工具，"
            "随后重新输出完整 JSON；不要用旧闻、推测或无时间戳材料补齐："
            + json.dumps({
                "problems": validation.get("problems") or [],
                "missing_requirements": validation.get("missing_requirements") or [],
                "attempted_backends": validation.get("attempted_backends") or [],
            }, ensure_ascii=False)
        )

    @staticmethod
    def _repair_tool(validation: dict[str, Any], trace: list[dict[str, Any]]) -> str:
        attempted = {str(item.get("tool") or "") for item in trace}
        failed_browse = any(item.get("tool") == "browse_page" and item.get("status") == "failed" for item in trace)
        if failed_browse and "search_searxng" not in attempted:
            return "search_searxng"
        if "search_searxng" in attempted and "browse_page" not in attempted:
            return "browse_page"
        return "search_searxng"

    def _payload(self, prompt: str, model: str, effort: str, *, stream: bool = False) -> dict[str, Any]:
        return self._payload_from_messages([{"role": "user", "content": prompt}], model, effort, stream=stream)

    def _payload_from_messages(self, messages: list[dict[str, Any]], model: str, effort: str, *, schema: Path | None = None, tools: ResearchTools | None = None, stream: bool = False, forced_tool: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": model, "messages": messages, "reasoning_effort": effort}
        if stream:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}
        if schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": _schema_name(schema),
                    "strict": True,
                    "schema": _provider_json_schema(json.loads(schema.read_text(encoding="utf-8"))),
                },
            }
        if tools:
            payload["tools"] = tools.definitions()
            payload["parallel_tool_calls"] = False
            if forced_tool:
                payload["tool_choice"] = {"type": "function", "function": {"name": forced_tool}}
        return payload

    def _model(self, slot: str) -> dict[str, Any]:
        models = self.provider.get("models") if isinstance(self.provider.get("models"), dict) else {}
        model = models.get(slot) if isinstance(models.get(slot), dict) else {}
        if not model.get("id"):
            raise ProviderError(f"Provider model slot is not configured: {slot}", category="provider_not_configured")
        return model

    def _base_url(self) -> str:
        value = str(self.provider.get("base_url") or "").rstrip("/")
        if not value:
            raise ProviderError("Provider URL is not configured", category="provider_not_configured")
        return value

    def _request(self, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
        idempotency_key = str(uuid.uuid4())
        return self._with_retries(lambda attempt_timeout: self._request_once(payload, attempt_timeout, idempotency_key=idempotency_key), timeout)

    def _request_once(self, payload: dict[str, Any], timeout: int, *, idempotency_key: str | None = None) -> dict[str, Any]:
        request = self._request_object(payload, idempotency_key=idempotency_key)
        try:
            with urlopen(request, timeout=timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
                if isinstance(result, dict):
                    result.setdefault("_request_id", _response_request_id(response.headers))
                    return result
        except HTTPError as exc:
            raise ProviderError(_http_error_message(exc), category=_http_category(exc.code), request_id=_response_request_id(exc.headers), status=exc.code, retry_after=_retry_after(exc.headers.get("Retry-After"))) from exc
        except TimeoutError as exc:
            raise ProviderError("Provider request timed out", category="provider_timeout") from exc
        except (URLError, ConnectionError) as exc:
            raise ProviderError("Provider network request failed", category="provider_network") from exc
        except json.JSONDecodeError as exc:
            raise ProviderError("Provider returned invalid JSON", category="invalid_response") from exc
        raise ProviderError("Provider returned an invalid response", category="invalid_response")

    def _request_stream(
        self, payload: dict[str, Any], timeout: int, on_delta: Callable[[str], None],
        *, retry_after_delta: bool = False,
    ) -> tuple[dict[str, Any], list[str]]:
        published = False
        idempotency_key = str(uuid.uuid4())

        def guarded_delta(delta: str) -> None:
            nonlocal published
            published = True
            on_delta(delta)

        return self._with_retries(
            lambda attempt_timeout: self._request_stream_once(
                payload, attempt_timeout, guarded_delta, idempotency_key=idempotency_key,
            ),
            timeout, can_retry=lambda: retry_after_delta or not published,
        )

    def _request_stream_once(self, payload: dict[str, Any], timeout: int, on_delta: Callable[[str], None], *, idempotency_key: str | None = None) -> tuple[dict[str, Any], list[str]]:
        request = self._request_object(payload, idempotency_key=idempotency_key)
        response_data: dict[str, Any] = {"object": "chat.completion", "choices": [{"index": 0, "message": {"role": "assistant", "content": None}, "finish_reason": None}]}
        content: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}
        usage: dict[str, Any] = {}
        try:
            with urlopen(request, timeout=timeout) as response:
                for raw in response:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        continue
                    event = json.loads(data)
                    response_data["id"] = event.get("id") or response_data.get("id")
                    choice = (event.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}
                    text = delta.get("content")
                    if text:
                        text = str(text)
                        assert_safe(text, boundary="Provider stream output")
                        content.append(text)
                        on_delta(text)
                    for call_delta in delta.get("tool_calls") or []:
                        index = int(call_delta.get("index", 0))
                        call = tool_calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                        call["id"] = call["id"] or str(call_delta.get("id") or "")
                        function = call_delta.get("function") or {}
                        call["function"]["name"] += str(function.get("name") or "")
                        call["function"]["arguments"] += str(function.get("arguments") or "")
                    if choice.get("finish_reason") is not None:
                        response_data["choices"][0]["finish_reason"] = choice["finish_reason"]
                    if isinstance(event.get("usage"), dict):
                        usage = event["usage"]
                response_data.setdefault("_request_id", _response_request_id(response.headers))
        except HTTPError as exc:
            raise ProviderError(_http_error_message(exc), category=_http_category(exc.code), request_id=_response_request_id(exc.headers), status=exc.code, retry_after=_retry_after(exc.headers.get("Retry-After"))) from exc
        except TimeoutError as exc:
            raise ProviderError("Provider streaming request timed out", category="provider_timeout") from exc
        except (URLError, ConnectionError) as exc:
            raise ProviderError("Provider streaming request failed", category="provider_network") from exc
        except json.JSONDecodeError as exc:
            raise ProviderError("Provider stream returned invalid JSON", category="invalid_response") from exc
        if not response_data.get("id") or response_data["choices"][0].get("finish_reason") is None:
            raise ProviderError("Provider stream ended without a completed response", category="incomplete_response")
        message = response_data["choices"][0]["message"]
        message["content"] = "".join(content) or None
        if tool_calls:
            message["tool_calls"] = [tool_calls[index] for index in sorted(tool_calls)]
        if usage:
            response_data["usage"] = usage
        return response_data, content

    def _with_retries(self, operation: Callable[[int], Any], timeout: int, *, can_retry: Callable[[], bool] | None = None) -> Any:
        self._assert_circuit_closed()
        settings = self._retry_settings()
        started = time.monotonic()
        last_error: ProviderError | None = None
        for attempt in range(1, settings["max_attempts"] + 1):
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                break
            try:
                attempt_timeout = min(int(settings["per_attempt_timeout_seconds"]), max(1, int(remaining)))
                result = operation(attempt_timeout)
                self._record_circuit_success()
                return result
            except ProviderError as exc:
                last_error = exc
                retry_allowed = _is_retryable(exc) and attempt < settings["max_attempts"] and (can_retry is None or can_retry())
                if not retry_allowed:
                    break
                cap = min(settings["max_backoff_seconds"], settings["initial_backoff_seconds"] * (2 ** (attempt - 1)))
                delay = exc.retry_after if exc.retry_after is not None else random.uniform(0, cap)
                if delay >= timeout - (time.monotonic() - started):
                    break
                if delay > 0:
                    time.sleep(delay)
        if last_error is None:
            last_error = ProviderError("Provider request timed out", category="provider_timeout")
        last_error.retry_attempts = attempt if "attempt" in locals() else 0
        if _is_retryable(last_error):
            self._record_circuit_failure(settings)
        raise last_error

    def _retry_settings(self) -> dict[str, float | int]:
        raw = self.provider.get("retry") if isinstance(self.provider.get("retry"), dict) else {}
        return {
            "max_attempts": max(1, int(raw.get("max_attempts", 5))),
            "per_attempt_timeout_seconds": max(15, int(raw.get("per_attempt_timeout_seconds", 90))),
            "initial_backoff_seconds": max(0.0, float(raw.get("initial_backoff_seconds", 1))),
            "max_backoff_seconds": max(0.0, float(raw.get("max_backoff_seconds", 8))),
            "circuit_breaker_failures": max(1, int(raw.get("circuit_breaker_failures", 5))),
            "circuit_breaker_cooldown_seconds": max(1.0, float(raw.get("circuit_breaker_cooldown_seconds", 30))),
            "probe_timeout_seconds": max(30, int(raw.get("probe_timeout_seconds", 180))),
        }

    def _assert_circuit_closed(self) -> None:
        key, current = self._base_url(), time.monotonic()
        with self._circuit_lock:
            state = self._circuits.setdefault(key, _CircuitState())
            if state.open_until > current:
                raise ProviderError("Provider is temporarily unavailable; retry after the circuit-breaker cooldown", category="provider_circuit_open")
            if state.open_until:
                state.open_until, state.failures = 0.0, 0

    def _record_circuit_success(self) -> None:
        with self._circuit_lock:
            self._circuits[self._base_url()] = _CircuitState()

    def _record_circuit_failure(self, settings: dict[str, float | int]) -> None:
        key = self._base_url()
        with self._circuit_lock:
            state = self._circuits.setdefault(key, _CircuitState())
            state.failures += 1
            if state.failures >= int(settings["circuit_breaker_failures"]):
                state.open_until = time.monotonic() + float(settings["circuit_breaker_cooldown_seconds"])

    def _request_object(self, payload: dict[str, Any], *, idempotency_key: str | None = None) -> Request:
        key = read_secret(str(self.provider.get("credential_target") or ""))
        if not key:
            raise ProviderError("Provider API key is not configured in Windows Credential Manager", category="provider_not_configured")
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json", "Accept": "text/event-stream" if payload.get("stream") else "application/json", "User-Agent": self.user_agent}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return Request(f"{self._base_url()}/chat/completions", data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")


def current_codex_desktop_user_agent() -> str:
    """Build a current local Codex Desktop identifier without contacting Codex."""
    version = _local_codex_version()
    if not version:
        return _FALLBACK_CPA_USER_AGENT
    machine = platform.machine().lower()
    architecture = "x86_64" if machine in {"amd64", "x86_64"} else machine or "unknown"
    desktop_build = _local_codex_desktop_build()
    desktop_suffix = f"; {desktop_build}" if desktop_build else ""
    return f"Codex Desktop/{version} (Windows {platform.version()}; {architecture}) unknown (Codex Desktop{desktop_suffix})"


def _local_codex_version() -> str | None:
    root = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin"
    candidates = list(root.glob("*/codex.exe")) if root.is_dir() else []
    if not candidates:
        return None
    try:
        executable = max(candidates, key=lambda path: path.stat().st_mtime)
        completed = subprocess.run(
            [str(executable), "--version"], capture_output=True, text=True,
            timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"(?:codex(?:-cli)?\s+)([^\s]+)", completed.stdout or "", re.IGNORECASE)
    return match.group(1) if completed.returncode == 0 and match else None


def _local_codex_desktop_build() -> str | None:
    manifest = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "chrome-native-hosts-v2.json"
    try:
        entries = json.loads(manifest.read_text(encoding="utf-8")).get("entries", [])
    except (OSError, ValueError):
        return None
    current = max((entry for entry in entries if isinstance(entry, dict)), key=lambda entry: str(entry.get("updatedAt") or ""), default=None)
    return str(current.get("appVersion")) if isinstance(current, dict) and current.get("appVersion") else None


def _assistant_message(response: dict[str, Any]) -> dict[str, Any]:
    message = dict((response.get("choices") or [{}])[0].get("message") or {})
    message.setdefault("role", "assistant")
    return message


def _tool_calls(response: dict[str, Any]) -> list[dict[str, Any]]:
    calls = (response.get("choices") or [{}])[0].get("message", {}).get("tool_calls") or []
    return [call for call in calls if isinstance(call, dict) and call.get("id") and isinstance(call.get("function"), dict)]


def _message_content(response: dict[str, Any]) -> str:
    content = (response.get("choices") or [{}])[0].get("message", {}).get("content")
    return str(content or "")


def _request_id(response: dict[str, Any]) -> str | None:
    return response.get("_request_id") if isinstance(response.get("_request_id"), str) else None


def _response_request_id(headers: Any) -> str | None:
    for name in ("x-request-id", "request-id", "openai-request-id", "x-cpa-trace-id"):
        value = headers.get(name)
        if value:
            return str(value)
    return None


def _http_category(status: int) -> str:
    return "provider_auth" if status in {401, 403} else "provider_rate_limited" if status == 429 else "provider_quota" if status == 402 else "provider_http"


def _http_error_message(error: HTTPError) -> str:
    """Keep useful provider diagnostics without retaining arbitrary response bodies."""
    try:
        raw = error.read(64 * 1024).decode("utf-8", errors="replace")
        payload = json.loads(raw)
        if isinstance(payload, dict):
            detail = payload.get("error") if isinstance(payload.get("error"), dict) else payload
            if isinstance(detail, dict):
                message = detail.get("message") or detail.get("type")
                if message:
                    return f"Provider HTTP {error.code}: {str(message)[:500]}"
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    return f"Provider HTTP {error.code}"


def _is_retryable(error: ProviderError) -> bool:
    if error.category in {"provider_network", "provider_timeout", "provider_rate_limited", "incomplete_response"}:
        return True
    if error.category != "provider_http":
        return False
    if error.status in {408, 425, 500, 502, 503, 504}:
        return True
    # CPA can surface a transient upstream Responses/WebSocket failure as a
    # 400 even though the request itself is valid. Do not retry ordinary 400s.
    message = str(error).lower()
    return error.status == 400 and ("upstream request failed" in message or "upstream_error" in message)


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _hash_json(value: dict[str, Any]) -> str:
    import hashlib
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _source_family(url: str) -> str:
    host = urlsplit(url).netloc.lower().split(":", 1)[0]
    if host.endswith(".gov.cn") or host in {
        "gov.cn", "www.gov.cn", "www.csrc.gov.cn", "www.sse.com.cn", "www.szse.cn",
        "www.bse.cn", "www.cninfo.com.cn", "www.pbc.gov.cn", "www.stats.gov.cn",
    }:
        return "official"
    if host in {"quote.eastmoney.com", "push2.eastmoney.com", "finance.sina.com.cn"}:
        return "market_data"
    return "media"


def _schema_name(schema: Path) -> str:
    """CPA accepts only OpenAI schema names made from alphanumerics, '_' and '-'."""
    name = re.sub(r"[^A-Za-z0-9_-]", "_", schema.stem)
    return name or "structured_output"


def _provider_json_schema(value: Any) -> Any:
    """Make semantically implied scalar types explicit for strict providers."""
    if isinstance(value, list):
        return [_provider_json_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    normalized = {key: _provider_json_schema(item) for key, item in value.items()}
    if "type" not in normalized:
        candidates = [normalized["const"]] if "const" in normalized else normalized.get("enum")
        inferred = {_json_schema_scalar_type(item) for item in candidates or []}
        inferred.discard(None)
        if len(inferred) == 1:
            normalized["type"] = inferred.pop()
    return normalized


def _json_schema_scalar_type(value: Any) -> str | None:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    return None
