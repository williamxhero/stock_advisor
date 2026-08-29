from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from ai_trading_companion.local_research import BrokerResearchPlanner, LocalResearchChain, RESEARCH_PLAN_SCHEMA, ReadOnlyResearchExecutor, WebAccessGatewayBackend

CONTRACT = {"version": 3, "as_of": "2026-08-27T07:00:00Z", "requirements": [{"key": "market", "blocking": True, "allowed_coverage": ["covered"], "window": {"mode": "exact", "start": "2026-08-27T07:00:00Z", "end": "2026-08-27T07:00:00Z"}}]}

def row(operation: str, *, query: str | None = None, url: str | None = None) -> dict:
    return {"requirement_key": "market", "backend": "gateway", "operation": operation, "arguments": {"query": query, "categories": "news", "url": url, "symbol": None, "render": "auto", "session_id": None, "actions": None}, "fallback_backends": []}

class LocalResearchTests(unittest.TestCase):
    def test_planner_requires_an_explicit_effort_decision(self) -> None:
        with self.assertRaises(TypeError):
            BrokerResearchPlanner(mock.Mock(), deadline=lambda: 123.0)

    def test_browser_action_schema_is_strict_and_defines_array_items(self) -> None:
        actions = RESEARCH_PLAN_SCHEMA["properties"]["operations"]["items"]["properties"]["arguments"]["properties"]["actions"]
        self.assertEqual("object", actions["items"]["type"])
        self.assertFalse(actions["items"]["additionalProperties"])
        self.assertEqual(set(actions["items"]["properties"]), set(actions["items"]["required"]))

    def test_planner_is_strict_and_names_gateway_only(self) -> None:
        broker = mock.Mock(); broker.invoke.return_value = SimpleNamespace(result={"version": 1, "operations": []})
        planner = BrokerResearchPlanner(broker, intellect="smart", effort="medium", deadline=lambda: 123.0)
        planner({"as_of": CONTRACT["as_of"], "evidence_contract": CONTRACT, "public_research_scope": {
            "standing_questions": ["当前 A 股主要指数发生了什么变化"],
            "selected_memory": [{"text": "must not reach planner"}],
        }}, [], 0)
        request = broker.invoke.call_args.args[0]
        self.assertEqual(["gateway"], request.packet["available_backends"])
        self.assertEqual(["当前 A 股主要指数发生了什么变化"], request.packet["research_scope"]["standing_questions"])
        self.assertNotIn("selected_memory", request.packet["research_scope"])
        self.assertNotIn("tools", request.packet)
        self.assertTrue(request.verifier({"version": 1, "operations": [row("web_search", query="收盘")]})["passed"])

    def test_planner_converts_chinese_market_close_to_shanghai_time(self) -> None:
        broker = mock.Mock(); broker.invoke.return_value = SimpleNamespace(result={"version": 1, "operations": []})
        planner = BrokerResearchPlanner(broker, intellect="smart", effort="medium", deadline=lambda: 123.0)
        packet = {
            "as_of": "2026-08-27T07:20:00Z",
            "evidence_contract": {
                "version": 3, "as_of": "2026-08-27T07:20:00Z", "requirements": [{
                    "key": "current_market_state", "blocking": True,
                    "window": {"mode": "exact", "start": "2026-08-27T07:00:00Z", "end": "2026-08-27T07:00:00Z"},
                }],
            },
        }
        planner(packet, [], 0)
        request = broker.invoke.call_args.args[0]
        time_row = request.packet["market_time_context"]["requirements"][0]
        self.assertEqual("2026-08-27T15:00:00+08:00", time_row["start_local"])
        self.assertTrue(time_row["is_local_market_close"])
        market_url = request.packet["research_discoveries"][0]["url"]
        self.assertIn("web.ifzq.gtimg.cn", market_url)
        valid = {"version": 1, "operations": [
            {**row("web_search", query="2026年8月27日 15:00 A股收盘"), "requirement_key": "current_market_state"},
            {**row("web_read", url=market_url), "requirement_key": "current_market_state"},
        ]}
        self.assertTrue(request.verifier(valid)["passed"])
        invalid = {"version": 1, "operations": [{**row("web_search", query="2026年8月27日 07:00 A股早盘"), "requirement_key": "current_market_state"}]}
        result = request.verifier(invalid)
        self.assertFalse(result["passed"])
        self.assertIn("research_plan_market_close_query_uses_open_semantics", result["problems"])
        self.assertIn("research_plan_market_query_uses_utc_clock_as_local", result["problems"])
        self.assertIn("research_plan_missing_frozen_public_market_read", result["problems"])

    def test_plan_verifier_requires_every_blocking_requirement(self) -> None:
        broker = mock.Mock(); broker.invoke.return_value = SimpleNamespace(result={"version": 1, "operations": []})
        contract = {**CONTRACT, "requirements": [
            *CONTRACT["requirements"],
            {"key": "events", "blocking": True, "window": CONTRACT["requirements"][0]["window"]},
        ]}
        planner = BrokerResearchPlanner(broker, intellect="smart", effort="medium", deadline=lambda: 123.0)
        planner({"as_of": CONTRACT["as_of"], "evidence_contract": contract}, [], 0)
        request = broker.invoke.call_args.args[0]
        result = request.verifier({"version": 1, "operations": [row("web_search", query="收盘")]})
        self.assertFalse(result["passed"])
        self.assertIn("research_plan_missing_requirement:events", result["problems"])

    def test_gateway_adapter_exposes_only_read_operations(self) -> None:
        client = mock.Mock(); backend = WebAccessGatewayBackend(client, as_of=CONTRACT["as_of"])
        backend("web_search", row("web_search", query="收盘")["arguments"])
        backend("web_read", row("web_read", url="https://example.test")["arguments"])
        client.search.assert_called_once(); client.read.assert_called_once_with("https://example.test", "auto", CONTRACT["as_of"])
        with self.assertRaises(ValueError): backend("download", {})

    def test_search_listing_is_discovery_not_evidence(self) -> None:
        search = {"results": [{"url": "https://example.test/2026-08-27", "title": "收盘", "excerpt_text": "2026-08-27", "fact_as_of": "2026-08-27T07:00:00Z"}]}
        plan = {"version": 1, "operations": [row("web_search", query="收盘")]}
        result = LocalResearchChain(lambda *_: plan, ReadOnlyResearchExecutor({"gateway": lambda *_: search}), max_repairs=0).run({"as_of": CONTRACT["as_of"]}, CONTRACT, attempt_id="x")
        self.assertFalse(result.qualified); self.assertEqual([], result.evidence["sources"])

    def test_verified_read_can_cover_contract(self) -> None:
        read = {"results": [{"url": "https://example.test/2026-08-27", "title": "收盘", "excerpt_text": "收盘事实", "fact_as_of": "2026-08-27T07:00:00Z", "primary": True}]}
        plan = {"version": 1, "operations": [row("web_read", url="https://example.test/2026-08-27")]}
        result = LocalResearchChain(lambda *_: plan, ReadOnlyResearchExecutor({"gateway": lambda *_: read}), max_repairs=0).run({"as_of": CONTRACT["as_of"]}, CONTRACT, attempt_id="x")
        self.assertTrue(result.qualified)
        self.assertEqual("收盘事实", result.evidence["sources"][0]["excerpt"])

    def test_failed_planned_read_uses_an_untried_discovery_without_new_search(self) -> None:
        search = {"results": [
            {"url": "https://blocked.test/2026-08-27", "title": "blocked", "excerpt_text": "2026-08-27", "fact_as_of": CONTRACT["as_of"]},
            {"url": "https://readable.test/2026-08-27", "title": "readable", "excerpt_text": "2026-08-27", "fact_as_of": CONTRACT["as_of"]},
        ]}
        read = {"results": [{"url": "https://readable.test/2026-08-27", "title": "收盘", "excerpt_text": "收盘事实", "fact_as_of": CONTRACT["as_of"], "primary": True}]}
        plan = {"version": 1, "operations": [
            row("web_search", query="收盘"), row("web_read", url="https://blocked.test/2026-08-27"),
        ]}
        calls: list[tuple[str, str | None]] = []
        def backend(operation: str, arguments: dict) -> dict:
            calls.append((operation, arguments.get("url")))
            if operation == "web_search": return search
            if arguments.get("url") == "https://blocked.test/2026-08-27": raise RuntimeError("blocked")
            return read
        result = LocalResearchChain(lambda *_: plan, ReadOnlyResearchExecutor({"gateway": backend}), max_repairs=0).run({"as_of": CONTRACT["as_of"]}, CONTRACT, attempt_id="x")
        self.assertTrue(result.qualified)
        self.assertIn(("web_read", "https://readable.test/2026-08-27"), calls)

    def test_post_close_publication_is_bound_to_exact_close_fact_time(self) -> None:
        contract = {
            **CONTRACT, "as_of": "2026-08-27T07:20:00Z",
            "requirements": [{**CONTRACT["requirements"][0], "window": {
                "mode": "exact", "start": CONTRACT["as_of"], "end": CONTRACT["as_of"],
            }}],
        }
        read = {"results": [{
            "url": "https://example.test/2026-08-27", "title": "收盘快报",
            "excerpt_text": "2026年8月27日 15:18 发布：A股收盘事实", "fact_as_of": "2026-08-27T07:18:00Z",
            "published_at": "2026-08-27T07:18:00Z", "primary": True,
        }]}
        plan = {"version": 1, "operations": [row("web_read", url="https://example.test/2026-08-27")]}
        result = LocalResearchChain(lambda *_: plan, ReadOnlyResearchExecutor({"gateway": lambda *_: read}), max_repairs=0).run({"as_of": contract["as_of"]}, contract, attempt_id="x")
        self.assertTrue(result.qualified, result.verifier["problems"])
        self.assertEqual(CONTRACT["as_of"], result.evidence["sources"][0]["fact_as_of"])
        self.assertEqual("2026-08-27T07:18:00Z", result.evidence["sources"][0]["published_at"])

    def test_web_excerpt_with_credential_shape_is_rejected_at_acquisition_boundary(self) -> None:
        unsafe = {"results": [{
            "url": "https://example.test/2026-08-27", "title": "unsafe",
            "excerpt_text": "token: ABCDEFGHIJKLMNOPQRSTUVWXYZ123456", "fact_as_of": CONTRACT["as_of"],
        }]}
        plan = {"version": 1, "operations": [row("web_read", url="https://example.test/2026-08-27")]}
        result = LocalResearchChain(lambda *_: plan, ReadOnlyResearchExecutor({"gateway": lambda *_: unsafe}), max_repairs=0).run({"as_of": CONTRACT["as_of"]}, CONTRACT, attempt_id="x")
        self.assertFalse(result.qualified)
        self.assertEqual(1, result.observations[0]["secret_rejected_items"])
        self.assertNotIn("ABCDEFGHIJKLMNOPQRSTUVWXYZ123456", str(result.observations))
