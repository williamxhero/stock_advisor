from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from trading_memory_hub import MemoryHub, SecretRejected


def episode(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "memory_space_id": "partner-main", "source_system": "stock-advisor",
        "source_event_id": "ledger-1", "content_hash": "auto",
        "episode_type": "user_message", "body": "正式历史",
        "occurred_at": "2026-09-20T01:00:00Z", "known_at": "2026-09-20T01:00:00Z",
        "submitted_at": "2026-09-20T01:00:00Z", "authority": "user_private_fact",
        "protocol_version": "memoryhub/v1",
    }
    value.update(overrides)
    return value


def test_a_single_historical_episode_cannot_be_updated_or_deleted(tmp_path: Path) -> None:
    hub = MemoryHub(tmp_path / "ledger.sqlite3")
    receipt = hub.append(episode())
    with sqlite3.connect(hub.database) as connection:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute("UPDATE episode SET body='改写' WHERE episode_id=?", (receipt.episode_id,))
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute("DELETE FROM episode WHERE episode_id=?", (receipt.episode_id,))
    assert hub.export_space("partner-main")["episodes"][0]["body"] == "正式历史"


def test_confirmed_whole_space_clear_still_works(tmp_path: Path) -> None:
    hub = MemoryHub(tmp_path / "ledger.sqlite3")
    hub.append(episode())
    prepared = hub.prepare_clear("partner-main", hub.export_space("partner-main")["export_sha256"])
    result = hub.clear_space("partner-main", prepared["confirmation_token"])
    assert result["state"] == "cleared" and result["deleted_episodes"] == 1


def test_existing_ledgers_gain_the_append_only_guards_on_open(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    hub = MemoryHub(path)
    receipt = hub.append(episode())
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER episode_append_only_update")
        connection.execute("DROP TRIGGER episode_append_only_delete")
    MemoryHub(path)
    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute("UPDATE episode SET body='改写' WHERE episode_id=?", (receipt.episode_id,))


@pytest.mark.parametrize("field,value", [
    ("metadata", {"note": "Bearer abcdefghijklmnop1234567890"}),
    ("source_reference", {"source_system": "markethub", "note": "token=abcdefgh12345678"}),
    ("body", "password: abcdefgh12345678"),
])
def test_secrets_are_blocked_in_every_persisted_field(tmp_path: Path, field: str, value: object) -> None:
    hub = MemoryHub(tmp_path / "ledger.sqlite3")
    with pytest.raises(SecretRejected):
        hub.append(episode(**{field: value}))
    assert hub.health()["ledger"]["episodes"] == 0
