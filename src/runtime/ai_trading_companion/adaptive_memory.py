"""AI-directed, snapshot-bound MemoryHub retrieval for one chat batch."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from .memory_port import MemoryPort


class MemoryResearchError(RuntimeError):
    """The chat cannot truthfully continue from its frozen memory context."""


@dataclass(frozen=True)
class MemoryResearchResult:
    snapshot: dict[str, Any]
    context: tuple[dict[str, Any], ...]
    actions: tuple[dict[str, Any], ...]


class AdaptiveMemoryResearch:
    """Execute only MemoryHub operations selected by the unified cognition loop.

    This class has no investment or stopping judgement of its own.  Its only
    guards are immutable-snapshot access, duplicate suppression, and the
    caller's deadline so an unavailable or malformed model response cannot
    turn into an unbounded service loop.
    """

    def __init__(
        self, memory: MemoryPort, memory_space_id: str,
        decide: Callable[[dict[str, Any]], dict[str, Any]], *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.memory = memory
        self.memory_space_id = memory_space_id
        self.decide = decide
        self.monotonic = monotonic

    def collect(
        self, cycle_id: str, messages: list[dict[str, Any]], *, deadline: float,
    ) -> MemoryResearchResult:
        if not messages:
            raise MemoryResearchError("memory research requires submitted messages")
        as_of = max(str(message["known_at"]) for message in messages)
        snapshot = self.memory.begin_snapshot({
            "memory_space_id": self.memory_space_id,
            "as_of": as_of,
            "stage": "chat",
            "cycle_id": cycle_id,
        })
        snapshot_id = str(snapshot.get("snapshot_id") or "")
        if not snapshot_id:
            raise MemoryResearchError("MemoryHub returned a snapshot without an id")

        context: list[dict[str, Any]] = []
        actions: list[dict[str, Any]] = []
        known_episode_ids: set[str] = set()
        executed: set[tuple[str, str]] = set()
        last_observation: dict[str, Any] | None = None
        while True:
            if self.monotonic() >= deadline:
                raise MemoryResearchError("memory research reached its response deadline")
            decision = self.decide({
                "snapshot": snapshot,
                "messages": [
                    {"message_id": item["message_id"], "text": item["body_text"], "known_at": item["known_at"]}
                    for item in messages
                ],
                "prior_actions": actions,
                "known_episode_ids": sorted(known_episode_ids),
                "last_observation": last_observation,
            })
            action = self._validated_decision(decision)
            operation = action["operation"]
            if operation == "complete":
                actions.append(action)
                return MemoryResearchResult(snapshot, tuple(context), tuple(actions))

            target = str(action["query"] if operation == "search" else action["episode_id"])
            duplicate_key = (operation, target)
            if duplicate_key in executed:
                last_observation = {
                    "operation": operation,
                    "state": "rejected_duplicate",
                    "detail": "This exact MemoryHub operation already ran in this frozen snapshot; choose another operation or complete.",
                }
                actions.append({**action, "state": "rejected_duplicate"})
                continue
            if operation in {"expand", "related"} and target not in known_episode_ids:
                last_observation = {
                    "operation": operation,
                    "state": "rejected_unknown_episode",
                    "detail": "episode_id must be returned by an earlier search, expand, or related result in this snapshot.",
                }
                actions.append({**action, "state": "rejected_unknown_episode"})
                continue

            executed.add(duplicate_key)
            if operation == "search":
                result: Any = self.memory.search(snapshot_id, target, limit=20)
            elif operation == "expand":
                result = self.memory.expand(snapshot_id, target)
            else:
                result = self.memory.related(snapshot_id, target, limit=20)
            cards = result if isinstance(result, list) else [result]
            normalized = [self._context_item(operation, item) for item in cards if isinstance(item, dict)]
            for item in normalized:
                episode_id = item.get("episode_id")
                if isinstance(episode_id, str) and episode_id:
                    known_episode_ids.add(episode_id)
                if item not in context:
                    context.append(item)
            actions.append({**action, "state": "completed", "result_count": len(normalized)})
            last_observation = {"operation": operation, "state": "completed", "items": normalized}

    @staticmethod
    def _validated_decision(value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise MemoryResearchError("memory research decision is not an object")
        operation = str(value.get("operation") or "")
        if operation not in {"search", "expand", "related", "complete"}:
            raise MemoryResearchError("memory research selected an unsupported operation")
        query = value.get("query")
        episode_id = value.get("episode_id")
        if operation == "search" and not isinstance(query, str):
            raise MemoryResearchError("memory search requires a query")
        if operation == "search" and not query.strip():
            raise MemoryResearchError("memory search query is empty")
        if operation in {"expand", "related"} and (not isinstance(episode_id, str) or not episode_id.strip()):
            raise MemoryResearchError(f"memory {operation} requires an episode_id")
        return {
            "operation": operation,
            "query": query.strip() if isinstance(query, str) else None,
            "episode_id": episode_id.strip() if isinstance(episode_id, str) else None,
        }

    @staticmethod
    def _context_item(operation: str, item: dict[str, Any]) -> dict[str, Any]:
        # Keep the service-provided reference and timestamps intact.  MemoryHub
        # owns policy filtering and immutable content verification.
        return {"retrieved_by": operation, **item}
