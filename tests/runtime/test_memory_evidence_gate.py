from __future__ import annotations

import pytest

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
