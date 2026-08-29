from __future__ import annotations

import json
import inspect
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from ai_trading_companion.effort_policy import CognitiveEffortPolicy, EffortPolicyFacts
from ai_trading_companion.exchange import LocalExchange
from ai_trading_companion.governance import EvolutionGovernance, RouterGovernance, StrategyPolicyExecutor
from ai_trading_companion.observatory import (
    EvaluationObservatory, EvaluationRequest, ExperimentRequest, ForecastRequest, SnapshotQuery,
)
from ai_trading_companion.router import CognitiveRouter
from ai_trading_companion.runtime_strategy_policy import RuntimeStrategyPolicy
from ai_trading_companion.store import CompanionStore


class EvaluationObservatoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = CompanionStore(Path(self.temp.name) / "companion.sqlite3")
        self.store.initialize()
        self.observatory = EvaluationObservatory(self.store)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_0945_m0_published_at_1030_is_a_qualified_delivery(self) -> None:
        with patch("ai_trading_companion.store.now", return_value="2026-08-25T09:45:00+08:00"):
            cycle = self.store.create_cycle(
                "daily.execution.0945",
                "2026-08-25T09:45:00+08:00",
                "2026-08-25T09:45:00+08:00",
            )
        with patch("ai_trading_companion.store.now", return_value="2026-08-25T09:50:00+08:00"):
            self.store.queue_event(cycle["cycle_id"], "m0.started", {"cycle_id": cycle["cycle_id"]})
        with patch("ai_trading_companion.store.now", return_value="2026-08-25T09:51:00+08:00"):
            evidence = self.store.begin_attempt(cycle["cycle_id"], "m0_research", cycle["as_of"], "evidence")
        with patch("ai_trading_companion.store.now", return_value="2026-08-25T09:55:00+08:00"):
            self.store.finish_attempt(evidence["attempt_id"], "succeeded", output={"sources": []}, verifier={"passed": True})
        with patch("ai_trading_companion.store.now", return_value="2026-08-25T10:25:00+08:00"):
            compose = self.store.begin_attempt(cycle["cycle_id"], "m0_compose", cycle["as_of"], "compose")
        with patch("ai_trading_companion.store.now", return_value="2026-08-25T10:29:00+08:00"):
            self.store.finish_attempt(compose["attempt_id"], "succeeded", output={"m0_markdown": "M0"}, verifier={"passed": True})
        with patch("ai_trading_companion.store.now", return_value="2026-08-25T10:30:00+08:00"):
            self.store.append_artifact(
                cycle["cycle_id"], "m0", "model", "M0", cycle["as_of"],
                {"evidence_attempt_id": evidence["attempt_id"], "compose_attempt_id": compose["attempt_id"]},
            )

        snapshot = self.observatory.evaluate(EvaluationRequest(
            cycle_id=cycle["cycle_id"], observed_at="2026-08-25T10:31:00+08:00",
        ))

        self.assertEqual("qualified", snapshot.delivery_state)
        self.assertEqual("2026-08-25T09:45:00+08:00", snapshot.planned_start_at)
        self.assertEqual("2026-08-25T09:50:00+08:00", snapshot.actual_start_at)
        self.assertEqual("2026-08-25T10:30:00+08:00", snapshot.qualified_published_at)
        self.assertEqual(40 * 60, snapshot.qualified_duration_seconds)
        self.assertEqual(snapshot.snapshot_id, self.observatory.get_snapshot(snapshot.snapshot_id).snapshot_id)

    def test_public_observatory_surface_is_limited_to_the_five_contract_methods(self) -> None:
        methods = {
            name for name, value in inspect.getmembers(EvaluationObservatory, predicate=callable)
            if not name.startswith("_")
        }
        self.assertEqual({"evaluate", "forecast", "assess_experiment", "get_snapshot", "query"}, methods)

    def test_timeline_replay_is_ordered_idempotent_and_does_not_invent_failure_duration(self) -> None:
        with patch("ai_trading_companion.store.now", return_value="2026-08-25T09:45:00+08:00"):
            cycle = self.store.create_cycle(
                "daily.execution.0945", "2026-08-25T09:45:00+08:00", "2026-08-25T09:45:00+08:00",
            )
        with patch("ai_trading_companion.store.now", return_value="2026-08-25T09:50:00+08:00"):
            self.store.queue_event(cycle["cycle_id"], "m0.started", {"cycle_id": cycle["cycle_id"]})
            attempt = self.store.begin_attempt(cycle["cycle_id"], "m0_research", cycle["as_of"], "packet")
        search = {
            "event_id": "tool-search-1", "tool": "web_search", "status": "succeeded",
            "started_at": "2026-08-25T09:52:00+08:00", "completed_at": "2026-08-25T09:53:00+08:00",
        }
        browser = {
            "event_id": "tool-browser-1", "tool": "web_browser", "status": "failed",
            "started_at": "2026-08-25T09:54:00+08:00",
        }
        with patch("ai_trading_companion.store.now", return_value="2026-08-25T09:55:00+08:00"):
            self.store.finish_attempt(
                attempt["attempt_id"], "failed", error="browser unavailable",
                verifier={"passed": False}, tool_trace=[browser, search, search],
            )

        snapshot = self.observatory.evaluate(EvaluationRequest(
            cycle_id=cycle["cycle_id"], observed_at="2026-08-25T10:31:00+08:00",
        ))

        tool_events = [event for event in snapshot.timeline if event.source_kind == "tool_trace"]
        self.assertEqual(["tool-search-1", "tool-browser-1"], [event.event_id for event in tool_events])
        self.assertEqual(60, tool_events[0].duration_seconds)
        self.assertIsNone(tool_events[1].duration_seconds)
        self.assertEqual("failed", snapshot.delivery_state)
        self.assertIsNone(snapshot.qualified_duration_seconds)

    def test_forecast_combines_wilson_probability_with_conditional_empirical_quantiles(self) -> None:
        durations = (600, 1200, 1800)
        for index, duration in enumerate(durations, start=1):
            day = f"2026-08-{20 + index:02d}"
            scheduled = f"{day}T09:45:00+08:00"
            with patch("ai_trading_companion.store.now", return_value=scheduled):
                cycle = self.store.create_cycle("daily.execution.0945", scheduled, scheduled)
                self.store.save_market_regime(cycle["cycle_id"], scheduled, "trend_expansion", {}, "complete")
            started = f"{day}T09:50:00+08:00"
            with patch("ai_trading_companion.store.now", return_value=started):
                self.store.queue_event(cycle["cycle_id"], "m0.started", {})
                attempt = self.store.begin_attempt(cycle["cycle_id"], "m0_compose", scheduled, f"packet-{index}")
            published = (datetime.fromisoformat(started) + timedelta(seconds=duration)).isoformat()
            with patch("ai_trading_companion.store.now", return_value=published):
                self.store.finish_attempt(attempt["attempt_id"], "succeeded", output={"m0_markdown": "M0"}, verifier={"passed": True})
                self.store.append_artifact(
                    cycle["cycle_id"], "m0", "model", f"M0-{index}", scheduled,
                    {"compose_attempt_id": attempt["attempt_id"]},
                )
            self.observatory.evaluate(EvaluationRequest(cycle["cycle_id"], f"{day}T10:31:00+08:00"))

        failed_day = "2026-08-24"
        with patch("ai_trading_companion.store.now", return_value=f"{failed_day}T09:45:00+08:00"):
            failed = self.store.create_cycle(
                "daily.execution.0945", f"{failed_day}T09:45:00+08:00", f"{failed_day}T09:45:00+08:00",
            )
            self.store.save_market_regime(failed["cycle_id"], failed["as_of"], "trend_expansion", {}, "complete")
        with patch("ai_trading_companion.store.now", return_value=f"{failed_day}T09:50:00+08:00"):
            self.store.queue_event(failed["cycle_id"], "m0.started", {})
            attempt = self.store.begin_attempt(failed["cycle_id"], "m0_research", failed["as_of"], "failed")
        with patch("ai_trading_companion.store.now", return_value=f"{failed_day}T10:00:00+08:00"):
            self.store.finish_attempt(attempt["attempt_id"], "failed", error="dependency unavailable")
        self.observatory.evaluate(EvaluationRequest(failed["cycle_id"], f"{failed_day}T10:31:00+08:00"))

        forecast = self.observatory.forecast(ForecastRequest(
            task_key="daily.execution.0945", stage="m0", market_regime="trend_expansion",
            observed_at="2026-08-25T09:45:00+08:00",
        ))

        self.assertEqual("same_task_stage_market_regime", forecast.fallback_level)
        self.assertEqual(4, forecast.sample_size)
        self.assertEqual(0.75, forecast.qualified_probability)
        self.assertAlmostEqual(0.3562, forecast.wilson_90_low, places=4)
        self.assertAlmostEqual(0.9421, forecast.wilson_90_high, places=4)
        self.assertEqual((600, 1200, 1800, 1800, 1800), (
            forecast.min_seconds, forecast.p50_seconds, forecast.p90_seconds,
            forecast.p95_seconds, forecast.max_seconds,
        ))
        self.assertEqual(0.25, forecast.no_qualified_delivery_risk)

    def test_settled_delivery_appends_calibration_without_overwriting_the_forecast(self) -> None:
        history_day = "2026-08-25"
        with patch("ai_trading_companion.store.now", return_value=f"{history_day}T09:45:00+08:00"):
            history = self.store.create_cycle(
                "daily.execution.0945", f"{history_day}T09:45:00+08:00", f"{history_day}T09:45:00+08:00",
            )
        with patch("ai_trading_companion.store.now", return_value=f"{history_day}T09:50:00+08:00"):
            self.store.queue_event(history["cycle_id"], "m0.started", {})
            history_attempt = self.store.begin_attempt(history["cycle_id"], "m0_compose", history["as_of"], "history")
        with patch("ai_trading_companion.store.now", return_value=f"{history_day}T10:10:00+08:00"):
            self.store.finish_attempt(history_attempt["attempt_id"], "succeeded", output={"m0_markdown": "M0"}, verifier={"passed": True})
            self.store.append_artifact(
                history["cycle_id"], "m0", "model", "history M0", history["as_of"],
                {"compose_attempt_id": history_attempt["attempt_id"]},
            )
        self.observatory.evaluate(EvaluationRequest(history["cycle_id"], f"{history_day}T10:31:00+08:00"))

        day = "2026-08-26"
        scheduled = f"{day}T09:45:00+08:00"
        with patch("ai_trading_companion.store.now", return_value=scheduled):
            cycle = self.store.create_cycle("daily.execution.0945", scheduled, scheduled)
        with patch("ai_trading_companion.store.now", return_value=f"{day}T09:50:00+08:00"):
            self.store.queue_event(cycle["cycle_id"], "m0.started", {})
            attempt = self.store.begin_attempt(cycle["cycle_id"], "m0_compose", scheduled, "target")
        self.observatory.evaluate(EvaluationRequest(cycle["cycle_id"], f"{day}T10:00:00+08:00"))
        forecast = self.observatory.forecast(ForecastRequest(
            task_key=cycle["task_key"], stage="m0", cycle_id=cycle["cycle_id"],
            observed_at=f"{day}T10:00:00+08:00",
        ))

        with patch("ai_trading_companion.store.now", return_value=f"{day}T10:20:00+08:00"):
            self.store.finish_attempt(attempt["attempt_id"], "succeeded", output={"m0_markdown": "M0"}, verifier={"passed": True})
            self.store.append_artifact(
                cycle["cycle_id"], "m0", "model", "M0", scheduled,
                {"compose_attempt_id": attempt["attempt_id"]},
            )
        settled = self.observatory.evaluate(EvaluationRequest(cycle["cycle_id"], f"{day}T10:31:00+08:00"))

        summaries = self.observatory.query(SnapshotQuery(
            snapshot_kind="forecast_calibration", cycle_id=cycle["cycle_id"],
        ))
        self.assertEqual(1, len(summaries))
        calibration = self.observatory.get_snapshot(summaries[0].snapshot_id)
        self.assertEqual(forecast.snapshot_id, calibration.forecast_snapshot_id)
        self.assertEqual(settled.snapshot_id, calibration.evaluation_snapshot_id)
        self.assertEqual(1, calibration.observed_qualified)
        self.assertAlmostEqual(abs(1 - forecast.qualified_probability), calibration.absolute_calibration_error)
        self.assertEqual(forecast, self.observatory.get_snapshot(forecast.snapshot_id))

    def test_research_quality_keeps_gate_dimensions_separate_and_explains_rejection(self) -> None:
        scheduled = "2026-08-27T09:45:00+08:00"
        with patch("ai_trading_companion.store.now", return_value=scheduled):
            cycle = self.store.create_cycle("daily.execution.0945", scheduled, scheduled)
            self.store.record_evidence(cycle, "m0_research", {
                "sources": [
                    {"url": "https://a.test/fact", "title": "A", "excerpt": "fact A", "source_family": "exchange", "published_at": "2026-08-27T09:30:00+08:00"},
                    {"url": "https://b.test/fact", "title": "B", "excerpt": "fact B", "source_family": "media", "published_at": "2026-08-27T08:30:00+08:00"},
                ],
                "critical_gaps": ["northbound flow missing"],
            })
        with patch("ai_trading_companion.store.now", return_value="2026-08-27T09:50:00+08:00"):
            self.store.queue_event(cycle["cycle_id"], "m0.started", {})
            attempt = self.store.begin_attempt(cycle["cycle_id"], "m0_research", scheduled, "quality")
        verifier = {
            "passed": False,
            "evidence_gate": {
                "passed": False,
                "problems": ["blocking_requirement_stale:news", "source_from_future"],
                "normalized_evidence": {
                    "as_of": scheduled,
                    "sources": [
                        {"independence_group": "exchange", "fact_as_of": "2026-08-27T09:30:00+08:00"},
                        {"independence_group": "media", "fact_as_of": "2026-08-27T08:30:00+08:00"},
                    ],
                    "coverage": [
                        {"requirement_key": "price", "status": "covered"},
                        {"requirement_key": "news", "status": "missing"},
                    ],
                    "conflicts": [{"topic": "direction"}],
                },
            },
        }
        with patch("ai_trading_companion.store.now", return_value="2026-08-27T10:00:00+08:00"):
            self.store.finish_attempt(attempt["attempt_id"], "rejected", verifier=verifier)

        snapshot = self.observatory.evaluate(EvaluationRequest(cycle["cycle_id"], "2026-08-27T10:31:00+08:00"))

        quality = snapshot.research_quality
        self.assertEqual(0.5, quality.evidence_coverage)
        self.assertEqual(4500, quality.max_freshness_age_seconds)
        self.assertEqual(2, quality.independent_source_groups)
        self.assertEqual(1, quality.conflict_count)
        self.assertEqual(1, quality.factual_error_count)
        self.assertFalse(quality.evidence_gate_passed)
        self.assertEqual(("blocking_requirement_stale:news", "source_from_future"), quality.rejection_reasons)

    def test_prefetch_evidence_is_attributed_without_merging_0900_and_0945_cycles(self) -> None:
        day = "2026-08-27"
        with patch("ai_trading_companion.store.now", return_value=f"{day}T09:00:00+08:00"):
            prefetch = self.store.create_cycle("daily.opportunity.0900", f"{day}T09:00:00+08:00", f"{day}T09:00:00+08:00")
        source = {"url": "https://exchange.test/a", "title": "公告", "excerpt": "same fact"}
        with patch("ai_trading_companion.store.now", return_value=f"{day}T09:10:00+08:00"):
            self.store.record_evidence(prefetch, "m0_research", {"sources": [source]})
        with patch("ai_trading_companion.store.now", return_value=f"{day}T09:45:00+08:00"):
            m0 = self.store.create_cycle("daily.execution.0945", f"{day}T09:45:00+08:00", f"{day}T09:45:00+08:00")
        with patch("ai_trading_companion.store.now", return_value=f"{day}T09:50:00+08:00"):
            self.store.queue_event(m0["cycle_id"], "m0.started", {})
            self.store.record_evidence(m0, "m0_research", {"sources": [source]})

        snapshot = self.observatory.evaluate(EvaluationRequest(m0["cycle_id"], f"{day}T10:00:00+08:00"))

        self.assertEqual("daily.execution.0945", snapshot.task_key)
        self.assertEqual(1, len(snapshot.shared_evidence))
        shared = snapshot.shared_evidence[0]
        self.assertEqual(prefetch["cycle_id"], shared.source_cycle_id)
        self.assertEqual("daily.opportunity.0900", shared.source_task_key)
        self.assertEqual(40 * 60, shared.age_at_m0_start_seconds)
        self.assertIsNone(snapshot.qualified_duration_seconds)

    def test_forecast_stage_trigger_deduplicates_unchanged_context_and_appends_after_change(self) -> None:
        scheduled = "2026-08-27T09:45:00+08:00"
        with patch("ai_trading_companion.store.now", return_value=scheduled):
            cycle = self.store.create_cycle("daily.execution.0945", scheduled, scheduled)

        first = self.observatory.forecast(ForecastRequest(
            task_key=cycle["task_key"], cycle_id=cycle["cycle_id"], observed_at=scheduled,
            trigger="stage:m0_started",
        ))
        repeated = self.observatory.forecast(ForecastRequest(
            task_key=cycle["task_key"], cycle_id=cycle["cycle_id"], observed_at="2026-08-27T09:46:00+08:00",
            trigger="dependency:healthy",
        ))
        with patch("ai_trading_companion.store.now", return_value="2026-08-27T09:47:00+08:00"):
            self.store.begin_attempt(cycle["cycle_id"], "m0_research", scheduled, "packet")
        changed = self.observatory.forecast(ForecastRequest(
            task_key=cycle["task_key"], cycle_id=cycle["cycle_id"], observed_at="2026-08-27T09:47:00+08:00",
            trigger="stage:m0_research_started",
        ))

        self.assertEqual(first.snapshot_id, repeated.snapshot_id)
        self.assertNotEqual(first.snapshot_id, changed.snapshot_id)
        self.assertEqual("stage:m0_research_started", changed.trigger)
        self.assertIsNotNone(changed.target_context_fingerprint)

    def test_forecast_emits_a_read_only_precise_tasks_json_patch(self) -> None:
        schedule_path = Path(self.temp.name) / "tasks.json"
        schedule_path.write_text(json.dumps({"daily": [
            {"task_key": "daily.opportunity.0900", "at": "09:00"},
            {"task_key": "daily.execution.0945", "at": "09:45"},
        ]}), encoding="utf-8")
        observatory = EvaluationObservatory(self.store, schedule_path=schedule_path)
        original = schedule_path.read_bytes()
        for day in range(1, 10):
            scheduled = f"2026-08-{day:02d}T09:45:00+08:00"
            with patch("ai_trading_companion.store.now", return_value=scheduled):
                cycle = self.store.create_cycle("daily.execution.0945", scheduled, scheduled)
            with patch("ai_trading_companion.store.now", return_value=f"2026-08-{day:02d}T09:50:00+08:00"):
                self.store.queue_event(cycle["cycle_id"], "m0.started", {})
                attempt = self.store.begin_attempt(cycle["cycle_id"], "m0_compose", scheduled, f"packet-{day}")
            with patch("ai_trading_companion.store.now", return_value=f"2026-08-{day:02d}T10:00:00+08:00"):
                self.store.finish_attempt(attempt["attempt_id"], "succeeded", output={"m0_markdown": "M0"}, verifier={"passed": True})
                self.store.append_artifact(cycle["cycle_id"], "m0", "model", "M0", scheduled, {"compose_attempt_id": attempt["attempt_id"]})
            observatory.evaluate(EvaluationRequest(cycle["cycle_id"], f"2026-08-{day:02d}T10:31:00+08:00"))

        forecast = observatory.forecast(ForecastRequest(task_key="daily.execution.0945"))
        recommendation = forecast.schedule_start_recommendation

        self.assertEqual("delay", recommendation.action)
        self.assertEqual("10:00", recommendation.proposed_start_at)
        self.assertEqual(({"op": "replace", "path": "/daily/1/at", "value": "10:00"},), recommendation.json_patch)
        self.assertTrue(recommendation.requires_explicit_apply)
        self.assertEqual(original, schedule_path.read_bytes())

    def test_legacy_backfill_marks_derived_history_incomplete_without_inventing_a_duration(self) -> None:
        scheduled = "2026-08-27T09:45:00+08:00"
        with patch("ai_trading_companion.store.now", return_value=scheduled):
            cycle = self.store.create_cycle("daily.execution.0945", scheduled, scheduled)

        report = self.observatory._backfill_legacy()
        summary = self.observatory.query(SnapshotQuery(snapshot_kind="evaluation", cycle_id=cycle["cycle_id"]))[0]
        snapshot = self.observatory.get_snapshot(summary.snapshot_id)

        self.assertEqual(1, report["evaluation_snapshots"])
        self.assertTrue(snapshot.legacy_incomplete)
        self.assertIn("legacy_incomplete", snapshot.completeness)
        self.assertIsNone(snapshot.qualified_duration_seconds)

    def test_judgment_outcomes_preserve_the_original_conditions_at_each_horizon(self) -> None:
        scheduled = "2026-08-28T09:45:00+08:00"
        with patch("ai_trading_companion.store.now", return_value=scheduled):
            cycle = self.store.create_cycle("daily.execution.0945", scheduled, scheduled)
            artifact = self.store.append_artifact(cycle["cycle_id"], "m1", "model", "original bullish", scheduled)
            judgment = self.store.save_judgment_snapshot(
                artifact["artifact_id"], cycle["cycle_id"], "m1",
                {
                    "direction": "bullish", "triggers": ["volume expansion"],
                    "invalidations": ["break opening low"], "market_regime": "trend_expansion",
                    "original_judgment_text": "original bullish",
                    "strategy_policy_version": "cognitive-effort/v1",
                },
                scheduled,
            )
            checkpoints = {
                horizon: self.store.schedule_outcome(judgment["snapshot_id"], horizon, due_at)
                for horizon, due_at in (
                    ("T+1", "2026-08-29T15:00:00+08:00"),
                    ("T+3", "2026-08-31T15:00:00+08:00"),
                    ("T+5", "2026-09-02T15:00:00+08:00"),
                )
            }
        with patch("ai_trading_companion.store.now", return_value="2026-08-29T15:00:00+08:00"):
            result = self.store.append_artifact(cycle["cycle_id"], "outcome", "model", "direction verified", "2026-08-29T15:00:00+08:00")
            self.store.complete_outcome(checkpoints["T+1"]["checkpoint_id"], "2026-08-29T15:00:00+08:00", {
                "verification_status": "correct", "summary": "rose after trigger", "data_gaps": [],
            }, result["artifact_id"])

        snapshot = self.observatory.evaluate(EvaluationRequest(cycle["cycle_id"], "2026-08-29T15:01:00+08:00"))

        outcomes = {outcome.horizon: outcome for outcome in snapshot.judgment_outcomes}
        self.assertEqual({"T+1", "T+3", "T+5"}, set(outcomes))
        self.assertEqual("bullish", outcomes["T+1"].original_direction)
        self.assertEqual("original bullish", outcomes["T+1"].original_judgment_text)
        self.assertEqual("cognitive-effort/v1", outcomes["T+1"].original_policy_version)
        self.assertEqual(("volume expansion",), outcomes["T+1"].original_triggers)
        self.assertEqual(("break opening low",), outcomes["T+1"].original_invalidations)
        self.assertEqual("trend_expansion", outcomes["T+1"].original_market_regime)
        self.assertEqual("correct", outcomes["T+1"].verification_status)
        self.assertEqual("pending", outcomes["T+3"].checkpoint_status)
        self.assertIsNone(outcomes["T+3"].verification_status)

    def test_versioned_exchange_publishes_an_observatory_snapshot_without_database_access(self) -> None:
        exchange = LocalExchange(Path(self.temp.name) / "exchange")
        observatory = EvaluationObservatory(self.store, exchange=exchange)
        scheduled = "2026-08-29T09:45:00+08:00"
        with patch("ai_trading_companion.store.now", return_value=scheduled):
            cycle = self.store.create_cycle("daily.execution.0945", scheduled, scheduled)
            self.store.transition(cycle["cycle_id"], "missed")

        snapshot = observatory.evaluate(EvaluationRequest(cycle["cycle_id"], "2026-08-29T10:31:00+08:00"))

        messages = list((Path(self.temp.name) / "exchange" / "to-client" / "pending").glob("*.json"))
        self.assertEqual(1, len(messages))
        payload = json.loads(messages[0].read_text(encoding="utf-8"))
        self.assertEqual("evaluation-observatory-snapshot/v1", payload["contract"])
        self.assertEqual(1, payload["contract_version"])
        self.assertEqual(snapshot.snapshot_id, payload["snapshot"]["snapshot_id"])
        self.assertNotIn("model", json.dumps(payload["snapshot"]))
        self.assertNotIn("token", json.dumps(payload["snapshot"]))

    def test_snapshot_request_is_idempotent_and_conflicts_when_its_input_changes(self) -> None:
        scheduled = "2026-08-30T09:45:00+08:00"
        with patch("ai_trading_companion.store.now", return_value=scheduled):
            cycle = self.store.create_cycle("daily.execution.0945", scheduled, scheduled)
        request = EvaluationRequest(
            cycle["cycle_id"], "2026-08-30T10:00:00+08:00", request_id="evaluation-request-1",
        )

        first = self.observatory.evaluate(request)
        replay = self.observatory.evaluate(request)

        self.assertEqual(first.snapshot_id, replay.snapshot_id)
        with patch("ai_trading_companion.store.now", return_value="2026-08-30T10:01:00+08:00"):
            self.store.queue_event(cycle["cycle_id"], "research.retrying", {"attempt": 2})
        with self.assertRaisesRegex(ValueError, "request id conflict"):
            self.observatory.evaluate(request)


