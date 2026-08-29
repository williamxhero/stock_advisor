from __future__ import annotations

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from ai_trading_companion.broker_client import BrokerError, BrokerRequest, ProviderBrokerClient, canonical_packet_hash
from ai_trading_companion.config import load_settings, settings_path


SCHEMA = {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}}


class _BrokerHandler(BaseHTTPRequestHandler):
    calls: list[tuple[str, dict[str, str], dict[str, object]]] = []
    mode = "success"

    def do_POST(self) -> None:  # noqa: N802
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).calls.append((self.path, dict(self.headers), payload))
        if self.mode == "unavailable":
            self.send_response(503); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(b'{"attempts":[{"provider":"upstream","status":"failed"}]}'); return
        if self.mode == "effort_unsupported":
            self.send_response(400); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(b'{"error":"unsupported effort xhigh"}'); return
        if self.mode == "invalid":
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(b"not-json"); return
        if self.path.endswith("/stream"):
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            events = [
                'event: delta\ndata: {"text":"{\\"answer\\":\\"o"}\n\n',
                'event: delta\ndata: {"text":"k\\"}"}\n\n',
            ]
            if self.mode != "missing_final":
                events.append('event: final\ndata: {"status":"completed","output_text":"{\\"answer\\":\\"ok\\"}","actual_model":"chosen","provider":"broker-upstream","request_id":"req-1","fulfilled_intellect":"smart","usage":{"input_tokens":1},"cost_estimate":0.01,"ttft_ms":2}\n\n')
            self.wfile.write("".join(events).encode()); return
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(b'{"status":"completed","output_text":"{\\"answer\\":\\"ok\\"}","actual_model":"chosen","provider":"broker-upstream","request_id":"req-1","fulfilled_intellect":"smart","fulfilled_effort":"medium","usage":{"input_tokens":1},"cost_estimate":0.01,"ttft_ms":2}')

    def log_message(self, *_: object) -> None: pass


class BrokerClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _BrokerHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()
        cls.client = ProviderBrokerClient(f"http://127.0.0.1:{cls.server.server_port}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown(); cls.thread.join()

    def request(self, *, stream: bool = False, intellect: str = "smart") -> BrokerRequest:
        packet = {"task": "test"}
        return BrokerRequest("test", packet, canonical_packet_hash(packet), intellect, "medium", SCHEMA, stream,
                             absolute_deadline=time.monotonic() + 10)

    def setUp(self) -> None:
        _BrokerHandler.calls = []; _BrokerHandler.mode = "success"

    def test_non_stream_uses_only_broker_contract_without_authorization_or_model(self) -> None:
        result = self.client.invoke(self.request())
        _, headers, payload = _BrokerHandler.calls[-1]
        self.assertEqual(result.actual_model, "chosen")
        self.assertEqual(set(payload), {"prompt", "intellect", "effort", "deadline_ms", "output_token_limit"})
        self.assertEqual(payload["intellect"], "smart")
        self.assertEqual(result.fulfilled_effort, "medium")
        self.assertNotIn("Authorization", headers)
        self.assertNotIn("model", payload)
        self.assertNotIn("api_key", json.dumps(payload).lower())

    def test_stream_checks_final_and_keeps_safe_delta_order(self) -> None:
        deltas: list[str] = []
        request = self.request(stream=True)
        object.__setattr__(request, "on_delta", deltas.append)
        result = self.client.invoke(request)
        self.assertEqual(result.result, {"answer": "ok"})
        self.assertEqual("".join(deltas), result.output_text)

    def test_unavailable_and_incomplete_stream_are_distinct(self) -> None:
        _BrokerHandler.mode = "unavailable"
        with self.assertRaisesRegex(BrokerError, "HTTP 503") as unavailable:
            self.client.invoke(self.request())
        self.assertEqual(unavailable.exception.category, "broker_unavailable")
        _BrokerHandler.mode = "missing_final"
        with self.assertRaises(BrokerError) as incomplete:
            self.client.invoke(self.request(stream=True))
        self.assertEqual(incomplete.exception.category, "broker_stream_incomplete")

    def test_unsupported_effort_is_a_distinct_capability_fault(self) -> None:
        _BrokerHandler.mode = "effort_unsupported"
        with self.assertRaises(BrokerError) as raised:
            self.client.invoke(self.request())
        self.assertEqual("broker_effort_unsupported", raised.exception.category)
        self.assertEqual({"capability": "effort"}, raised.exception.metadata)

    def test_runtime_has_no_direct_provider_protocol_or_token_dependency(self) -> None:
        runtime = Path(__file__).resolve().parents[2] / "src" / "runtime" / "ai_trading_companion"
        source = "\n".join(path.read_text(encoding="utf-8") for path in runtime.glob("*.py"))
        for forbidden in ("/chat/completions", "/responses", "BROKER_CLIENT_TOKEN"):
            self.assertNotIn(forbidden, source)
        broker_source = (runtime / "broker_client.py").read_text(encoding="utf-8")
        self.assertNotIn('"model"', broker_source)
        self.assertNotIn("Authorization", broker_source)

    def test_settings_migration_removes_only_legacy_provider_block(self) -> None:
        with TemporaryDirectory() as temporary:
            home = Path(temporary)
            path = settings_path(home); path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"provider": {"api_key": "must-not-survive"}, "research": {"web_access_gateway": {"mcp_url": "http://gateway"}}, "embedding": {"enabled": False}, "experiments": {"fdr": .1}}), encoding="utf-8")
            settings = load_settings(home)
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("provider", persisted)
            self.assertEqual("http://yosef-server:8817", settings.broker["url"])
            self.assertEqual("http://yosef-server:8817", persisted["broker"]["url"])
            self.assertEqual("http://gateway", settings.research["web_access_gateway"]["mcp_url"])
            self.assertEqual(.1, settings.experiments["fdr"])

    def test_broker_url_is_configurable_but_must_stay_a_base_url(self) -> None:
        with TemporaryDirectory() as temporary:
            home = Path(temporary)
            path = settings_path(home); path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"broker": {"url": "http://broker.test:8817"}}), encoding="utf-8")
            self.assertEqual("http://broker.test:8817", load_settings(home).broker["url"])
            path.write_text(json.dumps({"broker": {"url": "http://broker.test:8817/v1"}}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Broker base URL"):
                load_settings(home)
            path.write_text(json.dumps({"broker": {"url": "http://token@broker.test:8817"}}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Broker base URL"):
                load_settings(home)
