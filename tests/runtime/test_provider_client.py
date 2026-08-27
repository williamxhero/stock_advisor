import json
from http.client import RemoteDisconnected
from pathlib import Path
from unittest.mock import Mock
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from ai_trading_companion.config import DEFAULT_PROVIDER, DEFAULT_RESEARCH, load_settings
from ai_trading_companion.provider_client import ProviderClient, ProviderError, current_codex_desktop_user_agent


class ProviderClientTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.provider = json.loads(json.dumps(DEFAULT_PROVIDER))
        self.provider["enabled"] = True

    def tearDown(self):
        self.temporary.cleanup()

    def test_uses_configured_slot_and_structured_output(self):
        client = ProviderClient(self.provider, DEFAULT_RESEARCH, self.home)
        response = {"id": "chatcmpl_1", "choices": [{"message": {"role": "assistant", "content": '{"ok":true}'}}], "usage": {"prompt_tokens": 2}}
        with patch.object(client, "_request", return_value=response) as request:
            schema = self.home / "schema.json"
            schema.write_text('{"type":"object","properties":{"ok":{"type":"boolean"}},"required":["ok"],"additionalProperties":false}', encoding="utf-8")
            result = client.run("hello", schema, slot="fast", effort="low", search=False, timeout=10)
        self.assertEqual('{"ok":true}', result.text)
        payload = request.call_args.args[0]
        self.assertEqual(self.provider["models"]["fast"]["id"], payload["model"])
        self.assertEqual("low", payload["reasoning_effort"])
        self.assertEqual("hello", payload["messages"][0]["content"])
        self.assertEqual("json_schema", payload["response_format"]["type"])
        self.assertRegex(payload["response_format"]["json_schema"]["name"], r"^[A-Za-z0-9_-]+$")

    def test_provider_requests_identify_as_the_companion_app(self):
        client = ProviderClient(self.provider, DEFAULT_RESEARCH, self.home)
        with patch("ai_trading_companion.provider_client.read_secret", return_value="test-key"):
            request = client._request_object({"model": "test"})
        self.assertEqual(client.user_agent, request.headers["User-agent"])

    def test_user_agent_reads_the_current_local_codex_version_at_startup(self):
        with patch("ai_trading_companion.provider_client._local_codex_version", return_value="0.150.0-alpha.8"), patch("ai_trading_companion.provider_client._local_codex_desktop_build", return_value="26.820.60940"), patch("ai_trading_companion.provider_client.platform.version", return_value="10.0.26200"), patch("ai_trading_companion.provider_client.platform.machine", return_value="AMD64"):
            self.assertEqual(
                "Codex Desktop/0.150.0-alpha.8 (Windows 10.0.26200; x86_64) unknown (Codex Desktop; 26.820.60940)",
                current_codex_desktop_user_agent(),
            )

    def test_defaults_target_the_small_computer_services(self):
        self.assertEqual("http://yosef-server:8317/v1", DEFAULT_PROVIDER["base_url"])
        self.assertEqual("http://yosef-server:8801", DEFAULT_RESEARCH["searxng"]["base_url"])
        self.assertEqual("gpt-5.6-luna", DEFAULT_PROVIDER["models"]["fast"]["id"])
        self.assertFalse(load_settings(self.home).provider_enabled)

    def test_default_circuit_breaker_allows_transient_cpa_failures_to_recover(self):
        self.assertEqual(5, DEFAULT_PROVIDER["retry"]["circuit_breaker_failures"])
        self.assertEqual(5, DEFAULT_PROVIDER["retry"]["max_attempts"])
        self.assertEqual(90, DEFAULT_PROVIDER["retry"]["per_attempt_timeout_seconds"])

    def test_long_stage_timeout_is_split_into_multiple_cpa_attempts(self):
        self.provider["retry"] = {
            "max_attempts": 3, "per_attempt_timeout_seconds": 90,
            "initial_backoff_seconds": 0, "max_backoff_seconds": 0,
        }
        client = ProviderClient(self.provider, DEFAULT_RESEARCH, self.home)
        observed: list[int] = []

        def fail(attempt_timeout: int):
            observed.append(attempt_timeout)
            raise ProviderError("CPA timeout", category="provider_timeout")

        with self.assertRaises(ProviderError):
            client._with_retries(fail, 300)
        self.assertEqual([90, 90, 90], observed)

    def test_retries_transient_provider_failure_before_returning_a_response(self):
        self.provider["retry"] = {"max_attempts": 3, "initial_backoff_seconds": 0, "max_backoff_seconds": 0}
        client = ProviderClient(self.provider, DEFAULT_RESEARCH, self.home)
        transient = ProviderError("Provider HTTP 503", category="provider_http", status=503)
        with patch.object(client, "_request_once", side_effect=[transient, {"id": "resp_1"}]) as request, patch("ai_trading_companion.provider_client.time.sleep"):
            response = client._request({"model": "test"}, 20)
        self.assertEqual("resp_1", response["id"])
        self.assertEqual(2, request.call_count)

    def test_does_not_retry_deterministic_provider_failure(self):
        client = ProviderClient(self.provider, DEFAULT_RESEARCH, self.home)
        deterministic = ProviderError("Provider HTTP 400", category="provider_http", status=400)
        with patch.object(client, "_request_once", side_effect=deterministic) as request:
            with self.assertRaises(ProviderError):
                client._request({"model": "test"}, 20)
        self.assertEqual(1, request.call_count)

    def test_retries_cpa_transient_upstream_400(self):
        self.provider["retry"] = {"max_attempts": 3, "initial_backoff_seconds": 0, "max_backoff_seconds": 0}
        client = ProviderClient(self.provider, DEFAULT_RESEARCH, self.home)
        transient = ProviderError("Provider HTTP 400: Upstream request failed", category="provider_http", status=400)
        with patch.object(client, "_request_once", side_effect=[transient, {"id": "resp_recovered"}]) as request, patch("ai_trading_companion.provider_client.time.sleep"):
            response = client._request({"model": "test"}, 20)
        self.assertEqual("resp_recovered", response["id"])
        self.assertEqual(2, request.call_count)

    def test_classifies_remote_disconnect_as_retryable_provider_network_failure(self):
        client = ProviderClient(self.provider, DEFAULT_RESEARCH, self.home)
        with patch("ai_trading_companion.provider_client.urlopen", side_effect=RemoteDisconnected("CPA closed connection")):
            with self.assertRaises(ProviderError) as raised:
                client._request_once({"model": "test"}, 20)
        self.assertEqual("provider_network", raised.exception.category)

    def test_classifies_provider_timeout_separately_from_network_failure(self):
        client = ProviderClient(self.provider, DEFAULT_RESEARCH, self.home)
        with patch("ai_trading_companion.provider_client.urlopen", side_effect=TimeoutError("CPA timeout")):
            with self.assertRaises(ProviderError) as raised:
                client._request_once({"model": "test"}, 20)
        self.assertEqual("provider_timeout", raised.exception.category)

    def test_preserves_only_provider_error_message_from_http_error(self):
        client = ProviderClient(self.provider, DEFAULT_RESEARCH, self.home)
        error = Mock()
        error.code = 400
        error.headers = {}
        error.read.return_value = b'{"error":{"type":"invalid_request_error","message":"unsupported field"}}'
        with patch.object(client, "_request_object", return_value=Mock()), patch("ai_trading_companion.provider_client.urlopen", side_effect=__import__("urllib.error", fromlist=["HTTPError"]).HTTPError("http://example.test", 400, "bad", {}, error)):
            with self.assertRaises(ProviderError) as raised:
                client._request_once({"model": "test"}, 20)
        self.assertIn("unsupported field", str(raised.exception))
        self.assertNotIn("Authorization", str(raised.exception))

    def test_stream_failure_after_first_delta_is_not_retried(self):
        self.provider["retry"] = {"max_attempts": 3, "initial_backoff_seconds": 0, "max_backoff_seconds": 0}
        client = ProviderClient(self.provider, DEFAULT_RESEARCH, self.home)

        def partial(_payload, _timeout, on_delta, **_kwargs):
            on_delta("already published")
            raise ProviderError("Provider network request failed", category="provider_network")

        with patch.object(client, "_request_stream_once", side_effect=partial) as request, patch("ai_trading_companion.provider_client.time.sleep"):
            with self.assertRaises(ProviderError):
                client._request_stream({"model": "test", "stream": True}, 20, lambda _delta: None)
        self.assertEqual(1, request.call_count)

    def test_internal_research_stream_can_retry_after_unpublished_delta(self):
        self.provider["retry"] = {"max_attempts": 3, "initial_backoff_seconds": 0, "max_backoff_seconds": 0}
        client = ProviderClient(self.provider, DEFAULT_RESEARCH, self.home)
        recovered = {"id": "stream-ok", "choices": [{"message": {"content": "done"}, "finish_reason": "stop"}]}

        calls = 0

        def partial_then_recover(_payload, _timeout, on_delta, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                on_delta("internal only")
                raise ProviderError("Provider network request failed", category="provider_network")
            return recovered, ["done"]

        with patch.object(client, "_request_stream_once", side_effect=partial_then_recover) as request:
            result, deltas = client._request_stream(
                {"model": "test", "stream": True}, 20, lambda _delta: None,
                retry_after_delta=True,
            )
        self.assertEqual("stream-ok", result["id"])
        self.assertEqual(["done"], deltas)
        self.assertEqual(2, request.call_count)

    def test_replays_assistant_tool_call_and_tool_output_in_standard_messages(self):
        client = ProviderClient(self.provider, DEFAULT_RESEARCH, self.home)
        call = {"id": "call_1", "type": "function", "function": {"name": "search_searxng", "arguments": '{"query":"A股"}'}}
        first = {"id": "chatcmpl_1", "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [call]}}]}
        second = {"id": "chatcmpl_2", "choices": [{"message": {"role": "assistant", "content": "已完成"}}]}
        with patch.object(client, "_request", side_effect=[first, second]) as request, patch("ai_trading_companion.provider_client.ResearchTools.call", return_value={"results": [{"url": "https://example.test"}]}):
            result = client.run("搜索 A 股", None, slot="fast", effort="medium", search=True, timeout=20)
        self.assertEqual("已完成", result.text)
        continuation = request.call_args_list[1].args[0]
        self.assertEqual("assistant", continuation["messages"][1]["role"])
        self.assertEqual(call, continuation["messages"][1]["tool_calls"][0])
        self.assertEqual("tool", continuation["messages"][2]["role"])
        self.assertEqual("call_1", continuation["messages"][2]["tool_call_id"])

    def test_separates_tool_loop_from_structured_output_for_cpa(self):
        client = ProviderClient(self.provider, DEFAULT_RESEARCH, self.home)
        call = {"id": "call_1", "type": "function", "function": {"name": "search_searxng", "arguments": '{"query":"A股"}'}}
        first = {"id": "chatcmpl_1", "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [call]}}]}
        research = {"id": "chatcmpl_2", "choices": [{"message": {"role": "assistant", "content": "检索完成"}}]}
        structured = {"id": "chatcmpl_3", "choices": [{"message": {"role": "assistant", "content": '{"ok":true}'}}]}
        with patch.object(client, "_request", side_effect=[first, research, structured]) as request, patch("ai_trading_companion.provider_client.ResearchTools.call", return_value={"results": [{"url": "https://example.test"}]}):
            schema = self.home / "schema.json"
            schema.write_text('{"type":"object","properties":{"ok":{"type":"boolean"}},"required":["ok"],"additionalProperties":false}', encoding="utf-8")
            result = client.run("搜索 A 股", schema, slot="fast", effort="medium", search=True, timeout=20)
        self.assertEqual('{"ok":true}', result.text)
        tool_payload = request.call_args_list[0].args[0]
        self.assertNotIn("response_format", tool_payload)
        final_payload = request.call_args_list[2].args[0]
        self.assertIn("response_format", final_payload)
        self.assertNotIn("tools", final_payload)

    def test_coverage_repair_forces_a_research_backend_after_zero_tool_calls(self):
        client = ProviderClient(self.provider, DEFAULT_RESEARCH, self.home)
        call = {"id": "call_1", "type": "function", "function": {"name": "search_searxng", "arguments": '{"query":"收盘"}'}}
        responses = [
            {"id": "r1", "choices": [{"message": {"role": "assistant", "content": "无需搜索"}}]},
            {"id": "r2", "choices": [{"message": {"role": "assistant", "content": '{"ok":false}'}}]},
            {"id": "r3", "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [call]}}]},
            {"id": "r4", "choices": [{"message": {"role": "assistant", "content": "已补查"}}]},
            {"id": "r5", "choices": [{"message": {"role": "assistant", "content": '{"ok":true}'}}]},
        ]
        schema = self.home / "schema.json"
        schema.write_text('{"type":"object","properties":{"ok":{"type":"boolean"}},"required":["ok"],"additionalProperties":false}', encoding="utf-8")
        validator = lambda output, trace: {"passed": bool(output.get("ok")) and bool(trace), "problems": [] if trace else ["no_current_information_tool_result"]}
        with patch.object(client, "_request", side_effect=responses) as request, patch("ai_trading_companion.provider_client.ResearchTools.call", return_value={"backend": "searxng", "results": [{"url": "https://example.test/close"}], "acquired_at": "2026-08-26T08:00:00Z"}):
            result = client.run("研究收盘", schema, slot="fast", effort="medium", search=True, timeout=30, research_validator=validator)
        self.assertEqual('{"ok":true}', result.text)
        self.assertEqual("search_searxng", request.call_args_list[2].args[0]["tool_choice"]["function"]["name"])
        self.assertTrue(result.validation["passed"])

    def test_rejected_candidate_keeps_provider_audit_information(self):
        client = ProviderClient(self.provider, DEFAULT_RESEARCH, self.home)
        schema = self.home / "schema.json"
        schema.write_text('{"type":"object","properties":{"ok":{"type":"boolean"}},"required":["ok"]}', encoding="utf-8")
        response = {"id": "response-1", "_request_id": "request-1", "usage": {"prompt_tokens": 7}, "choices": [{"message": {"role": "assistant", "content": '{"ok":false}'}}]}
        with patch.object(client, "_request", return_value=response):
            result = client.run("research", schema, slot="fast", effort="low", search=True, timeout=20, max_coverage_repairs=0, research_validator=lambda _output, _trace: {"passed": False, "problems": ["missing"]})
        self.assertFalse(result.validation["passed"])
        self.assertEqual('{"ok":false}', result.text)
        self.assertEqual("response-1", result.response_id)
        self.assertEqual("request-1", result.request_id)
        self.assertEqual({"prompt_tokens": 7}, result.usage)
        self.assertEqual([], result.tool_trace)

    def test_research_loop_bounds_fail_before_unbounded_requests(self):
        client = ProviderClient(self.provider, DEFAULT_RESEARCH, self.home)
        with self.assertRaisesRegex(TimeoutError, "hard deadline"):
            client.run("research", None, slot="fast", effort="low", search=True, timeout=0)
        with self.assertRaisesRegex(ProviderError, "turn limit"):
            client.run("research", None, slot="fast", effort="low", search=True, timeout=20, max_model_turns=0)

    def test_research_does_not_stop_while_distinct_tool_calls_keep_adding_evidence(self):
        client = ProviderClient(self.provider, DEFAULT_RESEARCH, self.home)
        responses = []
        for number in range(13):
            call = {
                "id": f"call_{number}",
                "type": "function",
                "function": {
                    "name": "search_searxng",
                    "arguments": json.dumps({"query": f"current market evidence {number}"}),
                },
            }
            responses.append({"id": f"r{number}", "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [call]}}]})
        responses.append({"id": "done", "choices": [{"message": {"role": "assistant", "content": "research complete"}}]})

        result = {"backend": "searxng", "results": [{"url": "https://example.test/current", "snippet": "current evidence"}]}
        with patch.object(client, "_request", side_effect=responses), patch("ai_trading_companion.provider_client.ResearchTools.call", return_value=result):
            completed = client.run("research", None, slot="fast", effort="low", search=True, timeout=20)

        self.assertEqual("research complete", completed.text)
        self.assertEqual(13, len(completed.tool_trace))

    def test_research_loop_failure_preserves_completed_tool_trace(self):
        client = ProviderClient(self.provider, DEFAULT_RESEARCH, self.home)
        responses = []
        for number in range(2):
            call = {
                "id": f"empty_{number}",
                "type": "function",
                "function": {"name": "search_searxng", "arguments": json.dumps({"query": f"empty {number}"})},
            }
            responses.append({"id": f"r{number}", "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [call]}}]})

        with patch.object(client, "_request", side_effect=responses), patch("ai_trading_companion.provider_client.ResearchTools.call", return_value={"backend": "searxng", "results": []}):
            with self.assertRaisesRegex(ProviderError, "empty tool result limit") as raised:
                client.run(
                    "research", None, slot="fast", effort="low", search=True, timeout=20,
                    max_empty_tool_results=2,
                )

        self.assertEqual(2, len(raised.exception.tool_trace))

    def test_probe_uses_the_configured_slow_provider_timeout(self):
        self.provider["retry"] = {"probe_timeout_seconds": 91}
        client = ProviderClient(self.provider, DEFAULT_RESEARCH, self.home)
        tool_call = {"id": "call_1", "type": "function", "function": {"name": "capability_probe", "arguments": "{}"}}
        response = {"id": "chatcmpl_1", "choices": [{"message": {"role": "assistant", "content": "OK", "tool_calls": [tool_call]}}]}
        with patch.object(client, "_request", return_value=response) as request, patch.object(client, "_request_stream", return_value=({"id": "chatcmpl_stream", "choices": [{"message": {"role": "assistant", "content": "OK"}}]}, ["OK"])):
            client.probe()
        self.assertTrue(all(call.args[1] == 91 for call in request.call_args_list))
