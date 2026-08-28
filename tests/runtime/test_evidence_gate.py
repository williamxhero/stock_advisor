import unittest
import tempfile
from pathlib import Path

from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.evidence_gate import EvidenceGate
from ai_trading_companion.store import CompanionStore


class EvidenceGateTests(unittest.TestCase):
    def setUp(self):
        self.requirements = [
            {"key": "indices_close", "description": "15:00 收盘指数", "blocking": True},
            {"key": "market_breadth", "description": "收盘市场广度", "blocking": True},
        ]
        self.observations = [
            {
                "backend": "gateway",
                "status": "succeeded",
                "non_empty": True,
                "acquired_at": "2026-08-26T07:40:00Z",
                "result_urls": ["https://example.test/close"],
                "content_sha256": "a" * 64,
                "observation_id": "obs-1",
                "result_items": [{"url": "https://example.test/close", "result_item_hash": "item-1", "evidence_text": "8月26日收盘指数与市场广度数据", "published_at": None, "acquired_at": "2026-08-26T07:40:00Z"}],
            }
        ]

    def test_zero_tool_calls_are_rejected(self):
        evidence = {"as_of": "2026-08-26T07:40:00Z", "sources": [], "coverage": []}
        result = EvidenceGate().evaluate(evidence, self.requirements, [])
        self.assertFalse(result["passed"])
        self.assertIn("no_current_information_tool_result", result["problems"])

    def test_invented_source_is_rejected(self):
        evidence = {
            "as_of": "2026-08-26T07:40:00Z",
            "sources": [{"url": "https://invented.test/item", "fact_as_of": "2026-08-26T07:00:00Z"}],
            "coverage": [],
        }
        result = EvidenceGate().evaluate(evidence, self.requirements, self.observations)
        self.assertFalse(result["passed"])
        self.assertIn("source_not_in_current_tool_trace", result["problems"])

    def test_missing_blocking_coverage_is_rejected(self):
        evidence = {
            "as_of": "2026-08-26T07:40:00Z",
            "sources": [{"url": "https://example.test/close", "fact_as_of": "2026-08-26T07:00:00Z", "tool_observation_id": "obs-1", "result_item_hash": "item-1", "excerpt": "8月26日收盘指数"}],
            "coverage": [{"requirement_key": "indices_close", "status": "covered", "evidence_urls": ["https://example.test/close"], "reason": ""}],
        }
        result = EvidenceGate().evaluate(evidence, self.requirements, self.observations)
        self.assertFalse(result["passed"])
        self.assertIn("blocking_requirement_missing:market_breadth", result["problems"])

    def test_current_trace_and_complete_coverage_pass(self):
        evidence = {
            "as_of": "2026-08-26T07:40:00Z",
            "sources": [{"url": "https://example.test/close", "fact_as_of": "2026-08-26T07:00:00Z", "tool_observation_id": "obs-1", "result_item_hash": "item-1", "excerpt": "8月26日收盘指数与市场广度"}],
            "coverage": [
                {"requirement_key": "indices_close", "status": "covered", "evidence_urls": ["https://example.test/close"], "reason": ""},
                {"requirement_key": "market_breadth", "status": "checked_no_change", "evidence_urls": ["https://example.test/close"], "reason": "已查询"},
            ],
        }
        self.assertTrue(EvidenceGate().evaluate(evidence, self.requirements, self.observations)["passed"])

    def test_future_and_stale_sources_are_rejected(self):
        base_source = {"url": "https://example.test/close", "tool_observation_id": "obs-1", "result_item_hash": "item-1", "excerpt": "8月26日收盘指数"}
        future = {"as_of": "2026-08-26T07:40:00Z", "sources": [{**base_source, "fact_as_of": "2026-08-27T07:00:00Z"}], "coverage": []}
        self.assertIn("source_from_future", EvidenceGate().evaluate(future, self.requirements, self.observations)["problems"])
        stale = {
            "as_of": "2026-08-26T07:40:00Z", "sources": [{**base_source, "fact_as_of": "2026-08-25T07:00:00Z"}],
            "coverage": [
                {"requirement_key": "indices_close", "status": "covered", "evidence_urls": ["https://example.test/close"], "reason": ""},
                {"requirement_key": "market_breadth", "status": "covered", "evidence_urls": ["https://example.test/close"], "reason": ""},
            ],
        }
        result = EvidenceGate().evaluate(stale, self.requirements, self.observations)
        self.assertIn("blocking_requirement_stale:indices_close", result["problems"])

    def test_high_impact_fact_needs_primary_or_two_domains(self):
        evidence = {
            "as_of": "2026-08-26T07:40:00Z",
            "sources": [{"url": "https://example.test/close", "fact_as_of": "2026-08-26T07:00:00Z", "tool_observation_id": "obs-1", "result_item_hash": "item-1", "excerpt": "8月26日收盘指数"}],
            "coverage": [
                {"requirement_key": "indices_close", "status": "covered", "evidence_urls": ["https://example.test/close"], "reason": ""},
                {"requirement_key": "market_breadth", "status": "covered", "evidence_urls": ["https://example.test/close"], "reason": ""},
            ],
            "high_impact_events": [{"materiality": "high", "evidence_urls": ["https://example.test/close"]}],
        }
        self.assertIn("high_impact_fact_lacks_primary_or_independent_confirmation", EvidenceGate().evaluate(evidence, self.requirements, self.observations)["problems"])

    def test_model_cannot_relabel_a_stale_tool_result_as_current(self):
        observations = [{**self.observations[0], "result_items": [{
            "url": "https://example.test/close", "result_item_hash": "item-1",
            "evidence_text": "8月25日收盘指数与市场广度数据", "published_at": None,
            "acquired_at": "2026-08-26T07:40:00Z",
        }]}]
        evidence = {
            "as_of": "2026-08-26T07:40:00Z",
            "sources": [{
                "url": "https://example.test/close", "fact_as_of": "2026-08-26T07:00:00Z",
                "published_at": None, "tool_observation_id": "obs-1", "result_item_hash": "item-1",
                "excerpt": "8月25日收盘指数",
            }],
            "coverage": [],
        }
        result = EvidenceGate().evaluate(evidence, self.requirements, observations)
        self.assertIn("source_fact_time_not_supported_by_tool_result", result["problems"])

    def test_relative_today_wording_cannot_relabel_an_undated_old_page(self):
        observations = [{**self.observations[0], "result_items": [{
            "url": "https://example.test/close", "result_item_hash": "item-1",
            "title": "旧市场简讯", "source_family": "media", "upstream_id": "example.test",
            "evidence_text": "今日市场简讯", "published_at": None,
            "acquired_at": "2026-08-26T07:40:00Z",
        }]}]
        evidence = {
            "as_of": "2026-08-26T07:40:00Z",
            "sources": [{
                "url": "https://example.test/close", "title": "证监会正式公告",
                "fact_as_of": "2026-08-26T07:20:00Z", "published_at": None,
                "source_family": "official", "upstream_id": "csrc.gov.cn",
                "tool_observation_id": "obs-1", "result_item_hash": "item-1", "excerpt": "今日市场简讯",
            }],
            "coverage": [],
        }
        problems = EvidenceGate().evaluate(evidence, self.requirements, observations)["problems"]
        self.assertIn("source_fact_time_not_supported_by_tool_result", problems)
        self.assertIn("source_title_not_bound_to_tool_result", problems)
        self.assertIn("source_family_not_bound_to_tool_result", problems)
        self.assertIn("source_upstream_not_bound_to_tool_result", problems)

    def test_same_day_information_after_declared_fact_time_is_rejected(self):
        for clock in ("15:25", "15：25", "15点25分", "15时25分"):
            with self.subTest(clock=clock):
                excerpt = f"8月26日 {clock} 收盘后出现的新公告"
                observations = [{**self.observations[0], "result_items": [{
                    "url": "https://example.test/close", "result_item_hash": "item-1",
                    "evidence_text": excerpt, "published_at": None,
                    "acquired_at": "2026-08-26T07:30:00Z",
                }]}]
                evidence = {
                    "as_of": "2026-08-26T07:20:00Z",
                    "sources": [{
                        "url": "https://example.test/close", "fact_as_of": "2026-08-26T07:20:00Z",
                        "published_at": None, "tool_observation_id": "obs-1", "result_item_hash": "item-1",
                        "excerpt": excerpt,
                    }],
                    "coverage": [],
                }
                self.assertIn(
                    "source_fact_time_after_declared_as_of",
                    EvidenceGate().evaluate(evidence, self.requirements, observations)["problems"],
                )

    def test_one_generic_source_cannot_claim_unrelated_requirement_coverage(self):
        requirements = [{
            "key": "turnover_compare", "description": "成交额比较", "blocking": True,
            "evidence_terms": [["成交额", "成交"], ["亿", "万亿"], ["昨日", "前一交易日"]],
            "minimum_numeric_facts": 2,
        }]
        observations = [{**self.observations[0], "result_items": [{
            "url": "https://example.test/close", "result_item_hash": "item-1",
            "evidence_text": "8月26日成交额以亿元计，较昨日变化", "published_at": None,
            "acquired_at": "2026-08-26T07:40:00Z",
        }]}]
        evidence = {
            "as_of": "2026-08-26T07:40:00Z",
            "sources": [{
                "url": "https://example.test/close", "fact_as_of": "2026-08-26T07:00:00Z",
                "tool_observation_id": "obs-1", "result_item_hash": "item-1", "excerpt": "8月26日成交额以亿元计，较昨日变化",
            }],
            "coverage": [{
                "requirement_key": "turnover_compare", "status": "covered",
                "evidence_urls": ["https://example.test/close"], "reason": "模型声称已覆盖",
            }],
        }
        self.assertIn(
            "blocking_requirement_lacks_numeric_facts:turnover_compare",
            EvidenceGate().evaluate(evidence, requirements, observations)["problems"],
        )

    def test_generic_theme_words_without_named_themes_do_not_cover_theme_requirement(self):
        requirements = [{
            "key": "themes", "description": "题材强弱", "blocking": True,
            "evidence_terms": [["板块", "题材"], ["领涨"], ["领跌"]],
            "minimum_named_entities": 2,
        }]
        excerpt = "8月26日领涨板块强势，领跌板块弱势"
        observations = [{**self.observations[0], "result_items": [{
            "url": "https://example.test/close", "result_item_hash": "item-1",
            "evidence_text": excerpt, "published_at": None, "acquired_at": "2026-08-26T07:40:00Z",
        }]}]
        evidence = {
            "as_of": "2026-08-26T07:40:00Z",
            "sources": [{
                "url": "https://example.test/close", "fact_as_of": "2026-08-26T07:00:00Z",
                "tool_observation_id": "obs-1", "result_item_hash": "item-1", "excerpt": excerpt,
            }],
            "coverage": [{
                "requirement_key": "themes", "status": "covered",
                "evidence_urls": ["https://example.test/close"], "reason": "",
            }],
        }
        self.assertIn(
            "blocking_requirement_lacks_named_entities:themes",
            EvidenceGate().evaluate(evidence, requirements, observations)["problems"],
        )

    def test_one_rich_source_can_cover_multiple_requirements(self):
        requirements = [
            {"key": "turnover", "blocking": True, "evidence_terms": [["成交额"], ["亿元"], ["昨日"]], "minimum_numeric_facts": 2},
            {"key": "breadth", "blocking": True, "evidence_terms": [["上涨"], ["下跌"], ["家"]], "minimum_numeric_facts": 2},
        ]
        excerpt = "8月26日两市成交额23000亿元，昨日为25000亿元；上涨3200家、下跌1900家。"
        observations = [{**self.observations[0], "result_items": [{
            "url": "https://example.test/close", "result_item_hash": "item-1",
            "evidence_text": excerpt, "published_at": None, "acquired_at": "2026-08-26T07:40:00Z",
        }]}]
        evidence = {
            "as_of": "2026-08-26T07:40:00Z",
            "sources": [{
                "url": "https://example.test/close", "fact_as_of": "2026-08-26T07:00:00Z",
                "tool_observation_id": "obs-1", "result_item_hash": "item-1", "excerpt": excerpt,
            }],
            "coverage": [
                {"requirement_key": "turnover", "status": "covered", "evidence_urls": ["https://example.test/close"], "reason": ""},
                {"requirement_key": "breadth", "status": "covered", "evidence_urls": ["https://example.test/close"], "reason": ""},
            ],
        }
        self.assertTrue(EvidenceGate().evaluate(evidence, requirements, observations)["passed"])

    def test_partial_0900_evidence_never_becomes_a_baseline(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = CompanionStore(Path(temporary) / "companion.sqlite3")
            cycle = CompanionEngine(store).start_cycle(
                "daily.opportunity.0900", "2026-08-26T09:00:00+08:00", "2026-08-26T00:30:00Z"
            )
            store.record_evidence(cycle, "m0_research", {"sources": [{"url": "https://example.test", "title": "partial", "excerpt": "partial", "fact_as_of": "2026-08-26T00:20:00Z"}]})
            self.assertIsNone(store.valid_daily_baseline("2026-08-26", "2026-08-26T01:00:00Z"))

    def test_checkpoint_is_bound_to_verified_attempt_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = CompanionStore(Path(temporary) / "companion.sqlite3")
            cycle = CompanionEngine(store).start_cycle(
                "daily.review.1520", "2026-08-26T15:20:00+08:00", "2026-08-26T07:20:00Z"
            )
            attempt = store.begin_attempt(cycle["cycle_id"], "m0_compose", "2026-08-26T08:00:00Z", "packet")
            store.finish_attempt(attempt["attempt_id"], "succeeded", output={"m0_markdown": "frozen"}, verifier={"passed": True})
            with self.assertRaisesRegex(ValueError, "does not match"):
                store.save_stage_checkpoint(cycle["cycle_id"], "m0_compose", "packet", attempt["attempt_id"], {"m0_markdown": "altered"})


if __name__ == "__main__":
    unittest.main()
