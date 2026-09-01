from __future__ import annotations

import unittest
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ai_trading_companion.local_research import BrokerResearchPlanner, LocalResearchChain, RESEARCH_PLAN_SCHEMA, ReadOnlyResearchExecutor, ToolCatalogMarketBackend, ToolCatalogResearchBackend, WebAccessGatewayBackend
from ai_trading_companion.tooling import EvidenceResolution, FactRequest, ToolCatalog, ToolRunner

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

    def test_planner_supplies_and_requires_public_intraday_quote_read(self) -> None:
        broker = mock.Mock(); broker.invoke.return_value = SimpleNamespace(result={"version": 1, "operations": []})
        planner = BrokerResearchPlanner(broker, intellect="smart", effort="medium", deadline=lambda: 123.0)
        packet = {
            "as_of": "2026-08-31T05:10:00Z",
            "evidence_contract": {
                "version": 3, "as_of": "2026-08-31T05:10:00Z", "requirements": [{
                    "key": "current_market_state", "blocking": True,
                    "window": {"mode": "range", "start": "2026-08-31T04:55:00Z", "end": "2026-08-31T05:10:00Z"},
                }],
            },
        }
        planner(packet, [], 0)
        request = broker.invoke.call_args.args[0]
        market_urls = [row["url"] for row in request.packet["research_discoveries"]]
        market_url = market_urls[0]
        self.assertEqual(3, len(market_urls))
        self.assertEqual("https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=sh000001", market_url)
        valid = {"version": 1, "operations": [
            {**row("web_read", url=market_url), "requirement_key": "current_market_state"},
        ]}
        self.assertTrue(request.verifier(valid)["passed"])
        result = request.verifier({"version": 1, "operations": [
            {**row("web_search", query="A股 盘中"), "requirement_key": "current_market_state"},
        ]})
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

    def test_plan_verifier_rejects_a_backend_that_is_not_actually_available(self) -> None:
        broker = mock.Mock(); broker.invoke.return_value = SimpleNamespace(result={"version": 1, "operations": []})
        planner = BrokerResearchPlanner(broker, intellect="smart", effort="medium", deadline=lambda: 123.0)
        planner({
            "as_of": CONTRACT["as_of"], "evidence_contract": CONTRACT,
            "allowed_research_backends": ["gateway", "market"],
        }, [], 0)
        request = broker.invoke.call_args.args[0]
        market_operation = {
            "requirement_key": "market", "backend": "market", "operation": "market_snapshot",
            "arguments": {"query": None, "categories": "市场价量", "url": None, "symbol": None,
                          "render": None, "session_id": None, "actions": None},
            "fallback_backends": ["gateway"],
        }

        result = request.verifier({"version": 1, "operations": [market_operation]})

        self.assertFalse(result["passed"])
        self.assertIn("research_plan_backend_unavailable:market", result["problems"])

    def test_gateway_adapter_exposes_only_read_operations(self) -> None:
        client = mock.Mock(); backend = WebAccessGatewayBackend(client, as_of=CONTRACT["as_of"])
        backend("web_search", row("web_search", query="收盘")["arguments"])
        backend("web_read", row("web_read", url="https://example.test")["arguments"])
        client.search.assert_called_once(); client.read.assert_called_once_with("https://example.test", "auto", CONTRACT["as_of"])
        with self.assertRaises(ValueError): backend("download", {})

    def test_tool_catalog_adapter_maps_research_reads_to_fact_requests_without_context(self) -> None:
        runner = mock.Mock()
        runner.resolve_with_fallback.side_effect = [
            EvidenceResolution(True, "generic_web_search", "1.0.0", CONTRACT["as_of"], CONTRACT["as_of"], {
                "url": "https://search.test", "results": [{"url": "https://example.test/story", "title": "story"}],
            }, "artifact:sha256:" + "a" * 64, None, ("tool_result_schema_valid",)),
            EvidenceResolution(True, "generic_web_read", "1.0.0", CONTRACT["as_of"], CONTRACT["as_of"], {
                "url": "https://example.test/story", "text": "verified source text",
            }, "artifact:sha256:" + "b" * 64, None, ("tool_result_schema_valid",)),
        ]
        backend = ToolCatalogResearchBackend(runner, as_of=CONTRACT["as_of"], deadline=lambda: 30.0)

        found = backend("web_search", row("web_search", query="close") ["arguments"])
        read = backend("web_read", row("web_read", url="https://example.test/story")["arguments"])

        self.assertEqual("https://example.test/story", found["results"][0]["url"])
        self.assertEqual("verified source text", read["results"][0]["excerpt_text"])
        first_request = runner.resolve_with_fallback.call_args_list[0].args[0]
        second_request = runner.resolve_with_fallback.call_args_list[1].args[0]
        self.assertEqual("generic_web_search", first_request.capability)
        self.assertEqual("generic_web_read", second_request.capability)
        self.assertEqual({}, first_request.context)
        self.assertEqual(CONTRACT["as_of"], second_request.required_at)

    def test_market_tool_adapter_freezes_a_live_intraday_snapshot_as_qualified_evidence(self) -> None:
        contract = {
            "version": 3, "as_of": "2026-09-01T06:30:00Z", "requirements": [{
                "key": "market", "blocking": True, "allowed_coverage": ["covered"],
                "window": {"mode": "after_start_to_end", "start": "2026-09-01T06:15:00Z", "end": "2026-09-01T06:30:00Z"},
            }],
        }
        runner = mock.Mock()
        runner.resolve_with_fallback.return_value = EvidenceResolution(
            True, "cn_market_snapshot", "1.1.0", "2026-09-01T06:29:00Z", "2026-09-01T06:30:01Z", {
                "source": "tencent_quote+eastmoney_breadth",
                "source_urls": ["https://qt.gtimg.cn/q=sh000001", "https://push2delay.eastmoney.com/api/qt/clist/get"],
                "source_evidence": [
                    {"url": "https://qt.gtimg.cn/q=sh000001", "fact_as_of": "2026-09-01T06:29:00Z", "data": {"indices": [{"symbol": "000001", "price": 3500}]}},
                    {"url": "https://push2delay.eastmoney.com/api/qt/clist/get", "fact_as_of": "2026-09-01T06:29:00Z", "data": {"breadth": {"up": 2000, "down": 1000, "flat": 50}}},
                ],
                "indices": [{"symbol": "000001", "price": 3500}],
                "breadth": {"up": 2000, "down": 1000, "flat": 50},
                "finality": "intraday",
            }, "artifact:sha256:" + "c" * 64, None, ("tool_result_schema_valid",), attempts=("default:succeeded",),
        )
        backend = ToolCatalogMarketBackend(runner, contract=contract, deadline=lambda: 10.0)
        plan = {"version": 1, "operations": [{
            "requirement_key": "market", "backend": "market", "operation": "market_snapshot",
            "arguments": {"query": None, "categories": None, "url": None, "symbol": None, "render": None, "session_id": None, "actions": None},
            "fallback_backends": [],
        }]}

        result = LocalResearchChain(lambda *_: plan, ReadOnlyResearchExecutor({"market": backend}), max_repairs=0).run(
            {"as_of": contract["as_of"]}, contract, attempt_id="live-market",
        )

        self.assertTrue(result.qualified, result.verifier["problems"])
        request = runner.resolve_with_fallback.call_args.args[0]
        self.assertEqual("cn_market_snapshot", request.capability)
        self.assertEqual("intraday", request.finality)
        self.assertEqual("2026-09-01T06:30:00Z", request.required_at)
        self.assertEqual(2, len(result.evidence["sources"]))
        self.assertIn("indices", result.evidence["sources"][0]["excerpt"])
        self.assertNotIn("breadth", result.evidence["sources"][0]["excerpt"])
        self.assertIn("breadth", result.evidence["sources"][1]["excerpt"])

    def test_post_close_research_uses_tool_fallback_then_freezes_qualified_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            default = root / "generic_web_read" / "versions" / "1.0.0"; default.mkdir(parents=True)
            (default / "tool.py").write_text("import sys; sys.exit(75)\n", encoding="utf-8")
            (default / "manifest.json").write_text(json.dumps({"contract": "ai-trading-tool-manifest/v1", "capability": "generic_web_read", "version": "1.0.0", "state": "promoted", "command": [sys.executable, "tool.py"]}), encoding="utf-8")
            (root / "generic_web_read" / "current.json").write_text(json.dumps({"contract": "ai-trading-tool-current/v1", "version": "1.0.0"}), encoding="utf-8")
            backup = root / "generic_web_read" / "adapters" / "backup" / "versions" / "1.0.0"; backup.mkdir(parents=True)
            (backup / "tool.py").write_text("""import json
print(json.dumps({'contract':'ai-trading-tool-result/v1','fact_as_of':'2026-08-27T07:00:00Z','data':{'url':'https://public.example.test/close','text':'official close evidence'}}))
""", encoding="utf-8")
            (backup / "manifest.json").write_text(json.dumps({"contract": "ai-trading-tool-manifest/v1", "capability": "generic_web_read", "version": "1.0.0", "state": "promoted", "command": [sys.executable, "tool.py"]}), encoding="utf-8")
            (root / "generic_web_read" / "routing.json").write_text(json.dumps({"contract": "ai-trading-tool-routing/v1", "candidates": [{"adapter": "default", "version": "1.0.0"}, {"adapter": "backup", "version": "1.0.0"}]}), encoding="utf-8")
            backend = ToolCatalogResearchBackend(ToolRunner(ToolCatalog(root)), as_of=CONTRACT["as_of"], deadline=lambda: 2)
            plan = {"version": 1, "operations": [row("web_read", url="https://public.example.test/close")]}

            result = LocalResearchChain(lambda *_: plan, ReadOnlyResearchExecutor({"gateway": backend}), max_repairs=0).run({"as_of": CONTRACT["as_of"]}, CONTRACT, attempt_id="post-close")

            self.assertTrue(result.qualified, result.verifier["problems"])
            self.assertEqual("official close evidence", result.evidence["sources"][0]["excerpt"])
            self.assertIn("backup:succeeded", (root / ".audit" / "resolutions.ndjson").read_text(encoding="utf-8"))

    def test_post_close_all_tool_sources_fail_returns_unqualified_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"; version = root / "generic_web_read" / "versions" / "1.0.0"; version.mkdir(parents=True)
            (version / "tool.py").write_text("import sys; sys.exit(75)\n", encoding="utf-8")
            (version / "manifest.json").write_text(json.dumps({"contract": "ai-trading-tool-manifest/v1", "capability": "generic_web_read", "version": "1.0.0", "state": "promoted", "command": [sys.executable, "tool.py"]}), encoding="utf-8")
            (root / "generic_web_read" / "current.json").write_text(json.dumps({"contract": "ai-trading-tool-current/v1", "version": "1.0.0"}), encoding="utf-8")
            backend = ToolCatalogResearchBackend(ToolRunner(ToolCatalog(root)), as_of=CONTRACT["as_of"], deadline=lambda: 2)
            plan = {"version": 1, "operations": [row("web_read", url="https://public.example.test/close")]}

            result = LocalResearchChain(lambda *_: plan, ReadOnlyResearchExecutor({"gateway": backend}), max_repairs=0).run({"as_of": CONTRACT["as_of"]}, CONTRACT, attempt_id="post-close")

            self.assertFalse(result.qualified)
            self.assertEqual("evidence_insufficient", result.stage_failures[0]["category"])

    def test_search_listing_is_discovery_not_evidence(self) -> None:
        search = {"results": [{"url": "https://example.test/2026-08-27", "title": "收盘", "excerpt_text": "2026-08-27", "fact_as_of": "2026-08-27T07:00:00Z"}]}
        plan = {"version": 1, "operations": [row("web_search", query="收盘")]}
        result = LocalResearchChain(lambda *_: plan, ReadOnlyResearchExecutor({"gateway": lambda *_: search}), max_repairs=0).run({"as_of": CONTRACT["as_of"]}, CONTRACT, attempt_id="x")
        self.assertFalse(result.qualified); self.assertEqual([], result.evidence["sources"])

    def test_successful_negative_event_query_can_record_checked_no_change_without_promoting_search_results(self) -> None:
        contract = {
            **CONTRACT,
            "requirements": [
                *CONTRACT["requirements"],
                {
                    "key": "events", "blocking": True,
                    "allowed_coverage": ["covered", "checked_no_change"],
                    "window": {
                        "mode": "after_start_to_end",
                        "start": "2026-08-27T06:00:00Z", "end": CONTRACT["as_of"],
                    },
                    "negative_query_terms": ["公告", "政策", "风险"],
                },
            ],
        }
        market_read = {
            "results": [{
                "url": "https://example.test/market", "title": "收盘", "excerpt_text": "收盘事实",
                "fact_as_of": CONTRACT["as_of"], "primary": True,
            }],
        }
        plan = {"version": 1, "operations": [
            row("web_read", url="https://example.test/market"),
            {**row("web_search", query="A股 公告 政策 风险"), "requirement_key": "events"},
        ]}

        def backend(operation: str, _arguments: dict) -> dict:
            return {"results": []} if operation == "web_search" else market_read

        result = LocalResearchChain(
            lambda *_: plan, ReadOnlyResearchExecutor({"gateway": backend}), max_repairs=0,
        ).run({"as_of": CONTRACT["as_of"]}, contract, attempt_id="x")

        self.assertTrue(result.qualified, result.verifier["problems"])
        events = next(row for row in result.evidence["coverage"] if row["requirement_key"] == "events")
        self.assertEqual("checked_no_change", events["status"])
        self.assertEqual([], events["evidence_refs"])
        self.assertEqual(1, len(result.evidence["sources"]))

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
