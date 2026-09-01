from __future__ import annotations

import json
from pathlib import Path
from threading import Thread
from urllib.request import urlopen

from trading_memory_hub import MemoryHub
from trading_memory_hub.server import make_server


def episode(event_id: str, *, space: str = "partner-main", body: str = "长期复利") -> dict[str, object]:
    return {
        "memory_space_id": space,
        "source_system": "stock-advisor",
        "source_event_id": event_id,
        "content_hash": "auto",
        "episode_type": "user_message",
        "body": body,
        "occurred_at": "2026-08-31T01:00:00Z",
        "known_at": "2026-08-31T01:00:00Z",
        "submitted_at": "2026-08-31T01:00:01Z",
        "authority": "user_private_fact",
        "protocol_version": "memoryhub/v1",
    }


def get_json(base_url: str, path: str) -> dict[str, object]:
    with urlopen(base_url + path) as response:
        return json.loads(response.read())


def test_operator_can_open_admin_and_page_recent_episodes(tmp_path: Path) -> None:
    database = tmp_path / "ledger.sqlite3"
    hub = MemoryHub(database)
    hub.append(episode("older", body="较早记忆"))
    hub.append(episode("newer", body="最新记忆"))
    hub.append(episode("other", space="partner-two", body="其他伙伴"))

    server = make_server("127.0.0.1", 0, database, source_adapters={})
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(base_url + "/admin/") as response:
            html = response.read().decode("utf-8")
        spaces = get_json(base_url, "/v1/admin/memory-spaces")
        first_page = get_json(
            base_url,
            "/v1/admin/episodes?memory_space_id=partner-main&limit=1",
        )
        second_page = get_json(
            base_url,
            "/v1/admin/episodes?memory_space_id=partner-main&limit=1&cursor=2",
        )
    finally:
        server.shutdown()
        thread.join()
        server.server_close()

    assert "MemoryHub" in html
    assert "只读" in html
    assert [item["memory_space_id"] for item in spaces["result"]] == [
        "partner-two",
        "partner-main",
    ]
    assert spaces["result"][1]["episode_count"] == 2
    assert first_page["result"]["items"][0]["body"] == "最新记忆"
    assert first_page["result"]["next_cursor"] == "2"
    assert second_page["result"]["items"][0]["body"] == "较早记忆"
