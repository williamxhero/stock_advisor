from __future__ import annotations

import unittest
import hashlib
import json
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ai_trading_companion.broker_client import BrokerError
from ai_trading_companion.local_research import BrokerResearchPlanner, LocalResearchChain, RESEARCH_PLAN_SCHEMA, ReadOnlyResearchExecutor, ToolCatalogMarketBackend, ToolCatalogResearchBackend, ToolResolutionError, WebAccessGatewayBackend, _bounded_research_plan, _discovery_digest, _discovery_read_repair_plan, _merge_mandatory_operations, _verify_research_plan
from ai_trading_companion.market_breadth_cache import MarketBreadthSnapshotCache
from ai_trading_companion.store import CompanionStore
from ai_trading_companion.tooling import EvidenceResolution, FactRequest, ToolCatalog, ToolRunner

CONTRACT = {"version": 3, "as_of": "2026-08-27T07:00:00Z", "requirements": [{"key": "market", "blocking": True, "allowed_coverage": ["covered"], "window": {"mode": "exact", "start": "2026-08-27T07:00:00Z", "end": "2026-08-27T07:00:00Z"}}]}

def row(operation: str, *, query: str | None = None, url: str | None = None) -> dict:
    return {"requirement_key": "market", "backend": "gateway", "operation": operation, "arguments": {"query": query, "categories": "news", "url": url, "symbol": None, "render": "auto", "session_id": None, "actions": None}, "fallback_backends": []}

