from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ai_trading_companion.__main__ import _m2_portfolio_fact_gate
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.memory_port import InMemoryMemoryAdapter
from ai_trading_companion.packet_builder import RuntimePacketBuilder
from ai_trading_companion.portfolio import PortfolioService
from ai_trading_companion.store import CompanionStore


class M2PortfolioFactGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = CompanionStore(self.root / "runtime.sqlite3")
        self.engine = CompanionEngine(self.store)
        self.portfolio = PortfolioService(self.store)
        self.cycle = self.engine.start_cycle(
            "daily.execution.1430", "2026-09-18T14:30:00+08:00", "2026-09-18T06:30:00Z",
        )
        self.h0_text = """今天全部持仓情况如下：
| 证券代码 | 证券名称 | 当前拥股数 |
| --- | --- | ---: |
| 603179 | 新泉股份 | 300 |"""
        self.h0 = self.store.append_artifact(
            self.cycle["cycle_id"], "h0", "human", self.h0_text, self.cycle["as_of"], {},
        )
        self.cycle = self.store.transition(
            self.cycle["cycle_id"], "synthesizing_m2", has_h0=1,
            h0_artifact_id=self.h0["artifact_id"], m1_completed_at=self.cycle["as_of"],
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_failed_h0_cognition_blocks_m2_before_a_fact_view_can_be_created(self) -> None:
        job = self.store.start_cognition_job(self.cycle["cycle_id"], self.h0["artifact_id"], "h0", self.h0_text)
        self.store.finish_cognition_job(job["job_id"], error="snapshot verification failed")

        view, problem = _m2_portfolio_fact_gate(self.store, self.cycle)

        self.assertIsNone(view)
        self.assertEqual("h0_portfolio_fact_unavailable", problem)
        self.assertIsNone(self.store.m2_portfolio_fact_view(self.cycle["cycle_id"]))

    def test_completed_h0_snapshot_freezes_one_runtime_fact_view_for_m2(self) -> None:
        receipt = self.portfolio.replace_complete_snapshot(self.h0_text, [{
            "action": "position_correction", "code": "603179", "name": "新泉股份", "shares": 300,
            "price": 38.1, "average_cost": 38.1, "occurred_at": None,
            "evidence": {"instrument": "603179", "action": "当前拥股数", "shares": "300"},
        }], self.cycle["cycle_id"], self.h0["artifact_id"])
        job = self.store.start_cognition_job(self.cycle["cycle_id"], self.h0["artifact_id"], "h0", self.h0_text)
        applied_receipt = {"action_id": "snapshot-action", "action_type": "portfolio.replace_complete_snapshot", **receipt}
        self.store.save_action_receipt(
            "snapshot-action", job["job_id"], "portfolio.replace_complete_snapshot", {}, "applied", applied_receipt,
        )
        self.store.finish_cognition_job(job["job_id"], {"receipts": [{
            **applied_receipt,
        }]})

        view, problem = _m2_portfolio_fact_gate(self.store, self.cycle)
        packet = RuntimePacketBuilder(
            Path(__file__).resolve().parents[2] / "resources", self.store, memory=InMemoryMemoryAdapter(),
        ).build(
            self.cycle, "m2", as_of=self.cycle["as_of"],
        )

        self.assertIsNone(problem)
        self.assertEqual("603179", view["positions"][0]["code"])
        self.assertEqual(view["fact_view_sha256"], packet["business_context"]["portfolio_fact_view"]["fact_view_sha256"])
        self.assertNotIn("portfolio", packet["business_context"])
        self.assertEqual(view, _m2_portfolio_fact_gate(self.store, self.cycle)[0])


if __name__ == "__main__":
    unittest.main()
