from __future__ import annotations

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from ai_trading_companion.provider_broker import ChatCompletionsTransport, ProviderBroker, StageRequest, canonical_packet_hash
from ai_trading_companion.provider_routes import normalize_provider


class _CompatibleHandler(BaseHTTPRequestHandler):
    requests: list[tuple[str, dict[str, object], str]] = []

    def log_message(self, _format: str, *_args: object) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/v1/models":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"data":[{"id":"local-model"},{"id":"claude-local"}]}')

    def do_POST(self) -> None:  # noqa: N802
        size = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(size).decode("utf-8"))
        type(self).requests.append((self.path, body, self.headers.get("Authorization", "")))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Request-Id", "local-compatible-request")
        self.end_headers()
        if self.path.endswith("/responses"):
            self.wfile.write(json.dumps({
                "id": "local-compatible-response", "model": body["model"], "status": "completed",
                "usage": {"input_tokens": 3, "output_tokens": 2}, "output_text": '{"answer":"local"}',
            }).encode("utf-8"))
        else:
            self.wfile.write(json.dumps({
                "id": "local-compatible-response", "model": body["model"],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
                "choices": [{"message": {"content": '{"answer":"local"}'}}],
            }).encode("utf-8"))


class ProviderCompatibleHttpTests(unittest.TestCase):
    def test_codex_route_uses_responses_from_a_legacy_compatible_url(self) -> None:
        _CompatibleHandler.requests.clear()
        server = ThreadingHTTPServer(("127.0.0.1", 0), _CompatibleHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
            config = normalize_provider({
                "enabled": True,
                "endpoints": [{"id": "local", "base_url": base_url, "api_key": "local-test-key", "weight": 0.3}],
                "routes": [{
                    "id": "local-route", "endpoint": "local", "model": "local-model", "model_family": "openai",
                    "cost": {"tier": 0, "mode": "relative"}, "preference": 0,
                    "stages": ["research"], "capabilities": ["json_schema", "race"],
                }],
            })
            packet = {"evidence": ["local compatible service"]}
            request = StageRequest(
                stage="research", packet=packet, packet_sha256=canonical_packet_hash(packet), effort="medium",
                schema={"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}},
                absolute_deadline=time.monotonic() + 5,
            )
            with TemporaryDirectory() as directory:
                outcome = ProviderBroker(config, ChatCompletionsTransport(Path(directory), {})).invoke(request)
            self.assertEqual("local-route", outcome.winner_route)
            self.assertEqual(1, len(_CompatibleHandler.requests))
            path, payload, authorization = _CompatibleHandler.requests[0]
            self.assertEqual("/v1/responses", path)
            self.assertEqual("local-model", payload["model"])
            self.assertIn("input", payload)
            self.assertNotIn("messages", payload)
            self.assertNotIn("tools", payload)
            self.assertNotIn("tool_choice", payload)
            self.assertTrue(authorization.startswith("Bearer "))
        finally:
            server.shutdown()
            server.server_close()

    def test_claude_route_keeps_chat_completions(self) -> None:
        _CompatibleHandler.requests.clear()
        server = ThreadingHTTPServer(("127.0.0.1", 0), _CompatibleHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            config = normalize_provider({
                "enabled": True,
                "endpoints": [{"id": "local", "base_url": f"http://127.0.0.1:{server.server_port}/v1", "api_key": "local-test-key", "weight": 0.3}],
                "routes": [{"id": "local-route", "endpoint": "local", "model": "claude-local", "model_family": "anthropic", "cost": {"tier": 0, "mode": "relative"}, "preference": 0, "stages": ["research"], "capabilities": ["json_schema", "race"]}],
            })
            packet = {"evidence": ["local compatible service"]}
            request = StageRequest(stage="research", packet=packet, packet_sha256=canonical_packet_hash(packet), effort="medium", schema={"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}}, absolute_deadline=time.monotonic() + 5)
            with TemporaryDirectory() as directory:
                outcome = ProviderBroker(config, ChatCompletionsTransport(Path(directory), {})).invoke(request)
            self.assertEqual("local-route", outcome.winner_route)
            path, payload, _authorization = _CompatibleHandler.requests[0]
            self.assertEqual("/v1/chat/completions", path)
            self.assertIn("messages", payload)
            self.assertNotIn("input", payload)
        finally:
            server.shutdown(); server.server_close()


if __name__ == "__main__":
    unittest.main()