class LocalResearchTests(unittest.TestCase):
    def test_company_read_uses_article_time_instead_of_late_acquisition_time(self):
        as_of = "2026-09-07T05:30:55Z"
        contract = {"version": 4, "as_of": as_of, "requirements": [{
            "key": "candidate_business_research", "blocking": True,
            "allowed_coverage": ["covered"],
            "window": {
                "mode": "after_start_to_end",
                "start": "2025-08-03T05:30:55Z",
                "end": as_of,
            },
        }]}

        def backend(_operation, _arguments):
            return {"results": [{
                "url": "https://company.test/notice",
                "title": "神农集团2026年半年度报告",
                "excerpt_text": "神农集团生猪业务跟踪 2026-09-04 16:34 星期五 神农集团605296披露养殖成本和出栏量；新希望000876可作同业比较。",
                # This is when the read completed, not when the public fact existed.
                "fact_as_of": "2026-09-07T05:32:53Z",
            }]}

        plan = {"version": 1, "operations": [{
            **row("web_read", url="https://company.test/notice"),
            "requirement_key": "candidate_business_research",
        }]}
        result = LocalResearchChain(
            lambda *_: plan,
            ReadOnlyResearchExecutor({"gateway": backend}),
            max_repairs=0,
        ).run({"stage": "m0_research", "as_of": as_of}, contract, attempt_id="late-company-read")

        self.assertTrue(result.qualified, result.verifier)
        self.assertEqual("2026-09-04T08:34:00Z", result.evidence["sources"][0]["fact_as_of"])

    def test_company_read_does_not_backdate_an_unlabelled_business_date(self):
        result = self._run_late_company_read(
            "神农集团2026年半年度报告，报告期截至2026-06-30，披露养殖成本和出栏量。",
        )

        self.assertFalse(result.qualified)
        self.assertIn("candidate_business_research", result.verifier["missing_requirements"])

    def test_company_read_rejects_a_publication_time_after_the_frozen_as_of(self):
        result = self._run_late_company_read(
            "神农集团生猪业务跟踪 发布时间：2026-09-07 14:00，披露养殖成本和出栏量。",
        )

        self.assertFalse(result.qualified)
        self.assertIn("candidate_business_research", result.verifier["missing_requirements"])

    def test_company_read_uses_prior_official_pdf_path_date(self):
        result = self._run_late_company_read(
            "北京同有飞骥科技股份有限公司半年度报告，披露存储产品业务。",
            url="https://static.cninfo.com.cn/finalpage/2026-09-04/1225516182.PDF",
        )

        self.assertTrue(result.qualified, result.verifier)
        self.assertEqual("2026-09-03T16:00:00Z", result.evidence["sources"][0]["fact_as_of"])

    def test_company_read_uses_a_leading_article_publication_timestamp(self):
        result = self._run_late_company_read(
            "景旺电子再度递表港交所：核心刚性PCB产品毛利率持续走低 "
            "2026-07-06 19:35 每经记者｜蔡鼎。景旺电子603228通信与数据基础设施收入增长，"
            "但核心产品毛利率下滑。",
            url="https://m.nbd.com.cn/articles/2026-07-06/4455824.html",
        )

        self.assertTrue(result.qualified, result.verifier)
        self.assertEqual("2026-07-06T11:35:00Z", result.evidence["sources"][0]["fact_as_of"])

    def test_raw_pdf_bytes_do_not_count_as_company_research(self):
        result = self._run_late_company_read(
            "%PDF-1.7 %\ufffd\ufffd 1 0 obj stream x\ufffd\ufffd\u0001\u0002\u0003\ufffd\ufffd endstream",
            url="https://static.cninfo.com.cn/finalpage/2026-09-04/1225516182.PDF",
        )

        self.assertFalse(result.qualified)
        self.assertEqual([], result.evidence["sources"])
        self.assertIn("candidate_business_research", result.verifier["missing_requirements"])

    def test_company_read_does_not_backdate_same_day_pdf_without_a_time(self):
        result = self._run_late_company_read(
            "北京同有飞骥科技股份有限公司公告，披露存储产品业务。",
            url="https://static.cninfo.com.cn/finalpage/2026-09-07/1225516182.PDF",
        )

        self.assertFalse(result.qualified)
        self.assertIn("candidate_business_research", result.verifier["missing_requirements"])

    def _run_late_company_read(
        self, excerpt_text: str, *, url: str = "https://company.test/notice",
    ):
        as_of = "2026-09-07T05:30:55Z"
        contract = {"version": 4, "as_of": as_of, "requirements": [{
            "key": "candidate_business_research", "blocking": True,
            "allowed_coverage": ["covered"],
            "window": {
                "mode": "after_start_to_end",
                "start": "2025-08-03T05:30:55Z",
                "end": as_of,
            },
        }]}

        def backend(_operation, _arguments):
            return {"results": [{
                "url": url,
                "title": "神农集团2026年半年度报告",
                "excerpt_text": excerpt_text,
                "fact_as_of": "2026-09-07T05:32:53Z",
            }]}

        plan = {"version": 1, "operations": [{
            **row("web_read", url=url),
            "requirement_key": "candidate_business_research",
        }]}
        return LocalResearchChain(
            lambda *_: plan,
            ReadOnlyResearchExecutor({"gateway": backend}),
            max_repairs=0,
        ).run({"stage": "m0_research", "as_of": as_of}, contract, attempt_id="late-company-read")

    def test_company_research_continues_when_semantics_require_a_second_source(self):
        contract = {"version": 4, "as_of": CONTRACT["as_of"], "requirements": [{
            "key": "candidate_business_research", "blocking": True, "allowed_coverage": ["covered"],
            "window": {"mode": "exact", "start": CONTRACT["as_of"], "end": CONTRACT["as_of"]},
        }]}
        def planner(packet, gaps, round_number):
            return {"version": 1, "operations": [{
                **row("web_read", url="https://company.test/" + ("counter" if round_number else "business")),
                "requirement_key": "candidate_business_research",
            }]}
        def backend(operation, arguments):
            return {"results": [{"url": arguments["url"], "excerpt_text": "样本科技600001业务公告与订单反证",
                                 "fact_as_of": CONTRACT["as_of"]}]}
        def qualify(evidence):
            complete = len(evidence["sources"]) >= 2
            return {"passed": complete, "problems": [] if complete else ["缺少订单反证正文"]}
        result = LocalResearchChain(planner, ReadOnlyResearchExecutor({"gateway": backend}),
                                    semantic_qualifier=qualify).run(
            {"task_key": "daily.opportunity.0900", "as_of": CONTRACT["as_of"]}, contract, attempt_id="candidate")
        self.assertTrue(result.qualified)
        self.assertEqual(2, len(result.evidence["sources"]))

    def test_holding_announcement_research_maps_each_frozen_entity_without_false_negative(self) -> None:
        contract = {
            "version": 4,
            "as_of": "2026-09-05T02:00:00Z",
            "requirements": [{
                "key": "portfolio_events_and_counterevidence", "blocking": True,
                "allowed_coverage": ["covered", "checked_no_change"],
                "evidence_class": "public_if_present",
                "required_entities": ["600001", "600002", "600001", "600003"],
                "entity_names": {"600001": "标准甲", "600002": "标准乙", "600003": "标准丙"},
                "negative_query_terms": ["公告", "停复牌", "财报", "风险"],
                "window": {"mode": "after_start_to_end", "start": "2026-09-01T07:00:00Z", "end": "2026-09-05T02:00:00Z"},
            }],
        }
        queries: list[str] = []

        def backend(operation: str, arguments: dict) -> dict:
            if operation == "announcement_snapshot":
                raise RuntimeError("dedicated announcement source unavailable")
            if operation == "web_search":
                query = str(arguments.get("query") or "")
                queries.append(query)
                symbol = next(code for code in ("600001", "600002", "600003") if code in query)
                return {"results": [{"url": f"https://www.cninfo.com.cn/{symbol}", "title": symbol}]}
            symbol = str(arguments.get("url") or "").rsplit("/", 1)[-1]
            announcements = []
            if symbol == "600001":
                announcements = [{
                    "symbol": symbol, "issuer": "标准甲", "title": "回购进展公告",
                    "published_at": "2026-09-03T01:00:00Z", "announcement_date": "2026-09-03",
                    "source_url": f"https://www.cninfo.com.cn/{symbol}/notice-1", "content_verified": False,
                }, {
                    "symbol": symbol, "issuer": "标准甲", "title": "回购进展公告",
                    "published_at": "2026-09-03T01:00:00Z", "announcement_date": "2026-09-03",
                    "source_url": f"https://www.cninfo.com.cn/{symbol}/notice-1", "content_verified": False,
                }]
            proof = {
                "authority": "cninfo", "query_symbol": symbol,
                "start_date": "2026-09-01", "end_date": "2026-09-05",
                "pagination_complete": symbol != "600003",
            }
            payload = {"checked_symbol": symbol, "announcements": announcements, "enumeration_proof": proof}
            return {"results": [{
                "url": f"https://www.cninfo.com.cn/{symbol}", "title": symbol,
                "excerpt_text": json.dumps(payload, ensure_ascii=False), "fact_as_of": contract["as_of"],
            }]}

        def planner(packet: dict, _gaps: list[str], _round: int) -> dict:
            discoveries = [
                item for item in packet.get("research_discoveries") or []
                if "cninfo.com.cn" in str(item.get("url") or "")
            ]
            return {"version": 1, "operations": [
                {**row("web_read", url=str(item["url"])), "requirement_key": "portfolio_events_and_counterevidence"}
                for item in discoveries
            ]}

        result = LocalResearchChain(
            planner,
            ReadOnlyResearchExecutor({"market": backend, "gateway": backend}),
            max_repairs=2,
        ).run({"stage": "m0_research", "as_of": contract["as_of"]}, contract, attempt_id="holding-events")

        self.assertFalse(result.qualified)
        self.assertEqual(3, len(queries))
        for code, name in contract["requirements"][0]["entity_names"].items():
            query = next(value for value in queries if code in value)
            self.assertIn(name, query)
            self.assertIn("2026-09-01", query)
            self.assertIn("2026-09-05", query)
            self.assertIn("公告 停复牌 财报 风险", query)
        coverage = result.evidence["coverage"][0]
        checks = {item["symbol"]: item for item in coverage["entity_checks"]}
        self.assertEqual("disclosed_pending_content", checks["600001"]["state"])
        self.assertEqual(1, len(checks["600001"]["announcements"]))
        self.assertEqual("checked_no_change", checks["600002"]["state"])
        self.assertEqual("missing", checks["600003"]["state"])
        self.assertEqual("partial", coverage["coverage_level"])
        self.assertIn("600003", coverage["unresolved_entities"])
        self.assertNotIn("600001", coverage["unresolved_entities"])

    def test_weekend_research_preserves_directional_fund_flow_and_event_boundaries(self) -> None:
        close = "2026-09-04T07:00:00Z"
        contract = {
            "version": 4,
            "as_of": "2026-09-05T02:00:00Z",
            "requirements": [{
                "key": "themes_and_capacity_cores", "blocking": True,
                "allowed_coverage": ["covered"], "requires_distribution": True,
                "minimum_named_entities": 2,
                "window": {"mode": "after_start_to_end", "start": "2026-09-01T07:00:00Z", "end": close},
            }, {
                "key": "market_fund_flow", "blocking": True,
                "allowed_coverage": ["covered"], "minimum_numeric_facts": 3,
                "window": {"mode": "exact", "start": close, "end": close},
            }, {
                "key": "material_events_and_counterevidence", "blocking": True,
                "allowed_coverage": ["covered", "checked_no_change"],
                "window": {"mode": "after_start_to_end", "start": "2026-09-01T07:00:00Z", "end": "2026-09-05T02:00:00Z"},
            }],
        }
        urls = {
            "market_fund_flow": "https://fund.example.test/2026-09-04",
            "material_events_and_counterevidence": "https://event.example.test/policy",
        }
        calls: list[tuple[str, str]] = []

        def backend(operation: str, arguments: dict) -> dict:
            key = str(arguments.get("_requirement_key") or "")
            calls.append((key, operation))
            if operation == "sector_snapshot":
                payload = {
                    "leaders": [{"name": "消费板块", "kind": "industry"}],
                    "laggards": [{"name": "科技题材", "kind": "theme"}],
                    "distribution": {
                        "industry": {"total": 31, "up": 10, "down": 20, "flat": 1, "median_change_percent": -0.4},
                        "theme": {"total": 120, "up": 45, "down": 70, "flat": 5, "median_change_percent": -0.2},
                    },
                }
                return {"results": [{
                    "url": "https://sector.example.test/all", "title": "板块全量分布",
                    "excerpt_text": json.dumps(payload, ensure_ascii=False), "fact_as_of": close,
                }]}
            if operation in {"fund_flow_snapshot", "market_event_snapshot"}:
                raise RuntimeError("preferred structured source unavailable")
            if operation == "web_search":
                return {"results": [{"url": urls[key], "title": key}]}
            if key == "market_fund_flow":
                payload = {
                    "trading_date": "2026-09-04", "coverage_level": "directional_sector",
                    "currency": "CNY", "unit": "CNY",
                    "sector_inflow_leaders": [
                        {"name": "数字人", "direction": "inflow", "net_inflow": 5_281_000_000.0, "rank": 1},
                        {"name": "AI应用", "direction": "inflow", "net_inflow": 5_163_000_000.0, "rank": 2},
                        {"name": "文化传媒", "direction": "inflow", "net_inflow": 4_208_000_000.0, "rank": 3},
                    ],
                    "sector_outflow_leaders": [{"name": "电子", "direction": "outflow", "rank": 1}],
                    "limitations": ["full_market_net_flow_unavailable", "order_size_breakdown_unavailable"],
                }
                fact_as_of = close
            else:
                payload = {
                    "title": "政策组合拳发布", "content": "监管部门于周五发布新政策。",
                    "event_truth": "reported", "market_impact": "可能改善风险偏好",
                }
                fact_as_of = "2026-09-05T01:00:00Z"
            return {"results": [{
                "url": urls[key], "title": str(payload.get("title") or key),
                "excerpt_text": json.dumps(payload, ensure_ascii=False), "fact_as_of": fact_as_of,
            }]}

        def planner(packet: dict, _gaps: list[str], _round: int) -> dict:
            discoveries = [
                item for item in packet.get("research_discoveries") or []
                if item.get("url") in set(urls.values())
            ]
            return {"version": 1, "operations": [
                {**row("web_read", url=str(item["url"])), "requirement_key": item["requirement_key"]}
                for item in discoveries
            ]}

        result = LocalResearchChain(
            planner,
            ReadOnlyResearchExecutor({"market": backend, "gateway": backend}),
            max_repairs=2,
        ).run({"stage": "m0_research", "as_of": contract["as_of"]}, contract, attempt_id="weekend-drivers")

        self.assertTrue(result.qualified, result.verifier["problems"])
        self.assertIn(("market_fund_flow", "web_search"), calls)
        self.assertIn(("material_events_and_counterevidence", "web_search"), calls)
        coverage = {item["requirement_key"]: item for item in result.evidence["coverage"]}
        self.assertEqual("complete", coverage["themes_and_capacity_cores"]["coverage_level"])
        self.assertEqual(["industry", "theme"], coverage["themes_and_capacity_cores"]["target_sets"])
        self.assertEqual(31, coverage["themes_and_capacity_cores"]["distribution_counts"]["industry"]["total"])
        self.assertEqual(close, coverage["themes_and_capacity_cores"]["fact_as_of"])
        fund = coverage["market_fund_flow"]
        self.assertEqual("directional", fund["coverage_level"])
        self.assertEqual("CNY", fund["currency"])
        self.assertEqual(close, fund["fact_as_of"])
        self.assertEqual("数字人", fund["directional_facts"][0]["name"])
        self.assertIn("sector_flow_direction", fund["supported_propositions"])
        self.assertIn("full_market_net_flow", fund["prohibited_propositions"])
        self.assertNotIn("combined", json.dumps(result.evidence, ensure_ascii=False))
        event = coverage["material_events_and_counterevidence"]
        self.assertEqual("reported", event["fact_status"])
        self.assertEqual("inference_only", event["impact_status"])

    def test_close_review_recovers_from_incomplete_structured_data_through_receipted_public_reads(self) -> None:
        contract = {
            "version": 4,
            "as_of": CONTRACT["as_of"],
            "requirements": [{
                "key": "market_breadth",
                "blocking": True,
                "allowed_coverage": ["covered"],
                "minimum_numeric_facts": 3,
                "window": {"mode": "exact", "start": CONTRACT["as_of"], "end": CONTRACT["as_of"]},
            }],
        }
        candidates = [
            "https://blocked.test/breadth",
            "https://stale.test/breadth",
            "https://independent.test/breadth",
        ]
        calls: list[tuple[str, str]] = []
        planner_packets: list[dict] = []

        def source(operation: str, arguments: dict) -> dict:
            calls.append((operation, str(arguments.get("query") or arguments.get("url") or "")))
            if operation == "market_breadth":
                return {"results": [{
                    "url": "https://structured.test/breadth",
                    "title": "incomplete breadth",
                    "excerpt_text": json.dumps({"breadth": {"up": 100}}),
                    "fact_as_of": CONTRACT["as_of"],
                }]}
            if operation == "web_search":
                return {"results": [{"url": url, "title": url} for url in candidates]}
            url = str(arguments["url"])
            if url == candidates[0]:
                raise TimeoutError("first candidate timed out")
            fact_as_of = "2026-08-26T07:00:00Z" if url == candidates[1] else CONTRACT["as_of"]
            return {"results": [{
                "url": url,
                "title": "breadth evidence",
                "excerpt_text": json.dumps({"breadth": {"up": 3100, "down": 1900, "flat": 100}}),
                "fact_as_of": fact_as_of,
            }]}

        def receipt(observation: dict) -> None:
            for index, item in enumerate(observation.get("evidence_items") or []):
                body = str(item["excerpt_text"])
                item.update({
                    "memory_episode_id": f"episode-{observation['observation_id']}-{index}",
                    "known_at": observation["acquired_at"],
                    "memory_content_hash": "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest(),
                })

        def planner(packet: dict, _gaps: list[str], _round: int) -> dict:
            planner_packets.append(packet)
            discovered = [
                item for item in packet.get("research_discoveries") or []
                if item.get("url") in candidates
            ]
            if not discovered:
                return {"version": 1, "operations": []}
            self.assertTrue(all(item.get("memory_episode_id") for item in discovered))
            self.assertTrue(all(item.get("known_at") for item in discovered))
            self.assertTrue(all(item.get("content_sha256") for item in discovered))
            return {"version": 1, "operations": [
                {**row("web_read", url=str(item["url"])), "requirement_key": "market_breadth"}
                for item in discovered
            ]}

        result = LocalResearchChain(
            planner,
            ReadOnlyResearchExecutor({"market": source, "gateway": source}),
            max_repairs=2,
            observation_registrar=receipt,
        ).run({"stage": "m0_research", "as_of": CONTRACT["as_of"]}, contract, attempt_id="close-fallback")

        self.assertTrue(result.qualified)
        search = next(value for operation, value in calls if operation == "web_search")
        self.assertIn("2026年08月27日", search)
        self.assertIn("市场宽度", search)
        self.assertIn(("web_read", candidates[0]), calls)
        self.assertIn(("web_read", candidates[2]), calls)
        valid = next(source for source in result.evidence["sources"] if source["url"] == candidates[2])
        self.assertTrue(valid["memory_episode_id"].startswith("episode-"))
        self.assertEqual(CONTRACT["as_of"], valid["fact_as_of"])
        self.assertTrue(planner_packets)

    def test_weekend_history_urls_are_mandatory_reads(self) -> None:
        urls = [
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh000001,day,2026-08-31,2026-09-04,10,qfq",
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sz399001,day,2026-08-31,2026-09-04,10,qfq",
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sz399006,day,2026-08-31,2026-09-04,10,qfq",
        ]
        plan = _merge_mandatory_operations({"version": 1, "operations": []}, {
            "version": 4,
            "requirements": [{
                "key": "weekly_market_history", "blocking": True,
                "source_urls": urls,
                "window": {"mode": "after_start_to_end", "start": "2026-08-31T07:00:00Z", "end": "2026-09-04T07:00:00Z"},
            }],
        }, max_operations=24)

        reads = [row for row in plan["operations"] if row["operation"] == "web_read"]
        self.assertEqual(urls, [row["arguments"]["url"] for row in reads])
        self.assertTrue(all(row["requirement_key"] == "weekly_market_history" for row in reads))

    def test_weekend_driver_and_announcement_facts_are_mandatory_typed_reads(self) -> None:
        contract = {
            "version": 4,
            "requirements": [
                {"key": "market_fund_flow", "blocking": True, "window": {
                    "mode": "exact", "start": "2026-09-04T07:00:00Z", "end": "2026-09-04T07:00:00Z",
                }},
                {"key": "themes_and_capacity_cores", "blocking": True, "requires_distribution": True,
                 "window": {"mode": "after_start_to_end", "start": "2026-08-31T07:00:00Z", "end": "2026-09-04T07:30:00Z"}},
                {"key": "material_events_and_counterevidence", "blocking": True,
                 "allowed_coverage": ["covered", "checked_no_change"], "window": {
                     "mode": "after_start_to_end", "start": "2026-08-31T07:00:00Z", "end": "2026-09-05T02:00:00Z",
                 }},
                {"key": "portfolio_events_and_counterevidence", "blocking": True,
                 "required_entities": ["603861", "300421"], "window": {
                     "mode": "after_start_to_end", "start": "2026-08-31T07:00:00Z", "end": "2026-09-05T02:00:00Z",
                 }},
            ],
        }

        plan = _merge_mandatory_operations(
            {"version": 1, "operations": []}, contract, max_operations=24,
        )

        typed = {(row["requirement_key"], row["operation"]) for row in plan["operations"]}
        self.assertIn(("market_fund_flow", "fund_flow_snapshot"), typed)
        self.assertIn(("themes_and_capacity_cores", "sector_snapshot"), typed)
        self.assertIn(("material_events_and_counterevidence", "market_event_snapshot"), typed)
        self.assertIn(("portfolio_events_and_counterevidence", "announcement_snapshot"), typed)
        self.assertFalse(any(
            row["requirement_key"] in {
                "material_events_and_counterevidence", "portfolio_events_and_counterevidence",
            }
            and row["operation"] == "web_search"
            for row in plan["operations"]
        ))

    def test_close_review_mandatory_research_attempts_turnover_themes_and_forum_sentiment(self) -> None:
        contract = {
            "version": 4,
            "as_of": "2026-09-04T07:20:00Z",
            "requirements": [
                {
                    "key": "turnover_compare", "blocking": True,
                    "allowed_coverage": ["covered"], "finality": "official_close",
                    "window": {"mode": "exact", "start": "2026-09-04T07:00:00Z", "end": "2026-09-04T07:00:00Z"},
                },
                {
                    "key": "themes_and_capacity_cores", "blocking": True,
                    "allowed_coverage": ["covered"], "finality": "official_close",
                    "window": {"mode": "after_start_to_end", "start": "2026-09-04T07:00:00Z", "end": "2026-09-04T07:20:00Z"},
                },
                {
                    "key": "forum_and_sentiment", "blocking": True,
                    "allowed_coverage": ["covered"],
                    "window": {"mode": "after_start_to_end", "start": "2026-09-03T07:00:00Z", "end": "2026-09-04T07:20:00Z"},
                },
            ],
        }
        calls: list[tuple[str, str, str]] = []

        def backend(operation: str, arguments: dict) -> dict:
            calls.append((str(arguments["_requirement_key"]), operation, str(arguments.get("query") or "")))
            return {"results": []}

        class AlwaysMissing:
            def evaluate(self, *_args, **_kwargs):
                return {"passed": False, "problems": ["needs_repair"], "missing_requirements": ["needs_repair"]}

        LocalResearchChain(
            lambda *_args: {"version": 1, "operations": []},
            ReadOnlyResearchExecutor({"market": backend, "gateway": backend}),
            gate=AlwaysMissing(), max_repairs=0,
        ).run({"task_key": "daily.review.1520", "as_of": contract["as_of"]}, contract, attempt_id="close-review")

        self.assertIn(("turnover_compare", "turnover_compare", ""), calls)
        self.assertIn(("themes_and_capacity_cores", "sector_snapshot", ""), calls)
        self.assertIn(("forum_and_sentiment", "sentiment_snapshot", ""), calls)
        self.assertTrue(any(key == "forum_and_sentiment" and operation == "web_search" for key, operation, _ in calls))

    def test_close_review_market_operations_use_typed_capabilities_and_close_finality(self) -> None:
        contract = {
            "version": 4, "as_of": "2026-09-04T07:20:00Z", "requirements": [
                {"key": "turnover_compare", "blocking": True, "window": {
                    "mode": "exact", "start": "2026-09-04T07:00:00Z", "end": "2026-09-04T07:00:00Z",
                }},
                {"key": "themes_and_capacity_cores", "blocking": True, "window": {
                    "mode": "after_start_to_end", "start": "2026-09-04T07:00:00Z", "end": "2026-09-04T07:20:00Z",
                }},
                {"key": "forum_and_sentiment", "blocking": True, "window": {
                    "mode": "after_start_to_end", "start": "2026-09-03T07:00:00Z", "end": "2026-09-04T07:20:00Z",
                }},
            ],
        }
        runner = mock.Mock()

        def resolve(request: FactRequest) -> EvidenceResolution:
            data = {
                "source": request.capability, "source_urls": [f"https://example.test/{request.capability}"],
                "source_evidence": [{
                    "url": f"https://example.test/{request.capability}",
                    "fact_as_of": "2026-09-04T07:00:00Z", "data": {"summary": request.capability},
                }],
            }
            return EvidenceResolution(
                True, request.capability, "1.0.0", "2026-09-04T07:00:00Z",
                "2026-09-04T07:20:01Z", data, "artifact:sha256:" + "a" * 64,
                None, ("tool_result_schema_valid",), attempts=("primary:succeeded",),
            )

        runner.resolve_with_fallback.side_effect = resolve
        backend = ToolCatalogMarketBackend(runner, contract=contract, deadline=lambda: 10.0)

        backend("turnover_compare", {"_requirement_key": "turnover_compare"})
        backend("sector_snapshot", {"_requirement_key": "themes_and_capacity_cores"})
        backend("sentiment_snapshot", {"_requirement_key": "forum_and_sentiment"})

        requests = [call.args[0] for call in runner.resolve_with_fallback.call_args_list]
        self.assertEqual(
            ["cn_market_turnover_compare", "cn_market_sector_snapshot", "cn_market_breadth"],
            [request.capability for request in requests],
        )
        self.assertEqual(["official_close"] * 3, [request.finality for request in requests])
        self.assertEqual("2026-09-04T07:00:00Z", requests[0].required_at)
        self.assertEqual("2026-09-04T07:20:00Z", requests[1].required_at)

    def test_weekend_driver_operations_use_typed_capabilities_and_frozen_inputs(self) -> None:
        contract = {"version": 4, "as_of": "2026-09-05T02:00:00Z", "requirements": [
            {"key": "market_fund_flow", "finality": "official_close", "window": {
                "mode": "exact", "start": "2026-09-04T07:00:00Z", "end": "2026-09-04T07:00:00Z",
            }},
            {"key": "themes_and_capacity_cores", "requires_distribution": True, "window": {
                "mode": "after_start_to_end", "start": "2026-08-31T07:00:00Z", "end": "2026-09-04T07:30:00Z",
            }},
            {"key": "material_events_and_counterevidence", "window": {
                "mode": "after_start_to_end", "start": "2026-08-31T07:00:00Z", "end": "2026-09-05T02:00:00Z",
            }},
            {"key": "portfolio_events_and_counterevidence", "required_entities": ["603861"], "window": {
                "mode": "after_start_to_end", "start": "2026-08-31T07:00:00Z", "end": "2026-09-05T02:00:00Z",
            }},
        ]}
        runner = mock.Mock()
        runner.resolve_with_fallback.return_value = EvidenceResolution(
            True, "test", "1.0.0", "2026-09-04T07:00:00Z", "2026-09-05T02:00:01Z",
            {"source": "test", "source_urls": ["https://example.test/source"], "source_evidence": [{
                "url": "https://example.test/source", "fact_as_of": "2026-09-04T07:00:00Z", "data": {"summary": "test"},
            }]}, "artifact:sha256:" + "d" * 64, None, ("tool_result_schema_valid",),
        )
        backend = ToolCatalogMarketBackend(runner, contract=contract, deadline=lambda: 10.0)

        backend("fund_flow_snapshot", {"_requirement_key": "market_fund_flow"})
        backend("sector_snapshot", {"_requirement_key": "themes_and_capacity_cores"})
        backend("market_event_snapshot", {"_requirement_key": "material_events_and_counterevidence"})
        backend("announcement_snapshot", {"_requirement_key": "portfolio_events_and_counterevidence"})

        requests = [call.args[0] for call in runner.resolve_with_fallback.call_args_list]
        self.assertEqual(
            [
                "cn_market_fund_flow_snapshot", "cn_market_sector_snapshot",
                "cn_market_event_snapshot", "cn_equity_announcement_snapshot",
            ],
            [request.capability for request in requests],
        )
        self.assertTrue(requests[1].inputs["require_distribution"])
        self.assertEqual("2026-08-31T07:00:00Z", requests[2].inputs["start_at"])
        self.assertEqual("2026-09-05T02:00:00Z", requests[2].inputs["end_at"])
        self.assertEqual(["603861"], requests[3].inputs["symbols"])
        self.assertEqual("2026-08-31", requests[3].inputs["start_date"])
        self.assertEqual("2026-09-05", requests[3].inputs["end_date"])

    def test_forum_failure_keeps_technical_classification_and_has_a_bounded_retry(self) -> None:
        contract = {
            "version": 4, "as_of": "2026-09-04T07:20:00Z", "requirements": [{
                "key": "forum_and_sentiment", "blocking": True, "allowed_coverage": ["covered"],
                "window": {"mode": "after_start_to_end", "start": "2026-09-03T07:00:00Z", "end": "2026-09-04T07:20:00Z"},
            }],
        }
        runner = mock.Mock()
        runner.resolve_with_fallback.return_value = EvidenceResolution.failed(
            "generic_web_search", "tool_network_transient",
        )
        gateway = ToolCatalogResearchBackend(
            runner, as_of=contract["as_of"], deadline=lambda: 10.0, contract=contract,
        )
        market_calls: list[str] = []

        def market(operation: str, _arguments: dict) -> dict:
            market_calls.append(operation)
            return {"results": [{
                "url": "https://example.test/sentiment", "title": "市场情绪替代源",
                "excerpt_text": "2026-09-04 收盘市场情绪，上涨1000家，下跌4000家",
                "fact_as_of": "2026-09-04T07:00:00Z",
            }]}

        class AlwaysMissing:
            def evaluate(self, *_args, **_kwargs):
                return {"passed": False, "problems": ["needs_repair"], "missing_requirements": ["needs_repair"]}

        result = LocalResearchChain(
            lambda *_args: {"version": 1, "operations": []},
            ReadOnlyResearchExecutor({"market": market, "gateway": gateway}),
            gate=AlwaysMissing(), max_repairs=4,
        ).run({"task_key": "daily.review.1520", "as_of": contract["as_of"]}, contract, attempt_id="forum-failure")

        forum_failures = [row for row in result.observations if row.get("operation") == "web_search"]
        self.assertEqual(2, runner.resolve_with_fallback.call_count)
        self.assertEqual(["tool_network_transient", "tool_network_transient"], [
            row.get("tool_error_code") for row in forum_failures
        ])
        self.assertEqual(["sentiment_snapshot"], market_calls)
        coverage = next(row for row in result.evidence["coverage"] if row["requirement_key"] == "forum_and_sentiment")
        self.assertEqual("covered", coverage["status"])

    def test_close_review_typed_facts_qualify_all_three_requirements_without_broker_planning(self) -> None:
        contract = {
            "version": 4, "as_of": "2026-09-04T07:20:00Z", "requirements": [
                {"key": "turnover_compare", "blocking": True, "allowed_coverage": ["covered"],
                 "window": {"mode": "exact", "start": "2026-09-04T07:00:00Z", "end": "2026-09-04T07:00:00Z"},
                 "evidence_terms": [["成交额"], ["亿元"], ["上一交易日"]], "minimum_numeric_facts": 2},
                {"key": "themes_and_capacity_cores", "blocking": True, "allowed_coverage": ["covered"],
                 "window": {"mode": "after_start_to_end", "start": "2026-09-04T07:00:00Z", "end": "2026-09-04T07:20:00Z"},
                 "evidence_terms": [["板块"], ["领涨"], ["领跌"]], "minimum_named_entities": 2},
                {"key": "forum_and_sentiment", "blocking": True, "allowed_coverage": ["covered"],
                 "window": {"mode": "after_start_to_end", "start": "2026-09-03T07:00:00Z", "end": "2026-09-04T07:20:00Z"}},
            ],
        }
        runner = mock.Mock()

        def resolution(request: FactRequest) -> EvidenceResolution:
            if request.capability == "cn_market_turnover_compare":
                summary = "2026-09-04两市成交额25000.00亿元，上一交易日成交额24000.00亿元，较前一交易日+1000.00亿元（+4.17%）"
            elif request.capability == "cn_market_sector_snapshot":
                summary = "2026-09-04半导体板块领涨，容量核心中芯国际；房地产板块领跌，容量核心万科A"
            else:
                summary = "2026-09-04收盘市场情绪：上涨1500家，下跌3500家，涨停60只，跌停12只"
            url = f"https://example.test/{request.capability}"
            fact_as_of = (
                "2026-09-04T07:00:01Z"
                if request.capability == "cn_market_sector_snapshot" else "2026-09-04T07:00:00Z"
            )
            return EvidenceResolution(
                True, request.capability, "1.0.0", fact_as_of, "2026-09-04T07:20:01Z",
                {"source": request.capability, "source_urls": [url], "source_evidence": [{
                    "url": url, "fact_as_of": fact_as_of, "data": {"summary": summary},
                }]}, "artifact:sha256:" + "b" * 64, None, ("tool_result_schema_valid",),
            )

        runner.resolve_with_fallback.side_effect = resolution
        backend = ToolCatalogMarketBackend(runner, contract=contract, deadline=lambda: 10.0)
        result = LocalResearchChain(
            lambda *_args: {"version": 1, "operations": []},
            ReadOnlyResearchExecutor({"market": backend, "gateway": lambda *_args: {"results": []}}),
            max_repairs=0,
        ).run({"task_key": "daily.review.1520", "as_of": contract["as_of"]}, contract, attempt_id="typed-close")

        self.assertTrue(result.qualified, result.verifier["problems"])
        self.assertEqual(
            {"turnover_compare": "covered", "themes_and_capacity_cores": "covered", "forum_and_sentiment": "covered"},
            {row["requirement_key"]: row["status"] for row in result.evidence["coverage"]},
        )

    def test_empty_forum_search_cannot_claim_checked_no_change(self) -> None:
        contract = {
            "version": 4, "as_of": "2026-09-04T07:20:00Z", "requirements": [{
                "key": "forum_and_sentiment", "blocking": True,
                "allowed_coverage": ["covered", "checked_no_change"],
                "window": {"mode": "after_start_to_end", "start": "2026-09-03T07:00:00Z", "end": "2026-09-04T07:20:00Z"},
            }],
        }

        def market(_operation: str, _arguments: dict) -> dict:
            raise RuntimeError("sentiment provider unavailable")

        result = LocalResearchChain(
            lambda *_args: {"version": 1, "operations": []},
            ReadOnlyResearchExecutor({"market": market, "gateway": lambda *_args: {"results": []}}),
            max_repairs=0,
        ).run({"task_key": "daily.review.1520", "as_of": contract["as_of"]}, contract, attempt_id="no-fake-no-change")

        self.assertFalse(result.qualified)
        coverage = next(row for row in result.evidence["coverage"] if row["requirement_key"] == "forum_and_sentiment")
        self.assertEqual("missing", coverage["status"])

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
                ("portfolio_events_and_counterevidence", "announcement_snapshot"),
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

    def test_planner_reserves_the_final_deadline_for_candidate_qualification(self) -> None:
        requests = []
        broker = mock.Mock()

        def invoke(request):
            requests.append(request)
            if request.packet.get("stage") == "research_plan":
                return SimpleNamespace(result={"version": 1, "operations": []})
            return SimpleNamespace(result={"passed": True, "problems": []})

        broker.invoke.side_effect = invoke
        planner = BrokerResearchPlanner(
            broker, intellect="smart", effort="medium", deadline=lambda: 1_000.0,
            completion_reserve_seconds=90,
        )

        planner({"as_of": CONTRACT["as_of"], "evidence_contract": CONTRACT}, [], 0)
        planner.qualify_candidates({"as_of": CONTRACT["as_of"], "sources": []})

        self.assertEqual(910.0, requests[0].absolute_deadline)
        self.assertEqual(1_000.0, requests[1].absolute_deadline)

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

    def test_browser_last_mile_is_rejected_before_search_and_plain_read_are_attempted(self) -> None:
        packet = {
            "evidence_contract": {"requirements": [{"key": "market", "blocking": True}]},
            "coverage_gaps": ["blocking_requirement_missing:market"],
            "available_backends": ["gateway"], "deterministic_requirement_keys": [],
            "research_discoveries": [{"requirement_key": "market", "url": "https://example.test/fact"}],
            "research_route_state": {"market": {
                "search_attempted": True, "plain_read_attempted": False,
            }},
        }
        plan = {"version": 1, "operations": [{
            **row("web_browser", url="https://example.test/fact"), "requirement_key": "market",
        }]}

        problems = _verify_research_plan(packet, plan)["problems"]

        self.assertIn("research_plan_browser_before_public_routes:market", problems)

        packet["research_route_state"]["market"]["plain_read_attempted"] = True
        self.assertNotIn(
            "research_plan_browser_before_public_routes:market",
            _verify_research_plan(packet, plan)["problems"],
        )

    def test_plan_verifier_rejects_a_company_disclosure_listing_page(self) -> None:
        packet = {
            "evidence_contract": {"requirements": [{
                "key": "candidate_business_research", "blocking": True,
            }]},
            "coverage_gaps": ["candidate_business_research"],
            "available_backends": ["gateway"],
            "deterministic_requirement_keys": [],
        }
        plan = {"version": 1, "operations": [{
            **row(
                "web_read",
                url="https://www.cninfo.com.cn/new/disclosure/stock?stockCode=002050",
            ),
            "requirement_key": "candidate_business_research",
        }]}

        problems = _verify_research_plan(packet, plan)["problems"]

        self.assertIn(
            "research_plan_non_document_url:candidate_business_research",
            problems,
        )

    def test_plan_verifier_allows_read_only_research_for_an_exploratory_candidate(self) -> None:
        key = "candidate_business_research"
        packet = {
            "evidence_contract": {"requirements": [{"key": key, "blocking": True}]},
            "coverage_gaps": [key],
            "available_backends": ["market"],
            "deterministic_requirement_keys": [],
            "research_discoveries": [],
        }
        plan = {"version": 1, "operations": [{
            "requirement_key": key,
            "backend": "market",
            "operation": "announcement_snapshot",
            "arguments": {
                "query": None, "categories": None, "url": None, "symbol": "002463",
                "render": None, "session_id": None, "actions": None,
            },
            "fallback_backends": [],
        }]}

        result = _verify_research_plan(packet, plan)

        self.assertTrue(result["passed"], result["problems"])

    def test_bounded_plan_drops_company_listing_page_but_keeps_useful_operations(self) -> None:
        listing = "https://www.cninfo.com.cn/new/disclosure/stock?stockCode=002050"
        homepage = "https://www.cninfo.com.cn/"
        dynamic_detail = (
            "https://www.cninfo.com.cn/new/disclosure/detail?stockCode=688825"
            "&announcementId=1225425451"
        )
        pdf = "https://static.cninfo.com.cn/finalpage/2026-09-04/1225516182.PDF"
        plan = {"version": 1, "operations": [
            {
                **row("web_search", query=""),
                "requirement_key": "candidate_business_research",
            },
            {
                **row("web_search", query="002050 三花智控 半年度报告"),
                "requirement_key": "candidate_business_research",
            },
            {
                **row("web_read", url=""),
                "requirement_key": "candidate_business_research",
            },
            {
                **row("web_read", url=listing),
                "requirement_key": "candidate_business_research",
            },
            {
                **row("web_read", url=homepage),
                "requirement_key": "candidate_business_research",
            },
            {
                **row("web_read", url=dynamic_detail),
                "requirement_key": "candidate_business_research",
            },
            {
                **row("web_read", url=pdf),
                "requirement_key": "candidate_business_research",
            },
        ]}

        bounded = _bounded_research_plan(plan)

        self.assertEqual(
            ["web_search", "web_read"],
            [operation["operation"] for operation in bounded["operations"]],
        )
        self.assertEqual(pdf, bounded["operations"][1]["arguments"]["url"])

    def test_discovery_repair_skips_listing_page_and_prioritizes_readable_article(self) -> None:
        contract = {"requirements": [{
            "key": "candidate_business_research", "blocking": True,
        }]}
        listing = "https://www.cninfo.com.cn/new/disclosure/stock?stockCode=002050"
        article = "https://stock.10jqka.com.cn/20260904/c123.shtml"
        pdf = "https://static.cninfo.com.cn/finalpage/2026-09-04/1225516182.PDF"

        plan = _discovery_read_repair_plan(contract, [
            {"requirement_key": "candidate_business_research", "url": listing},
            {"requirement_key": "candidate_business_research", "url": article},
            {"requirement_key": "candidate_business_research", "url": pdf},
        ], ["candidate_business_research"], 1)

        self.assertIsNotNone(plan)
        urls = [item["arguments"]["url"] for item in plan["operations"]]
        self.assertEqual([article, pdf], urls)

    def test_company_discovery_repair_reads_across_search_queries_before_repeating_one_company(self) -> None:
        key = "candidate_business_research"
        contract = {"requirements": [{"key": key, "blocking": True}]}
        discoveries = [
            {
                "requirement_key": key,
                "url": f"https://news.example.test/2026090{group}/company-{group}-{item}.shtml",
                "discovery_query": f"公司{group} 业务 风险",
                "discovery_observation_id": f"search-{group}",
            }
            for group in range(1, 5)
            for item in range(1, 4)
        ]

        plan = _discovery_read_repair_plan(contract, discoveries, [key], 1)

        self.assertIsNotNone(plan)
        urls = [item["arguments"]["url"] for item in plan["operations"]]
        self.assertEqual(4, len(urls))
        self.assertEqual(
            {f"https://news.example.test/2026090{group}/company-{group}-1.shtml" for group in range(1, 5)},
            set(urls),
        )

    def test_company_discovery_repair_adds_quotes_and_disclosures_for_named_candidates(self) -> None:
        key = "candidate_business_research"
        contract = {"requirements": [{"key": key, "blocking": True}]}
        discoveries = [
            {
                "requirement_key": key,
                "url": f"https://news.example.test/2026090{group}/company-{group}.shtml",
                "title": title,
                "discovery_query": query,
                "discovery_observation_id": f"search-{group}",
            }
            for group, title, query in (
                (1, "中际旭创(300308)业务进展", "中际旭创 300308 业务 风险"),
                (2, "剑桥科技(603083)订单跟踪", "剑桥科技 603083 业务 风险"),
                (3, "景旺电子(603228)产能变化", "景旺电子 603228 业务 风险"),
                (4, "沪电股份(002463)业绩预告", "沪电股份 002463 业务 风险"),
            )
        ]

        plan = _discovery_read_repair_plan(
            contract, discoveries, [key], 1,
            available_backends={"gateway", "market"},
        )

        self.assertIsNotNone(plan)
        self.assertEqual(8, len(plan["operations"]))
        structured = [item for item in plan["operations"] if item["backend"] == "market"]
        self.assertEqual(
            [
                ("holding_snapshot", "300308"), ("announcement_snapshot", "300308"),
                ("holding_snapshot", "603083"), ("announcement_snapshot", "603083"),
            ],
            [(item["operation"], item["arguments"]["symbol"]) for item in structured],
        )
        self.assertEqual(4, len([item for item in plan["operations"] if item["operation"] == "web_read"]))

    def test_company_discovery_digest_keeps_multiple_search_queries_in_the_shortlist(self) -> None:
        key = "candidate_business_research"
        contract = {"requirements": [{
            "key": key,
            "window": {
                "mode": "after_start_to_end",
                "start": "2025-08-27T07:00:00Z",
                "end": CONTRACT["as_of"],
            },
        }]}
        observations = [{
            "observation_id": f"search-{group}",
            "operation": "web_search",
            "status": "succeeded",
            "arguments": {"requirement_key": key, "query": f"公司{group} 业务 风险"},
            "evidence_items": [{
                "url": f"https://news.example.test/2026082{group}/company-{group}-{item}.shtml",
                "title": f"公司{group} 业务文章 {item}",
                "excerpt_text": f"公司{group} 搜索线索 {item}",
                "fact_as_of": CONTRACT["as_of"],
            } for item in range(1, 7)],
        } for group in range(1, 5)]

        discoveries = _discovery_digest(observations, contract)

        self.assertEqual(16, len(discoveries))
        self.assertEqual(4, len({item["discovery_observation_id"] for item in discoveries}))
        self.assertEqual(4, len({item["discovery_query"] for item in discoveries}))
        self.assertTrue(all(item.get("discovery_query") for item in discoveries))

    def test_search_url_remains_a_lead_when_discovered_after_the_frozen_as_of(self) -> None:
        contract = {"requirements": [{
            "key": "candidate_business_research",
            "window": {
                "mode": "after_start_to_end",
                "start": "2025-08-03T05:30:55Z",
                "end": "2026-09-07T05:30:55Z",
            },
        }]}
        url = "https://static.cninfo.com.cn/finalpage/2026-09-04/1225516182.PDF"
        observations = [{
            "operation": "web_search",
            "status": "succeeded",
            "arguments": {"requirement_key": "candidate_business_research"},
            "evidence_items": [{
                "url": url,
                "title": "公司半年度报告",
                "excerpt_text": "搜索结果线索",
                "fact_as_of": "2026-09-07T05:35:00Z",
            }],
        }]

        discoveries = _discovery_digest(observations, contract)

        self.assertEqual([url], [item["url"] for item in discoveries])

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

    def test_planner_deterministically_caps_an_oversubscribed_requirement(self) -> None:
        operations = [
            {**row("web_search", query=f"公司业务核验 {index}"), "requirement_key": "market"}
            for index in range(8)
        ]
        operations.append({
            **row("web_read", url="https://company.test/disclosure"),
            "requirement_key": "market",
        })
        proposed = {"version": 1, "operations": operations}
        broker = mock.Mock()

        def invoke(request):
            self.assertTrue(request.verifier(proposed)["passed"])
            return SimpleNamespace(result=proposed)

        broker.invoke.side_effect = invoke
        planner = BrokerResearchPlanner(
            broker, intellect="smart", effort="medium", deadline=lambda: 123.0,
        )

        plan = planner({
            "as_of": CONTRACT["as_of"],
            "evidence_contract": CONTRACT,
            "research_discoveries": [{
                "requirement_key": "market",
                "url": "https://company.test/disclosure",
            }],
        }, ["market"], 0)

        self.assertEqual(8, len(plan["operations"]))
        self.assertEqual(
            [f"公司业务核验 {index}" for index in range(7)],
            [
                item["arguments"]["query"] for item in plan["operations"]
                if item["operation"] == "web_search"
            ],
        )
        self.assertEqual("web_read", plan["operations"][-1]["operation"])

    def test_planner_reuses_a_direct_discovery_when_model_only_reads_a_listing(self) -> None:
        key = "candidate_business_research"
        listing = "https://www.cninfo.com.cn/new/disclosure/stock?stockCode=002050"
        pdf = "https://static.cninfo.com.cn/finalpage/2026-09-04/1225516182.PDF"
        contract = {
            "version": 4,
            "as_of": CONTRACT["as_of"],
            "requirements": [{
                "key": key,
                "blocking": True,
                "window": CONTRACT["requirements"][0]["window"],
            }],
        }
        proposed = {"version": 1, "operations": [
            {**row("web_search", query="002050 三花智控 半年度报告"), "requirement_key": key},
            {**row("web_read", url=listing), "requirement_key": key},
        ]}
        broker = mock.Mock()

        def invoke(request):
            verification = request.verifier(proposed)
            self.assertTrue(verification["passed"], verification)
            return SimpleNamespace(result=proposed)

        broker.invoke.side_effect = invoke
        planner = BrokerResearchPlanner(
            broker, intellect="smart", effort="medium", deadline=lambda: 123.0,
        )

        plan = planner({
            "as_of": CONTRACT["as_of"],
            "evidence_contract": contract,
            "research_discoveries": [
                {"requirement_key": key, "url": listing},
                {"requirement_key": key, "url": pdf},
            ],
        }, [key], 0)

        urls = [
            operation["arguments"]["url"] for operation in plan["operations"]
            if operation["operation"] == "web_read"
        ]
        self.assertEqual([pdf], urls)

    def test_planner_prioritizes_unread_direct_document_over_a_generic_model_read(self) -> None:
        key = "candidate_business_research"
        article = "https://finance.example.com/company-overview"
        pdf = "https://static.cninfo.com.cn/finalpage/2026-09-04/1225516182.PDF"
        contract = {
            "version": 4,
            "as_of": CONTRACT["as_of"],
            "requirements": [{
                "key": key,
                "blocking": True,
                "window": CONTRACT["requirements"][0]["window"],
            }],
        }
        proposed = {"version": 1, "operations": [{
            **row("web_read", url=article),
            "requirement_key": key,
        }]}
        broker = mock.Mock()

        def invoke(request):
            verification = request.verifier(proposed)
            self.assertTrue(verification["passed"], verification)
            return SimpleNamespace(result=proposed)

        broker.invoke.side_effect = invoke
        planner = BrokerResearchPlanner(
            broker, intellect="smart", effort="medium", deadline=lambda: 123.0,
        )

        plan = planner({
            "as_of": CONTRACT["as_of"],
            "evidence_contract": contract,
            "research_discoveries": [{"requirement_key": key, "url": pdf}],
            "attempted_research_urls": ["https://already-read.example.com/report.pdf"],
        }, [key], 0)

        urls = [
            operation["arguments"]["url"] for operation in plan["operations"]
            if operation["operation"] == "web_read"
        ]
        self.assertEqual([pdf, article], urls)

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

        proposed = {"version": 1, "operations": [row("web_search", query="")]}
        result = _verify_research_plan(request.packet, proposed)

        self.assertFalse(result["passed"])
        self.assertIn(
            "research_plan_operation_argument_missing:market:web_search:query",
            result["problems"],
        )
        salvaged = request.verifier(proposed)
        self.assertFalse(salvaged["passed"])
        self.assertIn("research_plan_missing_requirement:market", salvaged["problems"])

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

    def test_market_adapter_accepts_an_explicit_candidate_symbol_outside_the_portfolio(self) -> None:
        key = "candidate_business_research"
        contract = {"version": 4, "as_of": CONTRACT["as_of"], "requirements": [{
            "key": key,
            "window": {"mode": "after_start_to_end", "start": "2025-08-27T07:00:00Z", "end": CONTRACT["as_of"]},
        }]}
        runner = mock.Mock()
        runner.resolve_with_fallback.return_value = EvidenceResolution(
            True, "cn_equity_quote_batch", "1.0.0", CONTRACT["as_of"], CONTRACT["as_of"],
            {
                "source": "tencent_quote",
                "source_evidence": [{
                    "url": "https://qt.gtimg.cn/q=sz300308",
                    "fact_as_of": CONTRACT["as_of"],
                    "data": {"quotes": [{"symbol": "300308", "name": "中际旭创", "price": 100.0}]},
                }],
            },
            "artifact:sha256:" + "c" * 64, None, ("tool_result_schema_valid",),
        )
        backend = ToolCatalogMarketBackend(runner, contract=contract, deadline=lambda: 30.0)

        result = backend("holding_snapshot", {"_requirement_key": key, "symbol": "300308"})

        request = runner.resolve_with_fallback.call_args.args[0]
        self.assertEqual({"symbols": ["300308"]}, request.inputs)
        self.assertEqual("300308", json.loads(result["results"][0]["excerpt_text"])["quotes"][0]["symbol"])

    def test_tool_catalog_adapter_normalizes_weekly_tencent_history_before_evidence(self) -> None:
        url = (
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
            "param=sh000001,day,2026-08-31,2026-09-04,10,qfq"
        )
        body = json.dumps({"code": 0, "data": {"sh000001": {
            "day": [
                ["2026-08-31", "3926.53", "3986.30", "3986.30", "3926.50", "576656606"],
                ["2026-09-04", "3955.55", "3930.12", "3980.20", "3915.22", "537286161"],
            ],
            "qt": {"sh000001": ["current", "20260905191449"]},
        }}})
        contract = {"version": 4, "as_of": "2026-09-05T10:00:00Z", "requirements": [{
            "key": "weekly_market_history", "blocking": True, "allowed_coverage": ["covered"],
            "window": {"mode": "after_start_to_end", "start": "2026-08-31T07:00:00Z", "end": "2026-09-04T07:00:00Z"},
            "required_entities": ["sh000001"], "minimum_numeric_facts": 9,
        }]}
        runner = mock.Mock()
        runner.resolve_with_fallback.return_value = EvidenceResolution(
            True, "generic_web_read", "1.0.0", "2026-09-05T10:00:00Z", "2026-09-05T10:00:01Z",
            {"url": url, "text": body}, "artifact:sha256:" + "c" * 64, None,
            ("tool_result_schema_valid",),
        )
        backend = ToolCatalogResearchBackend(
            runner, as_of=contract["as_of"], deadline=lambda: 30.0, contract=contract,
        )

        result = backend("web_read", {
            **row("web_read", url=url)["arguments"], "_requirement_key": "weekly_market_history",
        })

        payload = json.loads(result["results"][0]["excerpt_text"])
        self.assertEqual(["2026-08-31", "2026-09-04"], [item["date"] for item in payload["series"]])
        self.assertEqual(3930.12, payload["series"][-1]["close"])
        self.assertIsInstance(payload["series"][-1]["volume"], float)
        self.assertEqual("2026-09-04T07:00:00Z", result["results"][0]["fact_as_of"])
        self.assertEqual("2026-09-04T07:00:00Z", runner.resolve_with_fallback.call_args.args[0].required_at)

        plan = {"version": 1, "operations": [{
            "requirement_key": "weekly_market_history", "backend": "gateway", "operation": "web_read",
            "arguments": row("web_read", url=url)["arguments"], "fallback_backends": [],
        }]}
        qualified = LocalResearchChain(
            lambda *_: plan, ReadOnlyResearchExecutor({"gateway": backend}), max_repairs=0,
        ).run({"as_of": contract["as_of"]}, contract, attempt_id="weekly-typed")
        self.assertTrue(qualified.qualified, qualified.verifier["problems"])

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

    def test_exhausted_research_returns_traceable_gap_and_truthful_public_boundary(self) -> None:
        contract = {
            "version": 4,
            "as_of": CONTRACT["as_of"],
            "requirements": [
                {
                    "key": "market_breadth",
                    "blocking": True,
                    "allowed_coverage": ["covered"],
                    "minimum_numeric_facts": 2,
                    "window": {
                        "mode": "exact", "start": CONTRACT["as_of"], "end": CONTRACT["as_of"],
                    },
                },
                {
                    "key": "forum_and_sentiment",
                    "blocking": False,
                    "allowed_coverage": ["covered"],
                    "window": {
                        "mode": "after_start_to_end",
                        "start": "2026-08-27T06:00:00Z", "end": CONTRACT["as_of"],
                    },
                },
            ],
        }

        def unavailable(_operation: str, _arguments: dict) -> dict:
            raise RuntimeError("source unavailable")

        result = LocalResearchChain(
            lambda *_args: {
                "version": 1,
                "operations": [{
                    **row("web_read", url="https://example.test/breadth"),
                    "requirement_key": "market_breadth",
                }],
            },
            ReadOnlyResearchExecutor({"gateway": unavailable}),
            max_repairs=0,
        ).run(
            {"stage": "m0_research", "as_of": CONTRACT["as_of"]},
            contract,
            attempt_id="traceable-gap",
        )

        self.assertFalse(result.qualified)
        self.assertEqual(["market_breadth"], result.evidence["critical_gaps"])
        gap = next(item for item in result.verifier["gap_states"] if item["requirement_key"] == "market_breadth")
        self.assertEqual("市场宽度", gap["target_proposition"])
        self.assertEqual(["至少 2 个数值事实"], gap["required_fields"])
        self.assertEqual("missing", gap["coverage_state"])
        self.assertEqual("routes_exhausted", gap["research_state"])
        self.assertTrue(gap["blocking"])
        self.assertEqual(
            ["not_attempted", "in_progress", "routes_exhausted"],
            [item["state"] for item in gap["transitions"]],
        )
        self.assertEqual(["公开搜索与网页", "结构化市场数据"], gap["attempted_source_categories"])
        public = result.verifier["public_failure_message"]
        self.assertIn("截至 2026-08-27T07:00:00Z", public)
        self.assertIn("市场宽度", public)
        self.assertIn("公开搜索与网页、结构化市场数据", public)
        self.assertIn("其他已核验事实保持有效", public)
        self.assertNotIn("market_breadth", public)
        self.assertNotIn("web_read", public)

    def test_research_gap_states_preserve_coverage_and_terminal_outcomes_in_frozen_bundle(self) -> None:
        keys = ["complete", "directional", "partial", "conflicted", "permission", "exhausted", "unattempted"]
        contract = {
            "version": 4,
            "as_of": CONTRACT["as_of"],
            "requirements": [{
                "key": key,
                "blocking": True,
                "allowed_coverage": ["covered"],
                "window": {"mode": "exact", "start": CONTRACT["as_of"], "end": CONTRACT["as_of"]},
            } for key in keys],
        }

        class CoverageGate:
            def evaluate(self, evidence: dict, _contract: dict, observations: list, _as_of: str, **_kwargs: object) -> dict:
                attempted = {
                    str((item.get("arguments") or {}).get("requirement_key") or "")
                    for item in observations
                }
                if not attempted:
                    return {"passed": False, "problems": ["not_attempted"], "missing_requirements": keys}
                missing = [key for key in keys if key != "complete"]
                return {
                    "passed": False,
                    "problems": ["conflicting_fact:conflicted"],
                    "missing_requirements": missing,
                    "normalized_evidence": evidence,
                }

        def source(_operation: str, arguments: dict) -> dict:
            key = str(arguments["_requirement_key"])
            if key == "permission":
                raise PermissionError("browser permission required")
            if key == "exhausted":
                raise RuntimeError("all routes unavailable")
            payload = {"fact": key}
            if key == "directional":
                payload["coverage_level"] = "directional_sector"
            return {"results": [{
                "url": f"https://example.test/{key}",
                "title": key,
                "excerpt_text": json.dumps(payload),
                "fact_as_of": CONTRACT["as_of"],
            }]}

        operations = [
            {**row("web_read", url=f"https://example.test/{key}"), "requirement_key": key}
            for key in keys if key != "unattempted"
        ]
        result = LocalResearchChain(
            lambda *_args: {"version": 1, "operations": operations},
            ReadOnlyResearchExecutor({"gateway": source}),
            gate=CoverageGate(),
            max_repairs=0,
        ).run({"stage": "m0_research", "as_of": CONTRACT["as_of"]}, contract, attempt_id="gap-states")

        states = {row["requirement_key"]: row for row in result.verifier["gap_states"]}
        self.assertEqual("complete", states["complete"]["coverage_state"])
        self.assertEqual("complete", states["complete"]["research_state"])
        self.assertEqual("directional", states["directional"]["coverage_state"])
        self.assertEqual("partial", states["partial"]["coverage_state"])
        self.assertEqual("conflicted", states["conflicted"]["coverage_state"])
        self.assertEqual("permission_required", states["permission"]["research_state"])
        self.assertEqual("routes_exhausted", states["exhausted"]["research_state"])
        self.assertEqual("not_attempted", states["unattempted"]["research_state"])
        self.assertEqual(result.evidence["research_gaps"], json.loads(result.bundle_bytes)["research_gaps"])

    def test_search_listing_is_discovery_not_evidence(self) -> None:
        search = {"results": [{"url": "https://example.test/2026-08-27", "title": "收盘", "excerpt_text": "2026-08-27", "fact_as_of": "2026-08-27T07:00:00Z"}]}
        plan = {"version": 1, "operations": [row("web_search", query="收盘")]}
        result = LocalResearchChain(lambda *_: plan, ReadOnlyResearchExecutor({"gateway": lambda *_: search}), max_repairs=0).run({"as_of": CONTRACT["as_of"]}, CONTRACT, attempt_id="x")
        self.assertFalse(result.qualified); self.assertEqual([], result.evidence["sources"])

    def test_successful_negative_event_query_cannot_record_checked_no_change_without_traceable_results(self) -> None:
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

        self.assertFalse(result.qualified)
        events = next(row for row in result.evidence["coverage"] if row["requirement_key"] == "events")
        self.assertEqual("missing", events["status"])
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

    def test_research_does_not_spend_the_candidate_qualification_reserve_on_new_tools(self) -> None:
        remaining = [91.0]
        planner_calls: list[int] = []
        tool_calls: list[str] = []

        def planner(_packet: dict, _gaps: list[str], round_number: int) -> dict:
            planner_calls.append(round_number)
            remaining[0] = 90.0
            return {"version": 1, "operations": [row("web_search", query="new company lead")]}

        class AlwaysMissing:
            def evaluate(self, *_args, **_kwargs):
                return {"passed": False, "problems": ["needs_repair"], "missing_requirements": ["market"]}

        result = LocalResearchChain(
            planner,
            ReadOnlyResearchExecutor({"gateway": lambda operation, _arguments: tool_calls.append(operation) or {"results": []}}),
            gate=AlwaysMissing(), max_repairs=None, deadline=lambda: remaining[0],
            completion_reserve_seconds=90,
            semantic_qualifier=lambda _evidence: {"passed": False, "problems": ["still incomplete"]},
        ).run({"as_of": CONTRACT["as_of"]}, {"version": 4, "as_of": CONTRACT["as_of"], "requirements": []}, attempt_id="reserve")

        self.assertFalse(result.qualified)
        self.assertEqual([0], planner_calls)
        self.assertEqual([], tool_calls)
        self.assertEqual("semantic_qualification_reserve", result.verifier["stop_reason"])

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

    def test_cancelled_research_checkpoints_and_resume_fetches_only_the_missing_gap(self) -> None:
        contract = {"version": 4, "as_of": CONTRACT["as_of"], "requirements": [{
            "key": key, "blocking": True, "allowed_coverage": ["covered"],
            "window": {"mode": "exact", "start": CONTRACT["as_of"], "end": CONTRACT["as_of"]},
        } for key in ("stable_close", "dynamic_news")]}
        operations = [{
            **row("web_read", url=f"https://example.test/{key}"), "requirement_key": key,
        } for key in ("stable_close", "dynamic_news")]
        calls: list[str] = []
        checkpoints: list[dict] = []

        def backend(_operation: str, arguments: dict) -> dict:
            key = str(arguments["_requirement_key"])
            calls.append(key)
            return {"results": [{
                "url": f"https://example.test/{key}", "title": key, "excerpt_text": key,
                "fact_as_of": CONTRACT["as_of"], "primary": True,
            }]}

        first = LocalResearchChain(
            lambda *_: {"version": 1, "operations": operations},
            ReadOnlyResearchExecutor({"gateway": backend}), max_repairs=None,
            deadline=lambda: 60.0, cancelled=lambda: len(calls) >= 1,
            on_checkpoint=checkpoints.append,
        ).run({"stage": "chat_research", "as_of": CONTRACT["as_of"]}, contract, attempt_id="first")

        self.assertFalse(first.qualified)
        self.assertEqual("user_cancelled", first.verifier["stop_reason"])
        self.assertEqual("cancelled", checkpoints[-1]["terminal_status"])
        self.assertEqual(["stable_close"], calls)

        calls.clear()
        resumed = LocalResearchChain(
            lambda *_: {"version": 1, "operations": operations},
            ReadOnlyResearchExecutor({"gateway": backend}), max_repairs=None,
            deadline=lambda: 60.0, resume_checkpoint=checkpoints[-1],
        ).run({"stage": "chat_research", "as_of": CONTRACT["as_of"]}, contract, attempt_id="resumed")

        self.assertTrue(resumed.qualified, resumed.verifier["problems"])
        self.assertEqual(["dynamic_news"], calls)
        self.assertEqual({"stable_close", "dynamic_news"}, {
            str((item.get("arguments") or {}).get("requirement_key"))
            for item in resumed.observations if item.get("status") == "succeeded"
        })

    def test_research_stops_after_repeated_rounds_with_no_information_gain(self) -> None:
        plans: list[int] = []

        def planner(_packet: dict, _gaps: list[str], round_number: int) -> dict:
            plans.append(round_number)
            return {"version": 1, "operations": [row("web_search", query="same empty query")]}

        result = LocalResearchChain(
            planner, ReadOnlyResearchExecutor({"gateway": lambda *_: {"results": []}}),
            max_repairs=None, deadline=lambda: 60.0,
        ).run({"stage": "m0_research", "as_of": CONTRACT["as_of"]}, CONTRACT, attempt_id="no-gain")

        self.assertFalse(result.qualified)
        self.assertEqual("no_information_gain", result.verifier["stop_reason"])
        self.assertLessEqual(len(plans), 3)

    def test_persistent_research_checkpoint_is_idempotent_for_one_frozen_packet(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CompanionStore(Path(directory) / "companion.sqlite3")
            cycle = store.create_cycle("daily.execution.0945", "2026-08-27T01:45:00Z", CONTRACT["as_of"])
            checkpoint = {
                "version": 1, "frozen_as_of": CONTRACT["as_of"], "contract_sha256": "contract",
                "observations": [], "unresolved_gaps": ["market"], "attempted_routes": [],
                "terminal_status": "running", "stop_reason": None,
            }

            first = store.save_research_checkpoint(
                cycle["cycle_id"], "m0_research", "packet", "attempt-1", checkpoint,
            )
            second = store.save_research_checkpoint(
                cycle["cycle_id"], "m0_research", "packet", "attempt-1", checkpoint,
            )

            self.assertEqual(first["checkpoint_id"], second["checkpoint_id"])
            self.assertEqual(checkpoint, store.research_checkpoint(
                cycle["cycle_id"], "m0_research", "packet",
            )["checkpoint"])
