from pathlib import Path

from ai_trading_companion.portfolio import PortfolioService
from ai_trading_companion.store import CompanionStore


ROOT = Path(__file__).resolve().parents[2]


def test_production_runtime_has_no_markdown_or_csv_business_file_dependency() -> None:
    source = ROOT / "src" / "runtime" / "ai_trading_companion"
    findings = {
        str(path.relative_to(ROOT)): token
        for path in source.glob("*.py")
        if path.name != "memoryhub_migration.py"
        for token in (".md", ".csv", "01_CURRENT_PORTFOLIO", "STOCK_STATE", "THEME_STATE", "OPPORTUNITY_LOG")
        if token in path.read_text(encoding="utf-8")
    }
    assert findings == {}
    assert list((ROOT / "resources").rglob("*.md")) == []
    assert list((ROOT / "resources").rglob("*.csv")) == []
    assert "retired Markdown/CSV files" in (ROOT / "scripts" / "publish.ps1").read_text(encoding="utf-8")


def test_portfolio_updates_do_not_create_file_projections(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "runtime.sqlite3")
    service = PortfolioService(store)
    result = service.apply_extraction(
        "买入浦发银行600000 100股，价格10元",
        {"statement_type": "executed", "changes": [{
            "action": "buy", "code": "600000", "name": "浦发银行", "shares": 100,
            "price": 10, "occurred_at": "2026-09-01T02:00:00Z",
            "evidence": {"instrument": "600000", "action": "买入", "shares": "100", "price": "10"},
        }]},
        None, None,
    )
    assert result["state"] == "applied"
    assert list(tmp_path.rglob("*.md")) == []
    assert list(tmp_path.rglob("*.csv")) == []