class CognitiveEffortPolicyTests(unittest.TestCase):
    @staticmethod
    def _facts(**overrides):
        values = {
            "cell_key": "m0:daily.execution.0945", "family": "research", "stage": "m0_research",
            "major": False, "evidence_gaps": 0, "source_count": 3, "source_conflicts": 0,
            "high_impact_events": 0, "data_blocked": False, "deadline_seconds": 180,
            "dependency_health": "healthy", "market_regime": "range",
        }
        values.update(overrides)
        return EffortPolicyFacts(**values)

    def test_first_policy_exposes_stable_strata_and_only_proposes_known_adjacent_effort(self) -> None:
        policy = CognitiveEffortPolicy.bootstrap()

        routine = policy.select(self._facts())
        sparse = policy.select(self._facts(source_count=1))
        tight = policy.select(self._facts(source_count=1, deadline_seconds=90))
        blocked = policy.select(self._facts(source_count=1, data_blocked=True))

        self.assertEqual("routine", routine.stratum)
        self.assertIsNone(policy.propose_shadow(routine))
        self.assertEqual("evidence_sparse", sparse.stratum)
        self.assertEqual("high", policy.propose_shadow(sparse).effort)
        self.assertEqual("deadline_tight", tight.stratum)
        self.assertIsNone(policy.propose_shadow(tight))
        self.assertEqual("data_blocked", blocked.stratum)
        self.assertIsNone(policy.propose_shadow(blocked))

    def test_same_frozen_facts_are_deterministic_and_major_work_only_changes_the_shadow_candidate(self) -> None:
        policy = CognitiveEffortPolicy.bootstrap()
        facts = EffortPolicyFacts(
            cell_key="m1:daily.execution.1430", family="judgment", stage="m1_judgment",
            major=True, evidence_gaps=0, source_count=3, source_conflicts=0,
            high_impact_events=1, data_blocked=False, deadline_seconds=300,
            dependency_health="healthy", market_regime="trend_expansion",
        )

        first = policy.select(facts, mode="shadow")
        replay = policy.select(facts, mode="shadow")
        promoted = policy.select(facts, mode="promoted")

        self.assertEqual("medium", first.selected_effort)
        self.assertEqual("xhigh", first.candidate_effort)
        self.assertEqual("xhigh", promoted.selected_effort)
        self.assertEqual("cognitive-effort/v1", first.policy_version)
        self.assertEqual(first.input_fingerprint, replay.input_fingerprint)
        self.assertEqual(first, replay)

    def test_router_exposes_the_policy_version_and_frozen_input_on_each_route(self) -> None:
        router = CognitiveRouter(effort_policy=CognitiveEffortPolicy.bootstrap())

        plan = router.plan(
            "m1_judgment",
            {"task_key": "daily.execution.1430", "evidence": {"sources": [{}, {}, {}], "high_impact_events": [{}]}},
            300,
            False,
        )

        self.assertEqual("cognitive-effort/v1", plan.baseline.effort_policy_version)
        self.assertEqual(plan.baseline.effort_input_fingerprint, plan.candidate.effort_input_fingerprint)
        self.assertEqual("medium", plan.selected.reasoning_effort)
        self.assertEqual("xhigh", plan.candidate.reasoning_effort)

    def test_llm_attempt_freezes_the_active_effort_policy_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CompanionStore(Path(directory) / "companion.sqlite3")
            store.initialize()
            policy = CognitiveEffortPolicy.load(store)
            router = CognitiveRouter(effort_policy=policy)
            cycle = store.create_cycle(
                "daily.execution.0945", "2026-08-25T09:45:00+08:00", "2026-08-25T09:45:00+08:00",
            )
            decision = router.route("m0_research", {"task_key": cycle["task_key"]}, 300, True)

            store.begin_attempt(
                cycle["cycle_id"], "m0_research", cycle["as_of"], "packet",
                reasoning_effort=decision.reasoning_effort,
                effort_policy_version=decision.effort_policy_version,
                effort_input_fingerprint=decision.effort_input_fingerprint,
            )

            attempt = store.attempts(cycle["cycle_id"])[0]
            self.assertEqual("cognitive-effort/v1", attempt["effort_policy_version"])
            self.assertEqual(decision.effort_input_fingerprint, attempt["effort_input_fingerprint"])


