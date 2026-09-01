from __future__ import annotations

import json
from pathlib import Path
from threading import Thread
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.request import urlopen

from trading_memory_hub import MemoryHub
from trading_memory_hub.server import make_server


class ControllableSource:
    def __init__(self) -> None:
        self.body = "权威原文：机器人订单增长。"
        self.calls = 0

    def hydrate(self, reference: dict[str, str]) -> dict[str, str]:
        self.calls += 1
        return {"title": "权威记录", "body": self.body, "occurred_at": reference["date"] + "T00:00:00Z"}

    def health(self) -> dict[str, str]:
        return {"state": "ready"}


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


def test_operator_can_filter_inspect_timeline_and_download_without_writes(tmp_path: Path) -> None:
    database = tmp_path / "ledger.sqlite3"
    hub = MemoryHub(database)
    original = hub.append(episode("user-1", body="机器人订单增长"))
    correction = episode("ai-1", body="更正：订单增速存在疑问")
    correction.update({
        "episode_type": "ai_message",
        "authority": "ai_judgment",
        "corrects_episode_id": original.episode_id,
        "metadata": {"state": "published", "kind": "ai_chat"},
    })
    corrected = hub.append(correction)
    hub.append({**episode("candidate", body="内部候选"), "episode_type": "ai_candidate"})
    hub.derive_pending(
        lambda text: {"summary": text[:8], "propositions": [{"span": text[:4]}]},
        extractor_version="test-extractor/v1",
        limit=10,
    )
    before = hub.health()["ledger"]["episodes"]

    server = make_server("127.0.0.1", 0, database, source_adapters={})
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        query = urlencode({
            "memory_space_id": "partner-main",
            "q": "增速",
            "episode_type": "ai_message",
            "authority": "ai_judgment",
            "derivation_state": "complete",
            "known_from": "2026-08-31T00:00:00Z",
            "known_to": "2026-09-01T00:00:00Z",
        })
        filtered = get_json(base_url, "/v1/admin/episodes?" + query)
        detail = get_json(base_url, f"/v1/admin/episodes/{corrected.episode_id}")
        timeline = get_json(base_url, "/v1/admin/timeline?memory_space_id=partner-main")
        with urlopen(base_url + "/v1/admin/episodes/export?" + query) as response:
            exported = json.loads(response.read())
            disposition = response.headers["Content-Disposition"]
    finally:
        server.shutdown()
        thread.join()
        server.server_close()

    assert [item["episode_id"] for item in filtered["result"]["items"]] == [corrected.episode_id]
    assert detail["result"]["derivation"]["state"] == "complete"
    assert detail["result"]["derivation"]["extractor_version"] == "test-extractor/v1"
    assert detail["result"]["episode"]["corrects_episode_id"] == original.episode_id
    assert detail["result"]["relations"][0]["episode_id"] == original.episode_id
    assert [item["episode_id"] for item in timeline["result"]] == [original.episode_id, corrected.episode_id]
    assert exported["result"]["items"][0]["episode_id"] == corrected.episode_id
    assert "attachment" in disposition
    assert MemoryHub(database).health()["ledger"]["episodes"] == before


def test_external_source_is_hydrated_only_on_explicit_admin_request(tmp_path: Path) -> None:
    database = tmp_path / "ledger.sqlite3"
    source = ControllableSource()
    hub = MemoryHub(database, source_adapters={"8815": source})
    receipt = hub.append({
        **episode("article-1"),
        "source_system": "8815",
        "body": None,
        "source_reference": {
            "source_system": "8815", "record_type": "news",
            "date": "2026-08-31", "code": "600000", "event_id": "article-1",
        },
    })
    assert source.calls == 1

    server = make_server("127.0.0.1", 0, database, source_adapters={"8815": source})
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        detail = get_json(base_url, f"/v1/admin/episodes/{receipt.episode_id}")
        assert source.calls == 1
        hydrated = get_json(base_url, f"/v1/admin/episodes/{receipt.episode_id}/source")
        source.body = "被修改的原文"
        try:
            get_json(base_url, f"/v1/admin/episodes/{receipt.episode_id}/source")
            raise AssertionError("changed authority source must fail integrity verification")
        except HTTPError as error:
            integrity_status = error.code
        detail_after_failure = get_json(base_url, f"/v1/admin/episodes/{receipt.episode_id}")
    finally:
        server.shutdown()
        thread.join()
        server.server_close()

    assert detail["result"]["episode"]["body"] is None
    assert hydrated["result"]["body"] == "权威原文：机器人订单增长。"
    assert hydrated["result"]["retrieval"] == "on_demand"
    assert integrity_status == 409
    assert detail_after_failure["result"]["episode"]["episode_id"] == receipt.episode_id
    assert MemoryHub(database).health()["ledger"]["episodes"] == 1
