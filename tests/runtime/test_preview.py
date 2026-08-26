import hashlib
import copy
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.preview import approve_bundle, prepare_preview_home, seal_bundle, source_fingerprint
from ai_trading_companion.store import CompanionStore


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "home"
        self.database = self.home / "data" / "trading-companion.sqlite3"
        self.store = CompanionStore(self.database)
        self.engine = CompanionEngine(self.store)
        self.source = self.engine.start_cycle(
            "daily.review.1520", "2026-08-26T15:20:00+08:00", "2026-08-26T07:20:00Z"
        )

    def tearDown(self):
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
            "observation_id": "obs-1", "backend": "search_searxng", "status": "succeeded",
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
            "schema_version": 1, "preview_id": "preview-approval", "source_cycle_id": self.source["cycle_id"],
            "source_task_key": self.source["task_key"], "source_scheduled_for": self.source["scheduled_for"],
            "source_fingerprint": source_fingerprint(self.store, self.source["cycle_id"], known_at),
            "preview_cycle_id": "preview-cycle", "task_key": "daily.review.1520",
            "scheduled_for": self.source["scheduled_for"], "known_at": known_at,
            "replay_mode": "current_reassessment", "qualification_version": 2, "cycle_state": "complete",
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


if __name__ == "__main__":
    unittest.main()
