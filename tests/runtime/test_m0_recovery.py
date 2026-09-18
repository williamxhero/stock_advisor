from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.store import CompanionStore


class M0RecoveryTests(unittest.TestCase):
    def test_provider_gap_is_durable_retry_wait_and_same_cycle_is_claimed_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CompanionStore(Path(directory) / "companion.sqlite3")
            engine = CompanionEngine(store)
            cycle = engine.start_cycle(
                "daily.review.1520", "2099-09-18T15:20:00+08:00", "2099-09-18T07:20:00Z",
            )
            engine.research_started(cycle["cycle_id"])

            waiting = engine.research_waiting(
                cycle["cycle_id"], "evidence_insufficient: blocking_requirement_missing:portfolio_market_state",
            )

            self.assertEqual("m0_retry_wait", waiting["state"])
            self.assertEqual(1, waiting["m0_retry_attempt"])
            self.assertTrue(waiting["m0_retry_at"])
            self.assertTrue(waiting["m0_retry_deadline"])
            self.assertIsNone(store.latest_artifact(cycle["cycle_id"], "m0"))

            retry_at = datetime.fromisoformat(waiting["m0_retry_at"].replace("Z", "+00:00"))
            claimed = store.claim_scheduled_workers(at=retry_at + timedelta(seconds=1))
            self.assertEqual([cycle["cycle_id"]], [row["cycle_id"] for row in claimed])
            self.assertEqual("m0_retry_wait", claimed[0]["state"])
            store.finish_scheduled_worker(cycle["cycle_id"])

    def test_retry_wait_becomes_one_terminal_fault_at_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CompanionStore(Path(directory) / "companion.sqlite3")
            engine = CompanionEngine(store)
            cycle = engine.start_cycle(
                "daily.review.1520", "2099-09-18T15:20:00+08:00", "2099-09-18T07:20:00Z",
            )
            engine.research_started(cycle["cycle_id"])
            expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
            store.transition(cycle["cycle_id"], "researching_m0", m0_retry_deadline=expired)

            failed = engine.research_waiting(cycle["cycle_id"], "provider unavailable")

            self.assertEqual("failed", failed["state"])
            with store.connection() as connection:
                events = [dict(row) for row in connection.execute(
                    "SELECT * FROM client_event_log WHERE cycle_id=? AND event_type=?",
                    (cycle["cycle_id"], "research.failed"),
                )]
            self.assertEqual(1, len(events))


if __name__ == "__main__":
    unittest.main()
