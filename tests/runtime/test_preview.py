import hashlib
import copy
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.preview import (
    BUNDLE_SCHEMA_VERSION, approve_bundle, build_bundle, prepare_preview_home, preview_signing_key, seal_bundle,
    source_fingerprint,
)
from ai_trading_companion.store import CompanionStore


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "home"
        self.environment = patch.dict(os.environ, {"AI_TRADING_COMPANION_HOME": str(self.home)})
        self.environment.start()
        self.database = self.home / "data" / "trading-companion.sqlite3"
        self.store = CompanionStore(self.database)
        self.engine = CompanionEngine(self.store)
        self.source = self.engine.start_cycle(
            "daily.review.1520", "2026-08-26T15:20:00+08:00", "2026-08-26T07:20:00Z"
        )

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def test_preview_database_snapshot_does_not_change_authoritative_files(self):
        exchange = self.home / "exchange" / "to-client" / "pending"
        exchange.mkdir(parents=True)
        marker = exchange / "existing.json"
        marker.write_text("unchanged", encoding="utf-8")
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        root, work = prepare_preview_home(self.home, self.database, "preview-1")
        after = hashlib.sha256(self.database.read_bytes()).hexdigest()
        self.assertEqual(before, after)
        self.assertEqual("unchanged", marker.read_text(encoding="utf-8"))
        self.assertTrue((work / "data" / "trading-companion.sqlite3").exists())
        self.assertTrue(str(root).endswith("runtime\\previews\\preview-1"))

    def test_default_preview_signer_stays_in_isolated_local_settings(self):
        bundle = {"schema_version": BUNDLE_SCHEMA_VERSION, "preview_id": "signing-key-test"}
        seal_bundle(bundle)
        saved = json.loads((self.home / "config" / "settings.local.json").read_text(encoding="utf-8"))
        self.assertTrue(saved["preview"]["signing_key"])
        self.assertNotIn("signing_key", bundle)

    def test_preview_signer_can_be_read_from_an_explicit_isolated_home(self):
        isolated_home = self.home / "runtime" / "previews" / "preview-1" / "work"
        isolated_home.mkdir(parents=True)
        seal_bundle({"schema_version": BUNDLE_SCHEMA_VERSION})
        settings = self.home / "config" / "settings.local.json"
        target = isolated_home / "config" / "settings.local.json"
        target.parent.mkdir(parents=True)
        target.write_bytes(settings.read_bytes())
        self.assertEqual(
            json.loads(settings.read_text(encoding="utf-8"))["preview"]["signing_key"],
            preview_signing_key(create=False, home=isolated_home),
        )

    def test_approval_is_provider_free_exact_and_idempotent(self):
        known_at = "2026-08-26T08:30:00Z"
        signing_key = "unit-test-preview-key"

        def artifact(kind, body):
            return {
                "artifact_id": f"preview-{kind}", "cycle_id": "preview-cycle", "kind": kind,
                "revision": 1, "actor": "model", "body_markdown": body,
                "body_sha256": hashlib.sha256(body.encode()).hexdigest(), "as_of": known_at,
                "sealed_at": known_at, "metadata_json": "{}", "occurred_at": known_at, "known_at": known_at,
            }

        source = {
            "url": "https://example.test/close", "title": "8月26日收盘",
            "fact_as_of": known_at, "published_at": None, "source_family": "media",
            "upstream_id": "example.test", "tool_observation_id": "obs-1",
            "result_item_hash": "item-1", "excerpt": "8月26日收盘数据",
        }
        research_output = {
            "as_of": known_at, "spoken_summary": "已取得收盘信息", "sources": [source],
            "coverage": [], "critical_gaps": [], "conflicts": [], "high_impact_events": [],
        }
        observation = {
            "observation_id": "obs-1", "backend": "gateway", "status": "succeeded",
            "non_empty": True, "result_urls": [source["url"]], "arguments": {"query": "8月26日收盘"},
            "result_items": [{
                "url": source["url"], "title": source["title"], "result_item_hash": "item-1",
                "evidence_text": source["excerpt"], "published_at": None, "acquired_at": known_at,
                "source_family": "media", "upstream_id": "example.test",
            }],
        }
        stage_outputs = {
            "m0_research": research_output, "m0_compose": {"m0_markdown": "M0 exact"},
            "m1_research": research_output, "m1_judgment": {"m1_markdown": "M1 exact"},
        }
        stages = list(stage_outputs)
        attempts = []
        checkpoints = []
        for index, stage in enumerate(stages, 1):
            packet = {
                "schema_version": 2, "cycle_id": "preview-cycle", "task_key": "daily.review.1520",
                "stage": stage, "as_of": known_at, "scheduled_for": self.source["scheduled_for"],
            }
            if stage in {"m0_research", "m1_research"}:
                packet["evidence_requirements"] = []
            packet_raw = json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            packet_hash = hashlib.sha256(packet_raw.encode()).hexdigest()
            packet["sha256"] = packet_hash
            attempts.append({
                "attempt_id": f"a-{index}", "stage": stage, "attempt_number": 1,
                "status": "succeeded", "as_of": known_at, "started_at": known_at, "completed_at": known_at,
                "input_sha256": packet_hash, "output_sha256": stage, "verifier": {"passed": True},
                "usage": {}, "tool_trace": [observation] if stage in {"m0_research", "m1_research"} else [],
                "input_packet": packet, "output": stage_outputs[stage],
            })
            raw = json.dumps(stage_outputs[stage], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            checkpoints.append({
                "cycle_id": "preview-cycle", "stage": stage, "packet_sha256": packet_hash,
                "attempt_id": f"a-{index}", "output": stage_outputs[stage],
                "output_sha256": hashlib.sha256(raw.encode()).hexdigest(), "created_at": known_at,
            })
        bundle = {
            "schema_version": BUNDLE_SCHEMA_VERSION, "preview_id": "preview-approval", "source_cycle_id": self.source["cycle_id"],
            "source_task_key": self.source["task_key"], "source_scheduled_for": self.source["scheduled_for"],
            "source_fingerprint": source_fingerprint(self.store, self.source["cycle_id"], known_at),
            "preview_cycle_id": "preview-cycle", "task_key": "daily.review.1520",
            "scheduled_for": self.source["scheduled_for"], "known_at": known_at,
            "replay_mode": "original_cycle_inputs", "qualification_version": 2, "cycle_state": "complete",
            "preview_status": "passed",
            "schedule_snapshot": {},
            "artifacts": [
                artifact("evidence", json.dumps(research_output, ensure_ascii=False, sort_keys=True)),
                artifact("m0", "M0 exact"),
                artifact("m1_evidence", json.dumps(research_output, ensure_ascii=False, sort_keys=True)),
                artifact("m1", "M1 exact"),
            ],
            "attempts": attempts, "stage_checkpoints": checkpoints, "judgment_snapshots": [], "evidence": {},
        }
        seal_bundle(bundle, signing_key)
        tampered = copy.deepcopy(bundle)
        next(item for item in tampered["artifacts"] if item["kind"] == "m0")["body_markdown"] = "altered"
        seal_bundle(tampered, "attacker-does-not-know-real-key")
        with self.assertRaisesRegex(ValueError, "signature mismatch"):
            approve_bundle(self.store, tampered, signing_key=signing_key)

        invalid_m2 = copy.deepcopy(bundle)
        m1_output = {
            "m1_markdown": "M1 unqualified", "judgment_qualified": False,
            "snapshot": {"qualified": False, "direction": "uncertain", "triggers": [], "invalidations": []},
        }
        m1_attempt = next(item for item in invalid_m2["attempts"] if item["stage"] == "m1_judgment")
        m1_attempt["output"] = m1_output
        m1_checkpoint = next(item for item in invalid_m2["stage_checkpoints"] if item["stage"] == "m1_judgment")
        m1_checkpoint["output"] = m1_output
        m1_raw = json.dumps(m1_output, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        m1_checkpoint["output_sha256"] = hashlib.sha256(m1_raw.encode()).hexdigest()
        invalid_m2["artifacts"] = [
            item for item in invalid_m2["artifacts"] if item["kind"] != "m1"
        ] + [artifact("m1", "M1 unqualified"), artifact("m2", "M2 must not import")]
        m2_packet = {
            "schema_version": 2, "cycle_id": "preview-cycle", "task_key": "daily.review.1520",
            "stage": "m2", "as_of": known_at, "scheduled_for": self.source["scheduled_for"],
        }
        m2_raw_packet = json.dumps(m2_packet, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        m2_hash = hashlib.sha256(m2_raw_packet.encode()).hexdigest()
        m2_packet["sha256"] = m2_hash
        m2_output = {
            "m2_markdown": "M2 must not import",
            "snapshot": {"qualified": True, "direction": "bullish", "triggers": ["x"], "invalidations": ["y"]},
        }
        invalid_m2["attempts"].append({
            "attempt_id": "a-m2", "stage": "m2", "attempt_number": 1, "status": "succeeded",
            "as_of": known_at, "started_at": known_at, "completed_at": known_at,
            "input_sha256": m2_hash, "output_sha256": "m2", "verifier": {"passed": True},
            "usage": {}, "tool_trace": [], "input_packet": m2_packet, "output": m2_output,
        })
        m2_raw = json.dumps(m2_output, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        invalid_m2["stage_checkpoints"].append({
            "cycle_id": "preview-cycle", "stage": "m2", "packet_sha256": m2_hash,
            "attempt_id": "a-m2", "output": m2_output,
            "output_sha256": hashlib.sha256(m2_raw.encode()).hexdigest(), "created_at": known_at,
        })
        seal_bundle(invalid_m2, signing_key)
        with self.assertRaisesRegex(ValueError, "qualified frozen M1"):
            approve_bundle(self.store, invalid_m2, signing_key=signing_key)

        with patch("ai_trading_companion.provider_client.ProviderClient.run", side_effect=AssertionError("provider must not run")):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(
                    lambda _index: approve_bundle(self.store, bundle, signing_key=signing_key), range(2),
                ))
            second = approve_bundle(self.store, bundle, signing_key=signing_key)
        self.assertEqual([False, True], sorted(result["already_imported"] for result in results))
        self.assertTrue(second["already_imported"])
        first = next(result for result in results if not result["already_imported"])
        cycle_id = first["cycle"]["cycle_id"]
        self.assertEqual("M0 exact", self.store.latest_artifact(cycle_id, "m0")["body_markdown"])
        self.assertEqual("M1 exact", self.store.latest_artifact(cycle_id, "m1")["body_markdown"])
        approval_events = [
            event for event in self.store.pending_events()
            if event["cycle_id"] == cycle_id and event["event_type"] == "cycle.preview_approved"
        ]
        self.assertEqual(1, len(approval_events))

    def test_source_fingerprint_changes_with_cycle_state(self):
        known_at = "2026-08-26T08:30:00Z"
        before = source_fingerprint(self.store, self.source["cycle_id"], known_at)
        self.store.transition(self.source["cycle_id"], "researching_m0")
        after = source_fingerprint(self.store, self.source["cycle_id"], known_at)
        self.assertNotEqual(before, after)

    def test_preview_replays_original_cycle_as_of_and_tracks_current_known_at(self):
        known_at = "2026-08-26T08:30:00Z"
        preview = self.store.create_preview_cycle(self.source["cycle_id"], known_at)

        self.assertEqual(self.source["as_of"], preview["as_of"])
        snapshot = json.loads(preview["schedule_snapshot_json"])
        self.assertEqual(known_at, snapshot["preview_known_at"])
        self.assertEqual(self.source["scheduled_for"], snapshot["original_scheduled_for"])

    def test_bundle_reports_provider_competition_and_evidence_coverage(self):
        known_at = "2026-08-26T08:30:00Z"
        preview = self.store.create_preview_cycle(self.source["cycle_id"], known_at)
        packet = {"stage": "m0_research", "as_of": known_at}
        packet_raw = json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        packet_hash = hashlib.sha256(packet_raw.encode()).hexdigest()
        packet["sha256"] = packet_hash
        attempt = self.store.begin_attempt(
            preview["cycle_id"], "m0_research", known_at, packet_hash, input_packet=packet,
        )
        output = {"coverage": [{"fact": "close", "covered": True}], "critical_gaps": [], "sources": [{"url": "https://example.test"}]}
        planner_hash = hashlib.sha256(b"planner-packet").hexdigest()
        self.store.finish_attempt(
            attempt["attempt_id"], "succeeded", output=output, verifier={"passed": True},
            tool_trace=[{"kind": "provider_invocation", "invocation_id": "inv-1", "packet_sha256": planner_hash}],
        )
        self.store.record_provider_audit("provider_invocation_started", {
            "invocation_id": "inv-1", "stage": "m0_research", "mode": "race",
            "packet_sha256": planner_hash, "absolute_deadline": 90.0, "route_timeout_seconds": 90.0,
        }, recorded_at=known_at)
        self.store.record_provider_audit("llm_attempt_started", {
            "attempt_id": "provider-1", "invocation_id": "inv-1", "packet_sha256": planner_hash,
            "stage": "m0_research", "route_id": "claude-cheap", "endpoint_id": "relay",
            "model": "claude-opus-5", "model_family": "anthropic", "tier": 100,
            "started_at": 1.0, "estimated_cost": 0.25,
        }, recorded_at=known_at)
        self.store.record_provider_audit("llm_attempt_finished", {
            "attempt_id": "provider-1", "first_token_at": 1.2, "completed_at": 2.0,
            "protocol_success": True, "product_success": True, "winner": True,
            "estimated_cost": 0.25, "actual_cost": None, "currency": "RELATIVE",
            "verifier": {"name": "evidence-gate/v1", "passed": True}, "usage": {},
        }, recorded_at=known_at)
        self.store.record_provider_audit("provider_invocation_finished", {
            "invocation_id": "inv-1", "winner_route": "claude-cheap", "winner_endpoint": "relay",
            "winner_model": "claude-opus-5", "winner_family": "anthropic",
            "product_disposition": "qualified", "attempt_count": 1, "probe_count": 1,
        }, recorded_at=known_at)

        bundle = build_bundle(
            self.store, preview["cycle_id"], self.source["cycle_id"], "preview-report", known_at,
        )

        self.assertEqual("claude-cheap", bundle["provider_summary"][0]["route_id"])
        self.assertEqual("anthropic", bundle["provider_summary"][0]["model_family"])
        self.assertEqual(100, bundle["provider_summary"][0]["cost_tier"])
        self.assertAlmostEqual(200.0, bundle["provider_summary"][0]["ttft_ms"])
        self.assertIsNone(bundle["provider_summary"][0]["actual_cost"])
        self.assertEqual(1, bundle["evidence_coverage"][0]["source_count"])


if __name__ == "__main__":
    unittest.main()
