from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class MemoryUnavailable(RuntimeError):
    pass


class MemoryPort(Protocol):
    def append(self, episode: dict[str, Any]) -> dict[str, Any]: ...
    def health(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class HttpMemoryAdapter:
    base_url: str
    timeout_seconds: float = 10.0
    opener: Callable[..., Any] = urlopen

    def append(self, episode: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/episodes", episode)

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def _request(self, method: str, path: str, value: dict[str, Any] | None = None) -> dict[str, Any]:
        request = Request(
            self.base_url.rstrip("/") + path,
            data=None if value is None else json.dumps(value, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read())
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise MemoryUnavailable(f"MemoryHub rejected {path}: HTTP {error.code}: {detail}") from error
        except (URLError, TimeoutError, OSError) as error:
            raise MemoryUnavailable(f"MemoryHub unavailable at {self.base_url}: {error}") from error


class InMemoryMemoryAdapter:
    """Controllable contract adapter for tests; never a production fallback."""

    def __init__(self) -> None:
        self._receipts: dict[tuple[str, str, str], dict[str, Any]] = {}

    def append(self, episode: dict[str, Any]) -> dict[str, Any]:
        key = (episode["memory_space_id"], episode["source_system"], episode["source_event_id"])
        existing = self._receipts.get(key)
        if existing:
            if existing["content_hash"] != episode["content_hash"]:
                raise MemoryUnavailable("immutable conflict")
            return dict(existing)
        receipt = {
            "episode_id": f"test-episode-{len(self._receipts) + 1}",
            "sequence": len(self._receipts) + 1,
            "content_hash": episode["content_hash"],
            "protocol_version": "memoryhub/v1",
        }
        self._receipts[key] = receipt
        return dict(receipt)

    def health(self) -> dict[str, Any]:
        return {"protocol_version": "memoryhub/v1", "ledger": {"state": "ready", "episodes": len(self._receipts)}}
