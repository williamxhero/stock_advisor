from __future__ import annotations

import unittest
import json
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ai_trading_companion.broker_client import BrokerError
from ai_trading_companion.local_research import BrokerResearchPlanner, LocalResearchChain, RESEARCH_PLAN_SCHEMA, ReadOnlyResearchExecutor, ToolCatalogMarketBackend, ToolCatalogResearchBackend, ToolResolutionError, WebAccessGatewayBackend, _merge_mandatory_operations
from ai_trading_companion.market_breadth_cache import MarketBreadthSnapshotCache
from ai_trading_companion.tooling import EvidenceResolution, FactRequest, ToolCatalog, ToolRunner

CONTRACT = {"version": 3, "as_of": "2026-08-27T07:00:00Z", "requirements": [{"key": "market", "blocking": True, "allowed_coverage": ["covered"], "window": {"mode": "exact", "start": "2026-08-27T07:00:00Z", "end": "2026-08-27T07:00:00Z"}}]}

def row(operation: str, *, query: str | None = None, url: str | None = None) -> dict:
    return {"requirement_key": "market", "backend": "gateway", "operation": operation, "arguments": {"query": query, "categories": "news", "url": url, "symbol": None, "render": "auto", "session_id": None, "actions": None}, "fallback_backends": []}

