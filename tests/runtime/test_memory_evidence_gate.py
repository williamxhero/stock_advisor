from __future__ import annotations

import hashlib
import pytest

from ai_trading_companion.__main__ import _research_memory_registrar
from ai_trading_companion.memory_evidence import MemoryEvidenceRegistrar
from ai_trading_companion.memory_port import InMemoryMemoryAdapter, MemoryUnavailable


def test_external_material_only_becomes_context_after_memoryhub_receipt() -> None:
    registrar = MemoryEvidenceRegistrar(InMemoryMemoryAdapter(), clock=lambda: "2026-08-31T01:00:00Z")

    registered = registrar.register_web_snapshot(
        memory_space_id="acceptance", source_event_id="wag-read-1",
        url="https://example.test/old", title="旧文章", body="机器人风险",
        occurred_at="2026-08-01T00:00:00Z",
    )

    assert registered.known_at == "2026-08-31T01:00:00Z"
    assert registered.context["text"] == "机器人风险"
    assert registered.context["memory_episode_id"].startswith("test-episode-")
    assert registered.context["content_hash"] == "sha256:" + hashlib.sha256("机器人风险".encode()).hexdigest()


def test_memoryhub_failure_closes_the_context_path() -> None:
    class FailingPort(InMemoryMemoryAdapter):
        def append(self, episode: dict[str, object]) -> dict[str, object]:
            raise MemoryUnavailable("ledger unavailable")

    registrar = MemoryEvidenceRegistrar(FailingPort(), clock=lambda: "2026-08-31T01:00:00Z")

    with pytest.raises(MemoryUnavailable):
        registrar.register_web_snapshot(
            memory_space_id="acceptance", source_event_id="wag-read-1",
            url="https://example.test/old", title="旧文章", body="机器人风险",
            occurred_at="2026-08-01T00:00:00Z",
        )


def test_formal_research_observation_receives_memoryhub_receipt_before_use() -> None:
    memory = InMemoryMemoryAdapter()
    observation = {
        "attempt_id": "attempt-1",
        "observation_id": "observation-1",
        "acquired_at": "2026-08-31T01:00:00Z",
        "evidence_items": [{
            "evidence_ref": "ev-1",
            "url": "https://example.test/close",
            "title": "收盘",
            "excerpt_text": "可验证收盘事实",
            "fact_as_of": "2026-08-29T07:00:00Z",
            "published_at": "2026-08-29T07:01:00Z",
        }],
    }

    _research_memory_registrar(memory, "acceptance", {"cycle_id": "cycle-1"})(observation)

    item = observation["evidence_items"][0]
    assert item["memory_episode_id"].startswith("test-episode-")
    assert item["known_at"]
    assert item["memory_content_hash"] == "sha256:" + hashlib.sha256("可验证收盘事实".encode()).hexdigest()
    episode = memory.export_space("acceptance")["episodes"][0]
    assert episode["occurred_at"] == "2026-08-29T07:01:00Z"
    assert episode["metadata"]["object_reference"]["evidence_ref"] == "ev-1"
