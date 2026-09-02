from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from ai_trading_companion.evidence_contract import EvidenceContractFactory
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.packet_builder import RuntimePacketBuilder as _RuntimePacketBuilder
from ai_trading_companion.memory_port import InMemoryMemoryAdapter
from ai_trading_companion.store import CompanionStore
from ai_trading_companion.task_profiles import (
    AnalysisClarificationRequired,
    ManualAnalysisProfileResolver,
)


class ManualAnalysisProfileResolverTests(TestCase):
    def setUp(self) -> None:
        self.resolver = ManualAnalysisProfileResolver(_Calendar())
        self.analysis = {
            "subject": "中证人工智能指数",
            "time_scope": "current_session",
            "goal": "核验当前风险与机会",
        }

    def test_selects_profiles_for_premarket_intraday_lunch_and_post_close(self) -> None:
        self.assertEqual(
            "pre_market_opportunity",
            self.resolver.resolve("2026-08-28T09:15:00+08:00", self.analysis)["profile_id"],
        )
        self.assertEqual(
            "intraday_execution",
            self.resolver.resolve("2026-08-28T10:00:00+08:00", self.analysis)["profile_id"],
        )
        self.assertEqual(
            "lunch_break_analysis",
            self.resolver.resolve("2026-08-28T11:30:00+08:00", self.analysis)["profile_id"],
        )
        self.assertEqual(
            "post_close_review",
            self.resolver.resolve("2026-08-28T15:00:00+08:00", self.analysis)["profile_id"],
        )

    def test_selects_non_trading_profile_for_weekend_and_exchange_holiday(self) -> None:
        weekend = self.resolver.resolve("2026-08-29T10:00:00+08:00", {
            **self.analysis,
            "time_scope": "next_trading_session",
            "goal": "周末市场环境总结和下一交易日预判",
        })
        holiday = self.resolver.resolve("2026-10-01T10:00:00+08:00", {
            **self.analysis,
            "time_scope": "next_trading_session",
            "goal": "节假日市场环境总结和下一交易日预判",
        })

        for profile in (weekend, holiday):
            self.assertEqual("non_trading_outlook", profile["profile_id"])
            self.assertEqual("manual.non_trading_outlook", profile["task_key"])
            self.assertEqual("非交易日市场环境总结与下一交易日预判", profile["display_name"])

    def test_accepts_next_trading_session_during_pre_market(self) -> None:
        profile = self.resolver.resolve("2026-08-31T07:48:00+08:00", {
            "subject": "A股市场",
            "time_scope": "next_trading_session",
            "goal": "周末市场环境总结和周一预判",
        })

        self.assertEqual("pre_market_opportunity", profile["profile_id"])
        self.assertEqual("daily.opportunity.0900", profile["task_key"])
        self.assertEqual("周末市场环境总结和周一预判", profile["analysis"]["goal"])

    def test_normalizes_unambiguous_weekend_to_monday_scope_during_pre_market(self) -> None:
        raw_scope = "\u5f53\u524d\u5468\u672b\u81f3\u4e0b\u5468\u4e00\uff082026-08-30\u81f32026-08-31\uff09"
        profile = self.resolver.resolve("2026-08-31T08:10:00+08:00", {
            "subject": "A股市场",
            "time_scope": raw_scope,
            "goal": "周末市场环境总结和周一预判",
        })

        self.assertEqual("pre_market_opportunity", profile["profile_id"])
        self.assertEqual("next_trading_session", profile["analysis"]["time_scope"])
        self.assertEqual(raw_scope, profile["analysis"]["requested_time_scope"])

    def test_requires_explicit_subject_time_scope_and_goal(self) -> None:
        with self.assertRaisesRegex(AnalysisClarificationRequired, "time_scope"):
            self.resolver.resolve("2026-08-28T10:00:00+08:00", {"subject": "券商", "goal": "复核"})
        with self.assertRaisesRegex(AnalysisClarificationRequired, "does not match"):
            self.resolver.resolve(
                "2026-08-28T10:00:00+08:00",
                {**self.analysis, "time_scope": "post_close"},
            )

    def test_profile_freezes_manual_window_instead_of_a_schedule_time(self) -> None:
        profile = self.resolver.resolve("2026-08-28T14:58:00+08:00", self.analysis)

        deadline = self.resolver.delivery_deadlines(profile, "2026-08-28T15:08:00+08:00")

        self.assertEqual("2026-08-28T15:28:00+08:00", deadline["h0_auto_submit_at"])
        self.assertEqual("2026-08-28T15:38:00+08:00", deadline["m1_publish_deadline"])

    def test_evidence_contract_is_bound_to_the_selected_profile_and_hash(self) -> None:
        profile = self.resolver.resolve("2026-08-28T10:00:00+08:00", self.analysis)
        contract = EvidenceContractFactory(_Calendar()).build(
            task_key=profile["task_key"], stage="m0_research",
            as_of="2026-08-28T10:00:00+08:00", task_profile=profile,
        )

        self.assertEqual("intraday_execution", contract["task_profile"]["profile_id"])
        self.assertEqual("intraday_snapshot", contract["task_profile"]["evidence_family"])
        self.assertEqual("2026-08-28T01:45:00Z", contract["requirements"][0]["window"]["start"])
        self.assertEqual("after_start_to_end", contract["requirements"][0]["window"]["mode"])
        self.assertEqual(contract["contract_hash"], EvidenceContractFactory.contract_hash(contract))

    def test_post_close_manual_contract_uses_close_quotes_and_a_bounded_official_breadth_snapshot(self) -> None:
        profile = self.resolver.resolve("2026-09-02T15:20:00+08:00", self.analysis)
        contract = EvidenceContractFactory(_Calendar()).build(
            task_key=profile["task_key"], stage="m0_research",
            as_of="2026-09-02T15:20:00+08:00", task_profile=profile,
            internal_context={"portfolio_entities": ["600487"]},
        )
        requirements = {row["key"]: row for row in contract["requirements"]}

        self.assertEqual("official_close", requirements["market_breadth"]["finality"])
        self.assertEqual({
            "start": "2026-09-02T07:00:00Z", "end": "2026-09-02T07:20:00Z",
            "mode": "after_start_to_end",
        }, requirements["market_breadth"]["window"])
        self.assertEqual("official_close", requirements["portfolio_market_state"]["finality"])
        self.assertEqual("exact", requirements["portfolio_market_state"]["window"]["mode"])

    def test_lunch_contract_accepts_late_published_morning_close_and_checks_events_since_1030(self) -> None:
        profile = self.resolver.resolve("2026-08-31T12:51:12.238+08:00", {
            **self.analysis, "time_scope": "lunch_break",
        })
        contract = EvidenceContractFactory(_Calendar()).build(
            task_key=profile["task_key"], stage="m0_research",
            as_of="2026-08-31T12:51:12.238+08:00", task_profile=profile,
        )
        market, events = contract["requirements"]

        self.assertEqual({
            "start": "2026-08-31T03:15:00Z",
            "end": "2026-08-31T04:51:12.238000Z",
            "mode": "after_start_to_end",
        }, market["window"])
        self.assertEqual("2026-08-31T02:30:00Z", events["window"]["start"])
        self.assertEqual(["公告", "政策", "风险"], events["negative_query_terms"])

    def test_weekend_request_creates_a_distinct_outlook_cycle_and_packet(self) -> None:
        calendar = _Calendar()
        with TemporaryDirectory() as temporary:
            store = CompanionStore(Path(temporary) / "runtime.sqlite3")
            engine = CompanionEngine(
                store,
                task_profiles=ManualAnalysisProfileResolver(calendar),
                evidence_contract_factory=EvidenceContractFactory(calendar),
            )
            created = engine.request_formal_analysis({
                "request_id": "weekend-outlook", "requested_at": "2026-08-29T10:00:00+08:00",
                "source": {"conversation_cycle_id": "conversation", "batch_id": "batch", "message_id": "message", "source_artifact_id": "artifact", "source_span": {"start": 0, "end": 1}},
                "analysis": {"subject": "A股市场", "time_scope": "next_trading_session", "goal": "周末市场环境总结和下一交易日预判"},
            })
            cycle = created["projection"]["cycle"]
            packet = RuntimePacketBuilder(Path("resources"), Path(temporary), store).build(cycle, "m0_compose")

            self.assertEqual("manual.non_trading_outlook", cycle["task_key"])
            self.assertEqual("non_trading_outlook", cycle["task_profile_id"])
            self.assertEqual("非交易日市场环境总结与下一交易日预判", packet["task_profile"]["display_name"])
            self.assertEqual("non_trading_outlook", packet["protocol"]["protocol_id"])


class _Calendar:
    def is_trading_day(self, value: date) -> bool:
        return value.weekday() < 5 and value != date(2026, 10, 1)
def RuntimePacketBuilder(*args, **kwargs):
    kwargs.setdefault("memory", InMemoryMemoryAdapter())
    return _RuntimePacketBuilder(*args, **kwargs)
