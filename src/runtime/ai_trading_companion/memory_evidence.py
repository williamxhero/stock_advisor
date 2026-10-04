from __future__ import annotations

import copy
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .evidence_qualification import qualify_record
from .evidence_spec import validate
from .memory_write import write_memory
from .temporal_integrity import canonical_time
from .memory_port import MemoryPort
from .secret_guard import assert_safe


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
        spec = copy.deepcopy(evidence_spec or {})
        if spec:
            # Validate acquisition identity without rewriting the historical record.
            validate(spec)
            expected_title = spec["source"]["title"] or spec["source"]["url"] or url
            spec_occurred_at = (
                canonical_time(spec["occurred_at"]) if spec["occurred_at"] else None
            )
            receipt_occurred_at = canonical_time(occurred_at) if occurred_at else None
            if (
                spec["content"] != body
                or (spec["source"]["url"] and spec["source"]["url"] != url)
                or expected_title != title
                or spec_occurred_at != receipt_occurred_at
            ):
                raise ValueError("snapshot does not match evidence content or source")
        qualification = None
        if spec:
            qualification = qualify_record(
                spec, as_of=known_at, source_refs=(source_event_id, url),
                memory_receipt={
                    "source_event_id": source_event_id,
                    "content_hash": content_hash,
                    "known_at": known_at,
                },
            )
        receipt = write_memory(
            self.memory, "evidence", {
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
                    **({"evidence_qualification": qualification} if qualification else {}),
                },
            }, semantic_type="evidence"
        )
        return RegisteredEvidence(
            episode_id=str(receipt["episode_id"]),
            known_at=known_at,
            context={
                "memory_episode_id": receipt["episode_id"], "url": url,
                "title": title, "text": body, "known_at": known_at,
                "content_hash": content_hash,
                **({"evidence_qualification": qualification} if qualification else {}),
            },
        )
