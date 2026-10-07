from __future__ import annotations

import copy
from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
import uuid

from .memory_retrieval import MemoryIsolationError, freeze_retrieval, qualify_episode, rank_bundle
from .temporal_integrity import canonical_time, timestamp


class MemoryUnavailable(RuntimeError):
    pass


class MemoryNotVisible(MemoryUnavailable, MemoryIsolationError):
    """An episode cannot be read within the requested frozen snapshot."""


class MemoryPort(Protocol):
    def append(self, episode: dict[str, Any]) -> dict[str, Any]: ...
    def append_batch(self, episodes: list[dict[str, Any]]) -> list[dict[str, Any]]: ...
    def begin_snapshot(self, request: dict[str, Any]) -> dict[str, Any]: ...
    def search(self, snapshot_id: str, query: str, *, limit: int = 20) -> list[dict[str, Any]]: ...
    def retrieve_bundle(self, snapshot_id: str, query: str, *, limit: int = 20, context: dict[str, Any] | None = None) -> dict[str, Any]: ...
    def expand(self, snapshot_id: str, episode_id: str) -> dict[str, Any]: ...
    def related(self, snapshot_id: str, episode_id: str, *, limit: int = 20) -> list[dict[str, Any]]: ...
    def timeline(self, memory_space_id: str, *, after_sequence: int = 0) -> list[dict[str, Any]]: ...
    def export_space(self, memory_space_id: str) -> dict[str, Any]: ...
    def prepare_clear(self, memory_space_id: str, export_sha256: str) -> dict[str, Any]: ...
    def clear_space(self, memory_space_id: str, confirmation_token: str) -> dict[str, Any]: ...
    def health(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class HttpMemoryAdapter:
    base_url: str
    timeout_seconds: float = 10.0
    opener: Callable[..., Any] = urlopen
    _snapshots: dict[str, dict[str, Any]] = field(default_factory=dict, init=False, repr=False, compare=False)

    def append(self, episode: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/episodes", episode)["result"]

    def append_batch(self, episodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return self._request("POST", "/v1/episodes/batch", {"episodes": episodes})["result"]

    def begin_snapshot(self, request: dict[str, Any]) -> dict[str, Any]:
        snapshot = self._request("POST", "/v1/snapshots", request)["result"]
        self._snapshots[snapshot["snapshot_id"]] = copy.deepcopy(snapshot)
        return snapshot

    def search(self, snapshot_id: str, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        return self.retrieve_bundle(snapshot_id, query, limit=limit)["results"]

    def retrieve_bundle(self, snapshot_id: str, query: str, *, limit: int = 20,
                        context: dict[str, Any] | None = None) -> dict[str, Any]:
        bundle = self._retrieval_bundle(snapshot_id, query)
        originals: dict[str, dict[str, Any]] = {}

        def resolve(episode_id: str) -> dict[str, Any]:
            if episode_id not in originals:
                originals[episode_id] = self._expand_original(snapshot_id, episode_id)
            return originals[episode_id]

        return rank_bundle(bundle, resolve, limit=max(1, min(limit, 100)), context=context)

    def _retrieval_bundle(self, snapshot_id: str, query: str) -> dict[str, Any]:
        bundle = self._request("POST", f"/v1/snapshots/{snapshot_id}/retrieve", {"query": query, "limit": 100})["result"]
        self._remember_snapshot(snapshot_id, bundle["snapshot"])
        if bundle.get("query") != query:
            raise MemoryUnavailable("MemoryHub retrieval query changed")
        return bundle

    def freeze_retrieval(self, snapshot_id: str, query: str, *, limit: int = 20,
                         context: dict[str, Any] | None = None) -> dict[str, Any]:
        return freeze_retrieval(self._retrieval_bundle(snapshot_id, query),
                                lambda parent: self._expand_original(snapshot_id, parent),
                                limit=max(1, min(limit, 100)), context=context)

    def _remember_snapshot(self, snapshot_id: str, snapshot: dict[str, Any]) -> None:
        if snapshot.get("snapshot_id") != snapshot_id or (
            snapshot_id in self._snapshots and self._snapshots[snapshot_id] != snapshot
        ):
            raise MemoryUnavailable("MemoryHub snapshot identity changed")
        self._snapshots[snapshot_id] = copy.deepcopy(snapshot)

    def _snapshot(self, snapshot_id: str) -> dict[str, Any]:
        if snapshot_id not in self._snapshots:
            bundle = self._request("POST", f"/v1/snapshots/{snapshot_id}/retrieve", {"query": "", "limit": 1})["result"]
            self._remember_snapshot(snapshot_id, bundle["snapshot"])
        return self._snapshots[snapshot_id]

    def _expand_original(self, snapshot_id: str, episode_id: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/snapshots/{snapshot_id}/expand", {"episode_id": episode_id})["result"]

    def expand(self, snapshot_id: str, episode_id: str) -> dict[str, Any]:
        original = self._expand_original(snapshot_id, episode_id)
        qualify_episode(original, self._snapshot(snapshot_id), lambda parent: self._expand_original(snapshot_id, parent))
        return original

    def related(self, snapshot_id: str, episode_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        self.expand(snapshot_id, episode_id)
        cards = self._request("POST", f"/v1/snapshots/{snapshot_id}/related", {"episode_id": episode_id, "limit": 100})["result"]
        bundle = {
            "snapshot": self._snapshot(snapshot_id), "query": "", "results": cards,
            "bundle_id": f"related:{snapshot_id}:{episode_id}", "audit_id": None,
            "versions": {"protocol": "memoryhub/v1"},
        }
        return rank_bundle(bundle, lambda parent: self._expand_original(snapshot_id, parent), limit=max(1, min(limit, 100)))["results"]

    def timeline(self, memory_space_id: str, *, after_sequence: int = 0) -> list[dict[str, Any]]:
        return self._request(
            "POST", f"/v1/memory-spaces/{quote(memory_space_id, safe='')}/timeline",
            {"after_sequence": after_sequence},
        )["result"]

    def export_space(self, memory_space_id: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/memory-spaces/{quote(memory_space_id, safe='')}/export", {})["result"]

    def prepare_clear(self, memory_space_id: str, export_sha256: str) -> dict[str, Any]:
        return self._request(
            "POST", f"/v1/memory-spaces/{quote(memory_space_id, safe='')}/clear/prepare",
            {"export_sha256": export_sha256},
        )["result"]

    def clear_space(self, memory_space_id: str, confirmation_token: str) -> dict[str, Any]:
        return self._request(
            "POST", f"/v1/memory-spaces/{quote(memory_space_id, safe='')}/clear",
            {"confirmation_token": confirmation_token},
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
            error_type = MemoryNotVisible if error.code == 400 and "episode is not visible in snapshot" in detail else MemoryUnavailable
            raise error_type(f"MemoryHub rejected {path}: HTTP {error.code}: {detail}") from error
        except (URLError, TimeoutError, OSError) as error:
            raise MemoryUnavailable(f"MemoryHub unavailable at {self.base_url}: {error}") from error


class InMemoryMemoryAdapter:
    """Controllable contract adapter for tests; never a production fallback."""

    def __init__(self) -> None:
        self._receipts: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._episodes: list[dict[str, Any]] = []
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._clear_requests: dict[str, dict[str, Any]] = {}

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
        stored = copy.deepcopy({**episode, **receipt})
        for field_name in ("occurred_at", "known_at", "submitted_at"):
            value = str(stored[field_name])
            stored[field_name] = canonical_time(value + "T00:00:00Z" if len(value) == 10 else value)
        self._receipts[key] = receipt
        self._episodes.append(stored)
        return dict(receipt)

    def append_batch(self, episodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results = []
        for episode in episodes:
            try:
                results.append({"receipt": self.append(episode)})
            except Exception as error:
                results.append({"error": type(error).__name__, "detail": str(error)})
        return results

    def begin_snapshot(self, request: dict[str, Any]) -> dict[str, Any]:
        snapshot_id = f"test-snapshot-{len(self._snapshots) + 1}"
        value = {**request, "snapshot_id": snapshot_id, "watermark": len(self._episodes), "policy_version": "memory-policy/v1", "protocol_version": "memoryhub/v1"}
        self._snapshots[snapshot_id] = copy.deepcopy(value)
        return copy.deepcopy(value)

    def search(self, snapshot_id: str, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        snapshot = self._snapshots[snapshot_id]
        cards = []
        for item in self._episodes[:snapshot["watermark"]]:
            if item["memory_space_id"] != snapshot["memory_space_id"] or timestamp(item["known_at"]) > timestamp(snapshot["as_of"]):
                continue
            if query and not any(term in item.get("body", "").casefold() for term in query.casefold().split()):
                continue
            if snapshot["stage"] in {"m1_research", "m1_judgment"}:
                try:
                    qualify_episode(item, snapshot, lambda parent: self._expand_original(snapshot_id, parent))
                except (ValueError, KeyError, TypeError):
                    continue
            cards.append({**copy.deepcopy(item), "summary": item.get("body", "")[:500]})
        return cards[:limit]

    def _retrieval_bundle(self, snapshot_id: str, query: str) -> dict[str, Any]:
        snapshot = copy.deepcopy(self._snapshots[snapshot_id])
        return {
            "bundle_id": f"test-bundle-{snapshot_id}", "audit_id": f"test-audit-{snapshot_id}",
            "snapshot": snapshot,
            "versions": {"policy": snapshot["policy_version"], "retriever": "test/v1", "index": "test/v1", "extractor": "test/v1", "protocol": snapshot["protocol_version"]},
            "query": query, "results": self.search(snapshot_id, query, limit=100),
        }

    def retrieve_bundle(self, snapshot_id: str, query: str, *, limit: int = 20,
                        context: dict[str, Any] | None = None) -> dict[str, Any]:
        return rank_bundle(self._retrieval_bundle(snapshot_id, query),
                           lambda parent: self._expand_original(snapshot_id, parent),
                           limit=max(1, min(limit, 100)), context=context)

    def freeze_retrieval(self, snapshot_id: str, query: str, *, limit: int = 20,
                         context: dict[str, Any] | None = None) -> dict[str, Any]:
        return freeze_retrieval(self._retrieval_bundle(snapshot_id, query),
                                lambda parent: self._expand_original(snapshot_id, parent),
                                limit=max(1, min(limit, 100)), context=context)

    def _expand_original(self, snapshot_id: str, episode_id: str) -> dict[str, Any]:
        snapshot = self._snapshots[snapshot_id]
        for item in self._episodes[:snapshot["watermark"]]:
            if item["episode_id"] == episode_id and item["memory_space_id"] == snapshot["memory_space_id"] and timestamp(item["known_at"]) <= timestamp(snapshot["as_of"]):
                return copy.deepcopy(item)
        raise MemoryNotVisible("episode is not visible in snapshot")

    def expand(self, snapshot_id: str, episode_id: str) -> dict[str, Any]:
        original = self._expand_original(snapshot_id, episode_id)
        qualify_episode(original, self._snapshots[snapshot_id], lambda parent: self._expand_original(snapshot_id, parent))
        return original

    def related(self, snapshot_id: str, episode_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        target = self._expand_original(snapshot_id, episode_id)
        qualify_episode(target, self._snapshots[snapshot_id], lambda parent: self._expand_original(snapshot_id, parent))
        target_links = target.get("metadata", {}).get("related_episode_ids", [])
        return [item for item in self.retrieve_bundle(snapshot_id, "", limit=100)["results"] if (
            item.get("corrects_episode_id") == episode_id or target.get("corrects_episode_id") == item["episode_id"]
            or item["episode_id"] in target_links or episode_id in item.get("metadata", {}).get("related_episode_ids", [])
        )][:limit]

    def timeline(self, memory_space_id: str, *, after_sequence: int = 0) -> list[dict[str, Any]]:
        return [
            dict(item) for item in self._episodes
            if item["memory_space_id"] == memory_space_id
            and item["sequence"] > after_sequence
            and item["episode_type"] in {"user_message", "ai_message"}
        ]

    def export_space(self, memory_space_id: str) -> dict[str, Any]:
        episodes = [dict(item) for item in self._episodes if item["memory_space_id"] == memory_space_id]
        machine = {"memory_space_id": memory_space_id, "protocol_version": "memoryhub/v1", "episodes": episodes}
        canonical = json.dumps(machine, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return {
            **machine, "human_markdown": "\n".join(str(item.get("body") or "") for item in episodes),
            "export_sha256": "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        }

    def prepare_clear(self, memory_space_id: str, export_sha256: str) -> dict[str, Any]:
        if self.export_space(memory_space_id)["export_sha256"] != export_sha256:
            raise MemoryUnavailable("clear requires a fresh successful export")
        token = str(uuid.uuid4())
        value = {"confirmation_token": token, "memory_space_id": memory_space_id, "export_sha256": export_sha256}
        self._clear_requests[token] = value
        return {**value, "state": "confirmation_required"}

    def clear_space(self, memory_space_id: str, confirmation_token: str) -> dict[str, Any]:
        request = self._clear_requests.get(confirmation_token)
        if not request or request["memory_space_id"] != memory_space_id:
            raise MemoryUnavailable("clear confirmation is invalid")
        prior = request.get("result")
        if prior:
            return dict(prior)
        before = len(self._episodes)
        self._episodes = [item for item in self._episodes if item["memory_space_id"] != memory_space_id]
        self._receipts = {
            key: receipt for key, receipt in self._receipts.items() if key[0] != memory_space_id
        }
        result = {"memory_space_id": memory_space_id, "state": "cleared", "deleted_episodes": before - len(self._episodes), "export_sha256": request["export_sha256"]}
        request["result"] = result
        return dict(result)

    def health(self) -> dict[str, Any]:
        return {"protocol_version": "memoryhub/v1", "ledger": {"state": "ready", "episodes": len(self._receipts)}}