class ExperimentAssessmentTests(unittest.TestCase):
    @staticmethod
    def _assess(rows: list[tuple[str, dict, dict]], *, source_kind: str = "live_paired_shadow"):
        with tempfile.TemporaryDirectory() as directory:
            store = CompanionStore(Path(directory) / "companion.sqlite3")
            store.initialize()
            observatory = EvaluationObservatory(store)
            key = "m1:daily.execution.1430"
            for index, (regime, baseline, candidate) in enumerate(rows):
                store.record_router_evaluation(
                    key, f"cycle-{index}", "T+1", regime, None, f"shadow-{index}",
                    baseline, candidate, "resolved",
                )
            return observatory.assess_experiment(ExperimentRequest(key, source_kind=source_kind))

    def test_assessment_preserves_each_dimension_without_a_total_score(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CompanionStore(Path(directory) / "companion.sqlite3")
            store.initialize()
            observatory = EvaluationObservatory(store)
            key = "m1:daily.execution.1430"
            for index, regime in enumerate(("trend_expansion", "divergence"), start=1):
                store.record_router_evaluation(
                    key, f"cycle-{index}", "T+1", regime, None, f"shadow-{index}",
                    {
                        "value": 0.5, "duration_seconds": 120, "qualified": True,
                        "research_quality": 0.80, "cost": 1.0, "stability": 0.95,
                    },
                    {
                        "value": 0.7, "duration_seconds": 90, "qualified": True,
                        "research_quality": 0.86, "cost": 1.2, "stability": 0.96,
                    },
                    "resolved",
                )

            assessment = observatory.assess_experiment(ExperimentRequest(key))

            self.assertEqual(2, assessment.paired_runs)
            self.assertEqual(-30, assessment.delivery_speed.mean_delta)
            self.assertAlmostEqual(0.06, assessment.research_quality.mean_delta)
            self.assertAlmostEqual(0.20, assessment.judgment_outcome.mean_delta)
            self.assertAlmostEqual(0.20, assessment.cost.mean_delta)
            self.assertEqual(("divergence", "trend_expansion"), assessment.market_regimes)
            self.assertFalse(hasattr(assessment, "total_score"))

    def test_dynamic_maturity_can_reject_four_runs_and_keep_six_runs_insufficient(self) -> None:
        baseline = {"value": .8, "duration_seconds": 120, "qualified": True, "research_quality": .8, "cost": 1, "stability": .95}
        degraded = {"value": .7, "duration_seconds": 90, "qualified": True, "research_quality": .5, "cost": 1, "stability": .9, "hard_fault": True}
        rejected = self._assess([
            (("trend_expansion", "divergence", "risk_contraction")[index % 3], baseline, degraded)
            for index in range(4)
        ])
        self.assertEqual("reject", rejected.decision)

        improved = {"value": .9, "duration_seconds": 90, "qualified": True, "research_quality": .9, "cost": .9, "stability": .96}
        insufficient = self._assess([("trend_expansion", baseline, improved) for _ in range(6)])
        self.assertEqual("insufficient_evidence", insufficient.decision)
        self.assertIn("market_regime_coverage_incomplete", insufficient.decision_reasons)

    def test_tradeoffs_ask_user_and_material_noninferior_live_evidence_recommends_promotion(self) -> None:
        baseline = {"value": .8, "duration_seconds": 120, "qualified": True, "research_quality": .8, "cost": 1, "stability": .95}
        regimes = ("trend_expansion", "divergence", "risk_contraction")
        faster_worse = {"value": .8, "duration_seconds": 80, "qualified": True, "research_quality": .72, "cost": .8, "stability": .95}
        tradeoff = self._assess([(regimes[index % 3], baseline, faster_worse) for index in range(9)])
        self.assertEqual("ask_user", tradeoff.decision)

        promoted = {"value": .9, "duration_seconds": 90, "qualified": True, "research_quality": .9, "cost": .9, "stability": .97}
        promotable = self._assess([(regimes[index % 3], baseline, promoted) for index in range(9)])
        self.assertEqual("recommend_promotion", promotable.decision)
        self.assertTrue(promotable.evidence_maturity.mature)

        replay = self._assess(
            [(regimes[index % 3], baseline, promoted) for index in range(9)],
            source_kind="historical_replay",
        )
        self.assertEqual("insufficient_evidence", replay.decision)
        self.assertIn("live_paired_shadow_required", replay.decision_reasons)

    def test_effort_promotion_and_rollback_require_governance_decisions_and_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CompanionStore(Path(directory) / "companion.sqlite3")
            store.initialize()
            CognitiveEffortPolicy.load(store)
            observatory = EvaluationObservatory(store)
            key = "m1:daily.execution.1430"
            store.router_policy_cell(key, {"reasoning_effort": "medium"}, {"reasoning_effort": "xhigh"})
            baseline = {"value": .8, "duration_seconds": 120, "qualified": True, "research_quality": .8, "cost": 1, "stability": .95}
            candidate = {"value": .9, "duration_seconds": 90, "qualified": True, "research_quality": .9, "cost": .9, "stability": .97}
            regimes = ("trend_expansion", "divergence", "risk_contraction")
            for index in range(9):
                store.record_router_evaluation(
                    key, f"cycle-{index}", "T+1", regimes[index % 3], None, f"shadow-{index}",
                    baseline, candidate, "resolved",
                )
            assessment = observatory.assess_experiment(ExperimentRequest(key))
            governance = EvolutionGovernance(store)
            executor = StrategyPolicyExecutor(store)

            decision = governance.decide(
                assessment.snapshot_id, "approve", approver="automatic-governance",
            )
            receipt = executor.apply(decision.decision_id)
            replayed = executor.apply(decision.decision_id)

            self.assertEqual("promoted", store.get_router_policy_cell(key)["mode"])
            self.assertEqual("applied", receipt.state)
            self.assertEqual(receipt.receipt_id, replayed.receipt_id)
            self.assertEqual(assessment.snapshot_id, receipt.evidence_snapshot_id)

            fault_cycle = store.create_cycle(
                "daily.execution.1430", "2026-09-01T14:30:00+08:00", "2026-09-01T14:30:00+08:00",
            )
            route_decision_id = store.record_route_decision(
                fault_cycle["cycle_id"], "m1_judgment", key, "promoted", {},
                {"reasoning_effort": "medium"}, {"reasoning_effort": "xhigh"},
                {"reasoning_effort": "xhigh"},
            )
            baseline_attempt = store.begin_attempt(
                fault_cycle["cycle_id"], "m1_judgment", fault_cycle["as_of"], "baseline-fault",
                route_decision_id=route_decision_id,
            )
            store.finish_attempt(
                baseline_attempt["attempt_id"], "succeeded", verifier={"passed": True},
                broker_metadata={"cost_estimate": 1.0},
            )
            self.assertEqual(
                key,
                RouterGovernance(store).record_effort_capability_fault(
                    route_decision_id, fault_cycle["cycle_id"], "production-fault",
                ),
            )
            rollback_assessment = observatory.assess_experiment(ExperimentRequest(
                key, source_kind="post_promotion_monitoring",
            ))
            self.assertEqual("recommend_rollback", rollback_assessment.decision)
            rollback = governance.decide(
                rollback_assessment.snapshot_id, "approve", approver="automatic-governance",
            )
            rollback_receipt = executor.apply(rollback.decision_id)
            self.assertEqual("rolled_back", store.get_router_policy_cell(key)["mode"])
            self.assertEqual("rollback_applied", rollback_receipt.state)

    def test_reading_effective_timing_policy_never_creates_or_evolves_a_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CompanionStore(Path(directory) / "companion.sqlite3")
            store.initialize()

            self.assertEqual((600, 1), store.effective_m1_reserve("daily.execution.0945", 600))

            with store.connection() as connection:
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM timing_policy").fetchone()[0])

    def test_runtime_budget_breadth_and_source_mix_follow_the_same_governance_receipt_chain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CompanionStore(Path(directory) / "companion.sqlite3")
            store.initialize()
            policy = RuntimeStrategyPolicy(store)
            cell = policy.register_shadow_candidate(
                "search_breadth", "m0_research", {"max_operations": 24}, {"max_operations": 12},
            )
            baseline = {"value": .8, "duration_seconds": 120, "qualified": True, "research_quality": .8, "cost": 1, "stability": .95}
            candidate = {"value": .9, "duration_seconds": 90, "qualified": True, "research_quality": .9, "cost": .9, "stability": .97}
            for index, regime in enumerate(("trend_expansion", "divergence", "risk_contraction") * 3):
                policy.record_evaluation(cell["cell_key"], f"cycle-{index}", "T+1", regime, baseline, candidate)

            assessment = EvaluationObservatory(store).assess_experiment(ExperimentRequest(cell["cell_key"]))
            decision = EvolutionGovernance(store).decide(
                assessment.snapshot_id, "approve", approver="automatic-governance",
            )
            receipt = StrategyPolicyExecutor(store).apply(decision.decision_id)

            self.assertEqual("applied", receipt.state)
            self.assertEqual(12, policy.controls("m0_research", timeout_seconds=300, search=True).max_operations)

            policy.record_evaluation(
                cell["cell_key"], "fault-cycle", "monitor", "trend_expansion", baseline,
                {**candidate, "hard_fault": True, "research_quality": 0.0, "stability": 0.0},
                source_kind="post_promotion_monitoring",
            )
            rollback_assessment = EvaluationObservatory(store).assess_experiment(ExperimentRequest(
                cell["cell_key"], source_kind="post_promotion_monitoring",
            ))
            self.assertEqual("recommend_rollback", rollback_assessment.decision)
            rollback = EvolutionGovernance(store).decide(
                rollback_assessment.snapshot_id, "approve", approver="automatic-governance",
            )
            StrategyPolicyExecutor(store).apply(rollback.decision_id)

            self.assertEqual(24, policy.controls("m0_research", timeout_seconds=300, search=True).max_operations)

    def test_runtime_strategy_shadow_jobs_are_isolated_from_the_official_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CompanionStore(Path(directory) / "companion.sqlite3")
            store.initialize()
            policy = RuntimeStrategyPolicy(store)
            cell = policy.register_shadow_candidate(
                "source_mix", "m0_research", {"enabled_backends": ["gateway", "market"]},
                {"enabled_backends": ["gateway"]},
            )
            queued = policy.queue_shadows(
                "cycle-1", "m0_research", {"task_key": "daily.execution.0945", "sha256": "frozen"},
                "companion-evidence-result-v3.schema.json", "official-attempt",
            )
            job = policy.next_shadow()

            self.assertEqual(1, len(queued))
            self.assertEqual(cell["cell_key"], job["cell_key"])
            self.assertEqual("official-attempt", job["baseline_attempt_id"])
            self.assertEqual("running", job["state"])
            policy.finish_shadow(job["job_id"], candidate_attempt_id="shadow-attempt")
            with store.connection() as connection:
                stored = connection.execute("SELECT * FROM runtime_strategy_shadow_job WHERE job_id=?", (job["job_id"],)).fetchone()
            self.assertEqual("succeeded", stored["state"])
            self.assertEqual("shadow-attempt", stored["candidate_attempt_id"])

    def test_authorized_runtime_strategy_promotes_and_rolls_back_from_live_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CompanionStore(Path(directory) / "companion.sqlite3")
            store.initialize()
            policy = RuntimeStrategyPolicy(store)
            cell = policy.register_shadow_candidate(
                "stage_budget", "m0_research", {"timeout_seconds": 300}, {"timeout_seconds": 180},
                automatic_authorized=True,
            )
            baseline = {"value": .8, "duration_seconds": 120, "qualified": True, "research_quality": .8, "cost": 1, "stability": .95}
            candidate = {"value": .9, "duration_seconds": 90, "qualified": True, "research_quality": .9, "cost": .9, "stability": .97}
            receipts = []
            for index, regime in enumerate(("trend_expansion", "divergence", "risk_contraction") * 3):
                receipt = policy.record_evaluation(cell["cell_key"], f"cycle-{index}", "T+1", regime, baseline, candidate)
                if receipt is not None:
                    receipts.append(receipt)

            self.assertEqual(1, len(receipts))
            self.assertEqual("applied", receipts[0].state)
            self.assertEqual(180, policy.controls("m0_research", timeout_seconds=300, search=True).timeout_seconds)
            self.assertIsNone(policy.record_evaluation(
                cell["cell_key"], "already-promoted", "T+3", "trend_expansion", baseline, candidate,
            ))
            rollback = policy.record_evaluation(
                cell["cell_key"], "fault-cycle", "monitor", "trend_expansion", baseline,
                {**candidate, "hard_fault": True, "research_quality": 0.0, "stability": 0.0},
                source_kind="post_promotion_monitoring",
            )
            self.assertIsNotNone(rollback)
            self.assertEqual("rollback_applied", rollback.state)
            self.assertEqual(300, policy.controls("m0_research", timeout_seconds=300, search=True).timeout_seconds)

    def test_live_router_outcome_writes_the_full_experiment_vector(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CompanionStore(Path(directory) / "companion.sqlite3")
            store.initialize()
            observatory = EvaluationObservatory(store)
            cycle = store.create_cycle(
                "daily.execution.1430", "2026-08-29T14:30:00+08:00", "2026-08-29T14:30:00+08:00",
            )
            key = "m1:daily.execution.1430"
            baseline_route = {"reasoning_effort": "medium"}
            candidate_route = {"reasoning_effort": "xhigh"}
            store.router_policy_cell(key, baseline_route, candidate_route)
            decision_id = store.record_route_decision(
                cycle["cycle_id"], "m1_judgment", key, "shadow", {}, baseline_route,
                candidate_route, baseline_route,
            )
            baseline_attempt = store.begin_attempt(
                cycle["cycle_id"], "m1_judgment", cycle["as_of"], "baseline",
                route_decision_id=decision_id,
            )
            store.finish_attempt(
                baseline_attempt["attempt_id"], "succeeded",
                output={"snapshot": {"direction": "bullish", "triggers": ["x"], "invalidations": ["y"]}},
                verifier={"passed": True}, broker_metadata={"cost_estimate": 1.0},
            )
            job_id = store.queue_router_shadow(
                decision_id, cycle["cycle_id"], "m1_judgment", {"task_key": cycle["task_key"]},
                "companion-m1-result-v1.schema.json", candidate_route,
            )
            self.assertIsNotNone(job_id)
            store.next_router_shadow()
            store.finish_router_shadow(job_id, output={
                "snapshot": {"direction": "bullish", "triggers": ["x"], "invalidations": ["y"]},
            }, verifier={"passed": True})
            shadow_attempt = store.begin_attempt(
                cycle["cycle_id"], "m1_judgment", cycle["as_of"], "candidate",
                route_decision_id=decision_id, is_shadow=True,
            )
            store.finish_attempt(
                shadow_attempt["attempt_id"], "succeeded",
                output={"snapshot": {"direction": "bullish", "triggers": ["x"], "invalidations": ["y"]}},
                verifier={"passed": True}, broker_metadata={"cost_estimate": 1.2},
            )
            store.save_market_regime(cycle["cycle_id"], cycle["as_of"], "trend_expansion", {}, "complete")

            RouterGovernance(store).evaluate_outcome(
                cycle["cycle_id"], "T+1", [{"excess_return": .02}],
                {"direction": "bullish", "triggers": ["x"], "invalidations": ["y"]}, None,
            )
            assessment = observatory.assess_experiment(ExperimentRequest(key))

            self.assertGreaterEqual(assessment.data_completeness, 5 / 6)
            self.assertEqual(1, assessment.delivery_speed.pair_count)
            self.assertEqual(1, assessment.qualification.pair_count)
            self.assertEqual(1, assessment.research_quality.pair_count)
            self.assertEqual(1, assessment.cost.pair_count)
            self.assertEqual(1, assessment.stability.pair_count)


if __name__ == "__main__":
    unittest.main()
