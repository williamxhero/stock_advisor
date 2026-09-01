from __future__ import annotations

import json
import unittest
from unittest import mock

from ai_trading_companion.web_access_gateway import WebAccessGatewayClient, WebAccessGatewayError, _fact_as_of


class _Response:
    headers = {}

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


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
            "trace_id": "trace", "url": url, "markdown": markdown,
        })}]}}
        with mock.patch("ai_trading_companion.web_access_gateway.urlopen", return_value=_Response(response)):
            item = self.client.read(url, not_after="2026-08-27T07:20:00Z")["results"][0]
        self.assertEqual("2026-08-27T07:00:00Z", item["fact_as_of"])
        self.assertIn('"close":"3956.570"', item["excerpt_text"])
        self.assertNotIn("future-value", item["excerpt_text"])
        self.assertFalse(item["primary"])

    def test_tencent_intraday_quote_exposes_compact_quote_time_and_index_values(self) -> None:
        url = "https://qt.gtimg.cn/q=sh000001,sz399001,sz399006"
        markdown = (
            'v\\_sh000001="1~name~000001~3945.53~3952.18~3926.53' + '~0' * 24
            + '~20260831131054~-6.65~-0.17"; '
            'v\\_sz399001="51~name~399001~13823.92~13953.07~13764.41' + '~0' * 24
            + '~20260831131054~-129.15~-0.93";'
        )
        response = {"jsonrpc": "2.0", "result": {"content": [{"type": "text", "text": json.dumps({
            "trace_id": "trace", "url": url, "markdown": markdown,
        })}]}}
        with mock.patch("ai_trading_companion.web_access_gateway.urlopen", return_value=_Response(response)):
            item = self.client.read(url, not_after="2026-08-31T05:11:00Z")["results"][0]
        self.assertEqual("2026-08-31T05:10:54Z", item["fact_as_of"])
        self.assertIn('"symbol":"sh000001"', item["excerpt_text"])
        self.assertIn('"current":"3945.53"', item["excerpt_text"])
        self.assertFalse(item["primary"])

    def test_tencent_intraday_minute_read_selects_latest_row_before_frozen_cutoff(self) -> None:
        url = "https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=sh000001"
        markdown = (
            '{"data":{"sh000001":{"data":{"data":\\['
            '"1313 3943.00 100 200.00","1314 3945.53 120 240.00","1315 3950.00 130 260.00"'
            '\\],"date":"20260831"}}}}'
        )
        response = {"jsonrpc": "2.0", "result": {"content": [{"type": "text", "text": json.dumps({
            "trace_id": "trace", "url": url, "markdown": markdown,
        })}]}}
        with mock.patch("ai_trading_companion.web_access_gateway.urlopen", return_value=_Response(response)):
            item = self.client.read(url, not_after="2026-08-31T05:14:19Z")["results"][0]
        self.assertEqual("2026-08-31T05:14:00Z", item["fact_as_of"])
        self.assertIn('"price":"3945.53"', item["excerpt_text"])
        self.assertNotIn("3950.00", item["excerpt_text"])
        self.assertFalse(item["primary"])

    def test_tencent_intraday_minute_read_uses_frozen_date_when_truncated_payload_omits_date(self) -> None:
        url = "https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=sh000001"
        markdown = (
            r'{"data":{"sh000001":{"data":{"data":\['
            '"1429 3981.00 100 200.00","1430 3980.81 120 240.00","1431 3982.00 130 260.00"'
            r'\]}}}}'
        )
        response = {"jsonrpc": "2.0", "result": {"content": [{"type": "text", "text": json.dumps({
            "trace_id": "trace", "url": url, "markdown": markdown,
        })}]}}
        with mock.patch("ai_trading_companion.web_access_gateway.urlopen", return_value=_Response(response)):
            item = self.client.read(url, not_after="2026-09-01T06:30:04Z")["results"][0]
        self.assertEqual("2026-09-01T06:30:00Z", item["fact_as_of"])
        self.assertIn('"price":"3980.81"', item["excerpt_text"])
        self.assertNotIn("3982.00", item["excerpt_text"])
        self.assertFalse(item["primary"])

    def test_browser_rejects_any_mutating_action_before_network(self) -> None:
        with mock.patch("ai_trading_companion.web_access_gateway.urlopen") as open_request:
            with self.assertRaisesRegex(WebAccessGatewayError, "read-only"):
                self.client.browser(None, [{"type": "type", "text": "submit"}])
        open_request.assert_not_called()

    def test_browser_removes_schema_null_placeholders_before_gateway_call(self) -> None:
        response = {"jsonrpc": "2.0", "result": {"content": [{"type": "text", "text": json.dumps({
            "trace_id": "trace", "snapshot": "done",
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
