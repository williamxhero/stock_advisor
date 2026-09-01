from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ai_trading_companion.portfolio import PortfolioService, explicit_fixture_extraction
from ai_trading_companion.memory_port import InMemoryMemoryAdapter
from ai_trading_companion.memoryhub_migration import LegacyWorkspaceImporter
from ai_trading_companion.store import CompanionStore


PORTFOLIO = """# 当前持仓与资产状态

> Single Source of Truth

## 总资产

- 当前总资产：约 **230,000元**
- 持仓与成交更新时间：**2026-08-24 15:09**

## 当前持仓

| 代码 | 名称 | 股数 | 收盘价 | 市值 | 成本价 | 浮动盈亏 | 仓位占总资产约 |
|---|---|---:|---:|---:|---:|---:|---:|
| 603179 | 新泉股份 | 100 | 38.08 | 3,808 | 38.230 | -15.04 | 1.66% |
| 603993 | 洛阳钼业 | 200 | 18.27 | 3,654 | 17.955 | +62.96 | 1.59% |

## 最近清仓

- 历史内容必须保留。

## 更新规则

- 只有用户明确报告真实成交才更新。
"""


class PortfolioServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "portfolio").mkdir(parents=True)
        (self.root / "portfolio/01_CURRENT_PORTFOLIO.md").write_text(PORTFOLIO, encoding="utf-8")
        self.store = CompanionStore(self.root / "runtime.sqlite3")
        self.store.initialize()
        self.memory = InMemoryMemoryAdapter()
        LegacyWorkspaceImporter(
            self.root, self.store, self.memory, "test", migrated_at="2026-08-25T00:00:00Z",
        ).run()
        self.service = PortfolioService(self.store)

    def test_empty_workspace_starts_with_an_empty_portfolio_baseline(self) -> None:
        service = PortfolioService(CompanionStore(Path(self.temp.name) / "empty.sqlite3"))
        self.assertEqual([], service.snapshot()["positions"])

    def tearDown(self) -> None:
        self.temp.cleanup()

    def extraction(self, *, action="buy", shares=100, price=39.5):
        return {"statement_type": "executed", "changes": [{"action": action, "code": "603179", "name": "新泉股份", "shares": shares, "price": price, "occurred_at": "2026-08-25T03:00:00Z", "evidence": {"instrument": "新泉股份", "action": "买入" if action == "buy" else "卖出", "shares": f"{shares}股" if shares else None, "price": str(price) if price else None}}]}

    def test_baseline_import_is_idempotent(self):
        snapshot = self.service.snapshot()
        self.assertEqual(2, len(snapshot["positions"]))
        replay = LegacyWorkspaceImporter(
            self.root, self.store, self.memory, "test", migrated_at="2026-08-25T00:00:00Z",
        ).run()
        PortfolioService(self.store)
        with self.store.connection() as connection:
            self.assertEqual(2, connection.execute("SELECT COUNT(*) FROM portfolio_position").fetchone()[0])
        self.assertEqual("already_imported", replay["state"])

    def test_complete_buy_updates_database_once_without_rewriting_legacy_file(self):
        legacy_before = (self.root / "portfolio/01_CURRENT_PORTFOLIO.md").read_text(encoding="utf-8")
        result = self.service.apply_extraction("我以39.5元买入新泉股份100股", self.extraction(), "cycle", "artifact")
        self.assertEqual("applied", result["state"])
        position = next(item for item in self.service.snapshot()["positions"] if item["code"] == "603179")
        self.assertEqual(200, position["shares"])
        self.assertEqual(legacy_before, (self.root / "portfolio/01_CURRENT_PORTFOLIO.md").read_text(encoding="utf-8"))
        result2 = self.service.apply_extraction("我以39.5元买入新泉股份100股", self.extraction(), "cycle", "artifact")
        self.assertEqual(result["transaction_ids"], result2["transaction_ids"])
        self.assertEqual(2, next(item for item in self.service.snapshot()["positions"] if item["code"] == "603179")["revision"])

    def test_missing_shares_does_not_change_position(self):
        result = self.service.apply_extraction("买入新泉股份，价格39.5", self.extraction(shares=None), "cycle", "artifact")
        self.assertEqual("needs_input", result["state"])
        self.assertIn("成交股数", result["missing_fields"])
        self.assertEqual(100, next(item for item in self.service.snapshot()["positions"] if item["code"] == "603179")["shares"])

    def test_plan_is_never_applied(self):
        result = self.service.apply_extraction("准备明天买入新泉股份100股", self.extraction(), "cycle", "artifact")
        self.assertEqual("rejected", result["state"])

    def test_actual_trade_is_not_lost_when_same_message_contains_a_later_condition(self):
        result = self.service.apply_extraction("我以39.5元买入新泉股份100股，如果跌破37我会卖出", self.extraction(), "cycle", "artifact")
        self.assertEqual("applied", result["state"])

    def test_model_evidence_not_present_in_source_is_rejected(self):
        extraction = self.extraction()
        extraction["changes"][0]["evidence"]["shares"] = "200股"
        result = self.service.apply_extraction("我以39.5元买入新泉股份100股", extraction, "cycle", "artifact")
        self.assertEqual("needs_input", result["state"])
        self.assertIn("可追溯的原文证据", result["missing_fields"])

    def test_sell_more_than_position_is_rejected_as_needs_input(self):
        result = self.service.apply_extraction("卖出新泉股份200股，价格39.5", self.extraction(action="sell", shares=200), "cycle", "artifact")
        self.assertEqual("needs_input", result["state"])
        self.assertIn("不超过当前持仓的卖出股数", result["missing_fields"])

    def test_multiple_changes_are_all_or_nothing(self):
        extraction = self.extraction()
        invalid_sell = self.extraction(action="sell", shares=999)["changes"][0]
        invalid_sell["code"] = "603993"
        invalid_sell["name"] = "洛阳钼业"
        invalid_sell["evidence"]["instrument"] = "洛阳钼业"
        invalid_sell["evidence"]["shares"] = "999股"
        extraction["changes"].append(invalid_sell)
        result = self.service.apply_extraction("买入新泉股份100股，价格39.5；卖出洛阳钼业999股，价格39.5", extraction, "cycle", "artifact")
        self.assertEqual("needs_input", result["state"])
        self.assertEqual(100, next(item for item in self.service.snapshot()["positions"] if item["code"] == "603179")["shares"])

    def test_sell_all_uses_current_shares_without_inventing_quantity(self):
        extraction = self.extraction(action="sell_all", shares=None, price=39.5)
        extraction["changes"][0]["evidence"]["action"] = "清仓"
        result = self.service.apply_extraction("新泉股份已清仓，成交价39.5", extraction, "cycle", "artifact")
        self.assertEqual("applied", result["state"])
        self.assertNotIn("603179", [item["code"] for item in self.service.snapshot()["positions"]])

    def test_explicit_current_position_correction_is_audited_without_pretending_to_be_a_trade(self):
        extraction = {"statement_type": "current_state", "changes": [{"action": "position_correction", "code": "603179", "name": "新泉股份", "shares": 300, "price": None, "average_cost": 38.1, "occurred_at": None, "evidence": {"instrument": "新泉股份", "action": "现在持有", "shares": "300股", "price": None, "average_cost": "成本38.1"}}]}
        result = self.service.apply_extraction("新泉股份现在持有300股，成本38.1", extraction, "cycle", "artifact")
        self.assertEqual("applied", result["state"])
        position = next(item for item in self.service.snapshot()["positions"] if item["code"] == "603179")
        self.assertEqual(300, position["shares"])
        self.assertEqual(38.1, position["average_cost"])
        self.assertEqual("position_correction", self.service.snapshot()["transactions"][0]["action"])

    def test_explicit_total_assets_correction_updates_account_without_fake_trade(self):
        extraction = {"statement_type": "current_state", "changes": [{"action": "asset_correction", "code": None, "name": None, "shares": None, "price": None, "total_assets": 240000, "occurred_at": None, "evidence": {"instrument": None, "action": "总资产", "shares": None, "price": None, "total_assets": "24万元"}}]}
        result = self.service.apply_extraction("现在总资产是24万元", extraction, "cycle", "artifact")
        self.assertEqual("applied", result["state"])
        self.assertEqual(240000, self.service.snapshot()["total_assets"])
        self.assertEqual("asset_correction", self.service.snapshot()["transactions"][0]["action"])

    def test_partial_position_table_never_zeros_omitted_holdings(self):
        extraction = {"statement_type": "current_state", "changes": [{"action": "position_correction", "code": "603179", "name": "新泉股份", "shares": 300, "price": 38.1, "average_cost": 38.1, "occurred_at": None, "evidence": {"instrument": "603179 新泉股份", "action": "持有", "shares": "300股", "price": "38.1", "average_cost": "38.1"}}]}

        self.service.apply_extraction("603179 新泉股份 持有300股，成本38.1", extraction, "cycle", "partial")

        self.assertEqual({"603179", "603993"}, {item["code"] for item in self.service.snapshot()["positions"]})

    def test_explicit_complete_snapshot_atomically_zeros_omitted_holdings(self):
        changes = [{"action": "position_correction", "code": "603179", "name": "新泉股份", "shares": 300, "price": 38.1, "average_cost": 38.1, "occurred_at": None, "evidence": {"instrument": "603179 新泉股份", "action": "持有", "shares": "300股", "price": "38.1", "average_cost": "38.1", "total_assets": None}}]

        result = self.service.replace_complete_snapshot(
            "这是全部持仓：603179 新泉股份 300股，成本38.1", changes, "cycle", "complete"
        )

        self.assertEqual("applied", result["state"])
        self.assertTrue(result["complete_snapshot"])
        self.assertEqual(["603179"], [item["code"] for item in self.service.snapshot()["positions"]])

        reverted = self.service.revert_latest()
        self.assertEqual({"603179", "603993"}, {item["code"] for item in reverted["snapshot"]["positions"]})
        self.assertEqual(2, len(reverted["reversal_transaction_ids"]))

    def test_complete_snapshot_requires_explicit_scope_marker(self):
        changes = [{"action": "position_correction", "code": "603179", "name": "新泉股份", "shares": 300, "price": 38.1, "average_cost": 38.1, "occurred_at": None, "evidence": {"instrument": "603179 新泉股份", "action": "持有", "shares": "300股", "price": "38.1", "average_cost": "38.1", "total_assets": None}}]

        result = self.service.replace_complete_snapshot(
            "603179 新泉股份 300股，成本38.1", changes, "cycle", "not-complete"
        )

        self.assertEqual("needs_input", result["state"])
        self.assertEqual({"603179", "603993"}, {item["code"] for item in self.service.snapshot()["positions"]})

    def test_asset_correction_can_be_reverted_from_a_later_conversation(self):
        extraction = {"statement_type": "current_state", "changes": [{"action": "asset_correction", "code": None, "name": None, "shares": None, "price": None, "total_assets": 240000, "occurred_at": None, "evidence": {"instrument": None, "action": "总资产", "shares": None, "price": None, "total_assets": "24万元"}}]}
        self.service.apply_extraction("现在总资产是24万元", extraction, "cycle-a", "asset-a")

        reverted = self.service.revert_latest()

        self.assertEqual(230000, reverted["snapshot"]["total_assets"])

    def test_new_stock_requires_explicit_code_and_name(self):
        extraction = {"statement_type": "executed", "changes": [{"action": "buy", "code": "600519", "name": "贵州茅台", "shares": 100, "price": 1500, "occurred_at": None, "evidence": {"instrument": "600519贵州茅台", "action": "买入", "shares": "100股", "price": "1500元"}}]}
        result = self.service.apply_extraction("买入600519贵州茅台100股，1500元成交", extraction, "cycle", "artifact")
        self.assertEqual("applied", result["state"])
        self.assertIn("600519", [item["code"] for item in self.service.snapshot()["positions"]])

    def test_revert_adds_inverse_event_and_restores_position(self):
        self.service.apply_extraction("我以39.5元买入新泉股份100股", self.extraction(), "cycle", "artifact")
        reverted = self.service.revert_latest()
        position = next(item for item in self.service.snapshot()["positions"] if item["code"] == "603179")
        self.assertEqual(100, position["shares"])
        transactions = self.service.snapshot()["transactions"]
        self.assertEqual(2, len(transactions))
        inverse = next(item for item in transactions if item["transaction_id"] == reverted["reversal_transaction_id"])
        self.assertIsNotNone(inverse["reversal_of"])

    def test_legacy_projection_intents_are_retired_without_file_access(self):
        self.service.apply_extraction("我以39.5元买入新泉股份100股", self.extraction(), "cycle", "artifact")
        transaction_id = self.service.snapshot()["transactions"][0]["transaction_id"]
        with self.store.connection() as connection:
            connection.execute(
                "INSERT INTO portfolio_render_intent VALUES('legacy-intent',?,NULL,'pending','2026-08-25T00:00:00Z',NULL,NULL)",
                (transaction_id,),
            )
        self.assertEqual([], self.service.reconcile())
        with self.store.connection() as connection:
            state = connection.execute("SELECT state FROM portfolio_render_intent WHERE intent_id='legacy-intent'").fetchone()[0]
        self.assertEqual("retired", state)

    def test_external_markdown_change_cannot_override_database(self):
        before = self.service.snapshot()
        path = self.root / "portfolio/01_CURRENT_PORTFOLIO.md"
        path.write_text("# 伪造持仓\n\n| 000001 | 不应读取 | 999999 |", encoding="utf-8")
        self.assertEqual([], self.service.reconcile())
        self.assertEqual(before, self.service.snapshot())

    def test_fixture_parser_distinguishes_plan_and_execution(self):
        self.assertEqual("planned", explicit_fixture_extraction("准备明天买入新泉股份100股，价格39.5")["statement_type"])
        parsed = explicit_fixture_extraction("我以39.5元买入新泉股份100股")
        self.assertEqual("executed", parsed["statement_type"])
        self.assertEqual(100, parsed["changes"][0]["shares"])


if __name__ == "__main__":
    unittest.main()
