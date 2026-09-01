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
    bundles: tuple[dict[str, Any], ...] = ()


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
        discover_external: Callable[[dict[str, Any], dict[str, Any]], list[dict[str, Any]]] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.memory = memory
        self.memory_space_id = memory_space_id
        self.decide = decide
        self.discover_external = discover_external
        self.monotonic = monotonic

    def collect(
        self, cycle_id: str, messages: list[dict[str, Any]], *, deadline: float, stage: str = "chat",
        resume: dict[str, Any] | None = None,
        on_checkpoint: Callable[[dict[str, Any]], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> MemoryResearchResult:
        if not messages:
            raise MemoryResearchError("memory research requires submitted messages")
        restored = self._restore(resume, cycle_id, stage)
        as_of = max(str(message["known_at"]) for message in messages)
        snapshot = restored["snapshot"] if restored else self.memory.begin_snapshot({
            "memory_space_id": self.memory_space_id, "as_of": as_of,
            "stage": stage, "cycle_id": cycle_id,
        })
        snapshot_id = str(snapshot.get("snapshot_id") or "")
        if not snapshot_id:
            raise MemoryResearchError("MemoryHub returned a snapshot without an id")
        context: list[dict[str, Any]] = list(restored["context"]) if restored else []
        actions: list[dict[str, Any]] = list(restored["actions"]) if restored else []
        bundles: list[dict[str, Any]] = list(restored.get("bundles", [])) if restored else []
        known_episode_ids: set[str] = set(restored["known_episode_ids"]) if restored else set()
        executed: set[tuple[str, str]] = set(restored["executed"]) if restored else set()
        last_observation: dict[str, Any] | None = restored["last_observation"] if restored else None

        def checkpoint() -> None:
            if on_checkpoint:
                on_checkpoint({
                    "cycle_id": cycle_id, "stage": stage, "snapshot": snapshot,
                    "context": context, "actions": actions,
                    "bundles": bundles,
                    "known_episode_ids": sorted(known_episode_ids),
                    "executed": [list(item) for item in sorted(executed)],
                    "last_observation": last_observation,
                })

        checkpoint()
        if actions and actions[-1].get("operation") == "complete":
            return MemoryResearchResult(snapshot, tuple(context), tuple(actions), tuple(bundles))
        while True:
            if self.monotonic() >= deadline:
                raise MemoryResearchError("memory research reached its response deadline")
            if cancelled and cancelled():
                checkpoint()
                raise MemoryResearchError("memory research was terminated by the user")
            decision = self.decide({
                "snapshot": snapshot,
                "messages": [{"message_id": item["message_id"], "text": item["body_text"], "known_at": item["known_at"]} for item in messages],
                "prior_actions": actions, "known_episode_ids": sorted(known_episode_ids), "last_observation": last_observation,
            })
            action = self._validated_decision(decision)
            operation = action["operation"]
            if operation == "complete":
                actions.append(action)
                checkpoint()
                return MemoryResearchResult(snapshot, tuple(context), tuple(actions), tuple(bundles))
            if operation in {"web_search", "web_read", "markethub_quote", "archive_article"}:
                if self.discover_external is None:
                    raise MemoryResearchError("external discovery is unavailable for this chat")
                rows = self.discover_external(action, snapshot)
                normalized = [self._context_item(operation, item) for item in rows if isinstance(item, dict)]
                context.extend(item for item in normalized if item not in context)
                actions.append({**action, "state": "completed", "result_count": len(normalized)})
                last_observation = {"operation": operation, "state": "completed", "items": normalized}
                checkpoint()
                continue
            target = str(action["query"] if operation == "search" else action["episode_id"])
            duplicate_key = (operation, target)
            if duplicate_key in executed:
                last_observation = {"operation": operation, "state": "rejected_duplicate", "detail": "This exact MemoryHub operation already ran in this frozen snapshot; choose another operation or complete."}
                actions.append({**action, "state": "rejected_duplicate"})
                checkpoint()
                continue
            if operation in {"expand", "related"} and target not in known_episode_ids:
                last_observation = {"operation": operation, "state": "rejected_unknown_episode", "detail": "episode_id must be returned by an earlier search, expand, or related result in this snapshot."}
                actions.append({**action, "state": "rejected_unknown_episode"})
                checkpoint()
                continue
            executed.add(duplicate_key)
            if operation == "search":
                bundle = self.memory.retrieve_bundle(snapshot_id, target, limit=20)
                bundles.append(bundle)
                result: Any = bundle.get("results", [])
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
            bundle_metadata = ({"bundle_id": bundle.get("bundle_id"), "versions": bundle.get("versions")} if operation == "search" else {})
            actions.append({**action, **bundle_metadata, "state": "completed", "result_count": len(normalized)})
            last_observation = {"operation": operation, "state": "completed", "items": normalized}
            checkpoint()

    @staticmethod
    def _restore(resume: dict[str, Any] | None, cycle_id: str, stage: str) -> dict[str, Any] | None:
        if not isinstance(resume, dict) or resume.get("cycle_id") != cycle_id or resume.get("stage") != stage:
            return None
        snapshot = resume.get("snapshot")
        context, actions = resume.get("context"), resume.get("actions")
        known, executed = resume.get("known_episode_ids"), resume.get("executed")
        if not isinstance(snapshot, dict) or not snapshot.get("snapshot_id") or not all(isinstance(value, list) for value in (context, actions, known, executed)):
            return None
        return {
            "snapshot": snapshot,
            "context": [value for value in context if isinstance(value, dict)],
            "actions": [value for value in actions if isinstance(value, dict)],
            "bundles": [value for value in resume.get("bundles", []) if isinstance(value, dict)] if isinstance(resume.get("bundles", []), list) else [],
            "known_episode_ids": [str(value) for value in known if isinstance(value, str)],
            "executed": {(str(value[0]), str(value[1])) for value in executed if isinstance(value, list) and len(value) == 2},
            "last_observation": resume.get("last_observation") if isinstance(resume.get("last_observation"), dict) else None,
        }

    @staticmethod
    def _validated_decision(value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise MemoryResearchError("memory research decision is not an object")
        operation = str(value.get("operation") or "")
        if operation not in {"search", "expand", "related", "web_search", "web_read", "markethub_quote", "archive_article", "complete"}:
            raise MemoryResearchError("memory research selected an unsupported operation")
        query = value.get("query")
        episode_id = value.get("episode_id")
        if operation == "search" and not isinstance(query, str):
            raise MemoryResearchError("memory search requires a query")
        if operation == "search" and not query.strip():
            raise MemoryResearchError("memory search query is empty")
        if operation in {"expand", "related"} and (not isinstance(episode_id, str) or not episode_id.strip()):
            raise MemoryResearchError(f"memory {operation} requires an episode_id")
        url = value.get("url")
        if operation == "web_search" and (not isinstance(query, str) or not query.strip()):
            raise MemoryResearchError("web search requires a query")
        if operation == "web_read" and (not isinstance(url, str) or not url.startswith(("http://", "https://"))):
            raise MemoryResearchError("web read requires an http(s) url")
        source_reference = value.get("source_reference")
        if operation == "markethub_quote" and (not isinstance(source_reference, dict) or source_reference.get("source_system") != "markethub"):
            raise MemoryResearchError("MarketHub discovery requires a markethub source_reference")
        if operation == "archive_article" and (not isinstance(source_reference, dict) or source_reference.get("source_system") != "8815"):
            raise MemoryResearchError("8815 discovery requires an 8815 source_reference")
        return {
            "operation": operation,
            "query": query.strip() if isinstance(query, str) else None,
            "episode_id": episode_id.strip() if isinstance(episode_id, str) else None,
            "url": url.strip() if isinstance(url, str) else None,
            "source_reference": source_reference if isinstance(source_reference, dict) else None,
        }

    @staticmethod
    def _context_item(operation: str, item: dict[str, Any]) -> dict[str, Any]:
        # Keep the service-provided reference and timestamps intact.  MemoryHub
        # owns policy filtering and immutable content verification.
        return {"retrieved_by": operation, **item}
