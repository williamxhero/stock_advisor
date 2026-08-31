from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from ai_trading_companion.__main__ import finalize_stage_packet
from ai_trading_companion.broker_client import BrokerRequest, canonical_packet_hash
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.runtime_strategy_policy import RuntimeStrategyControls
from ai_trading_companion.store import CompanionStore


class StagePacketFreezingTests(TestCase):
    controls = RuntimeStrategyControls(
        timeout_seconds=300,
        max_operations=0,
        enabled_backends=(),
        revisions=(("stage_budget", 3),),
    )

    def test_controls_are_bound_before_broker_hash_is_verified(self) -> None:
        builder_packet = {
            "task_key": "periodic.monthly",
            "stage": "m0_compose",
            "as_of": "2026-08-30T14:06:33.359Z",
            "frozen_evidence": {"schema_version": 3, "sources": []},
        }
        builder_packet["sha256"] = canonical_packet_hash(builder_packet)

        # This is the former late-binding sequence: the old builder hash does
        # not cover the controls that are actually handed to Broker.
        late_bound = {
            **builder_packet,
            "runtime_strategy_controls": {
                "timeout_seconds": 300,
                "max_operations": 0,
                "enabled_backends": [],
                "revisions": [("stage_budget", 3)],
            },
            "allowed_research_backends": [],
        }
        late_request = {key: value for key, value in late_bound.items() if key != "sha256"}
        self.assertNotEqual(builder_packet["sha256"], canonical_packet_hash(late_request))
        with self.assertRaisesRegex(ValueError, "hash does not match"):
            BrokerRequest("m0_compose", late_request, builder_packet["sha256"], "smart", "medium")

        final = finalize_stage_packet(builder_packet, self.controls)
        request_packet = {key: value for key, value in final.items() if key != "sha256"}
        self.assertEqual(final["sha256"], canonical_packet_hash(request_packet))
        request = BrokerRequest("m0_compose", request_packet, final["sha256"], "smart", "medium")
        self.assertEqual(final["sha256"], request.packet_sha256)
        self.assertEqual(builder_packet["sha256"], canonical_packet_hash({
            key: value for key, value in builder_packet.items() if key != "sha256"
        }))

    def test_m0_compose_attempt_and_checkpoint_share_final_packet_hash(self) -> None:
        packet = finalize_stage_packet({
            "task_key": "daily.execution.0945",
            "stage": "m0_compose",
            "as_of": "2026-08-30T14:06:33.359Z",
            "frozen_evidence": {"schema_version": 3, "sources": []},
        }, self.controls)
        with TemporaryDirectory() as temporary:
            store = CompanionStore(Path(temporary) / "companion.sqlite3")
            engine = CompanionEngine(store)
            cycle = engine.start_cycle(
                "daily.execution.0945", "2026-08-31T09:45:00+08:00", packet["as_of"],
            )
            cycle = engine.research_started(cycle["cycle_id"])
            evidence_packet = finalize_stage_packet({
                "task_key": cycle["task_key"], "stage": "m0_research", "as_of": packet["as_of"],
            }, self.controls)
            evidence_attempt = store.begin_attempt(
                cycle["cycle_id"], "m0_research", packet["as_of"], evidence_packet["sha256"], input_packet=evidence_packet,
            )
            evidence = {"schema_version": 3, "sources": []}
            store.finish_attempt(evidence_attempt["attempt_id"], "succeeded", output=evidence, verifier={"passed": True})
            attempt = store.begin_attempt(
                cycle["cycle_id"], "m0_compose", packet["as_of"], packet["sha256"], input_packet=packet,
            )
            output = {"m0_markdown": "frozen"}
            store.finish_attempt(attempt["attempt_id"], "succeeded", output=output, verifier={"passed": True})
            store.save_stage_checkpoint(
                cycle["cycle_id"], "m0_compose", packet["sha256"], attempt["attempt_id"], output,
            )

            persisted = next(item for item in store.attempts(cycle["cycle_id"]) if item["stage"] == "m0_compose")
            checkpoint = store.stage_checkpoint(cycle["cycle_id"], "m0_compose", packet["sha256"])
            self.assertEqual(packet["sha256"], persisted["input_sha256"])
            self.assertEqual(packet["sha256"], checkpoint["packet_sha256"])
            self.assertEqual(attempt["attempt_id"], checkpoint["attempt_id"])
            ready = engine.research_ready(
                cycle["cycle_id"], output["m0_markdown"],
                evidence_attempt_id=evidence_attempt["attempt_id"], compose_attempt_id=attempt["attempt_id"],
                evidence_packet_hash=evidence_packet["sha256"], packet_hash=packet["sha256"],
            )
            self.assertEqual(packet["sha256"], ready["packet_hash"])
