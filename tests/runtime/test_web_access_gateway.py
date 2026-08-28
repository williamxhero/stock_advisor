from __future__ import annotations

import json
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError

from ai_trading_companion.web_access_gateway import WebAccessGatewayClient, WebAccessGatewayError, _fact_as_of


class _Response:
    def __init__(
        self,
        payload: dict | None = None,
        *,
        raw: str | None = None,
        content_type: str = "application/json",
    ) -> None:
        self.payload = payload
        self.raw = raw
        self.headers = {"Content-Type": content_type}

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        value = self.raw if self.raw is not None else json.dumps(self.payload)
        return value.encode("utf-8")


class WebAccessGatewayTests(unittest.TestCase):
    def test_fact_time_preserves_explicit_publication_minutes(self) -> None:
        self.assertEqual("2026-08-27T07:18:00Z", _fact_as_of("2026年8月27日 15:18 发布"))
        self.assertEqual("2026-08-27T07:00:00Z", _fact_as_of("2026年8月27日收盘"))
        self.assertEqual(
            "2026-08-27T07:00:00Z",
            _fact_as_of("旧值 2026-06-30；目标 2026-08-27；页面 2026-08-28", not_after="2026-08-27T07:20:00Z"),
        )

    def setUp(self) -> None:
        self.client = WebAccessGatewayClient({"web_access_gateway": {
            "mcp_url": "http://gateway.test/mcp", "token": "test-token",
        }})

    def test_search_uses_mcp_tool_call_and_returns_discovery_only(self) -> None:
        response = {"jsonrpc": "2.0", "result": {"content": [{"type": "text", "text": json.dumps({
            "trace_id": "trace", "results": [{"url": "https://example.test/2026-08-27", "title": "title", "content": "2026-08-27"}],
        })}]}}
        with mock.patch("ai_trading_companion.web_access_gateway.urlopen", return_value=_Response(response)) as open_request:
            result = self.client.search("market close")
        request = open_request.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual("tools/call", payload["method"])
        self.assertEqual("web_search", payload["params"]["name"])
        self.assertEqual("Bearer test-token", request.headers["Authorization"])
        self.assertFalse(result["results"][0]["primary"])
        self.assertEqual([{"tool": "web_search", "status": "passed", "attempts": 1}], self.client.call_history)

    def test_search_accepts_sse_data_jsonrpc_response(self) -> None:
        inner = {"trace_id": "sse-trace", "results": [{
            "url": "https://example.test", "title": "title", "content": "content",
        }]}
        outer = {"jsonrpc": "2.0", "id": "request", "result": {"content": [{
            "type": "text", "text": json.dumps(inner),
        }]}}
        response = _Response(
            raw=f"event: message\ndata: {json.dumps(outer)}\n\n",
            content_type="text/event-stream; charset=utf-8",
        )

        with mock.patch("ai_trading_companion.web_access_gateway.urlopen", return_value=response):
            result = self.client.search("market close")

        self.assertEqual("sse-trace", result["trace_id"])
        self.assertEqual(1, len(result["results"]))

    def test_unsupported_success_content_type_is_a_non_retryable_compatibility_error(self) -> None:
        response = _Response(raw="not MCP", content_type="text/html; charset=utf-8")
        with mock.patch("ai_trading_companion.web_access_gateway.urlopen", return_value=response) as open_request:
            with self.assertRaises(WebAccessGatewayError) as raised:
                self.client.search("market close")

        self.assertEqual("WAG_CLIENT_COMPATIBILITY_ERROR", raised.exception.category)
        self.assertEqual(1, raised.exception.attempts)
        self.assertEqual(1, open_request.call_count)

    def test_jsonrpc_error_retries_then_becomes_wag_outage(self) -> None:
        response = _Response({"jsonrpc": "2.0", "id": "request", "error": {
            "code": -32000, "message": "sensitive upstream detail",
        }})
        with mock.patch("ai_trading_companion.web_access_gateway.urlopen", return_value=response) as open_request:
            with self.assertRaises(WebAccessGatewayError) as raised:
                self.client.search("market close")

        self.assertEqual("WAG_OUTAGE", raised.exception.category)
        self.assertEqual(3, raised.exception.attempts)
        self.assertNotIn("sensitive upstream detail", str(raised.exception))
        self.assertEqual(3, open_request.call_count)

    def test_result_is_error_retries_then_becomes_wag_outage(self) -> None:
        response = _Response({"jsonrpc": "2.0", "id": "request", "result": {
            "isError": True,
            "content": [{"type": "text", "text": "sensitive tool detail"}],
        }})
        with mock.patch("ai_trading_companion.web_access_gateway.urlopen", return_value=response) as open_request:
            with self.assertRaises(WebAccessGatewayError) as raised:
                self.client.search("market close")

        self.assertEqual("WAG_OUTAGE", raised.exception.category)
        self.assertEqual(3, raised.exception.attempts)
        self.assertNotIn("sensitive tool detail", str(raised.exception))
        self.assertEqual(3, open_request.call_count)

    def test_missing_text_content_is_a_non_retryable_compatibility_error(self) -> None:
        response = _Response({"jsonrpc": "2.0", "id": "request", "result": {
            "content": [{"type": "image", "data": "not-relevant"}],
        }})
        with mock.patch("ai_trading_companion.web_access_gateway.urlopen", return_value=response) as open_request:
            with self.assertRaises(WebAccessGatewayError) as raised:
                self.client.search("market close")

        self.assertEqual("WAG_CLIENT_COMPATIBILITY_ERROR", raised.exception.category)
        self.assertEqual(1, open_request.call_count)

    def test_malformed_inner_json_is_a_non_retryable_compatibility_error(self) -> None:
        response = _Response({"jsonrpc": "2.0", "id": "request", "result": {
            "content": [{"type": "text", "text": "{not-json"}],
        }})
        with mock.patch("ai_trading_companion.web_access_gateway.urlopen", return_value=response) as open_request:
            with self.assertRaises(WebAccessGatewayError) as raised:
                self.client.search("market close")

        self.assertEqual("WAG_CLIENT_COMPATIBILITY_ERROR", raised.exception.category)
        self.assertEqual(1, open_request.call_count)

    def test_empty_news_search_retries_general_discovery(self) -> None:
        empty = _Response({"jsonrpc": "2.0", "result": {"content": [{"type": "text", "text": json.dumps({
            "trace_id": "news", "results": [],
        })}]}})
        general = _Response({"jsonrpc": "2.0", "result": {"content": [{"type": "text", "text": json.dumps({
            "trace_id": "general", "results": [{"url": "https://example.test", "title": "close", "content": "close"}],
        })}]}})
        with mock.patch("ai_trading_companion.web_access_gateway.urlopen", side_effect=[empty, general]) as open_request:
            result = self.client.search("market close", "news")
        self.assertEqual(1, len(result["results"]))
        calls = [json.loads(call.args[0].data.decode("utf-8")) for call in open_request.call_args_list]
        self.assertEqual(["news", "general"], [call["params"]["arguments"]["categories"] for call in calls])

    def test_empty_news_and_general_search_raise_no_search_results_without_outage(self) -> None:
        empty = _Response({"jsonrpc": "2.0", "result": {"content": [{"type": "text", "text": json.dumps({
            "trace_id": "empty", "results": [],
        })}]}})
        with mock.patch("ai_trading_companion.web_access_gateway.urlopen", return_value=empty) as open_request:
            with self.assertRaises(WebAccessGatewayError) as raised:
                self.client.search("market close", "news")

        self.assertEqual("WAG_NO_SEARCH_RESULTS", raised.exception.category)
        self.assertEqual(1, raised.exception.attempts)
        self.assertEqual(2, open_request.call_count)

    def test_tencent_history_read_removes_current_quote_after_frozen_cutoff(self) -> None:
        url = (
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
            "?param=sh000001,day,2026-08-27,2026-08-27,1,qfq"
        )
        markdown = json.dumps({
            "code": 0,
            "data": {"sh000001": {
                "day": [["2026-08-27", "3911.890", "3956.570", "3958.030", "3909.310", "516777549.000"]],
                "qt": {"sh000001": ["current", "20260828161402", "future-value"]},
            }},
        }).replace("[", "\\[").replace("]", "\\]")
        response = {"jsonrpc": "2.0", "result": {"content": [{"type": "text", "text": json.dumps({
            "trace_id": "trace", "results": [{"url": url, "title": "history", "markdown": markdown}],
        })}]}}
        with mock.patch("ai_trading_companion.web_access_gateway.urlopen", return_value=_Response(response)):
            item = self.client.read(url, not_after="2026-08-27T07:20:00Z")["results"][0]
        self.assertEqual("2026-08-27T07:00:00Z", item["fact_as_of"])
        self.assertIn('"close":"3956.570"', item["excerpt_text"])
        self.assertNotIn("future-value", item["excerpt_text"])
        self.assertFalse(item["primary"])

    def test_empty_read_results_raise_no_read_content_without_outage(self) -> None:
        response = _Response({"jsonrpc": "2.0", "result": {"content": [{"type": "text", "text": json.dumps({
            "trace_id": "empty", "results": [],
        })}]}})
        with mock.patch("ai_trading_companion.web_access_gateway.urlopen", return_value=response) as open_request:
            with self.assertRaises(WebAccessGatewayError) as raised:
                self.client.read("https://example.test")

        self.assertEqual("WAG_NO_READ_CONTENT", raised.exception.category)
        self.assertEqual(1, raised.exception.attempts)
        self.assertEqual(1, open_request.call_count)

    def test_blank_read_markdown_raises_no_read_content_without_outage(self) -> None:
        response = _Response({"jsonrpc": "2.0", "result": {"content": [{"type": "text", "text": json.dumps({
            "trace_id": "empty", "results": [{"url": "https://example.test", "markdown": "  "}],
        })}]}})
        with mock.patch("ai_trading_companion.web_access_gateway.urlopen", return_value=response) as open_request:
            with self.assertRaises(WebAccessGatewayError) as raised:
                self.client.read("https://example.test")

        self.assertEqual("WAG_NO_READ_CONTENT", raised.exception.category)
        self.assertEqual(1, open_request.call_count)

    def test_browser_rejects_any_mutating_action_before_network(self) -> None:
        with mock.patch("ai_trading_companion.web_access_gateway.urlopen") as open_request:
            with self.assertRaisesRegex(WebAccessGatewayError, "read-only"):
                self.client.browser(None, [{"type": "type", "text": "submit"}])
        open_request.assert_not_called()

    def test_browser_removes_schema_null_placeholders_before_gateway_call(self) -> None:
        response = {"jsonrpc": "2.0", "result": {"content": [{"type": "text", "text": json.dumps({
            "trace_id": "trace", "results": [{"snapshot": "done"}],
        })}]}}
        with mock.patch("ai_trading_companion.web_access_gateway.urlopen", return_value=_Response(response)) as open_request:
            self.client.browser(None, [{"type": "navigate", "url": "https://example.test", "ref": None,
                                        "element": None, "ms": None, "pixels": None}])
        payload = json.loads(open_request.call_args.args[0].data.decode("utf-8"))
        self.assertEqual({"type": "navigate", "url": "https://example.test"}, payload["params"]["arguments"]["actions"][0])

    def test_missing_token_fails_without_network(self) -> None:
        client = WebAccessGatewayClient({"web_access_gateway": {"mcp_url": "http://gateway.test/mcp"}})
        with mock.patch("ai_trading_companion.web_access_gateway.urlopen") as open_request:
            with self.assertRaisesRegex(WebAccessGatewayError, "token is not configured"):
                client.read("https://example.test")
        open_request.assert_not_called()

    def test_three_consecutive_connection_failures_raise_explicit_wag_outage(self) -> None:
        with mock.patch(
            "ai_trading_companion.web_access_gateway.urlopen",
            side_effect=URLError("secret host detail must not escape"),
        ) as open_request:
            with self.assertRaises(WebAccessGatewayError) as raised:
                self.client.read("https://example.test")

        self.assertEqual("WAG_OUTAGE", raised.exception.category)
        self.assertEqual(3, raised.exception.attempts)
        self.assertIn("WAG_OUTAGE", str(raised.exception))
        self.assertNotIn("secret host detail", str(raised.exception))
        self.assertEqual(3, open_request.call_count)

    def test_three_consecutive_timeouts_raise_explicit_wag_outage(self) -> None:
        with mock.patch(
            "ai_trading_companion.web_access_gateway.urlopen",
            side_effect=TimeoutError("secret timeout detail must not escape"),
        ) as open_request:
            with self.assertRaises(WebAccessGatewayError) as raised:
                self.client.search("market close")

        self.assertEqual("WAG_OUTAGE", raised.exception.category)
        self.assertEqual(3, raised.exception.attempts)
        self.assertNotIn("secret timeout detail", str(raised.exception))
        self.assertEqual(3, open_request.call_count)

    def test_retryable_http_failure_retries_then_becomes_wag_outage(self) -> None:
        error = HTTPError("http://gateway.test/mcp", 503, "secret upstream detail", {}, None)
        with mock.patch("ai_trading_companion.web_access_gateway.urlopen", side_effect=error) as open_request:
            with self.assertRaises(WebAccessGatewayError) as raised:
                self.client.search("market close")

        self.assertEqual("WAG_OUTAGE", raised.exception.category)
        self.assertEqual(3, raised.exception.attempts)
        self.assertNotIn("secret upstream detail", str(raised.exception))
        self.assertEqual(3, open_request.call_count)

    def test_non_retryable_http_failure_is_not_an_outage(self) -> None:
        error = HTTPError("http://gateway.test/mcp", 401, "secret auth detail", {}, None)
        with mock.patch("ai_trading_companion.web_access_gateway.urlopen", side_effect=error) as open_request:
            with self.assertRaises(WebAccessGatewayError) as raised:
                self.client.search("market close")

        self.assertEqual("WAG_HTTP_ERROR", raised.exception.category)
        self.assertEqual(1, raised.exception.attempts)
        self.assertNotIn("secret auth detail", str(raised.exception))
        self.assertEqual(1, open_request.call_count)