class LocalResearchTests(unittest.TestCase):
    def test_circuit_broken_current_bar_is_not_reinserted_by_repair(self) -> None:
        contract = {
            "version": 4, "requirements": [{
                "key": "portfolio_current_bar", "blocking": True, "required_entities": ["600487"],
            }],
        }
        plan = _merge_mandatory_operations(
            {"version": 1, "operations": []}, contract, max_operations=24,
            observations=[{
                "status": "failed", "operation": "current_bar",
                "arguments": {"requirement_key": "portfolio_current_bar"},
                "tool_error_code": "tool_routes_exhausted_deterministic",
            }],
        )

        self.assertEqual([], plan["operations"])

    def test_invalid_broker_plan_uses_the_existing_bounded_repair_round(self) -> None:
        calls = 0
        received_gaps: list[list[str]] = []

        def planner(_packet: dict, gaps: list[str], _round_number: int) -> dict:
            nonlocal calls
            calls += 1
            received_gaps.append(list(gaps))
            if calls == 1:
                raise BrokerError(
                    "invalid plan",
                    category="broker_output_invalid",
                    verifier={
                        "passed": False,
                        "business": {
                            "passed": False,
                            "problems": ["research_plan_missing_requirement:market"],
                        },
                    },
                )
            return {"version": 1, "operations": [row("web_read", url="https://example.test/close")]}

        backend = lambda *_: {"results": [{
            "url": "https://example.test/close", "title": "close", "excerpt_text": "close",
            "fact_as_of": CONTRACT["as_of"], "primary": True,
        }]}
        result = LocalResearchChain(
            planner, ReadOnlyResearchExecutor({"gateway": backend}), max_repairs=1,
        ).run({"as_of": CONTRACT["as_of"]}, CONTRACT, attempt_id="repair-plan")

        self.assertTrue(result.qualified)
        self.assertEqual(2, calls)
        self.assertEqual(
            ["research_plan_missing_requirement:market"],
            received_gaps[1],
        )

    def test_successful_mandatory_operations_are_not_repeated_on_repair(self) -> None:
        """A repair may add missing evidence, but must not re-fetch frozen facts."""
        contract = {
            "version": 4, "as_of": CONTRACT["as_of"], "requirements": [
                {"key": "current_market_state", "blocking": True},
                {"key": "market_breadth", "blocking": True},
                {"key": "portfolio_market_state", "blocking": True, "required_entities": ["600487"]},
                {"key": "portfolio_events_and_counterevidence", "blocking": True,
                 "required_entities": ["600487"], "negative_query_terms": ["公告", "停复牌", "财报", "风险"]},
            ],
        }
        calls: list[tuple[str, str, str | None]] = []

        def backend(operation: str, arguments: dict) -> dict:
            calls.append((str(arguments["_requirement_key"]), operation, arguments.get("query")))
            return {"results": [{"url": "https://example.test/fact", "title": "fact", "excerpt_text": "fact",
                                 "fact_as_of": CONTRACT["as_of"], "primary": True}]}

        class AlwaysMissing:
            def evaluate(self, *_args, **_kwargs):
                return {"passed": False, "problems": ["needs_repair"], "missing_requirements": ["needs_repair"]}

        result = LocalResearchChain(
            lambda *_args: {"version": 1, "operations": []},
            ReadOnlyResearchExecutor({"market": backend, "gateway": backend}),
            gate=AlwaysMissing(), max_repairs=1,
        ).run({"as_of": CONTRACT["as_of"]}, contract, attempt_id="no-repeat")

        self.assertFalse(result.qualified)
        self.assertEqual(4, len(calls))
        self.assertEqual(
            {
                ("current_market_state", "market_snapshot"),
                ("market_breadth", "market_breadth"),
                ("portfolio_market_state", "holding_snapshot"),
                ("portfolio_events_and_counterevidence", "web_search"),
            },
            {(key, operation) for key, operation, _query in calls},
        )

    def test_transient_broker_failure_reuses_qualified_mandatory_facts(self) -> None:
        """A retry must not re-read a fact whose frozen observation is valid."""
        contract = {
            "version": 4, "as_of": CONTRACT["as_of"], "requirements": [
                {"key": "market_breadth", "blocking": True},
            ],
        }
        calls = 0
        breadth_reads = 0

        def planner(_packet: dict, _gaps: list[str], _round: int) -> dict:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise BrokerError("route has no outcome", category="broker_protocol")
            return {"version": 1, "operations": []}

        def market(operation: str, _arguments: dict) -> dict:
            nonlocal breadth_reads
            self.assertEqual("market_breadth", operation)
            breadth_reads += 1
            return {"results": [{
                "url": "https://example.test/breadth", "title": "breadth", "excerpt_text": "breadth",
                "fact_as_of": CONTRACT["as_of"], "primary": True,
            }]}

        class Gate:
            calls = 0

            def evaluate(self, *_args, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    return {"passed": False, "problems": ["needs_planner"], "missing_requirements": ["needs_planner"]}
                return {"passed": True, "problems": [], "missing_requirements": []}

        result = LocalResearchChain(
            planner, ReadOnlyResearchExecutor({"market": market}), gate=Gate(), max_repairs=1,
        ).run({"as_of": CONTRACT["as_of"]}, contract, attempt_id="broker-retry")

        self.assertTrue(result.qualified)
        self.assertEqual(2, calls)
        self.assertEqual(1, breadth_reads)

    def test_mandatory_operations_finish_before_broker_planning(self) -> None:
        contract = {
            "version": 4, "as_of": CONTRACT["as_of"], "requirements": [
                {"key": "current_market_state", "blocking": True},
                {"key": "market_breadth", "blocking": True},
                {"key": "portfolio_market_state", "blocking": True, "required_entities": ["600487"]},
                {"key": "portfolio_events_and_counterevidence", "blocking": True,
                 "required_entities": ["600487"], "negative_query_terms": ["公告", "停复牌", "财报", "风险"]},
            ],
        }
        order: list[str] = []

        def backend(operation: str, arguments: dict) -> dict:
            order.append(f"tool:{operation}")
            return {"results": [{"url": "https://example.test/fact", "title": "fact", "excerpt_text": "fact",
                                 "fact_as_of": CONTRACT["as_of"], "primary": True}]}

        class AlwaysMissing:
            def evaluate(self, *_args, **_kwargs):
                return {"passed": False, "problems": ["needs_repair"], "missing_requirements": ["needs_repair"]}

        def planner(*_args):
            order.append("planner")
            return {"version": 1, "operations": []}

        LocalResearchChain(
            planner, ReadOnlyResearchExecutor({"market": backend, "gateway": backend}),
            gate=AlwaysMissing(), max_repairs=0,
        ).run({"as_of": CONTRACT["as_of"]}, contract, attempt_id="preflight")

        self.assertEqual("planner", order[-1])
        self.assertEqual(4, len([item for item in order if item.startswith("tool:")]))

    def test_planner_requires_an_explicit_effort_decision(self) -> None:
        with self.assertRaises(TypeError):
            BrokerResearchPlanner(mock.Mock(), deadline=lambda: 123.0)

    def test_planner_repair_reads_existing_discoveries_without_another_broker_call(self) -> None:
        broker = mock.Mock()
        planner = BrokerResearchPlanner(
            broker, intellect="smart", effort="medium", deadline=lambda: 123.0,
        )
        packet = {
            "as_of": CONTRACT["as_of"],
            "evidence_contract": {
                **CONTRACT,
                "requirements": [
                    *CONTRACT["requirements"],
                    {
                        "key": "events",
                        "blocking": True,
                        "allowed_coverage": ["covered", "checked_no_change"],
                        "window": {"mode": "after_start_to_end", "start": "2026-08-26T07:00:00Z", "end": CONTRACT["as_of"]},
                    },
                ],
            },
            "research_discoveries": [
                {"requirement_key": "events", "url": f"https://example.test/event-{index}"}
                for index in range(6)
            ],
        }

        plan = planner(packet, ["events"], 1)

        broker.invoke.assert_not_called()
        self.assertEqual(4, len(plan["operations"]))
        self.assertEqual({"web_read"}, {item["operation"] for item in plan["operations"]})
        self.assertEqual({"events"}, {item["requirement_key"] for item in plan["operations"]})

    def test_planner_repair_never_reads_an_attempted_candidate_url_again(self) -> None:
        broker = mock.Mock()
        planner = BrokerResearchPlanner(
            broker, intellect="smart", effort="medium", deadline=lambda: 123.0,
        )
        attempted = "https://example.test/already-read"
        packet = {
            "as_of": CONTRACT["as_of"],
            "evidence_contract": {
                **CONTRACT,
                "requirements": [
                    *CONTRACT["requirements"],
                    {"key": "events", "blocking": True},
                ],
            },
            "research_discoveries": [
                {"requirement_key": "events", "url": attempted},
                {"requirement_key": "events", "url": "https://example.test/untried"},
            ],
            "attempted_research_urls": [attempted],
        }

        plan = planner(packet, ["events"], 1)

        broker.invoke.assert_not_called()
        self.assertEqual(
            ["https://example.test/untried"],
            [item["arguments"]["url"] for item in plan["operations"]],
        )

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

    def test_planner_schema_only_allows_exact_contract_requirement_keys(self) -> None:
        broker = mock.Mock(); broker.invoke.return_value = SimpleNamespace(result={"version": 1, "operations": []})
        contract = {**CONTRACT, "requirements": [
            *CONTRACT["requirements"],
            {"key": "events", "blocking": True, "window": CONTRACT["requirements"][0]["window"]},
        ]}
        planner = BrokerResearchPlanner(broker, intellect="smart", effort="medium", deadline=lambda: 123.0)

        planner({"as_of": CONTRACT["as_of"], "evidence_contract": contract}, [], 0)

        request = broker.invoke.call_args.args[0]
        requirement_schema = request.schema["properties"]["operations"]["items"]["properties"]["requirement_key"]
        self.assertEqual(["events", "market"], requirement_schema["enum"])
        self.assertIn("Copy requirement_key exactly", request.packet["instruction"])

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

    def test_plan_verifier_rejects_operations_with_missing_required_arguments(self) -> None:
        broker = mock.Mock(); broker.invoke.return_value = SimpleNamespace(result={"version": 1, "operations": []})
        planner = BrokerResearchPlanner(broker, intellect="smart", effort="medium", deadline=lambda: 123.0)
        planner({"as_of": CONTRACT["as_of"], "evidence_contract": CONTRACT}, [], 0)
        request = broker.invoke.call_args.args[0]

        result = request.verifier({"version": 1, "operations": [row("web_search", query="")]})

        self.assertFalse(result["passed"])
        self.assertIn(
            "research_plan_operation_argument_missing:market:web_search:query",
            result["problems"],
        )

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
        backend = ToolCatalogResearchBackend(runner, as_of="2026-08-27T08:00:00Z", deadline=lambda: 30.0,
                                             contract=CONTRACT)

        found = backend("web_search", {**row("web_search", query="close")["arguments"], "_requirement_key": "market"})
        read = backend("web_read", {**row("web_read", url="https://example.test/story")["arguments"], "_requirement_key": "market"})

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
            True, "cn_market_index_batch", "1.1.3", "2026-09-01T06:29:00Z", "2026-09-01T06:30:01Z", {
                "source": "tencent_minute",
                "source_urls": ["https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=sh000001"],
                "source_evidence": [
                    {"url": "https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=sh000001", "fact_as_of": "2026-09-01T06:29:00Z", "data": {"indices": [{"symbol": "000001", "price": 3500}]}},
                ],
                "indices": [{"symbol": "000001", "price": 3500}],
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
        self.assertEqual("cn_market_index_batch", request.capability)
        self.assertEqual("intraday", request.finality)
        self.assertEqual("2026-09-01T06:30:00Z", request.required_at)
        self.assertEqual(1, len(result.evidence["sources"]))
        self.assertIn("indices", result.evidence["sources"][0]["excerpt"])

    def test_holding_snapshot_uses_only_contract_frozen_symbols(self) -> None:
        contract = {
            "version": 4, "as_of": "2026-09-01T01:45:00Z",
            "requirements": [{
                "key": "portfolio_market_state", "blocking": True,
                "allowed_coverage": ["covered"], "required_entities": ["600487", "603861"],
                "window": {"mode": "after_start_to_end", "start": "2026-09-01T01:30:00Z", "end": "2026-09-01T01:45:00Z"},
            }],
        }
        runner = mock.Mock()
        runner.resolve_with_fallback.return_value = EvidenceResolution(
            True, "cn_equity_quote_batch", "1.1.3", "2026-09-01T01:45:00Z", "2026-09-01T01:45:01Z", {
                "source": "tencent_minute", "finality": "intraday",
                "source_evidence": [{
                    "url": "https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=sh600487",
                    "fact_as_of": "2026-09-01T01:45:00Z",
                    "data": {"quotes": [{"symbol": "600487", "price": 66.06, "previous_close": 67.34, "status": "trading"}]},
                }],
            }, "artifact:sha256:" + "d" * 64, None, (), attempts=("default:succeeded",),
        )

        ToolCatalogMarketBackend(runner, contract=contract, deadline=lambda: 10.0)("holding_snapshot", {
            "_requirement_key": "portfolio_market_state", "symbol": "000001",
        })

        request = runner.resolve_with_fallback.call_args.args[0]
        self.assertEqual("cn_equity_quote_batch", request.capability)
        self.assertEqual(["600487", "603861"], request.inputs["symbols"])
        self.assertEqual("2026-09-01T01:45:00Z", request.required_at)

    def test_exact_close_market_tools_fall_back_to_strict_daily_ledger_evidence(self) -> None:
        close = "2026-09-03T07:00:00Z"
        as_of = "2026-09-03T07:20:00Z"
        contracts = {
            "indices_close": {
                "operation": "market_snapshot", "capability": "cn_market_index_batch",
                "entities": ["000001", "399001", "399006"], "field": "indices",
            },
            "portfolio_market_state": {
                "operation": "holding_snapshot", "capability": "cn_equity_quote_batch",
                "entities": ["300378", "300421", "603861"], "field": "quotes",
            },
        }
        rows = {
            "indices_close": [
                {"symbol": "000001", "name": "上证指数", "exchange": "SSE", "source": "ledger",
                 "price": 3942.09, "previous_close": 3941.39, "change": 0.7, "change_percent": 0.0178,
                 "quote_at": close, "trading_date": "2026-09-03", "status": "closed"},
                {"symbol": "399001", "name": "深证成指", "exchange": "SZSE", "source": "ledger",
                 "price": 13625.12, "previous_close": 13611.55, "change": 13.57, "change_percent": 0.0997,
                 "quote_at": close, "trading_date": "2026-09-03", "status": "closed"},
                {"symbol": "399006", "name": "创业板指", "exchange": "SZSE", "source": "ledger",
                 "price": 3312.54, "previous_close": 3312.24, "change": 0.3, "change_percent": 0.0091,
                 "quote_at": close, "trading_date": "2026-09-03", "status": "closed"},
            ],
            "portfolio_market_state": [
                {"symbol": "300378", "name": "鼎捷数智", "market": "CN-A", "exchange": "SZSE", "source": "ledger",
                 "price": 40.0, "previous_close": 39.0, "change": 1.0, "change_percent": 2.5641,
                 "quote_at": close, "trading_date": "2026-09-03", "status": "closed"},
                {"symbol": "300421", "name": "力星股份", "market": "CN-A", "exchange": "SZSE", "source": "ledger",
                 "price": 20.0, "previous_close": 20.0, "change": 0.0, "change_percent": 0.0,
                 "quote_at": close, "trading_date": "2026-09-03", "status": "closed"},
                {"symbol": "603861", "name": "白云电器", "market": "CN-A", "exchange": "SSE", "source": "ledger",
                 "price": 10.0, "previous_close": 12.0, "change": -2.0, "change_percent": -16.6667,
                 "quote_at": close, "trading_date": "2026-09-03", "status": "closed"},
            ],
        }
        ledger = [
            {
                "title": f"verified {key} {item['symbol']}",
                "url": f"https://example.test/{key}/{item['symbol']}",
                "known_at": "2026-09-03T07:10:00Z", "coverage_state": "observed",
                "text": json.dumps({"finality": "official_close", spec["field"]: [item]}, ensure_ascii=False),
            }
            for key, spec in contracts.items() for item in rows[key]
        ]
        ledger.append({
            "title": "verified market breadth", "url": "https://example.test/market-breadth",
            "known_at": "2026-09-03T07:10:00Z", "coverage_state": "observed",
            "text": json.dumps({
                "trading_date": "2026-09-03", "finality": "official_close",
                "breadth": {"up": 2218, "down": 2827, "flat": 166, "limit_up": 51, "limit_down": 8},
            }, ensure_ascii=False),
        })
        runner = mock.Mock()
        runner.catalog.root = Path(tempfile.gettempdir()) / "missing-market-tools"
        runner.resolve_with_fallback.side_effect = lambda request: EvidenceResolution.failed(
            request.capability, "tool_process_failed",
        )

        for key, spec in contracts.items():
            contract = {
                "version": 4, "as_of": as_of, "requirements": [{
                    "key": key, "blocking": True, "allowed_coverage": ["covered"],
                    "required_entities": spec["entities"], "finality": "official_close",
                    "window": {"mode": "exact", "start": close, "end": close},
                }],
            }
            result = ToolCatalogMarketBackend(
                runner, contract=contract, deadline=lambda: 10.0, daily_ledger=ledger,
            )(spec["operation"], {"_requirement_key": key})

            self.assertEqual("daily_evidence_ledger", result["source"])
            self.assertIsNone(result["results"][0]["raw_artifact_ref"])
            payloads = [json.loads(item["excerpt_text"]) for item in result["results"]]
            self.assertEqual(set(spec["entities"]), {
                row["symbol"] for payload in payloads for row in payload[spec["field"]]
            })

        self.assertEqual(2, runner.resolve_with_fallback.call_count)

        runner.reset_mock()
        combined_contract = {
            "version": 4, "as_of": as_of, "requirements": [
                {
                    "key": key, "blocking": True, "allowed_coverage": ["covered"],
                    "required_entities": spec["entities"], "finality": "official_close",
                    "minimum_numeric_facts": 12 if key == "portfolio_market_state" else 3,
                    "window": {"mode": "exact", "start": close, "end": close},
                }
                for key, spec in contracts.items()
            ] + [{
                "key": "market_breadth", "blocking": True, "allowed_coverage": ["covered"],
                "finality": "official_close", "minimum_numeric_facts": 3,
                "window": {"mode": "exact", "start": close, "end": close},
            }],
        }
        backend = ToolCatalogMarketBackend(
            runner, contract=combined_contract, deadline=lambda: 10.0, daily_ledger=ledger,
        )
        self.assertEqual("daily_evidence_ledger", backend(
            "market_breadth", {"_requirement_key": "market_breadth"},
        )["source"])
        runner.reset_mock()
        research = LocalResearchChain(
            lambda *_: {"version": 1, "operations": []},
            ReadOnlyResearchExecutor({"market": backend}), max_repairs=0,
        ).run({"as_of": as_of}, combined_contract, attempt_id="ledger-close")

        self.assertTrue(research.qualified, (research.verifier["problems"], research.observations))
        self.assertEqual(3, runner.resolve_with_fallback.call_count)

    def test_exact_close_ledger_fallback_rejects_any_contract_downgrade(self) -> None:
        close = "2026-09-03T07:00:00Z"
        contract = {
            "version": 4, "as_of": "2026-09-03T07:20:00Z", "requirements": [{
                "key": "indices_close", "blocking": True, "allowed_coverage": ["covered"],
                "finality": "official_close",
                "window": {"mode": "exact", "start": close, "end": close},
            }],
        }

        def valid_ledger() -> list[dict]:
            values = [
                ("000001", "上证指数", "SSE", 3942.09, 3941.39, 0.7, 0.0178),
                ("399001", "深证成指", "SZSE", 13625.12, 13611.55, 13.57, 0.0997),
                ("399006", "创业板指", "SZSE", 3312.54, 3312.24, 0.3, 0.0091),
            ]
            return [{
                "title": name, "url": f"https://example.test/{symbol}",
                "known_at": "2026-09-03T07:10:00Z", "coverage_state": "observed",
                "text": json.dumps({"finality": "official_close", "indices": [{
                    "symbol": symbol, "name": name, "exchange": exchange, "source": "ledger",
                    "price": price, "previous_close": previous, "change": change,
                    "change_percent": percent, "quote_at": close,
                    "trading_date": "2026-09-03", "status": "closed",
                }]}, ensure_ascii=False),
            } for symbol, name, exchange, price, previous, change, percent in values]

        cases = {}
        wrong_time = valid_ledger()
        payload = json.loads(wrong_time[0]["text"]); payload["indices"][0]["quote_at"] = "2026-09-03T06:59:00Z"
        wrong_time[0]["text"] = json.dumps(payload, ensure_ascii=False); cases["wrong time"] = wrong_time
        wrong_finality = valid_ledger()
        payload = json.loads(wrong_finality[0]["text"]); payload["finality"] = "intraday"
        wrong_finality[0]["text"] = json.dumps(payload, ensure_ascii=False); cases["wrong finality"] = wrong_finality
        cases["missing entity"] = valid_ledger()[:-1]
        bad_numeric = valid_ledger()
        payload = json.loads(bad_numeric[0]["text"]); payload["indices"][0]["change_percent"] = "unknown"
        bad_numeric[0]["text"] = json.dumps(payload, ensure_ascii=False); cases["bad numeric"] = bad_numeric
        private_url = valid_ledger(); private_url[0]["url"] = "file:///trusted-looking.json"; cases["non-public url"] = private_url
        future_known = valid_ledger(); future_known[0]["known_at"] = "2026-09-03T07:21:00Z"; cases["future known"] = future_known
        conflicting = valid_ledger()
        conflict = json.loads(json.dumps(conflicting[0], ensure_ascii=False))
        payload = json.loads(conflict["text"]); payload["indices"][0]["price"] = 1.0
        conflict["text"] = json.dumps(payload, ensure_ascii=False); conflict["url"] += "?conflict=1"
        conflicting.append(conflict); cases["conflicting duplicate"] = conflicting

        for label, ledger in cases.items():
            with self.subTest(label):
                runner = mock.Mock()
                runner.resolve_with_fallback.return_value = EvidenceResolution.failed(
                    "cn_market_index_batch", "tool_process_failed",
                )
                backend = ToolCatalogMarketBackend(
                    runner, contract=contract, deadline=lambda: 10.0, daily_ledger=ledger,
                )
                with self.assertRaises(ToolResolutionError):
                    backend("market_snapshot", {"_requirement_key": "indices_close"})
                runner.resolve_with_fallback.assert_called_once()

    def test_current_bar_uses_only_contract_frozen_symbols(self) -> None:
        contract = {
            "version": 4, "as_of": "2026-09-01T01:46:00Z",
            "requirements": [{
                "key": "portfolio_current_bar", "blocking": True,
                "allowed_coverage": ["covered"], "required_entities": ["600487", "603861"],
                "window": {"mode": "after_start_to_end", "start": "2026-09-01T01:41:00Z", "end": "2026-09-01T01:46:00Z"},
            }],
        }
        runner = mock.Mock()
        runner.resolve_with_fallback.return_value = EvidenceResolution(
            True, "cn_equity_current_bar", "1.1.5", "2026-09-01T01:45:30Z", "2026-09-01T01:45:31Z", {
                "source": "markethub_current_bar", "finality": "intraday",
                "source_evidence": [{
                    "url": "http://yosef-server:8803/api/stocks/quotes?datetime=now",
                    "fact_as_of": "2026-09-01T01:45:30Z",
                    "data": {"bars": [{"symbol": "600487", "close": 66.06}, {"symbol": "603861", "close": 19.11}]},
                }],
            }, "artifact:sha256:" + "e" * 64, None, (), attempts=("default:succeeded",),
        )

        ToolCatalogMarketBackend(runner, contract=contract, deadline=lambda: 10.0)("current_bar", {
            "_requirement_key": "portfolio_current_bar", "symbol": "000001",
        })

        request = runner.resolve_with_fallback.call_args.args[0]
        self.assertEqual("cn_equity_current_bar", request.capability)
        self.assertEqual(["600487", "603861"], request.inputs["symbols"])
        self.assertEqual("1m", request.inputs["freq"])
        self.assertEqual("2026-09-01T01:46:00Z", request.required_at)

    def test_post_close_breadth_keeps_official_close_finality_in_a_bounded_window(self) -> None:
        contract = {
            "version": 4, "as_of": "2026-09-01T07:20:00Z",
            "requirements": [{
                "key": "market_breadth", "blocking": True, "allowed_coverage": ["covered"],
                "finality": "official_close",
                "window": {"mode": "after_start_to_end", "start": "2026-09-01T07:00:00Z", "end": "2026-09-01T07:20:00Z"},
            }],
        }
        runner = mock.Mock()
        runner.catalog.root = Path(tempfile.gettempdir()) / "missing-market-tools"
        runner.resolve_with_fallback.return_value = EvidenceResolution(
            True, "cn_market_breadth", "1.1.3", "2026-09-01T07:15:00Z", "2026-09-01T07:15:01Z", {
                "source": "verified_breadth", "finality": "official_close",
                "source_urls": ["https://example.test/breadth"],
                "source_evidence": [{
                    "url": "https://example.test/breadth", "fact_as_of": "2026-09-01T07:15:00Z",
                    "data": {"breadth": {"up": 1, "down": 2, "flat": 3}, "finality": "official_close"},
                }],
            }, "artifact:sha256:" + "e" * 64, None, (), attempts=("default:succeeded",),
        )

        with tempfile.TemporaryDirectory() as home:
            runtime = Path(home) / "runtime"
            runtime.mkdir()
            (runtime / "market-breadth-snapshot.json").write_text(json.dumps({
                "fact_as_of": "2026-09-01T07:15:00Z",
                "data": {
                    "source": "intraday_prefetch", "finality": "intraday",
                    "source_urls": ["https://example.test/intraday"],
                    "breadth": {"up": 9, "down": 8, "flat": 7},
                },
            }), encoding="utf-8")
            with mock.patch.dict("os.environ", {"AI_TRADING_COMPANION_HOME": home}):
                ToolCatalogMarketBackend(runner, contract=contract, deadline=lambda: 10.0)(
                    "market_breadth", {"_requirement_key": "market_breadth"},
                )

        request = runner.resolve_with_fallback.call_args.args[0]
        self.assertEqual("official_close", request.finality)
        self.assertEqual("2026-09-01T07:20:00Z", request.required_at)

    def test_frozen_market_breadth_selects_latest_cached_snapshot_not_after_required_at(self) -> None:
        contract = {
            "version": 4, "as_of": "2026-09-04T14:30:05.749+08:00",
            "requirements": [{
                "key": "market_breadth", "blocking": True, "allowed_coverage": ["covered"],
                "finality": "intraday",
                "window": {
                    "mode": "after_start_to_end",
                    "start": "2026-09-04T14:15:05.749+08:00",
                    "end": "2026-09-04T14:30:05.749+08:00",
                },
            }],
        }
        runner = mock.Mock()
        runner.catalog.root = Path(tempfile.gettempdir()) / "missing-market-tools"
        runner.resolve_with_fallback.side_effect = AssertionError("frozen cache hit must not fetch live evidence")

        with tempfile.TemporaryDirectory() as home:
            runtime = Path(home) / "runtime"
            runtime.mkdir()
            (runtime / "market-breadth-snapshot.json").write_text(json.dumps({
                "contract": "ai-trading-market-breadth-cache/v1",
                "snapshots": [
                    {
                        "result_contract": "ai-trading-tool-result/v1",
                        "fact_as_of": "2026-09-04T14:29:39+08:00",
                        "acquired_at": "2026-09-04T14:29:40+08:00",
                        "data": {
                            "source": "eastmoney", "finality": "intraday",
                            "source_urls": ["https://example.test/breadth-before-freeze"],
                            "breadth": {"up": 3000, "down": 2000, "flat": 100},
                        },
                        "raw_artifact_ref": "artifact:sha256:" + "a" * 64,
                        "technical_validation": ["tool_process_succeeded", "tool_result_schema_valid", "raw_output_archived"],
                    },
                    {
                        "result_contract": "ai-trading-tool-result/v1",
                        "fact_as_of": "2026-09-04T14:30:10+08:00",
                        "acquired_at": "2026-09-04T14:30:11+08:00",
                        "data": {
                            "source": "eastmoney", "finality": "intraday",
                            "source_urls": ["https://example.test/breadth-after-freeze"],
                            "breadth": {"up": 3100, "down": 1900, "flat": 100},
                        },
                        "raw_artifact_ref": "artifact:sha256:" + "b" * 64,
                        "technical_validation": ["tool_process_succeeded", "tool_result_schema_valid", "raw_output_archived"],
                    },
                    {
                        "result_contract": "ai-trading-tool-result/v1",
                        "fact_as_of": "2026-09-04T14:29:50+08:00",
                        "acquired_at": "2026-09-04T14:30:06+08:00",
                        "data": {
                            "source": "eastmoney", "finality": "intraday",
                            "source_urls": ["https://example.test/breadth-learned-after-freeze"],
                            "breadth": {"up": 3050, "down": 1950, "flat": 100},
                        },
                        "raw_artifact_ref": "artifact:sha256:" + "c" * 64,
                        "technical_validation": ["tool_process_succeeded", "tool_result_schema_valid", "raw_output_archived"],
                    },
                ],
            }), encoding="utf-8")
            with mock.patch.dict("os.environ", {"AI_TRADING_COMPANION_HOME": home}):
                result = ToolCatalogMarketBackend(runner, contract=contract, deadline=lambda: 10.0)(
                    "market_breadth", {"_requirement_key": "market_breadth"},
                )

        self.assertEqual("https://example.test/breadth-before-freeze", result["url"])
        self.assertEqual("2026-09-04T14:29:39+08:00", result["results"][0]["fact_as_of"])
        runner.resolve_with_fallback.assert_not_called()

    def test_market_breadth_cache_is_bounded_and_selects_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market-breadth-snapshot.json"
            cache = MarketBreadthSnapshotCache(path, max_snapshots=2)
            for second in (30, 39, 50):
                cache.append({
                    "result_contract": "ai-trading-tool-result/v1",
                    "fact_as_of": f"2026-09-04T14:29:{second}+08:00",
                    "acquired_at": f"2026-09-04T14:29:{second + 1}+08:00",
                    "data": {
                        "source": "eastmoney", "finality": "intraday",
                        "source_urls": [f"https://example.test/{second}"],
                    },
                    "raw_artifact_ref": "artifact:sha256:" + str(second)[0] * 64,
                    "technical_validation": ["tool_process_succeeded", "tool_result_schema_valid", "raw_output_archived"],
                    "attempts": ["markethub:tool_process_failed", "eastmoney:succeeded"],
                })

            restarted = MarketBreadthSnapshotCache(path, max_snapshots=2)
            selected = restarted.select(
                required_at="2026-09-04T14:29:45+08:00",
                window_start="2026-09-04T14:15:00+08:00",
                finality="intraday",
            )

            self.assertEqual(2, len(restarted.snapshots()))
            self.assertIsNotNone(selected)
            self.assertEqual("2026-09-04T14:29:39+08:00", selected["fact_as_of"])

    def test_market_breadth_cache_publishes_complete_generations_during_concurrent_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = MarketBreadthSnapshotCache(Path(directory) / "market-breadth-snapshot.json", max_snapshots=8)

            def snapshot(index: int) -> dict:
                return {
                    "result_contract": "ai-trading-tool-result/v1",
                    "fact_as_of": f"2026-09-04T14:29:{index:02d}+08:00",
                    "acquired_at": f"2026-09-04T14:30:{index:02d}+08:00",
                    "data": {
                        "source": "eastmoney", "finality": "intraday",
                        "source_urls": [f"https://example.test/{index}"],
                    },
                }

            cache.append(snapshot(0))
            start = threading.Event()

            def write(index: int) -> None:
                start.wait()
                cache.append(snapshot(index))

            def read() -> None:
                start.wait()
                for _ in range(100):
                    rows = MarketBreadthSnapshotCache(cache.path, max_snapshots=8).snapshots()
                    self.assertGreaterEqual(len(rows), 1)
                    self.assertLessEqual(len(rows), 8)
                    self.assertTrue(all(row.get("result_contract") == "ai-trading-tool-result/v1" for row in rows))

            with ThreadPoolExecutor(max_workers=9) as pool:
                futures = [pool.submit(write, index) for index in range(1, 9)]
                futures.append(pool.submit(read))
                start.set()
                for future in futures:
                    future.result()

            self.assertEqual(8, len(cache.snapshots()))

    def test_premarket_uses_yesterday_official_close_breadth_snapshot(self) -> None:
        contract = {
            "version": 4, "as_of": "2026-09-03T00:30:05Z",
            "requirements": [{
                "key": "market_breadth", "blocking": True, "allowed_coverage": ["covered"],
                "finality": "official_close",
                "window": {"mode": "exact", "start": "2026-09-02T07:00:00Z", "end": "2026-09-02T07:00:00Z"},
            }],
        }
        runner = mock.Mock()
        runner.catalog.root = Path(tempfile.gettempdir()) / "missing-market-tools"

        with tempfile.TemporaryDirectory() as home:
            runtime = Path(home) / "runtime"
            runtime.mkdir()
            (runtime / "market-breadth-official-close-snapshot.json").write_text(json.dumps({
                "fact_as_of": "2026-09-02T07:00:00Z",
                "data": {
                    "source": "official_close_prefetch", "finality": "official_close",
                    "source_urls": ["https://example.test/close"],
                    "breadth": {"up": 9, "down": 8, "flat": 7},
                },
            }), encoding="utf-8")
            with mock.patch.dict("os.environ", {"AI_TRADING_COMPANION_HOME": home}):
                result = ToolCatalogMarketBackend(runner, contract=contract, deadline=lambda: 10.0)(
                    "market_breadth", {"_requirement_key": "market_breadth"},
                )

        runner.resolve_with_fallback.assert_not_called()
        self.assertEqual("https://example.test/close", result["url"])
        self.assertEqual("2026-09-02T07:00:00Z", result["results"][0]["fact_as_of"])

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

    def test_production_research_keeps_replanning_past_two_repairs_until_qualified(self) -> None:
        read = {"results": [{
            "url": "https://example.test/2026-08-27", "title": "收盘",
            "excerpt_text": "收盘事实", "fact_as_of": CONTRACT["as_of"], "primary": True,
        }]}
        rounds: list[int] = []

        def planner(_packet: dict, _gaps: list[str], round_number: int) -> dict:
            rounds.append(round_number)
            operation = row("web_read", url="https://example.test/2026-08-27") if round_number >= 3 else row("web_search", query=f"收盘 修复 {round_number}")
            return {"version": 1, "operations": [operation]}

        result = LocalResearchChain(
            planner,
            ReadOnlyResearchExecutor({"gateway": lambda operation, _arguments: read if operation == "web_read" else {"results": []}}),
            max_repairs=None,
            deadline=lambda: 30.0,
        ).run({"as_of": CONTRACT["as_of"]}, CONTRACT, attempt_id="continuous")

        self.assertTrue(result.qualified, result.verifier["problems"])
        self.assertEqual([0, 1, 2, 3], rounds)

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
        self.assertFalse(result.qualified)
        self.assertIn("blocking_requirement_missing:market", result.verifier["problems"])

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
