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
    def begin_snapshot(self, request: dict[str, Any]) -> dict[str, Any]: ...
    def search(self, snapshot_id: str, query: str, *, limit: int = 20) -> list[dict[str, Any]]: ...
    def expand(self, snapshot_id: str, episode_id: str) -> dict[str, Any]: ...
    def related(self, snapshot_id: str, episode_id: str, *, limit: int = 20) -> list[dict[str, Any]]: ...
    def timeline(self, memory_space_id: str, *, after_sequence: int = 0) -> list[dict[str, Any]]: ...
    def health(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class HttpMemoryAdapter:
    base_url: str
    timeout_seconds: float = 10.0
    opener: Callable[..., Any] = urlopen

    def append(self, episode: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/episodes", episode)["result"]

    def begin_snapshot(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/snapshots", request)["result"]

    def search(self, snapshot_id: str, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        return self._request("POST", f"/v1/snapshots/{snapshot_id}/search", {"query": query, "limit": limit})["result"]

    def expand(self, snapshot_id: str, episode_id: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/snapshots/{snapshot_id}/expand", {"episode_id": episode_id})["result"]

    def related(self, snapshot_id: str, episode_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        return self._request("POST", f"/v1/snapshots/{snapshot_id}/related", {"episode_id": episode_id, "limit": limit})["result"]

    def timeline(self, memory_space_id: str, *, after_sequence: int = 0) -> list[dict[str, Any]]:
        return self._request(
            "POST", f"/v1/memory-spaces/{memory_space_id}/timeline",
            {"after_sequence": after_sequence},
        )["result"]

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
        self._episodes: list[dict[str, Any]] = []
        self._snapshots: dict[str, dict[str, Any]] = {}

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
        self._episodes.append({**episode, **receipt})
        return dict(receipt)

    def begin_snapshot(self, request: dict[str, Any]) -> dict[str, Any]:
        snapshot_id = f"test-snapshot-{len(self._snapshots) + 1}"
        value = {**request, "snapshot_id": snapshot_id, "watermark": len(self._episodes), "policy_version": "memory-policy/v1", "protocol_version": "memoryhub/v1"}
        self._snapshots[snapshot_id] = value
        return dict(value)

    def search(self, snapshot_id: str, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        snapshot = self._snapshots[snapshot_id]
        return [
            {"episode_id": item["episode_id"], "summary": item.get("body", ""), "known_at": item["known_at"]}
            for item in self._episodes[: snapshot["watermark"]]
            if item["memory_space_id"] == snapshot["memory_space_id"]
            and item["known_at"] <= snapshot["as_of"]
            and query.casefold() in item.get("body", "").casefold()
        ][:limit]

    def expand(self, snapshot_id: str, episode_id: str) -> dict[str, Any]:
        visible = {item["episode_id"] for item in self.search(snapshot_id, "", limit=100)}
        if episode_id not in visible:
            raise MemoryUnavailable("episode is not visible in snapshot")
        return dict(next(item for item in self._episodes if item["episode_id"] == episode_id))

    def related(self, snapshot_id: str, episode_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        return [item for item in self.search(snapshot_id, "", limit=100) if item.get("corrects_episode_id") == episode_id][:limit]

    def timeline(self, memory_space_id: str, *, after_sequence: int = 0) -> list[dict[str, Any]]:
        return [
            dict(item) for item in self._episodes
            if item["memory_space_id"] == memory_space_id
            and item["sequence"] > after_sequence
            and item["episode_type"] in {"user_message", "ai_message"}
        ]

    def health(self) -> dict[str, Any]:
        return {"protocol_version": "memoryhub/v1", "ledger": {"state": "ready", "episodes": len(self._receipts)}}
