from __future__ import annotations

from pathlib import Path

import pytest

from trading_memory_hub import MemoryHub, MemoryHubError, SourceIntegrityError


def episode(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "memory_space_id": "partner-main",
        "source_system": "stock-advisor",
        "source_event_id": "memory-type-1",
        "content_hash": "auto",
        "episode_type": "personal_fact",
        "body": "用户重视长期复利。",
        "occurred_at": "2026-09-20T01:00:00Z",
        "known_at": "2026-09-20T01:01:00Z",
        "submitted_at": "2026-09-20T01:02:00Z",
        "authority": "user_private_fact",
        "protocol_version": "memoryhub/v1",
    }
    value.update(overrides)
    return value


def typed(value: dict[str, object], semantic_type: str, *, correction_of: str | None = None) -> dict[str, object]:
    metadata = dict(value.get("metadata") or {})
    metadata["memory_type"] = {
        "contract": "MemoryTypeSpec/v1",
        "version": 1,
        "semantic_type": semantic_type,
        "source": {
            "source_system": value["source_system"],
            "source_event_id": value["source_event_id"],
        },
        "temporal": {
            "occurred_at": value["occurred_at"],
            "known_at": value["known_at"],
            "submitted_at": value["submitted_at"],
        },
        "authority": value["authority"],
        "correction_of": correction_of,
        "provenance": {"episode_type": value["episode_type"]},
    }
    return {**value, "metadata": metadata, **({"corrects_episode_id": correction_of} if correction_of else {})}


def test_typed_interface_round_trips_authority_and_projection_boundaries(tmp_path: Path) -> None:
    hub = MemoryHub(tmp_path / "ledger.sqlite3")
    first = hub.append(typed(episode(), "user_fact"))

    exported = hub.export_space("partner-main")
    record = exported["episodes"][0]
    assert record["metadata"]["memory_type"]["semantic_type"] == "user_fact"
    assert record["metadata"]["memory_type"]["authority"] == "user_private_fact"
    assert record["metadata"]["memory_type"]["temporal"]["known_at"] == record["known_at"]

    def extract(_: str) -> dict[str, object]:
        return {"summary": "派生摘要", "propositions": []}

    assert hub.derive_pending(extract, extractor_version="memory-type-test/v1") == 1
    assert "memory_type" not in hub.derived_memory(first.episode_id)


def test_typed_correction_is_append_only_and_must_match_target(tmp_path: Path) -> None:
    hub = MemoryHub(tmp_path / "ledger.sqlite3")
    original = hub.append(typed(episode(), "user_fact"))
    correction = episode(
        source_event_id="memory-type-correction",
        episode_type="correction",
        body="更正后的用户事实。",
    )
    correction_receipt = hub.append(typed(correction, "correction", correction_of=original.episode_id))

    assert correction_receipt.episode_id != original.episode_id
    assert hub.expand(hub.begin_snapshot("partner-main", as_of="2026-09-20T02:00:00Z", stage="chat").snapshot_id, original.episode_id)["body"] == episode()["body"]
    assert hub.export_space("partner-main")["episodes"][1]["metadata"]["memory_type"]["correction_of"] == original.episode_id

    tampered = typed(episode(source_event_id="tampered"), "correction", correction_of="missing")
    with pytest.raises(MemoryHubError, match="corrected episode"):
        hub.append(tampered)


def test_malformed_typed_envelope_is_rejected_but_legacy_records_still_work(tmp_path: Path) -> None:
    hub = MemoryHub(tmp_path / "ledger.sqlite3")
    malformed = typed(episode(), "user_fact")
    malformed["metadata"]["memory_type"]["temporal"]["known_at"] = "2026-09-20T09:00:00Z"
    with pytest.raises(SourceIntegrityError, match="known_at"):
        hub.append(malformed)

    legacy = hub.append(episode(source_event_id="legacy-memory-type", metadata={"legacy": True}))
    assert legacy.episode_id
