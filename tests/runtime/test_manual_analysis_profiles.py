from datetime import date
from unittest import TestCase

from ai_trading_companion.evidence_contract import EvidenceContractFactory
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
        self.assertEqual(
            "non_trading_research",
            self.resolver.resolve("2026-08-29T10:00:00+08:00", self.analysis)["profile_id"],
        )
        self.assertEqual(
            "non_trading_research",
            self.resolver.resolve("2026-10-01T10:00:00+08:00", self.analysis)["profile_id"],
        )

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
        self.assertEqual("2026-08-28T02:00:00Z", contract["requirements"][0]["window"]["start"])
        self.assertEqual(contract["contract_hash"], EvidenceContractFactory.contract_hash(contract))


class _Calendar:
    def is_trading_day(self, value: date) -> bool:
        return value.weekday() < 5 and value != date(2026, 10, 1)
