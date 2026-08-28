"""OpenAI-compatible Chat Completions transport used by ProviderBroker.

This seam intentionally has no Provider-native research tools. Research is
executed locally through the Web Access Gateway before a frozen packet reaches a
model.
"""
from __future__ import annotations

import json
import os
import platform
import random
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .secret_guard import assert_safe


_FALLBACK_USER_AGENT = "AITradingCompanion/1.0 (Windows; x64)"


class ProviderError(RuntimeError):
    def __init__(
        self, message: str, *, category: str = "provider_error",
        request_id: str | None = None, status: int | None = None,
        retry_after: float | None = None, retry_attempts: int = 1,
        tool_trace: list[dict[str, Any]] | None = None,
    ) -> None:
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
    """Transport seam for Responses (Codex) and compatible Chat Completions."""

    _circuits: dict[str, _CircuitState] = {}
    _circuit_lock = threading.Lock()

    def __init__(
        self, provider: dict[str, Any], research: dict[str, Any], home: Path,
        *, user_agent: str | None = None, transport: str = "chat_completions",
    ) -> None:
        self.provider, self.research, self.home = provider, research, home
        self.user_agent = user_agent or current_codex_desktop_user_agent()
        if transport not in {"responses", "chat_completions"}:
            raise ValueError("unsupported Provider transport")
        self.transport = transport

    def run(
        self, prompt: str, schema: Path | None, *, slot: str, effort: str,
        search: bool, timeout: int, on_delta: Callable[[str], None] | None = None,
        **_legacy: Any,
    ) -> ProviderResult:
        if search:
            raise ProviderError(
                "native Provider tools are retired; use LocalResearchChain",
                category="tool_policy",
            )
        assert_safe(prompt, boundary="Provider input")
        model = self._model(slot)
        payload = self._payload(
            prompt, str(model["id"]), effort or str(model.get("effort") or "medium"),
            schema=schema, stream=on_delta is not None,
        )
        if on_delta is None:
            response = self._request_single(payload, timeout)
            text = _message_content(response)
            assert_safe(text, boundary="Provider output")
            return ProviderResult(text, response.get("id"), [], _usage(response), _request_id(response), [])
        response, deltas = self._request_stream_single(payload, timeout, on_delta)
        text = _message_content(response) or "".join(deltas)
        assert_safe(text, boundary="Provider output")
        return ProviderResult(text, response.get("id"), [], _usage(response), _request_id(response), [])

    def _payload(
        self, prompt: str, model: str, effort: str, *, schema: Path | None = None,
        stream: bool = False,
    ) -> dict[str, Any]:
        if self.transport == "responses":
            payload: dict[str, Any] = {"model": model, "input": prompt, "reasoning": {"effort": effort}}
        else:
            payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "reasoning_effort": effort}
        if schema is not None:
            schema_value = _provider_json_schema(json.loads(schema.read_text(encoding="utf-8")))
            if self.transport == "responses":
                payload["text"] = {"format": {"type": "json_schema", "name": _schema_name(schema), "strict": True, "schema": schema_value}}
            else:
                payload["response_format"] = {"type": "json_schema", "json_schema": {"name": _schema_name(schema), "strict": True, "schema": schema_value}}
        if stream:
            payload["stream"] = True
            if self.transport == "chat_completions": payload["stream_options"] = {"include_usage": True}
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

    def _chat_completions_url(self) -> str:
        """Accept the v3 exact endpoint while keeping one-release legacy URLs."""
        base_url = self._base_url()
        return base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"

    def _responses_url(self) -> str:
        base_url = self._base_url()
        if base_url.endswith("/responses"):
            return base_url
        if base_url.endswith("/chat/completions"):
            return f"{base_url.removesuffix('/chat/completions')}/responses"
        return f"{base_url}/responses"

    def _models_url(self) -> str:
        base_url = self._base_url()
        for suffix in ("/chat/completions", "/responses"):
            if base_url.endswith(suffix):
                return f"{base_url.removesuffix(suffix)}/models"
        return f"{base_url}/models"

    def _probe_connectivity_once(self, timeout: int) -> bool:
        return bool(self._list_models_once(timeout))

    def _list_models_once(self, timeout: int) -> list[str]:
        """Return the concrete model directory used to admit inference routes."""
        request = Request(
            self._models_url(),
            headers=self._request_headers(accept="application/json"),
            method="GET",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                document = json.loads(response.read().decode("utf-8"))
                rows = document.get("data") if isinstance(document, dict) else None
                if not isinstance(rows, list):
                    raise ProviderError("Provider model list response is invalid", category="invalid_response")
                return sorted({
                    str(row.get("id") or "").strip()
                    for row in rows
                    if isinstance(row, dict) and str(row.get("id") or "").strip()
                })
        except HTTPError as exc:
            raise _provider_http_error(exc) from exc
        except TimeoutError as exc:
            raise ProviderError("Provider availability probe timed out", category="provider_timeout") from exc
        except (URLError, ConnectionError) as exc:
            raise ProviderError("Provider availability probe failed", category="provider_network") from exc
        except json.JSONDecodeError as exc:
            raise ProviderError("Provider model list response is invalid", category="invalid_response") from exc

    def _request_single(self, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
        return self._with_retries(
            lambda current: self._request_once(payload, current, idempotency_key=str(uuid.uuid4())),
            timeout,
        )

    # Compatibility seam retained for callers that used the former transport.
    # It deliberately has no pool, hedge, or native-tool behaviour.
    def _request(self, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
        return self._request_single(payload, timeout)

    def _request_once(self, payload: dict[str, Any], timeout: int, *, idempotency_key: str) -> dict[str, Any]:
        try:
            with urlopen(self._request_object(payload, idempotency_key=idempotency_key), timeout=timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
                if not isinstance(result, dict):
                    raise ProviderError("Provider returned an invalid response", category="invalid_response")
                result.setdefault("_request_id", _response_request_id(response.headers))
                return result
        except HTTPError as exc:
            raise _provider_http_error(exc) from exc
        except TimeoutError as exc:
            raise ProviderError("Provider request timed out", category="provider_timeout") from exc
        except (URLError, ConnectionError) as exc:
            raise ProviderError("Provider network request failed", category="provider_network") from exc
        except json.JSONDecodeError as exc:
            raise ProviderError("Provider returned invalid JSON", category="invalid_response") from exc

    def _request_stream_single(
        self, payload: dict[str, Any], timeout: int, on_delta: Callable[[str], None],
        *, retry_after_delta: bool = False,
    ) -> tuple[dict[str, Any], list[str]]:
        published = False

        def guarded(delta: str) -> None:
            nonlocal published
            published = True
            on_delta(delta)

        return self._with_retries(
            lambda current: self._request_stream_once(
                payload, current, guarded, idempotency_key=str(uuid.uuid4()),
            ),
            timeout,
            can_retry=lambda: retry_after_delta or not published,
        )

    def _request_stream_once(
        self, payload: dict[str, Any], timeout: int, on_delta: Callable[[str], None],
        *, idempotency_key: str,
    ) -> tuple[dict[str, Any], list[str]]:
        if self.transport == "responses":
            return self._request_responses_stream_once(payload, timeout, on_delta, idempotency_key=idempotency_key)
        response: dict[str, Any] = {
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": None}, "finish_reason": None}],
        }
        content: list[str] = []
        usage: dict[str, Any] = {}
        deadline = time.monotonic() + timeout
        try:
            with urlopen(self._request_object(payload, idempotency_key=idempotency_key), timeout=timeout) as raw_response:
                for raw in raw_response:
                    if time.monotonic() >= deadline:
                        raise ProviderError("Provider streaming request exceeded its absolute deadline", category="provider_timeout")
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    event = json.loads(data)
                    if not isinstance(event, dict):
                        raise ProviderError("Provider stream returned invalid JSON", category="invalid_response")
                    response["id"] = event.get("id") or response.get("id")
                    choice = (event.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}
                    text = delta.get("content")
                    if text:
                        text = str(text)
                        assert_safe(text, boundary="Provider stream output")
                        content.append(text)
                        on_delta(text)
                    if choice.get("finish_reason") is not None:
                        response["choices"][0]["finish_reason"] = choice["finish_reason"]
                    if isinstance(event.get("usage"), dict):
                        usage = event["usage"]
                response.setdefault("_request_id", _response_request_id(raw_response.headers))
        except HTTPError as exc:
            raise _provider_http_error(exc) from exc
        except TimeoutError as exc:
            raise ProviderError("Provider streaming request timed out", category="provider_timeout") from exc
        except (URLError, ConnectionError) as exc:
            raise ProviderError("Provider streaming request failed", category="provider_network") from exc
        except json.JSONDecodeError as exc:
            raise ProviderError("Provider stream returned invalid JSON", category="invalid_response") from exc
        if not response.get("id") or response["choices"][0].get("finish_reason") is None:
            raise ProviderError("Provider stream ended without a completed response", category="incomplete_response")
        response["choices"][0]["message"]["content"] = "".join(content) or None
        if usage:
            response["usage"] = usage
        return response, content

    def _request_responses_stream_once(
        self, payload: dict[str, Any], timeout: int, on_delta: Callable[[str], None], *, idempotency_key: str,
    ) -> tuple[dict[str, Any], list[str]]:
        response: dict[str, Any] | None = None
        content: list[str] = []
        deadline = time.monotonic() + timeout
        try:
            with urlopen(self._request_object(payload, idempotency_key=idempotency_key), timeout=timeout) as raw_response:
                for raw in raw_response:
                    if time.monotonic() >= deadline:
                        raise ProviderError("Provider streaming request exceeded its absolute deadline", category="provider_timeout")
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    event = json.loads(data)
                    if not isinstance(event, dict):
                        raise ProviderError("Provider stream returned invalid JSON", category="invalid_response")
                    event_type = str(event.get("type") or "")
                    if event_type == "response.output_text.delta":
                        text = str(event.get("delta") or "")
                        if text:
                            assert_safe(text, boundary="Provider stream output")
                            content.append(text); on_delta(text)
                    elif event_type == "response.completed":
                        completed = event.get("response")
                        if isinstance(completed, dict): response = completed
                    elif event_type in {"response.failed", "error"}:
                        raise ProviderError("Provider stream completed with an error", category="provider_protocol")
                if response is not None:
                    response.setdefault("_request_id", _response_request_id(raw_response.headers))
        except HTTPError as exc:
            raise _provider_http_error(exc) from exc
        except TimeoutError as exc:
            raise ProviderError("Provider streaming request timed out", category="provider_timeout") from exc
        except (URLError, ConnectionError) as exc:
            raise ProviderError("Provider streaming request failed", category="provider_network") from exc
        except json.JSONDecodeError as exc:
            raise ProviderError("Provider stream returned invalid JSON", category="invalid_response") from exc
        if not response or not response.get("id") or str(response.get("status") or "completed") != "completed":
            raise ProviderError("Provider stream ended without a completed response", category="incomplete_response")
        if content and not _message_content(response): response["output_text"] = "".join(content)
        return response, content

    def _with_retries(
        self, operation: Callable[[int], Any], timeout: int,
        *, can_retry: Callable[[], bool] | None = None,
    ) -> Any:
        self._assert_circuit_closed()
        settings = self._retry_settings()
        started = time.monotonic()
        last_error: ProviderError | None = None
        for attempt in range(1, int(settings["max_attempts"]) + 1):
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                break
            try:
                result = operation(min(int(settings["per_attempt_timeout_seconds"]), max(1, int(remaining))))
                self._record_circuit_success()
                return result
            except ProviderError as exc:
                last_error = exc
                retryable = _is_retryable(exc) and attempt < int(settings["max_attempts"])
                if not retryable or (can_retry is not None and not can_retry()):
                    break
                delay = exc.retry_after if exc.retry_after is not None else random.uniform(0, float(settings["max_backoff_seconds"]))
                if delay > 0 and delay < timeout - (time.monotonic() - started):
                    time.sleep(delay)
        error = last_error or ProviderError("Provider request timed out", category="provider_timeout")
        error.retry_attempts = attempt if "attempt" in locals() else 0
        if _is_retryable(error):
            self._record_circuit_failure(settings)
        raise error

    def _retry_settings(self) -> dict[str, float | int]:
        raw = self.provider.get("retry") if isinstance(self.provider.get("retry"), dict) else {}
        return {
            "max_attempts": max(1, int(raw.get("max_attempts", 1))),
            "per_attempt_timeout_seconds": max(1, int(raw.get("per_attempt_timeout_seconds", 90))),
            "max_backoff_seconds": max(0.0, float(raw.get("max_backoff_seconds", 0))),
            "circuit_breaker_failures": max(1, int(raw.get("circuit_breaker_failures", 5))),
            "circuit_breaker_cooldown_seconds": max(1.0, float(raw.get("circuit_breaker_cooldown_seconds", 30))),
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

    def _request_object(self, payload: dict[str, Any], *, idempotency_key: str) -> Request:
        headers = self._request_headers(accept="text/event-stream" if payload.get("stream") else "application/json")
        headers["Idempotency-Key"] = idempotency_key
        return Request(
            self._responses_url() if self.transport == "responses" else self._chat_completions_url(),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )

    def _request_headers(self, *, accept: str) -> dict[str, str]:
        key = str(self.provider.get("api_key") or "").strip()
        if not key:
            raise ProviderError("Provider API key is not configured", category="provider_not_configured")
        return {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": accept,
            "User-Agent": self.user_agent,
        }


def _provider_http_error(error: HTTPError) -> ProviderError:
    return ProviderError(
        _http_error_message(error), category=_http_category(error.code),
        request_id=_response_request_id(error.headers), status=error.code,
        retry_after=_retry_after(error.headers.get("Retry-After")),
    )


def _message_content(response: dict[str, Any]) -> str:
    direct = response.get("output_text")
    if isinstance(direct, str) and direct:
        return direct
    output = response.get("output")
    if isinstance(output, list):
        parts: list[str] = []
        for item in output:
            if not isinstance(item, dict): continue
            for content in item.get("content") or []:
                if isinstance(content, dict) and content.get("type") in {"output_text", "text"}:
                    text = content.get("text")
                    if isinstance(text, str): parts.append(text)
        if parts: return "".join(parts)
    choices = response.get("choices") or []
    message = choices[0].get("message") if choices and isinstance(choices[0], dict) else {}
    return str((message or {}).get("content") or "")


def _usage(response: dict[str, Any]) -> dict[str, Any]:
    return dict(response.get("usage") or {}) if isinstance(response.get("usage"), dict) else {}


def _request_id(response: dict[str, Any]) -> str | None:
    return str(response.get("_request_id") or response.get("id") or "") or None


def _response_request_id(headers: Any) -> str | None:
    for key in ("x-request-id", "request-id", "x-amzn-requestid"):
        value = headers.get(key) if headers else None
        if value:
            return str(value)
    return None


def _retry_after(value: str | None) -> float | None:
    try:
        return max(0.0, float(value)) if value else None
    except ValueError:
        return None


def _http_category(status: int) -> str:
    if status == 404:
        return "model_not_found"
    if status == 429:
        return "provider_rate_limit"
    if 400 <= status < 500:
        return "provider_protocol"
    return "provider_server"


def _http_error_message(error: HTTPError) -> str:
    try:
        detail = error.read().decode("utf-8", errors="replace")[:500]
    except OSError:
        detail = ""
    return f"Provider HTTP {error.code}{': ' + detail if detail else ''}"


def _is_retryable(error: ProviderError) -> bool:
    return error.category in {"provider_timeout", "provider_network", "provider_rate_limit", "provider_server"}


def _schema_name(schema: Path) -> str:
    return "_".join(part for part in schema.stem.lower().replace("-", "_").split("_") if part) or "response"


def _provider_json_schema(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _provider_json_schema(item) for key, item in value.items() if key not in {"$schema", "title"}}
    if isinstance(value, list):
        return [_provider_json_schema(item) for item in value]
    return value


def current_codex_desktop_user_agent() -> str:
    version = _local_codex_version()
    if not version:
        return _FALLBACK_USER_AGENT
    machine = platform.machine().lower()
    architecture = "x86_64" if machine in {"amd64", "x86_64"} else machine or "unknown"
    return f"Codex Desktop/{version} (Windows {platform.version()}; {architecture})"


def _local_codex_version() -> str | None:
    root = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin"
    candidates = list(root.glob("*/codex.exe")) if root.is_dir() else []
    if not candidates:
        return None
    try:
        executable = max(candidates, key=lambda path: path.stat().st_mtime)
        completed = subprocess.run(
            [str(executable), "--version"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=5, check=False,
        )
        return completed.stdout.strip().split()[-1] if completed.returncode == 0 and completed.stdout.strip() else None
    except (OSError, subprocess.SubprocessError):
        return None
