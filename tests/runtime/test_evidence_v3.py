import json
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from ai_trading_companion.__main__ import (
    M1_MAX_JUDGMENT_ATTEMPTS,
    _call_stage,
    _evidence_read_cutoff,
    _frozen_m0_source_attempt,
    _m1_research_as_of,
    _m1_retry_feedback,
    _m1_should_retry,
)
from ai_trading_companion.broker_client import BrokerError
from ai_trading_companion.evidence_contract import EvidenceContractFactory
from ai_trading_companion.evidence_gate import EvidenceGate
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.router import CognitiveRouter
from ai_trading_companion.store import CompanionStore


class EvidenceV3Tests(TestCase):
    def test_diagnostic_rerun_resolves_frozen_m0_attempt_from_source_cycle(self):
        evidence = {"as_of": "2026-08-31T07:20:00Z", "sources": []}
        source_attempt = {
            "attempt_id": "source-m0", "stage": "m0_research", "status": "succeeded",
            "output_json": json.dumps(evidence),
        }
        store = Mock()
        store.attempts.side_effect = lambda cycle_id: [] if cycle_id == "rerun" else [source_attempt]
        cycle = {
            "cycle_id": "rerun",
            "schedule_snapshot_json": json.dumps({
                "diagnostic_rerun": True, "diagnostic_rerun_of": "source",
            }),
        }

        self.assertEqual(source_attempt, _frozen_m0_source_attempt(store, cycle, evidence))
        self.assertEqual(["rerun", "source"], [call.args[0] for call in store.attempts.call_args_list])

    def test_gateway_reads_are_frozen_to_contract_not_later_stage_start(self):
        packet = {"as_of": "2026-08-31T05:22:27Z"}
        contract = {"as_of": "2026-08-31T05:21:50Z"}
        self.assertEqual("2026-08-31T05:21:50Z", _evidence_read_cutoff(packet, contract))

    def test_m1_reuse_keeps_the_frozen_m0_evidence_time(self):
        self.assertEqual(
            "2026-08-31T05:25:57Z",
            _m1_research_as_of({"as_of": "2026-08-31T05:25:57Z"}, None),
        )

    def test_1430_contract_requires_fresh_intraday_market_and_event_evidence(self):
        contract = EvidenceContractFactory(_WeekdayCalendar()).build(
            task_key="daily.execution.1430", stage="m0_research", as_of="2026-08-26T06:30:00Z",
        )
        market = next(item for item in contract["requirements"] if item["key"] == "current_market_state")
        events = next(item for item in contract["requirements"] if item["key"] == "material_events_and_counterevidence")

        self.assertEqual("after_start_to_end", market["window"]["mode"])
        self.assertEqual("2026-08-26T06:15:00Z", market["window"]["start"])
        self.assertEqual("2026-08-26T06:30:00Z", market["window"]["end"])
        self.assertEqual("2026-08-26T02:30:00Z", events["window"]["start"])

    def test_premarket_and_early_sessions_keep_separate_fact_windows(self):
        factory = EvidenceContractFactory(_WeekdayCalendar())
        premarket = factory.build(task_key="daily.opportunity.0900", stage="m0_research", as_of="2026-08-26T00:30:00Z")
        at_0945 = factory.build(task_key="daily.execution.0945", stage="m0_research", as_of="2026-08-26T01:45:00Z")
        at_1030 = factory.build(task_key="daily.execution.1030", stage="m0_research", as_of="2026-08-26T02:30:00Z")

        premarket_market = next(item for item in premarket["requirements"] if item["key"] == "current_market_state")
        early_market = next(item for item in at_0945["requirements"] if item["key"] == "current_market_state")
        middle_market = next(item for item in at_1030["requirements"] if item["key"] == "current_market_state")
        self.assertEqual("2026-08-25T07:00:00Z", premarket_market["window"]["end"])
        self.assertEqual("2026-08-26T01:30:00Z", early_market["window"]["start"])
        self.assertEqual("2026-08-26T02:15:00Z", middle_market["window"]["start"])

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

    def test_1520_review_binds_market_to_completed_close_and_events_to_cycle(self):
        as_of = "2026-08-27T07:20:02.555Z"
        contract = EvidenceContractFactory(_WeekdayCalendar()).build(
            task_key="daily.review.1520", stage="m0_research", as_of=as_of,
            internal_context={"prior_judgment_count": 3, "portfolio_entities": ["600000"]},
        )
        requirements = {row["key"]: row for row in contract["requirements"]}

        self.assertEqual(
            {"start": "2026-08-27T07:00:00Z", "end": "2026-08-27T07:00:00Z", "mode": "exact"},
            requirements["indices_close"]["window"],
        )
        self.assertEqual("2026-08-26T07:00:00Z", requirements["events_and_counterevidence"]["window"]["start"])
        self.assertEqual("2026-08-27T07:20:02.555000Z", requirements["events_and_counterevidence"]["window"]["end"])
        blockers = [row["key"] for row in contract["requirements"] if row["blocking"]]
        self.assertEqual([
            "indices_close", "turnover_compare", "market_breadth", "themes_and_capacity_cores",
            "events_and_counterevidence", "prior_judgment_changes", "portfolio_market_state",
            "portfolio_events_and_counterevidence",
        ], blockers)

        rejected = EvidenceGate().evaluate(
            {"schema_version": 3, "as_of": as_of, "sources": [], "coverage": [], "high_impact_events": []},
            contract, [], as_of, attempt_id="attempt",
        )
        self.assertFalse(rejected["passed"])
        self.assertEqual(set(blockers), set(rejected["missing_requirements"]))

    def test_intraday_contract_accepts_recent_market_facts_and_since_prior_checkpoint_events(self):
        contract_0945 = EvidenceContractFactory(_WeekdayCalendar()).build(
            task_key="daily.execution.0945", stage="m0_research",
            as_of="2026-08-31T01:45:05.144Z",
        )
        market_0945, events_0945 = contract_0945["requirements"]
        self.assertEqual({
            "start": "2026-08-31T01:30:05.144000Z",
            "end": "2026-08-31T01:45:05.144000Z",
            "mode": "after_start_to_end",
        }, market_0945["window"])
        self.assertEqual("2026-08-31T01:00:00Z", events_0945["window"]["start"])
        self.assertEqual("2026-08-31T01:45:05.144000Z", events_0945["window"]["end"])

        contract_1030 = EvidenceContractFactory(_WeekdayCalendar()).build(
            task_key="daily.execution.1030", stage="m0_research",
            as_of="2026-08-31T02:30:01.533Z",
        )
        market_1030, events_1030 = contract_1030["requirements"]
        self.assertEqual("2026-08-31T02:15:01.533000Z", market_1030["window"]["start"])
        self.assertEqual("2026-08-31T01:45:00Z", events_1030["window"]["start"])

    def test_intraday_contract_passes_with_minute_precision_market_and_incremental_event(self):
        as_of = "2026-08-31T01:45:05.144Z"
        contract = EvidenceContractFactory(_WeekdayCalendar()).build(
            task_key="daily.execution.0945", stage="m0_research", as_of=as_of,
        )
        observations = [{
            "attempt_id": "intraday-attempt", "status": "succeeded", "non_empty": True,
            "backend": "gateway", "arguments": {"query": "A股 公告 政策 风险"},
            "evidence_items": [{
                "evidence_ref": "ev_market", "excerpt_text": "09:44 A股市场行情",
                "fact_as_of": "2026-08-31T01:44:00Z", "published_at": None,
                "acquired_at": as_of, "primary": True,
            }, {
                "evidence_ref": "ev_event", "excerpt_text": "09:30 新增政策反证",
                "fact_as_of": "2026-08-31T01:30:00Z", "published_at": "2026-08-31T01:30:00Z",
                "acquired_at": as_of, "primary": True,
            }],
        }]
        evidence = {
            "schema_version": 3, "as_of": as_of,
            "sources": [
                {"evidence_ref": "ev_market", "excerpt": "09:44 A股市场行情", "analysis": "当前行情"},
                {"evidence_ref": "ev_event", "excerpt": "09:30 新增政策反证", "analysis": "新增事件"},
            ],
            "coverage": [
                {"requirement_key": "current_market_state", "status": "covered", "evidence_refs": ["ev_market"]},
                {"requirement_key": "material_events_and_counterevidence", "status": "covered", "evidence_refs": ["ev_event"]},
            ],
            "high_impact_events": [],
        }

        result = EvidenceGate().evaluate(evidence, contract, observations, as_of, attempt_id="intraday-attempt")

        self.assertTrue(result["passed"], result["problems"])

    def test_intraday_contract_v4_blocks_publication_without_market_breadth_and_all_holdings(self):
        contract = EvidenceContractFactory(_WeekdayCalendar()).build(
            task_key="daily.execution.0945", stage="m0_research",
            as_of="2026-08-31T01:45:00Z",
            internal_context={"portfolio_entities": ["600487", "603861", "300421"]},
        )

        requirements = {item["key"]: item for item in contract["requirements"]}

        self.assertEqual(4, contract["version"])
        self.assertEqual(["600487", "603861", "300421"], requirements["portfolio_market_state"]["required_entities"])
        self.assertEqual(["covered"], requirements["portfolio_market_state"]["allowed_coverage"])
        self.assertEqual(["covered", "checked_no_change"], requirements["portfolio_events_and_counterevidence"]["allowed_coverage"])
        self.assertTrue(requirements["market_breadth"]["blocking"])

    def test_v4_portfolio_quotes_are_qualified_from_structured_tool_json(self):
        as_of = "2026-08-31T01:45:00Z"
        contract = {"version": 4, "as_of": as_of, "requirements": [{
            "key": "portfolio_market_state", "blocking": True, "allowed_coverage": ["covered"],
            "required_entities": ["600487", "603861"], "minimum_numeric_facts": 8,
            "window": {"mode": "after_start_to_end", "start": "2026-08-31T01:30:00Z", "end": as_of},
        }]}
        quotes = [
            {"symbol": "600487", "previous_close": 1.0, "price": 1.1, "change": 0.1, "change_percent": 10.0,
             "quote_at": "2026-08-31T01:44:00Z", "trading_date": "2026-08-31", "status": "trading"},
            {"symbol": "603861", "previous_close": 2.0, "price": 1.9, "change": -0.1, "change_percent": -5.0,
             "quote_at": "2026-08-31T01:44:00Z", "trading_date": "2026-08-31", "status": "trading"},
        ]
        sources, evidence_items = [], []
        for index, quote in enumerate(quotes, 1):
            ref = f"quote-{index}"
            excerpt = json.dumps({"quotes": [quote]}, ensure_ascii=False, sort_keys=True)
            sources.append({"evidence_ref": ref, "excerpt": excerpt})
            evidence_items.append({"evidence_ref": ref, "excerpt_text": excerpt, "fact_as_of": quote["quote_at"],
                                   "published_at": None, "acquired_at": as_of})
        observations = [{"attempt_id": "attempt", "backend": "market", "status": "succeeded", "non_empty": True,
                         "evidence_items": evidence_items}]
        evidence = {"schema_version": 3, "as_of": as_of, "sources": sources,
                    "coverage": [{"requirement_key": "portfolio_market_state", "status": "covered",
                                  "evidence_refs": ["quote-1", "quote-2"]}], "high_impact_events": []}

        self.assertTrue(EvidenceGate().evaluate(evidence, contract, observations, as_of, attempt_id="attempt")["passed"])
        quotes[1].pop("change_percent")
        excerpt = json.dumps({"quotes": [quotes[1]]}, ensure_ascii=False, sort_keys=True)
        evidence["sources"][1]["excerpt"] = excerpt
        observations[0]["evidence_items"][1]["excerpt_text"] = excerpt
        failed = EvidenceGate().evaluate(evidence, contract, observations, as_of, attempt_id="attempt")
        self.assertIn("blocking_requirement_lacks_numeric_facts:portfolio_market_state", failed["problems"])

    def test_m0_rejects_utc_clock_and_requires_local_quote_time_and_status(self):
        packet = {
            "calendar_context": {},
            "evidence_contract": {"requirements": [{"key": "portfolio_market_state", "required_entities": ["600487"]}]},
            "verified_fact_digest": [{"excerpt": json.dumps({"quotes": [{
                "symbol": "600487", "price": 67.97, "previous_close": 67.34, "change": 0.63,
                "change_percent": 0.9356, "quote_at": "2026-09-02T05:22:00Z", "status": "trading",
            }]})}],
        }
        output = {"semantic": {"summary": "600487 价格67.97，前收67.34，变动0.63，变动幅度0.9356%，处于交易状态，截至今天早上五点二十二分。",
                               "observations": [], "risks": [], "unknowns": []}}
        rejected = CognitiveRouter().verify("m0_compose", packet, output)
        self.assertIn("m0_portfolio_quote_time_conflict:600487", rejected["problems"])

        output["semantic"]["summary"] = "600487 价格67.97，前收67.34，变动0.63，变动幅度0.9356%，处于交易状态，北京时间13:22。"
        self.assertTrue(CognitiveRouter().verify("m0_compose", packet, output)["passed"])

    def test_rejects_foreign_reference_and_naive_runtime_time(self):
        foreign = EvidenceGate().evaluate(self._evidence("ev_other_1"), self.contract, self.observations, self.as_of, attempt_id="attempt-1")
        self.assertIn("source_ref_not_in_current_attempt", foreign["problems"])
        self.observations[0]["evidence_items"][0]["fact_as_of"] = "2026-08-25T15:00:00"
        naive = EvidenceGate().evaluate(self._evidence(), self.contract, self.observations, self.as_of, attempt_id="attempt-1")
        self.assertIn("source_fact_as_of_missing_timezone", naive["problems"])

    def test_historical_replay_allows_acquisition_after_frozen_as_of(self):
        self.observations[0]["evidence_items"][0]["acquired_at"] = "2026-08-27T07:20:00Z"
        self.observations[0]["evidence_items"][1]["acquired_at"] = "2026-08-27T07:20:00Z"

        result = EvidenceGate().evaluate(
            self._evidence(), self.contract, self.observations, self.as_of, attempt_id="attempt-1",
        )

        self.assertTrue(result["passed"], result["problems"])

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
            failed_event = next(event for event in store.pending_events() if event["event_type"] == "research.failed")
            payload = json.loads(failed_event["payload_json"])
            self.assertEqual("companion-published-message/v2", payload["message"]["contract"])
            with store.connection() as connection:
                checkpoint = connection.execute("SELECT 1 FROM stage_checkpoint WHERE cycle_id=?", (cycle["cycle_id"],)).fetchone()
            self.assertIsNone(checkpoint)
            self.assertIsNone(store.valid_daily_baseline("2026-08-26", self.as_of))

    def test_failed_research_attempt_persists_completed_tool_trace(self):
        trace = [{
            "backend": "gateway", "tool": "web_read", "status": "succeeded",
            "non_empty": True, "arguments": {"query": "A股 盘前 公告"},
        }]
        with TemporaryDirectory() as temporary:
            store = CompanionStore(Path(temporary) / "companion.sqlite3")
            cycle = CompanionEngine(store).start_cycle(
                "daily.opportunity.0900", "2026-08-26T09:00:00+08:00", self.as_of,
            )
            broker = Mock()
            broker.invoke.side_effect = BrokerError(
                "research stopped", category="research_loop_limit", tool_trace=trace,
            )
            settings = SimpleNamespace(research={}, broker={"url": "http://broker.test:8817"})
            packet = {
                "task_key": "daily.opportunity.0900", "stage": "m0_research", "as_of": self.as_of,
                "evidence_contract": self.contract,
            }
            with patch("ai_trading_companion.__main__.load_settings", return_value=settings), patch(
                "ai_trading_companion.__main__.ProviderBrokerClient", return_value=broker,
            ):
                with self.assertRaisesRegex(BrokerError, "research stopped"):
                    _call_stage(
                        store, cycle, "m0_research", packet,
                        "companion-research-evidence-v2.schema.json", search=True, timeout=60,
                    )

            attempt = store.attempts(cycle["cycle_id"])[0]
            self.assertEqual("failed", attempt["status"])
            self.assertEqual(trace, json.loads(attempt["tool_trace_json"]))

    def test_failed_stage_persists_broker_verifier_for_auditable_repair(self):
        verifier = {
            "passed": False,
            "name": "cognitive-router/m1_judgment",
            "schema": {"passed": False, "problems": ["$.snapshot.triggers: required"]},
            "business": {"passed": True, "problems": []},
        }
        with TemporaryDirectory() as temporary:
            store = CompanionStore(Path(temporary) / "companion.sqlite3")
            cycle = CompanionEngine(store).start_cycle(
                "daily.review.1520", "2026-08-31T15:20:00+08:00", self.as_of,
            )
            broker = Mock()
            broker.invoke.side_effect = BrokerError(
                "Broker output did not pass local verification",
                category="broker_output_invalid",
                verifier=verifier,
                attempts=[{"provider": "test", "status": "completed"}],
            )
            settings = SimpleNamespace(research={}, broker={"url": "http://broker.test:8817"})
            packet = {"task_key": cycle["task_key"], "stage": "m1_judgment", "as_of": self.as_of}

            with patch("ai_trading_companion.__main__.load_settings", return_value=settings), patch(
                "ai_trading_companion.__main__.ProviderBrokerClient", return_value=broker,
            ), self.assertRaisesRegex(BrokerError, "local verification"):
                _call_stage(
                    store, cycle, "m1_judgment", packet,
                    "companion-m1-result-v1.schema.json", search=False, timeout=60,
                )

            attempt = store.attempts(cycle["cycle_id"])[0]
            self.assertEqual(verifier, json.loads(attempt["verifier_json"]))
            self.assertEqual(6000, broker.invoke.call_args.args[0].output_token_limit)

    def test_m1_retry_policy_uses_four_attempts_and_supplies_verifier_feedback(self):
        verifier = {
            "passed": False,
            "schema": {"passed": False, "problems": ["$.snapshot.triggers: required"]},
            "business": {"passed": False, "problems": ["qualified_snapshot_lacks_execution_boundary"]},
        }
        error = BrokerError(
            "Broker output did not pass local verification",
            category="broker_output_invalid",
            verifier=verifier,
        )

        self.assertEqual(4, M1_MAX_JUDGMENT_ATTEMPTS)
        self.assertTrue(_m1_should_retry(error, attempt_number=1, remaining_seconds=300))
        self.assertTrue(_m1_should_retry(error, attempt_number=3, remaining_seconds=60))
        self.assertFalse(_m1_should_retry(error, attempt_number=4, remaining_seconds=300))
        self.assertFalse(_m1_should_retry(error, attempt_number=1, remaining_seconds=20))
        self.assertEqual(
            {
                "category": "broker_output_invalid",
                "schema_problems": ["$.snapshot.triggers: required"],
                "business_problems": ["qualified_snapshot_lacks_execution_boundary"],
            },
            _m1_retry_feedback(error),
        )

    def test_research_planning_uses_broker_and_never_calls_legacy_provider_tool_loop(self):
        with TemporaryDirectory() as temporary:
            store = CompanionStore(Path(temporary) / "companion.sqlite3")
            cycle = CompanionEngine(store).start_cycle(
                "daily.opportunity.0900", "2026-08-26T09:00:00+08:00", self.as_of,
            )
            settings = SimpleNamespace(research={}, broker={"url": "http://broker.test:8817"})
            packet = {
                "task_key": "daily.opportunity.0900", "stage": "m0_research", "as_of": self.as_of,
                "evidence_contract": self.contract,
            }

            broker = Mock()
            broker.invoke.side_effect = BrokerError("stop after inspecting request", category="test")
            with patch("ai_trading_companion.__main__.load_settings", return_value=settings), patch(
                "ai_trading_companion.__main__.ProviderBrokerClient", return_value=broker,
            ):
                with self.assertRaisesRegex(BrokerError, "stop after inspecting request"):
                    _call_stage(
                        store, cycle, "m0_research", packet,
                        "companion-research-evidence-v2.schema.json", search=True, timeout=60,
                    )

            request = broker.invoke.call_args.args[0]
            self.assertEqual("research", request.stage)
            self.assertFalse(request.visible_stream)
            self.assertIsNotNone(request.schema)


class _WeekdayCalendar:
    def is_trading_day(self, value: date) -> bool:
        return value.weekday() < 5
