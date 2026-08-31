from __future__ import annotations

import json
from threading import Event, Thread
from typing import Any
from urllib.request import Request, urlopen


class OllamaExtractor:
    def __init__(self, *, base_url: str, model: str, timeout_seconds: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.version = f"ollama:{model}/memory-derivation-v1"

    def __call__(self, text: str) -> dict[str, Any]:
        prompt = (
            "从给定交易记忆原文提取检索提示。只返回 JSON 对象，字段为 summary、entities、"
            "relations、propositions。每个 proposition 必须包含 text 和逐字出现在原文中的 span。"
            "这些输出不是事实或证据。\n\n原文：\n" + text
        )
        payload = json.dumps(
            {"model": self.model, "prompt": prompt, "format": "json", "stream": False},
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(
            self.base_url + "/api/generate", data=payload, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:
            envelope = json.loads(response.read())
        value = json.loads(envelope.get("response") or "{}")
        if not isinstance(value, dict):
            raise ValueError("Ollama derivation is not an object")
        return value


class DerivationWorker:
    def __init__(self, hub: Any, extractor: OllamaExtractor, *, interval_seconds: float = 5.0) -> None:
        self.hub = hub
        self.extractor = extractor
        self.interval_seconds = interval_seconds
        self._stop = Event()
        self._thread = Thread(target=self._run, name="memoryhub-derivation", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=max(1.0, self.interval_seconds + 1.0))

    def _run(self) -> None:
        while not self._stop.is_set():
            self.hub.derive_pending(self.extractor, extractor_version=self.extractor.version)
            self._stop.wait(self.interval_seconds)

