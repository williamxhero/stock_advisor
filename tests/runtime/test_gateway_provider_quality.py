from __future__ import annotations

import json
from datetime import datetime, timezone
import pytest
from aiohttp.test_utils import TestClient, TestServer

from ai_trading_companion.gateway import RuntimeGateway
from ai_trading_companion.store import CompanionStore


def seed(store: CompanionStore, attempt_id: str, family: str, model: str, error: str | None = None) -> None:
    with store.connection() as connection:
        connection.execute(
            """INSERT INTO provider_llm_attempt(
               attempt_id,invocation_id,stage,route_id,endpoint_id,model,model_family,tier,recorded_at,
               protocol_success,product_success,terminal_error,ttft_ms,duration_ms,estimated_cost)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (attempt_id, "inv", "research", f"route-{family}", f"endpoint-{family}", model, family, 100,
             datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), int(error is None), int(error is None), error, 120, 300, .1),
        )


@pytest.mark.asyncio
async def test_quality_comparison_errors_and_redacted_export_are_read_only(tmp_path):
    store = CompanionStore(tmp_path / "companion.sqlite3")
    store.initialize()
    seed(store, "gpt-ok", "openai", "gpt-test")
    seed(store, "claude-bad", "anthropic", "claude-test", "rate_limited")
    gateway = RuntimeGateway(tmp_path, store, lambda _: {}, lambda *_: {}, lambda: None)
    async with TestClient(TestServer(gateway.app())) as client:
        headers = {"Authorization": f"Bearer {gateway._token}"}
        quality = await client.get("/v1/provider-quality?window=24h&sort=sample_size&limit=1", headers=headers)
        comparison = await client.get("/v1/provider-quality/comparison?stage=research", headers=headers)
        errors = await client.get("/v1/provider-quality/errors?window=24h", headers=headers)
        exported = await client.get("/v1/provider-quality/export?format=json&window=24h", headers=headers)

        assert quality.status == 200
        quality_json = await quality.json()
        assert quality_json["pagination"] == {"limit": 1, "offset": 0, "total": 2}
        assert {item["model_family"] for item in (await comparison.json())["items"]} == {"openai", "anthropic"}
        assert (await errors.json())["items"][0]["error"] == "rate_limited"
        exported_text = await exported.text()
        assert json.loads(exported_text)["contract"] == "provider-quality-export/v1"
        assert "api_key" not in exported_text and "prompt" not in exported_text


@pytest.mark.asyncio
async def test_quality_rejects_unallowlisted_sort_and_requires_authentication(tmp_path):
    store = CompanionStore(tmp_path / "companion.sqlite3")
    store.initialize()
    gateway = RuntimeGateway(tmp_path, store, lambda _: {}, lambda *_: {}, lambda: None)
    async with TestClient(TestServer(gateway.app())) as client:
        assert (await client.get("/v1/provider-quality")).status == 401
        response = await client.get("/v1/provider-quality?sort=sql_injection", headers={"Authorization": f"Bearer {gateway._token}"})
        assert response.status == 400


@pytest.mark.asyncio
async def test_gateway_descriptor_contains_the_process_local_loopback_token(tmp_path):
    store = CompanionStore(tmp_path / "companion.sqlite3")
    store.initialize()
    gateway = RuntimeGateway(tmp_path, store, lambda _: {}, lambda *_: {}, lambda: None)

    class Socket:
        def getsockname(self):
            return ("127.0.0.1", 32123)

    class Server:
        sockets = [Socket()]

    class Site:
        _server = Server()

    class Runner:
        sites = [Site()]

    await gateway.write_descriptor(Runner())
    descriptor = json.loads((tmp_path / "runtime" / "gateway.json").read_text(encoding="utf-8"))
    assert descriptor == {
        "contract": "companion-gateway/v1",
        "host": "127.0.0.1",
        "port": 32123,
        "token": gateway._token,
    }
