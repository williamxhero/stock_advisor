from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any, Callable

from .memory_port import MemoryPort
from .secret_guard import assert_safe
from .evidence_spec import VERSION, fingerprint, validate


@dataclass(frozen=True)
class RegisteredEvidence:
    episode_id: str
    known_at: str
    context: dict[str, Any]


class MemoryEvidenceRegistrar:
    """Receipt gate: no external material is returned before MemoryHub accepts it."""

    def __init__(self, memory: MemoryPort, *, clock: Callable[[], str]) -> None:
        self.memory = memory
        self.clock = clock

    def register_web_snapshot(
        self, *, memory_space_id: str, source_event_id: str, url: str,
        title: str, body: str, occurred_at: str,
        object_reference: dict[str, Any] | None = None,
        evidence_spec: dict[str, Any] | None = None,
    ) -> RegisteredEvidence:
        assert_safe(body, boundary="MemoryHub web snapshot")
        known_at = self.clock()
        content_hash = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
        spec = dict(evidence_spec or {})
        if spec:
            spec.setdefault("contract", VERSION)
            spec["known_at"] = known_at
            spec["record_id"] = fingerprint({k: v for k, v in spec.items() if k != "record_id"})
            validate(spec)
        receipt = self.memory.append(
            {
                "memory_space_id": memory_space_id,
                "source_system": "wag",
                "source_event_id": source_event_id,
                "content_hash": content_hash,
                "episode_type": "external_evidence",
                "body": body,
                "occurred_at": occurred_at,
                "known_at": known_at,
                "submitted_at": known_at,
                "authority": "mutable_source_snapshot",
                "protocol_version": "memoryhub/v1",
                "metadata": {
                    "url": url, "title": title,
                    "object_reference": object_reference,
                    **({"evidence_spec": spec} if spec else {}),
                },
            }
        )
        return RegisteredEvidence(
            episode_id=str(receipt["episode_id"]),
            known_at=known_at,
            context={
                "memory_episode_id": receipt["episode_id"], "url": url,
                "title": title, "text": body, "known_at": known_at,
                "content_hash": content_hash,
            },
        )
