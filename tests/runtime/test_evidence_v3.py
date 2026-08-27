from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from ai_trading_companion.evidence_contract import EvidenceContractFactory
from ai_trading_companion.evidence_gate import EvidenceGate
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.store import CompanionStore


class EvidenceV3Tests(TestCase):
    def setUp(self):
        self.as_of = "2026-08-26T01:00:00Z"
        self.contract = EvidenceContractFactory(_WeekdayCalendar()).build(
            task_key="daily.opportunity.0900", stage="m0_research", as_of=self.as_of,
        )
        self.market = next(item for item in self.contract["requirements"] if item["key"] == "current_market_state")
        self.events = next(item for item in self.contract["requirements"] if item["key"] == "material_events_and_counterevidence")
        self.observations = [{
            "attempt_id": "attempt-1", "status": "succeeded", "non_empty": True,
            "arguments": {"query": "A股 公告 政策 风险"},
            "evidence_items": [{
                "evidence_ref": "ev_attempt-1_1", "url": "https://www.sse.com.cn/close",
                "title": "收盘", "source_identity": "www.sse.com.cn", "independence_group": "sse",
                "primary": True, "excerpt_text": "8月25日收盘", "fact_as_of": "2026-08-25T07:00:00Z",
                "published_at": "2026-08-25T07:05:00Z", "acquired_at": "2026-08-26T00:50:00Z",
            }, {
                "evidence_ref": "ev_attempt-1_2", "url": "https://example.test/events",
                "title": "事件检索", "source_identity": "example.test", "independence_group": "example",
                "primary": False, "excerpt_text": "无新增重大公告", "fact_as_of": "2026-08-26T00:50:00Z",
                "published_at": "2026-08-26T00:50:00Z", "acquired_at": "2026-08-26T00:50:00Z",
            }],
        }]

    def _evidence(self, ref="ev_attempt-1_1", *, status="covered", fact_as_of="2026-08-25T07:00:00Z"):
        return {
            "schema_version": 3, "as_of": self.as_of,
            "sources": [
                {"evidence_ref": ref, "excerpt": "8月25日收盘", "analysis": "前收盘市场状态"},
                {"evidence_ref": "ev_attempt-1_2", "excerpt": "无新增重大公告", "analysis": "事件与反证"},
            ],
            "coverage": [
                {"requirement_key": "current_market_state", "status": "covered", "evidence_refs": [ref]},
                {"requirement_key": "material_events_and_counterevidence", "status": status, "evidence_refs": ["ev_attempt-1_2"]},
            ],
            "high_impact_events": [],
        }

    def test_0900_contract_binds_market_to_latest_completed_xshg_close(self):
        self.assertEqual("2026-08-25T07:00:00Z", self.market["window"]["start"])
        self.assertEqual("2026-08-25T07:00:00Z", self.market["window"]["end"])
        self.assertEqual(["covered"], self.market["allowed_coverage"])
        self.assertEqual("2026-08-25T07:00:00Z", self.events["window"]["start"])
        self.assertEqual(self.as_of, self.events["window"]["end"])

    def test_rejects_foreign_reference_and_naive_runtime_time(self):
        foreign = EvidenceGate().evaluate(self._evidence("ev_other_1"), self.contract, self.observations, self.as_of, attempt_id="attempt-1")
        self.assertIn("source_ref_not_in_current_attempt", foreign["problems"])
        self.observations[0]["evidence_items"][0]["fact_as_of"] = "2026-08-25T15:00:00"
        naive = EvidenceGate().evaluate(self._evidence(), self.contract, self.observations, self.as_of, attempt_id="attempt-1")
        self.assertIn("source_fact_as_of_missing_timezone", naive["problems"])

    def test_checked_no_change_requires_matching_current_attempt_query(self):
        self.observations[0]["arguments"] = {"query": "A股 公告"}
        result = EvidenceGate().evaluate(self._evidence(status="checked_no_change"), self.contract, self.observations, self.as_of, attempt_id="attempt-1")
        self.assertIn("checked_no_change_query_not_matched:material_events_and_counterevidence", result["problems"])

    def test_checked_no_change_with_matching_query_is_allowed(self):
        self.assertTrue(EvidenceGate().evaluate(self._evidence(status="checked_no_change"), self.contract, self.observations, self.as_of, attempt_id="attempt-1")["passed"])

    def test_high_impact_fact_needs_primary_or_independent_corroboration(self):
        evidence = self._evidence()
        evidence["high_impact_events"] = [{"materiality": "high", "evidence_refs": ["ev_attempt-1_2"]}]
        result = EvidenceGate().evaluate(evidence, self.contract, self.observations, self.as_of, attempt_id="attempt-1")
        self.assertIn("high_impact_fact_lacks_primary_or_independent_confirmation", result["problems"])

    def test_terminal_attempt_cannot_be_finalized_twice(self):
        with TemporaryDirectory() as temporary:
            store = CompanionStore(Path(temporary) / "companion.sqlite3")
            cycle = CompanionEngine(store).start_cycle("daily.execution.0945", "2026-08-26T09:45:00+08:00", self.as_of)
            attempt = store.begin_attempt(cycle["cycle_id"], "m0_research", self.as_of, "packet")
            store.finish_attempt(attempt["attempt_id"], "rejected", verifier={"passed": False})
            with self.assertRaisesRegex(ValueError, "already terminal"):
                store.finish_attempt(attempt["attempt_id"], "failed", error="again")

    def test_rejected_evidence_only_emits_failure_event(self):
        with TemporaryDirectory() as temporary:
            store = CompanionStore(Path(temporary) / "companion.sqlite3")
            engine = CompanionEngine(store)
            cycle = engine.start_cycle("daily.execution.0945", "2026-08-26T09:45:00+08:00", self.as_of)
            engine.research_started(cycle["cycle_id"])
            engine.research_failed(cycle["cycle_id"], "evidence rejected", details={"problems": ["foreign ref"]})
            self.assertEqual([], store.artifacts(cycle["cycle_id"]))
            with store.connection() as connection:
                checkpoint = connection.execute("SELECT 1 FROM stage_checkpoint WHERE cycle_id=?", (cycle["cycle_id"],)).fetchone()
            self.assertIsNone(checkpoint)
            self.assertIsNone(store.valid_daily_baseline("2026-08-26", self.as_of))


class _WeekdayCalendar:
    def is_trading_day(self, value: date) -> bool:
        return value.weekday() < 5
